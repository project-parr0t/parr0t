"""New synthetic format bytes, generated without player files or font assets."""
from __future__ import annotations

import io
import struct
from types import SimpleNamespace


def clr_fixture():
    from game_l10n.unity6000.managed import user_string
    text = "Synthetic {name}"
    tables = struct.pack("<IBBBBQQI", 0, 2, 0, 0, 1, 1 << 6, 0, 1)
    tables += struct.pack("<IHHHHH", 0x2050, 0, 0x16, 1, 1, 1)
    streams = [(b"#~", tables), (b"#Strings", b"\0Synthetic\0"), (b"#US", b"\0" + user_string(text)), (b"#Blob", b"\0\x03\x00\x00\x01")]
    version = b"v4.0.30319\0\0"
    header = struct.pack("<IHHII", 0x424A5342, 1, 1, 0, len(version)) + version + struct.pack("<HH", 0, len(streams))
    first = len(header) + sum(8 + ((len(name) + 1 + 3) // 4 * 4) for name, _ in streams)
    body = bytearray()
    for name, blob in streams:
        body.extend(b"\0" * (-len(body) % 4))
        header += struct.pack("<II", first + len(body), len(blob)) + name + b"\0" * ((len(name) + 1 + 3) // 4 * 4 - len(name))
        body.extend(blob)
    metadata = header + body
    data = bytearray(0x800)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x14C, 1, 0, 0, 0, 224, 0x2102)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x10B)
    struct.pack_into("<IIII", data, optional + 20, 0x2000, 0x2000, 0x400000, 0x2000)
    struct.pack_into("<I", data, optional + 36, 0x200)
    struct.pack_into("<HH", data, optional + 40, 4, 0)
    struct.pack_into("<II", data, optional + 56, 0x4000, 0x200)
    struct.pack_into("<H", data, optional + 68, 3)
    struct.pack_into("<I", data, optional + 92, 16)
    struct.pack_into("<II", data, optional + 96 + 14 * 8, 0x2000, 72)
    section = 0x178
    data[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", data, section + 8, 0x600, 0x2000, 0x600, 0x200)
    struct.pack_into("<I", data, section + 36, 0x60000020)
    struct.pack_into("<IHHIIII", data, 0x200, 72, 2, 5, 0x2100, len(metadata), 1, 0x06000001)
    code = b"\x72\x01\x00\x00\x70\x26\x72\x01\x00\x00\x70\x26\x2a"
    data[0x250:0x250 + len(code) + 1] = bytes([(len(code) << 2) | 2]) + code
    data[0x300:0x300 + len(metadata)] = metadata
    return bytes(data), text


def synthetic_font():
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    glyphs = {}
    for name in (".notdef", "A"):
        pen = TTGlyphPen(None)
        if name == "A":
            pen.moveTo((0, 0)); pen.lineTo((250, 700)); pen.lineTo((500, 0)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 0) for name in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupCharacterMap({65: "A"})
    builder.setupNameTable({"familyName": "Synthetic", "styleName": "Regular", "uniqueFontIdentifier": "Synthetic", "fullName": "Synthetic", "psName": "Synthetic"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.font["head"].created = builder.font["head"].modified = 2082844800
    output = io.BytesIO(); builder.save(output)
    return output.getvalue()


def serialized_fixture(objects, name="synthetic", version="6000.0.1f1"):
    """Generate an embedded-typetree SerializedFile, using only synthetic fields."""
    from UnityPy.enums import ClassIDType
    from UnityPy.files.SerializedFile import SerializedFile, SerializedType
    from UnityPy.files.ObjectReader import ObjectReader
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    from UnityPy.streams import EndianBinaryReader

    def node(value, field, level):
        args = dict(m_Level=level, m_Name=field, m_ByteSize=-1, m_Version=1, m_TypeFlags=0, m_Index=0, m_MetaFlag=0, m_RefTypeHash=0)
        if isinstance(value, str):
            return TypeTreeNode(m_Type="string", m_Children=[], **args)
        if isinstance(value, float):
            return TypeTreeNode(m_Type="float", m_Children=[], **{**args, "m_ByteSize": 4})
        if isinstance(value, int):
            return TypeTreeNode(m_Type="int", m_Children=[], **{**args, "m_ByteSize": 4})
        if isinstance(value, tuple):
            return TypeTreeNode(m_Type="pair", m_Children=[node(value[0], "first", level + 1), node(value[1], "second", level + 1)], **args)
        if isinstance(value, list):
            array = TypeTreeNode(m_Type="Array", m_Children=[node(0, "size", level + 2), node(value[0] if value else 0, "data", level + 2)], **{**args, "m_Level": level + 1, "m_Name": "Array", "m_TypeFlags": 1})
            return TypeTreeNode(m_Type="vector", m_Children=[array], **args)
        return TypeTreeNode(m_Type="synthetic", m_Children=[node(v, k, level + 1) for k, v in value.items()], **args)

    sf = SerializedFile.__new__(SerializedFile)
    sf.name = name; sf.parent = None
    sf.reader = EndianBinaryReader(b"", endian="<")
    sf.header = SimpleNamespace(version=22, endian="<", reserved=b"\0\0\0")
    sf.set_version(version)
    sf._m_target_platform = 19; sf._enable_type_tree = True
    sf.big_id_enabled = 0; sf.unknown = 0
    sf.types = []; sf.objects = {}; sf.script_types = []; sf.externals = []; sf.ref_types = []; sf.userInformation = ""; sf._cache = {}
    for pid, class_id, tree in objects:
        typed = SerializedType.__new__(SerializedType)
        typed.__attrs_init__(class_id)
        typed.is_stripped_type = False; typed.script_type_index = -1
        typed.script_id = b"\0" * 16; typed.old_type_hash = b"\0" * 16
        typed.node = node(tree, "Base", 0); typed.type_dependencies = ()
        index = len(sf.types); sf.types.append(typed)
        obj = ObjectReader(sf, sf.reader, pid, index, typed, class_id, ClassIDType(class_id), 0, 0, None, None)
        obj.save_typetree(tree)
        sf.objects[pid] = obj
    return sf.save()
