"""Read exact Windows Player media metadata without opening any references."""
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import struct

from unity_objects import _native_object
from unity_raw import FormatError, serialized, tree_object

_MAX_ARCHIVE_BYTES = 256 << 20
_MAX_METADATA_BYTES = 8 << 20
_MAX_OBJECT_BYTES = 1 << 20
_MAX_OBJECT_IDS = 1024
_PLAYER_VERSION = re.compile(r'2022\.3\.[0-9]+[abfp][0-9]+')
_SCENE = re.compile(r'level[0-9]+')
_SHA256 = re.compile(r'[a-f0-9]{64}')
_RESOURCE_NAMES = frozenset({'unity default resources', 'unity_builtin_extra'})


@dataclass(frozen=True)
class FileBinding:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class FileReference:
    relative_path: str
    evidence: FileBinding


@dataclass(frozen=True)
class TextureStream:
    companion: FileReference
    object_id: int
    offset: int
    size: int


def _path_parts(value):
    # Check both POSIX wire syntax and Windows Player filename restrictions.
    if not isinstance(value, str) or not value or len(value) > 1024:
        return ()
    parts = tuple(value.split('/'))
    if any(not part or part[-1] in ' .' or PureWindowsPath(part).is_reserved()
           or any(ch in '\\:<>"|?*' or ord(ch) < 32 for ch in part) for part in parts):
        return ()
    return parts


def _read_player(player_data, binding, *, scene=False):
    parts = _path_parts(binding.relative_path)
    if (len(parts) != 1 or (scene and not _SCENE.fullmatch(parts[0]))
            or not isinstance(binding.sha256, str) or not _SHA256.fullmatch(binding.sha256)):
        raise FormatError('MEDIA_SOURCE_BINDING_INVALID')
    try:
        root = Path(player_data).resolve(strict=True)
        if not root.name.casefold().endswith('_data') or not root.is_dir():
            raise FormatError('MEDIA_PLAYER_DATA_INVALID')
        path = root / parts[0]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise FormatError('MEDIA_SOURCE_NOT_REGULAR')
        if not 48 <= info.st_size <= _MAX_ARCHIVE_BYTES:
            raise FormatError('MEDIA_ARCHIVE_SIZE_UNSUPPORTED')
        with path.open('rb') as source:
            opened = os.fstat(source.fileno())
            if not os.path.samestat(info, opened) or opened.st_size != info.st_size:
                raise FormatError('MEDIA_SOURCE_CHANGED')
            raw = source.read(info.st_size)
            if len(raw) != info.st_size or source.read(1):
                raise FormatError('MEDIA_SOURCE_CHANGED')
    except FormatError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise FormatError('MEDIA_SOURCE_UNAVAILABLE') from None
    if hashlib.sha256(raw).hexdigest() != binding.sha256:
        raise FormatError('MEDIA_SOURCE_DIGEST_MISMATCH')
    if (struct.unpack_from('>I', raw, 8)[0] == 22
            and struct.unpack_from('>I', raw, 20)[0] > _MAX_METADATA_BYTES):
        raise FormatError('MEDIA_METADATA_SIZE_UNSUPPORTED')
    document = serialized(raw)
    if not _PLAYER_VERSION.fullmatch(document['version']) or document['platform'] not in {5, 19}:
        raise FormatError('MEDIA_PLAYER_LAYOUT_UNSUPPORTED')
    return raw, document


def _objects(document, object_ids, class_id):
    if not isinstance(object_ids, Sequence) or len(object_ids) > _MAX_OBJECT_IDS:
        raise FormatError('MEDIA_OBJECT_BINDINGS_INVALID')
    ids = tuple(object_ids)
    if any(type(pid) is not int or pid == 0 for pid in ids):
        raise FormatError('MEDIA_OBJECT_BINDINGS_INVALID')
    wanted = set(ids)
    objects = {obj['path_id']: obj for obj in document['objects'] if obj['path_id'] in wanted
               and obj['class_id'] == class_id}
    if objects.keys() != wanted:
        raise FormatError('MEDIA_OBJECT_BINDING_NOT_FOUND')
    if any(obj['size'] > _MAX_OBJECT_BYTES for obj in objects.values()):
        raise FormatError('MEDIA_OBJECT_SIZE_UNSUPPORTED')
    return ids, objects


def _scene_external(path):
    parts = _path_parts(path)
    return (len(parts) == 1 and Path(parts[0]).suffix.casefold() == '.assets'
            or len(parts) == 2 and parts[0] == 'Resources' and parts[1] in _RESOURCE_NAMES)


def scene_metadata_references(player_data, scene: FileBinding, *, monoscript_ids: Sequence[int] = ()):
    """Return unique sibling metadata references from one bound levelN scene.

    Managed DLL references require exact caller-resolved MonoScript PathIDs in
    this same scene. External targets are only proposed, never opened.
    """
    raw, document = _read_player(player_data, scene, scene=True)
    ids, scripts = _objects(document, monoscript_ids, 115)
    paths = dict.fromkeys(row['path'] for row in document['externals'] if _scene_external(row['path']))
    for pid in dict.fromkeys(ids):
        obj = scripts[pid]
        typ = document['types'][obj['type_index']]
        if typ['nodes'] and typ['nodes'][0]['type'] != 'MonoScript':
            raise FormatError('MEDIA_MONOSCRIPT_LAYOUT_UNSUPPORTED')
        fields = (tree_object(raw, obj, typ) if typ['nodes'] else _native_object(raw, obj))['value']
        assembly = fields.get('m_AssemblyName') if isinstance(fields, dict) else None
        parts = _path_parts(assembly)
        if len(parts) == 1 and Path(parts[0]).suffix.casefold() == '.dll':
            paths['Managed/' + assembly] = None
    return tuple(FileReference(path, scene) for path in paths)


def _texture_stream(raw, document, obj, archive):
    typ = document['types'][obj['type_index']]
    nodes = typ['nodes']
    if not nodes or nodes[0]['type'] != 'Texture2D':
        raise FormatError('MEDIA_TEXTURE_LAYOUT_UNSUPPORTED')
    fields = [i for i, node in enumerate(nodes) if node['level'] == 1 and node['name'] == 'm_StreamData']
    if len(fields) != 1 or nodes[fields[0]]['type'] != 'StreamingInfo':
        raise FormatError('MEDIA_TEXTURE_LAYOUT_UNSUPPORTED')
    field = fields[0]
    children = [(node['name'], node['type']) for node in nodes[field + 1:nodes[field]['end']]
                if node['level'] == 2]
    if (len(children) != 3 or children[0][0] != 'offset'
            or children[0][1] not in {'UInt64', 'unsigned long long', 'FileSize'}
            or children[1][0] != 'size' or children[1][1] not in {'UInt32', 'unsigned int'}
            or children[2] != ('path', 'string')):
        raise FormatError('MEDIA_TEXTURE_LAYOUT_UNSUPPORTED')
    texture = tree_object(raw, obj, typ)['value']
    stream = texture.get('m_StreamData') if isinstance(texture, dict) else None
    if not isinstance(stream, dict) or stream.keys() != {'offset', 'size', 'path'}:
        raise FormatError('MEDIA_TEXTURE_LAYOUT_UNSUPPORTED')
    offset, size, path = stream['offset'], stream['size'], stream['path']
    if (type(offset) is not int or type(size) is not int or not isinstance(path, str)
            or not 0 <= offset < 1 << 64 or not 0 <= size < 1 << 32 or offset + size >= 1 << 64):
        raise FormatError('MEDIA_TEXTURE_STREAM_RANGE_INVALID')
    if (offset, size, path) == (0, 0, ''):
        return None
    companion = archive.relative_path + '.resS'
    if not size or path != companion:
        raise FormatError('MEDIA_TEXTURE_COMPANION_UNSUPPORTED')
    return TextureStream(FileReference(companion, archive), obj['path_id'], offset, size)


def texture_stream_references(player_data, archive: FileBinding, object_ids: Sequence[int]):
    """Return ordered Texture2D stream references (None for empty StreamData).

    Only embedded type trees and a bare, exact archive-name + '.resS' path are
    supported. The reported range is not checked against any companion bytes.
    """
    raw, document = _read_player(player_data, archive)
    ids, objects = _objects(document, object_ids, 28)
    streams = {pid: _texture_stream(raw, document, objects[pid], archive) for pid in dict.fromkeys(ids)}
    return tuple(streams[pid] for pid in ids)
