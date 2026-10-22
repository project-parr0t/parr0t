"""Explicit-template dynamic TMP fallback serialization; no bundled fonts."""
import copy
import struct
import math

from unity_serialized import aligned_behaviour_node, environment, externals, metadata


def pointer(path_id=0, file_id=0):
    return {'m_FileID': file_id, 'm_PathID': path_id}


def _clone_object_reader(template, path_id):
    """Clone UnityPy reader metadata without depending on attrs internals."""
    state = getattr(template, '__dict__', None)
    if not isinstance(state, dict):
        raise ValueError('FONT_OBJECT_READER_LAYOUT_UNSUPPORTED')
    clone = object.__new__(type(template))
    clone.__dict__.update(state)
    clone.path_id = path_id
    clone.data = None
    return clone


def _same_tree(left, right):
    # Unity scalar float serialization rounds to binary32. Only that bounded
    # numeric rounding is tolerated; keys, sequences and all other values match.
    if isinstance(left, float) and isinstance(right, float):
        return math.isfinite(left) and math.isfinite(right) and math.isclose(left,right,rel_tol=1e-7,abs_tol=1e-7)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_tree(left[k],right[k]) for k in left)
    if isinstance(left,(list,tuple)) and isinstance(right,(list,tuple)):
        return len(left) == len(right) and all(_same_tree(a,b) for a,b in zip(left,right))
    return left == right


def native_metrics(data):
    """Read the bounded SFNT metrics used by an embedded dynamic font.

    No glyph outline, texture or image is decoded. Unsupported/short tables fail.
    """
    def read(fmt, offset):
        if offset < 0 or offset + struct.calcsize(fmt) > len(data):
            raise ValueError('FONT_TABLE_BOUNDARY_INVALID')
        return struct.unpack_from(fmt, data, offset)[0]
    u16, s16, u32 = (lambda p: read('>H', p)), (lambda p: read('>h', p)), (lambda p: read('>I', p))
    if data[:4] not in (b'OTTO', b'\0\1\0\0'):
        raise ValueError('SFNT_FONT_REQUIRED')
    tables = {}
    for i in range(u16(4)):
        pos = 12 + i * 16
        tag = data[pos:pos+4]
        offset, size = u32(pos+8), u32(pos+12)
        if tag in tables or offset + size > len(data):
            raise ValueError('FONT_TABLE_BOUNDARY_INVALID')
        tables[tag] = (offset, size)
    for tag, size in ((b'head', 20), (b'hhea', 10), (b'OS/2', 90), (b'post', 12)):
        if tag not in tables or tables[tag][1] < size:
            raise ValueError('FONT_METRICS_LAYOUT_UNSUPPORTED')
    hhea, head, os2, post = [tables[k][0] for k in (b'hhea', b'head', b'OS/2', b'post')]
    if u16(os2) < 2 or u16(head+18) == 0:
        raise ValueError('FONT_METRICS_LAYOUT_UNSUPPORTED')
    return dict(units=u16(head+18), ascent=s16(hhea+4), descent=s16(hhea+6),
                gap=s16(hhea+8), cap=s16(os2+88), mean=s16(os2+86),
                strike=s16(os2+28), strike_size=s16(os2+26),
                underline=s16(post+8), underline_size=s16(post+10))


def build_group(trees, font_bytes, *, names, label, ids, repertoire, space_advance):
    """Clone a caller-bound Font/TMP/material/1x1 Alpha8 dynamic atlas group."""
    if (len(ids) != 4 or len(set(ids)) != 4 or any(type(i) is not int or i <= 0 for i in ids)
            or len(names) != 2 or any(not isinstance(n, str) or not n for n in names)
            or not isinstance(label, str) or not label or space_advance < 0):
        raise ValueError('FONT_GROUP_BINDING_INVALID')
    repertoire = set(repertoire)
    if any(type(cp) is not int or not 0 <= cp <= 0x10ffff or 0xd800 <= cp <= 0xdfff for cp in repertoire):
        raise ValueError('FONT_REPERTOIRE_INVALID')
    font, tmp, material, texture = copy.deepcopy(trees)
    source_id, tmp_id, material_id, texture_id = ids
    metrics = native_metrics(font_bytes)
    face = tmp['m_FaceInfo']
    scale = float(face['m_PointSize']) / metrics['units']
    face.update(m_FamilyName=names[0], m_StyleName=names[1], m_UnitsPerEM=metrics['units'],
                m_Scale=1.0, m_Baseline=0.0)
    for key, value in {
        'm_LineHeight': metrics['ascent']-metrics['descent']+metrics['gap'],
        'm_AscentLine': metrics['ascent'], 'm_CapLine': metrics['cap'],
        'm_MeanLine': metrics['mean'], 'm_DescentLine': metrics['descent'],
        'm_SuperscriptOffset': metrics['ascent'], 'm_SubscriptOffset': metrics['descent'],
        'm_UnderlineOffset': metrics['underline'], 'm_UnderlineThickness': metrics['underline_size'],
        'm_StrikethroughOffset': metrics['strike'], 'm_StrikethroughThickness': metrics['strike_size'],
    }.items():
        face[key] = value * scale
    face['m_TabWidth'] = space_advance * scale
    font.update(m_Name=label, m_FontNames=[names[0]], m_FontData=font_bytes,
                m_DefaultMaterial=pointer(), m_Texture=pointer(), m_CharacterRects=[],
                m_KerningValues=[], m_FallbackFonts=[])
    native_scale = float(font['m_FontSize']) / metrics['units']
    font.update(m_LineSpacing=(metrics['ascent']-metrics['descent']+metrics['gap'])*native_scale,
                m_Ascent=metrics['ascent']*native_scale, m_Descent=metrics['descent']*native_scale)
    tmp.update(m_Name=label+' SDF Fallback', m_Material=pointer(material_id),
               m_SourceFontFile=pointer(source_id), m_SourceFontFileGUID='', m_SourceFontFilePath='',
               m_AtlasPopulationMode=1, InternalDynamicOS=0, m_GlyphTable=[], m_CharacterTable=[],
               m_AtlasTextures=[pointer(texture_id)], m_AtlasTextureIndex=0,
               m_IsMultiAtlasTexturesEnabled=1, m_UsedGlyphRects=[], m_FallbackFontAssetTable=[],
               m_glyphInfoList=[], fallbackFontAssets=[], atlas=pointer())
    width, height = tmp['m_AtlasWidth'], tmp['m_AtlasHeight']
    if width <= 1 or height <= 1:
        raise ValueError('DYNAMIC_ATLAS_DIMENSIONS_INVALID')
    tmp['m_FreeGlyphRects'] = [dict(m_X=0, m_Y=0, m_Width=width-1, m_Height=height-1)]
    tmp['m_FontFeatureTable'] = {k: [] for k in tmp['m_FontFeatureTable']}
    tmp['m_KerningTable'] = {k: [] for k in tmp['m_KerningTable']}
    tmp['m_CreationSettings'].update(sourceFontFileName=label, sourceFontFileGUID='',
        referencedFontAssetGUID='', referencedTextAssetGUID='',
        characterSequence=','.join(str(cp) for cp in sorted(repertoire)))
    tmp['m_fontInfo'].update(Name=names[0], PointSize=face['m_PointSize'], Scale=1.0,
        CharacterCount=0, LineHeight=face['m_LineHeight'], Baseline=0.0,
        Ascender=face['m_AscentLine'], CapHeight=face['m_CapLine'], Descender=face['m_DescentLine'],
        SuperscriptOffset=face['m_SuperscriptOffset'], SubscriptOffset=face['m_SubscriptOffset'],
        Underline=face['m_UnderlineOffset'], UnderlineThickness=face['m_UnderlineThickness'],
        strikethrough=face['m_StrikethroughOffset'],
        strikethroughThickness=face['m_StrikethroughThickness'],
        TabWidth=face['m_TabWidth']*tmp['tabSize'], AtlasWidth=float(width), AtlasHeight=float(height))
    material['m_Name'] = label+' SDF Material'
    main = [v for k, v in material['m_SavedProperties']['m_TexEnvs'] if k == '_MainTex']
    if len(main) != 1:
        raise ValueError('ATLAS_MATERIAL_BINDING_AMBIGUOUS')
    main[0]['m_Texture'] = pointer(texture_id)
    texture['m_Name'] = label+' SDF Atlas'
    if (texture['m_Width'], texture['m_Height'], texture['m_TextureFormat']) != (1, 1, 1):
        raise ValueError('DYNAMIC_ATLAS_TEMPLATE_CHANGED')
    if texture['m_StreamData']['size'] or texture['m_StreamData']['path']:
        raise ValueError('DYNAMIC_ATLAS_STREAM_NOT_EMPTY')
    return font, tmp, material, texture


def integrate(members, groups, fallbacks):
    """Return changed standalone member bytes from explicit declarative bindings.

    Group: member, template_ids/new_ids, font_node/tmp_node records, font_bytes,
    names, label, repertoire, space_advance, license_confirmed=True.
    Fallback: member, object_id, group (zero-based index). No IDs are discovered.
    """
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    from UnityPy.helpers.TypeTreeHelper import read_typetree
    from UnityPy.streams import EndianBinaryReader
    from UnityPy.files.SerializedFile import FileIdentifier
    files = {name: environment(raw).file for name, raw in members.items()}
    before = {name: (metadata(f), externals(f),
              {oid: (o.type_id, o.get_raw_data()) for oid, o in f.objects.items()})
              for name, f in files.items()}
    additions, changes, expected, node_map = {}, {}, {}, {}
    prepared = []
    for group in groups:
        name = group['member']
        if name not in files or group.get('license_confirmed') is not True:
            raise ValueError('FONT_INPUT_OR_LICENSE_REQUIRED')
        file = files[name]
        ids, templates = group['new_ids'], group['template_ids']
        if len(templates) != 4 or any(i in file.objects for i in ids):
            raise ValueError('FONT_OBJECT_IDS_NOT_NEW')
        node = aligned_behaviour_node(group['tmp_node'])
        nodes = [TypeTreeNode.from_list(group['font_node']), node, None, None]
        originals = [file.objects[i] for i in templates]
        if [o.type.name for o in originals] != ['Font', 'MonoBehaviour', 'Material', 'Texture2D']:
            raise ValueError('FONT_TEMPLATE_TYPES_INVALID')
        trees = []
        for obj, schema in zip(originals, nodes):
            raw = obj.get_raw_data()
            tree = obj.read_typetree(nodes=schema)
            if obj.save_typetree(tree, nodes=schema) != raw:
                raise ValueError('FONT_TEMPLATE_NOT_LOSSLESS')
            trees.append(tree)
        built = build_group(trees, group['font_bytes'], names=group['names'], label=group['label'],
            ids=ids, repertoire=group['repertoire'], space_advance=group['space_advance'])
        for template, schema, oid, tree in zip(originals, nodes, ids, built):
            clone = _clone_object_reader(template, oid)
            payload = clone.save_typetree(tree, nodes=schema)
            clone.byte_size = len(payload)
            canonical = read_typetree(schema or clone.serialized_type.node,
                EndianBinaryReader(payload,endian=file.header.endian),
                assetsfile=file,byte_size=len(payload))
            if not _same_tree(tree,canonical):
                raise ValueError('FONT_GROUP_SERIALIZATION_CHANGED')
            file.objects[oid] = clone
            additions.setdefault(name, set()).add(oid)
            expected[name, oid], node_map[name, oid] = canonical, schema
        prepared.append((name, ids[1], node))
    for binding in fallbacks:
        name, oid = binding['member'], binding['object_id']
        index = binding.get('group')
        if type(index) is not int or index < 0 or index >= len(prepared):
            raise ValueError('FALLBACK_GROUP_BINDING_INVALID')
        group_name, tmp_id, node = prepared[index]
        file = files[name]
        obj = file.objects[oid]
        if oid in additions.get(name, set()) or (name, oid) in expected or obj.type.name != 'MonoBehaviour':
            raise ValueError('FALLBACK_BINDING_AMBIGUOUS')
        tree = obj.read_typetree(nodes=node)
        if obj.save_typetree(tree, nodes=node) != before[name][2][oid][1]:
            raise ValueError('FALLBACK_TREE_NOT_LOSSLESS')
        file_id = 0
        if name != group_name:
            matches = [i+1 for i, e in enumerate(file.externals) if e.path == group_name]
            if len(matches) > 1:
                raise ValueError('FONT_EXTERNAL_AMBIGUOUS')
            if matches:
                file_id = matches[0]
            else:
                e = object.__new__(FileIdentifier)
                e.path, e.temp_empty, e.guid, e.type = group_name, '', bytes(16), 0
                file.externals.append(e)
                file_id = len(file.externals)
        ref = pointer(tmp_id, file_id)
        if ref in tree['m_FallbackFontAssetTable']:
            raise ValueError('FALLBACK_ALREADY_BOUND')
        tree['m_FallbackFontAssetTable'].append(ref)
        obj.save_typetree(tree, nodes=node)
        changes.setdefault(name, set()).add(oid)
        expected[name, oid], node_map[name, oid] = tree, node
    outputs = {name: file.save() for name, file in files.items()
               if additions.get(name) or changes.get(name)}
    for name, raw in outputs.items():
        check = environment(raw).file
        if metadata(check) != before[name][0] or externals(check) != externals(files[name]):
            raise ValueError('FONT_METADATA_READBACK_CHANGED')
        if set(check.objects) != set(before[name][2]) | additions.get(name, set()):
            raise ValueError('FONT_OBJECT_SET_CHANGED')
        for oid, obj in check.objects.items():
            if oid in before[name][2] and obj.type_id != before[name][2][oid][0]:
                raise ValueError('FONT_ORIGINAL_TYPE_CHANGED')
            if (name, oid) in expected:
                if obj.read_typetree(nodes=node_map[name, oid]) != expected[name, oid]:
                    raise ValueError('FONT_TREE_READBACK_CHANGED')
            elif obj.get_raw_data() != before[name][2][oid][1]:
                raise ValueError('FONT_UNOWNED_OBJECT_CHANGED')
    return outputs
