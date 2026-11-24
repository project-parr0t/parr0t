"""Synthetic v22 Player metadata; never use installed assets or a decoder."""
import hashlib
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-2022.3/tools'
sys.path.insert(0, str(TOOLS))
import unity_media as media
from unity_objects_fixture import node, serialized, string


def script_payload(assembly):
    return (string('Synthetic Script') + bytes(20) + string('Fixture')
            + string('Synthetic') + string(assembly))


def texture_tree(offset_type='UInt64'):
    return [node(0, 'Texture2D', 'Base'), node(1, 'string', 'm_Name'),
            node(1, 'int', 'm_Width', 4), node(1, 'TypelessData', 'image data', meta=0x4000),
            node(1, 'StreamingInfo', 'm_StreamData'), node(2, offset_type, 'offset', 8),
            node(2, 'UInt32', 'size', 4), node(2, 'string', 'path')]


def texture_payload(path='', offset=0, size=0, inline=b''):
    header = string('Synthetic Texture') + struct.pack('<ii', 8, len(inline))
    return header + inline + bytes((-len(inline)) % 4) + struct.pack('<QI', offset, size) + string(path)


class UnityMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Synthetic_Data'
        self.root.mkdir()

    def bind(self, name, raw):
        (self.root / name).write_bytes(raw)
        return media.FileBinding(name, hashlib.sha256(raw).hexdigest())

    def test_scene_projects_allowed_paths_without_touching_any_reference(self):
        raw = serialized([(115, []), (49, [])], [
            (1, 0, script_payload('Synthetic.dll')), (2, 0, script_payload('../Outside.dll')),
            (3, 0, script_payload('NoExtension')), (4, 0, b'not parsed unless selected'),
            (5, 1, b'not a script'), (6, 0, script_payload('CON.dll')),
        ], trees=False, externals=[
            'relative.assets', 'Resources/unity default resources', 'Resources/unity_builtin_extra',
            'relative.assets', 'SECOND.ASSETS', 'Resources/not_engine_metadata', 'resources/unity_builtin_extra',
            './bad.assets', 'folder/bad.assets', '../bad.assets', '/bad.assets', 'X:/bad.assets',
            'archive:/cab/bad.assets', r'Resources\unity_builtin_extra', 'bad.assets ', 'NUL.assets',
            'Managed/Unproven.dll', 'loose.dll', 'level2', 'unbound.png', 'relative.assets.resS',
        ])
        scene = self.bind('level0', raw)
        second = self.bind('level1', raw)
        self.assertEqual(scene.sha256, second.sha256)
        open_path = Path.open
        lstat_path = Path.lstat
        touches = []

        def tracked_open(path, mode):
            touches.append(('open', path.name))
            self.assertEqual(mode, 'rb')
            return open_path(path, mode)

        def tracked_lstat(path, *args, **kwargs):
            touches.append(('lstat', path.name))
            return lstat_path(path, *args, **kwargs)

        paths = ('relative.assets', 'Resources/unity default resources', 'Resources/unity_builtin_extra',
                 'SECOND.ASSETS', 'Managed/Synthetic.dll')
        selected = (1, 2, 1, 3, 6)
        with (patch.object(Path, 'open', tracked_open), patch.object(Path, 'lstat', tracked_lstat),
              patch('os.scandir', side_effect=AssertionError('directory enumeration'))):
            for seed in (scene, second, scene):
                self.assertEqual(media.scene_metadata_references(self.root, seed, monoscript_ids=selected),
                                 tuple(media.FileReference(path, seed) for path in paths))
        self.assertEqual([name for action, name in touches if action == 'open'], ['level0', 'level1', 'level0'])
        self.assertTrue({name for _, name in touches} <= {'Synthetic_Data', 'level0', 'level1'})
        self.assertEqual(media.scene_metadata_references(self.root, scene),
                         tuple(media.FileReference(path, scene) for path in paths[:-1]))
        with self.assertRaisesRegex(media.FormatError, 'MEDIA_OBJECT_BINDING_NOT_FOUND'):
            media.scene_metadata_references(self.root, scene, monoscript_ids=(5,))

    def test_scene_binding_denials_and_player_layout(self):
        raw = serialized([], [])
        scene = self.bind('level1', raw)
        for name, digest in [('../level1', scene.sha256), ('/level1', scene.sha256),
                             ('nested/level1', scene.sha256), ('level1:stream', scene.sha256),
                             ('level１', scene.sha256), ('level', scene.sha256),
                             ('level1', 'x' * 64), ('level1.assets', scene.sha256)]:
            with self.subTest(name=name, digest=digest), self.assertRaisesRegex(
                    media.FormatError, 'MEDIA_SOURCE_BINDING_INVALID'):
                media.scene_metadata_references(self.root, media.FileBinding(name, digest))
        with self.assertRaisesRegex(media.FormatError, 'MEDIA_SOURCE_DIGEST_MISMATCH'):
            media.scene_metadata_references(self.root, media.FileBinding('level1', '0' * 64))
        wrong_revision = self.bind('level2', raw.replace(b'2022.3.99f1', b'2021.3.99f1', 1))
        wrong_platform = bytearray(raw)
        struct.pack_into('<i', wrong_platform, 48 + len(b'2022.3.99f1\0'), 13)
        for seed in (wrong_revision, self.bind('level3', wrong_platform)):
            with self.assertRaisesRegex(media.FormatError, 'MEDIA_PLAYER_LAYOUT_UNSUPPORTED'):
                media.scene_metadata_references(self.root, seed)
        with patch.object(media, '_MAX_ARCHIVE_BYTES', len(raw) - 1), self.assertRaisesRegex(
                media.FormatError, 'MEDIA_ARCHIVE_SIZE_UNSUPPORTED'):
            media.scene_metadata_references(self.root, scene)
        with patch.object(media, '_MAX_METADATA_BYTES', 1), self.assertRaisesRegex(
                media.FormatError, 'MEDIA_METADATA_SIZE_UNSUPPORTED'):
            media.scene_metadata_references(self.root, scene)

    def test_scene_rejects_link_even_when_digest_matches(self):
        raw = serialized([], [])
        outside = self.root.parent / 'outside'
        outside.write_bytes(raw)
        path = self.root / 'level0'
        binding = media.FileBinding(path.name, hashlib.sha256(raw).hexdigest())
        try:
            path.symlink_to(outside)
        except OSError as exc:
            if sys.platform != 'win32' or getattr(exc, 'winerror', None) != 1314:
                raise
            # Exercise the Windows file-reparse check without requiring
            # SeCreateSymbolicLinkPrivilege on a standard account.
            path.write_bytes(raw)
            lstat = Path.lstat
            reparse = SimpleNamespace(st_mode=lstat(path).st_mode,
                                      st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)

            def file_reparse(candidate):
                return reparse if candidate == path else lstat(candidate)

            with patch.object(Path, 'lstat', file_reparse), self.assertRaisesRegex(
                    media.FormatError, 'MEDIA_SOURCE_NOT_REGULAR'):
                media.scene_metadata_references(self.root, binding)
        else:
            with self.assertRaisesRegex(media.FormatError, 'MEDIA_SOURCE_NOT_REGULAR'):
                media.scene_metadata_references(self.root, binding)

    def test_texture_selects_exact_companion_and_preserves_repeated_bindings(self):
        name = 'synthetic.assets'
        stream = name + '.resS'
        raw = serialized([(28, texture_tree()), (49, [])], [
            (1, 0, texture_payload(stream, 17, 29)), (2, 0, texture_payload(inline=b'RGB')),
            (3, 0, texture_payload(stream, 104, 5)), (4, 1, b''),
            (5, 0, b'not parsed unless selected'),
        ])
        seed = self.bind(name, raw)
        ref = media.FileReference(stream, seed)
        with patch.object(media, 'tree_object', wraps=media.tree_object) as parse:
            self.assertEqual(media.texture_stream_references(self.root, seed, (3, 1, 3, 2, 1)),
                             (media.TextureStream(ref, 3, 104, 5), media.TextureStream(ref, 1, 17, 29),
                              media.TextureStream(ref, 3, 104, 5), None, media.TextureStream(ref, 1, 17, 29)))
        self.assertEqual(parse.call_count, 3)
        same_bytes = self.bind('other.assets', raw)
        self.assertEqual(same_bytes.sha256, seed.sha256)
        with self.assertRaisesRegex(media.FormatError, 'MEDIA_TEXTURE_COMPANION_UNSUPPORTED'):
            media.texture_stream_references(self.root, same_bytes, (1,))
        for pid in (4, 99):
            with self.assertRaisesRegex(media.FormatError, 'MEDIA_OBJECT_BINDING_NOT_FOUND'):
                media.texture_stream_references(self.root, seed, (pid,))
        for ids in ((True,), (0,), (1,) * 1025):
            with self.assertRaisesRegex(media.FormatError, 'MEDIA_OBJECT_BINDINGS_INVALID'):
                media.texture_stream_references(self.root, seed, ids)

    def test_texture_rejects_unproven_stream_paths_and_ranges(self):
        for path in ('', 'other.assets.resS', '../synthetic.assets.resS', 'synthetic.assets.ress',
                     'archive:/cab/synthetic.assets.resS', 'synthetic.assets.resS/other'):
            with self.subTest(path=path), self.assertRaisesRegex(media.FormatError, 'MEDIA_TEXTURE_COMPANION_UNSUPPORTED'):
                seed = self.bind('synthetic.assets', serialized([(28, texture_tree())],
                                 [(1, 0, texture_payload(path, 0, 3))]))
                media.texture_stream_references(self.root, seed, (1,))
        seed = self.bind('synthetic.assets', serialized([(28, texture_tree())],
                         [(1, 0, texture_payload('synthetic.assets.resS', (1 << 64) - 1, 2))]))
        with self.assertRaisesRegex(media.FormatError, 'MEDIA_TEXTURE_STREAM_RANGE_INVALID'):
            media.texture_stream_references(self.root, seed, (1,))

    def test_texture_rejects_stripped_and_unknown_wire_layout(self):
        raw_texture = texture_payload('synthetic.assets.resS', 0, 4)
        for raw in (serialized([(28, [])], [(1, 0, raw_texture)], trees=False),
                    serialized([(28, texture_tree('SInt64'))], [(1, 0, raw_texture)])):
            seed = self.bind('synthetic.assets', raw)
            with self.assertRaisesRegex(media.FormatError, 'MEDIA_TEXTURE_LAYOUT_UNSUPPORTED'):
                media.texture_stream_references(self.root, seed, (1,))


if __name__ == '__main__':
    unittest.main()
