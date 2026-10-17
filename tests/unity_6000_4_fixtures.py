"""Construct every binary in memory from visibly synthetic values."""
from pathlib import Path
import struct
import sys

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-6000.4/tools'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def schema(tree, root='Synthetic'):
    records = []
    def emit(name, value, level, forced=None):
        typ = forced or ('PPtr<Object>' if isinstance(value, dict) and set(value) == {'m_FileID','m_PathID'}
                         else 'SyntheticStruct' if isinstance(value, dict)
                         else 'vector' if isinstance(value, list)
                         else 'pair' if isinstance(value, tuple)
                         else 'TypelessData' if isinstance(value, bytes)
                         else 'string' if isinstance(value, str)
                         else 'float' if isinstance(value, float)
                         else 'SInt64' if name == 'm_PathID'
                         else 'UInt8' if name == 'm_Enabled' else 'int')
        records.append(dict(m_Level=level, m_Type=typ, m_Name=name,
            m_ByteSize=-1 if typ in ('string','vector','TypelessData') else 0,
            m_Version=1, m_MetaFlag=0x4000 if name == 'm_Enabled' else 0))
        if isinstance(value, dict):
            for key, child in value.items(): emit(key,child,level+1)
        elif typ in ('string', 'TypelessData', 'vector'):
            records.append(dict(m_Level=level+1,m_Type='Array',m_Name='Array',
                m_ByteSize=-1,m_Version=1,m_MetaFlag=0x4000 if isinstance(value,(str,bytes)) else 0))
            emit('size',0,level+2)
            emit('data', 'A' if isinstance(value,str) else 0 if isinstance(value,bytes)
                 else value[0] if value else 0, level+2,
                 'char' if isinstance(value,str) else 'UInt8' if isinstance(value,bytes) else None)
        elif isinstance(value, tuple):
            emit('first',value[0],level+1)
            emit('second',value[1],level+1)
    emit('Base',tree,0,root)
    for index, record in enumerate(records):
        record.update(m_TypeFlags=0,m_Index=index,m_RefTypeHash=0)
    return records


def serialized(specs, version='6000.4.0f1'):
    """spec: (object id, class id, schema records, tree or opaque bytes)."""
    from UnityPy.helpers.TypeTreeHelper import write_typetree
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    from UnityPy.streams import EndianBinaryWriter
    meta, data = EndianBinaryWriter(endian='<'), EndianBinaryWriter(endian='<')
    meta.write_string_to_null(version)
    meta.write_int(19)
    meta.write_boolean(True)
    meta.write_int(len(specs))
    payloads = []
    for oid, class_id, records, value in specs:
        node = TypeTreeNode.from_list(records)
        meta.write_int(class_id)
        meta.write_boolean(False)
        meta.write_short(-1)
        if class_id == 114: meta.write_bytes(b'S'*16)
        meta.write_bytes(b'T'*16)
        node.dump_blob(meta,22)
        meta.write_int(0)
        writer = EndianBinaryWriter(endian='<')
        if isinstance(value, bytes): writer.write_bytes(value)
        else: write_typetree(value,node,writer)
        payloads.append(writer.bytes)
    meta.write_int(len(specs))
    for index, ((oid, _class_id, _records, _value), payload) in enumerate(zip(specs,payloads)):
        meta.align_stream(4)
        data.align_stream(8)
        meta.write_long(oid)
        meta.write_long(data.Position)
        meta.write_u_int(len(payload))
        meta.write_int(index)
        data.write_bytes(payload)
    meta.write_int(0)  # script references
    meta.write_int(0)  # externals
    meta.write_int(0)  # referenced types
    meta.write_string_to_null('SYNTHETIC_METADATA_SENTINEL')
    offset = (48+len(meta.bytes)+15) & ~15
    total = offset+len(data.bytes)
    header = struct.pack('>IIII?3sIqqq',0,0,22,0,False,b'\0'*3,len(meta.bytes),total,offset,0)
    return header+meta.bytes+b'\0'*(offset-48-len(meta.bytes))+data.bytes


def behaviour(text='SYNTHETIC_SOURCE_{0}'):
    return {'m_GameObject': {'m_FileID':0,'m_PathID':0}, 'm_Enabled':1,
            'm_Script': {'m_FileID':0,'m_PathID':999},
            'm_Name':'SYNTHETIC_OBJECT', 'm_Label':text, 'm_Number':123}


def text_file():
    tree = behaviour()
    records = schema(tree,'MonoBehaviour')
    from unity_serialized import pack_string
    raw = pack_string('SYNTHETIC_RAW')
    return serialized([(11,114,records,tree),(22,114,records,tree),
                       (33,114,records,raw)]), records


def bundle(members):
    from UnityPy.files.BundleFile import BundleFile
    from UnityPy.enums import ArchiveFlags
    from UnityPy.streams import EndianBinaryReader
    file = object.__new__(BundleFile)
    file.signature, file.version = 'UnityFS',7
    file.version_player, file.version_engine = '5.x.x','6000.4.0f1'
    file._uses_block_alignment = True
    file.dataflags, file._block_info_flags = ArchiveFlags(64),0
    file.files = {}
    for index, (name, raw) in enumerate(members.items()):
        entry = EndianBinaryReader(raw)
        entry.flags, entry.name = 4 if raw[:4] == bytes(4) else 0,name
        file.files[name] = entry
    return file.save(packer='lz4')


def sfnt():
    """Minimal metric-only synthetic SFNT; never a usable font program."""
    tags = [(b'head',20),(b'hhea',10),(b'OS/2',90),(b'post',12)]
    raw = bytearray(b'OTTO'+struct.pack('>HHHH',len(tags),0,0,0))
    offset = 12+len(tags)*16
    positions = {}
    for tag,size in tags:
        raw.extend(tag+struct.pack('>III',0,offset,size))
        positions[tag] = offset
        offset += size
    raw.extend(bytes(offset-len(raw)))
    for tag, pos, value in [(b'head',18,1000),(b'hhea',4,800),(b'hhea',6,-200),
                           (b'hhea',8,100),(b'OS/2',0,2),(b'OS/2',86,500),
                           (b'OS/2',88,700),(b'post',8,-100)]:
        struct.pack_into('>h',raw,positions[tag]+pos,value)
    return bytes(raw)


def font_trees():
    from unity_fonts import pointer
    font = {'m_Name':'SYNTHETIC_TEMPLATE_FONT', 'm_FontNames':['Synthetic Sans'],
        'm_FontData':b'SYNTHETIC_TEMPLATE_BYTES', 'm_FontSize':16,
        'm_DefaultMaterial':pointer(), 'm_Texture':pointer(), 'm_CharacterRects':[],
        'm_KerningValues':[], 'm_FallbackFonts':[], 'm_LineSpacing':1.0,
        'm_Ascent':1.0, 'm_Descent':-1.0}
    tmp = behaviour()
    tmp.update(m_FaceInfo={k:1.0 for k in ['m_PointSize','m_Scale','m_Baseline','m_LineHeight',
        'm_AscentLine','m_CapLine','m_MeanLine','m_DescentLine','m_SuperscriptOffset',
        'm_SubscriptOffset','m_UnderlineOffset','m_UnderlineThickness','m_StrikethroughOffset',
        'm_StrikethroughThickness','m_TabWidth']}, m_Material=pointer(30),
        m_SourceFontFile=pointer(10),m_SourceFontFileGUID='',m_SourceFontFilePath='',
        m_AtlasPopulationMode=1,InternalDynamicOS=0,m_GlyphTable=[],m_CharacterTable=[],
        m_AtlasTextures=[pointer(40)],m_AtlasTextureIndex=0,m_IsMultiAtlasTexturesEnabled=1,
        m_UsedGlyphRects=[],m_FallbackFontAssetTable=[pointer(777)],m_glyphInfoList=[],
        fallbackFontAssets=[],atlas=pointer(),m_AtlasWidth=512,m_AtlasHeight=512,
        m_FreeGlyphRects=[dict(m_X=0,m_Y=0,m_Width=511,m_Height=511)],
        m_FontFeatureTable={'syntheticFeature':[]}, m_KerningTable={'syntheticPair':[]},
        m_CreationSettings=dict(sourceFontFileName='',sourceFontFileGUID='',
            referencedFontAssetGUID='',referencedTextAssetGUID='',characterSequence=''),
        m_fontInfo={k:1.0 for k in ['PointSize','Scale','LineHeight','Baseline','Ascender','CapHeight',
            'Descender','SuperscriptOffset','SubscriptOffset','Underline','UnderlineThickness',
            'strikethrough','strikethroughThickness','TabWidth','AtlasWidth','AtlasHeight']},tabSize=4)
    tmp['m_FaceInfo'].update(m_FamilyName='Synthetic Sans',m_StyleName='Regular',m_UnitsPerEM=1000)
    tmp['m_fontInfo'].update(Name='Synthetic Sans',CharacterCount=0)
    material = {'m_Name':'SYNTHETIC_MATERIAL','m_Shader':pointer(888),
                'm_SavedProperties':{'m_TexEnvs':[('_MainTex',{'m_Texture':pointer(40)})]}}
    texture = {'m_Name':'SYNTHETIC_ATLAS','m_Width':1,'m_Height':1,'m_TextureFormat':1,
               'm_StreamData':{'size':0,'path':''},'image data':b'\0'}
    return font,tmp,material,texture


def font_members():
    trees = font_trees()
    records = [schema(t, root) for t,root in zip(trees,['Font','MonoBehaviour','Material','Texture2D'])]
    a = serialized([(oid,cid,rec,tree) for oid,cid,rec,tree in
                    zip([10,20,30,40],[128,114,21,28],records,trees)])
    b = serialized([(50,114,records[1],trees[1]),(60,114,records[1],trees[1])])
    group = dict(member='synthetic-a',template_ids=[10,20,30,40],new_ids=[100,101,102,103],
        font_node=records[0],tmp_node=records[1],font_bytes=sfnt(),names=['Synthetic Target','Regular'],
        label='SYNTHETIC_DYNAMIC_FONT',repertoire=[65,66],space_advance=500,license_confirmed=True)
    return {'synthetic-a':a,'synthetic-b':b},group
