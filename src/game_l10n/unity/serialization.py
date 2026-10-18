"""Exact typed field rewrites; callers own immutable inputs and new outputs."""
from __future__ import annotations
import copy
import struct


def read_complete_typetree(obj, *, node=None):
    """Read indexed v1 managed references through their exact terminal frame.

    A v1 tree describes one repeated reference record, not its instance count.
    Empty and populated registries end with the same terminal managed type.
    All object bytes remain accounted for; no untyped tail is accepted.
    """
    from UnityPy.helpers import TypeTreeHelper as helper
    from UnityPy.streams import EndianBinaryReader
    root = obj._get_typetree_node() if node is None else obj._get_typetree_node(node)

    def contains_registry(node):
        return node.m_Type == "ManagedReferencesRegistry" or any(
            contains_registry(child) for child in node.m_Children)

    if not contains_registry(root):
        return obj.read_typetree(root, check_read=True)
    raw = obj.get_raw_data()
    reader = EndianBinaryReader(raw, obj.reader.endian)
    config = helper.TypeTreeConfig(True, obj.assets_file, False)
    terminal = {"class": "Terminus", "ns": "UnityEngine.DMAT", "asm": "FAKE_ASM"}

    def read(node, cfg):
        if not contains_registry(node):
            return helper.read_value(node, reader, cfg)
        align = helper.metaflag_is_aligned(node.m_MetaFlag)
        children = node.m_Children
        if node.m_Type == "ManagedReferencesRegistry":
            if not (len(children) == 2 and children[0].m_Type == "int"
                    and children[0].m_Name == "version" and children[1].m_Type == "ReferencedObject"):
                return helper.read_value(node, reader, cfg)
            version = reader.read_int()
            if version != 1:
                raise ValueError("managed_reference_version_invalid")
            record = children[1]
            if (len(record.m_Children) != 2 or record.m_Children[0].m_Type != "ReferencedManagedType"
                    or record.m_Children[1].m_Type != "ReferencedObjectData"):
                raise ValueError("managed_reference_schema_invalid")
            value = {"version": version}
            index = 0
            while True:
                typ = helper.read_value(record.m_Children[0], reader, cfg)
                if typ == terminal:
                    break
                if typ.get("class") == "Terminus" or index > len(raw) // 12:
                    raise ValueError("managed_reference_terminator_invalid")
                from .schema import reference_schema
                ref_node = reference_schema(typ, cfg.assetsfile)
                if ref_node is None:
                    raise ValueError("managed_reference_schema_invalid")
                value[f"{index:08x}"] = {"type": typ, "data": read(ref_node, cfg)}
                index += 1
        elif children and children[0].m_Type == "Array":
            align |= helper.metaflag_is_aligned(children[0].m_MetaFlag)
            size = reader.read_int()
            if size < 0 or size > len(raw):
                raise ValueError("managed_reference_array_size_invalid")
            value = [read(children[0].m_Children[1], cfg) for _ in range(size)]
        else:
            value = {}
            for child in children:
                if child.m_Type == "ManagedReferencesRegistry":
                    if cfg.has_registry:
                        continue
                    cfg = cfg.copy()
                    cfg.has_registry = True
                value[child.m_Name] = read(child, cfg)
        if align:
            reader.align_stream()
        return value

    fields = read(root, config)
    if reader.Position != len(raw):
        raise ValueError("managed_reference_byte_accounting_invalid")
    return fields

def rewrite_fields(obj, node, replacements):
    """Return a rebuilt object; exact string paths and source guards are mandatory.

    No container is saved and no source handle is written. The caller retains
    source ownership and supplies the later artifact recipe.
    """
    from UnityPy.helpers import TypeTreeHelper
    from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter
    before = obj.get_raw_data()
    original = read_complete_typetree(obj, node=node)
    writer = EndianBinaryWriter(endian=obj.reader.endian)
    TypeTreeHelper.write_typetree(original, node, writer, obj.assets_file)
    if writer.bytes != before:
        raise ValueError('schema_lossless_roundtrip_required')
    fields = copy.deepcopy(original)
    used = set()
    for row in replacements:
        path = row.get('path')
        if not isinstance(path, list) or not path or any(type(k) not in (str, int) for k in path):
            raise ValueError('schema_field_path_invalid')
        key = tuple(path)
        if key in used:
            raise ValueError('schema_field_duplicate')
        used.add(key)
        parent = fields
        try:
            for item in path[:-1]:
                parent = parent[item]
            value = parent[path[-1]]
        except (KeyError, IndexError, TypeError):
            raise ValueError('schema_field_missing') from None
        if not isinstance(value, str) or value != row.get('source') or not isinstance(row.get('text'), str):
            raise ValueError('schema_field_source_mismatch')
        parent[path[-1]] = row['text']
    writer = EndianBinaryWriter(endian=obj.reader.endian)
    TypeTreeHelper.write_typetree(fields, node, writer, obj.assets_file)
    raw = writer.bytes
    reader = EndianBinaryReader(raw, obj.reader.endian)
    checked = TypeTreeHelper.read_typetree(node, reader, as_dict=True, assetsfile=obj.assets_file,
                                          byte_size=len(raw), check_read=True)
    if checked != fields or obj.get_raw_data() != before:
        raise ValueError('schema_rebuild_verification_failed')
    return raw

def get(d,p):
 for k in p:d=d[k]
 return d

def put(d,p,v):
 for k in p[:-1]:d=d[k]
 d[p[-1]]=v

def rewrite_registry_fields(o,n,repls):
 # Focused fallback for v1 managed-reference registry writer limitation.
 before=o.get_raw_data();original=read_complete_typetree(o,node=n);expected=copy.deepcopy(original);raw=before
 for r in repls:
  assert get(expected,r['path'])==r['source'];put(expected,r['path'],r['text'])
  if r['source']==r['text']:continue
  old=r['source'].encode('utf-8');new=r['text'].encode('utf-8');needle=struct.pack(o.reader.endian+'i',len(old))+old
  hits=[];start=0
  while True:
   pos=raw.find(needle,start)
   if pos<0:break
   hits.append(pos);start=pos+1
  candidates=[]
  for pos in hits:
   end=pos+len(needle);aligned=(end+3)&~3
   if raw[end:aligned]!=b'\0'*(aligned-end):continue
   fresh=struct.pack(o.reader.endian+'i',len(new))+new;fresh+=b'\0'*((-(pos+len(fresh)))%4);trial=raw[:pos]+fresh+raw[aligned:]
   class Proxy:
    def __getattr__(self,k):return getattr(o,k)
    def get_raw_data(self):return trial
   try:checked=read_complete_typetree(Proxy(),node=n)
   except Exception:continue
   if checked==expected:candidates.append(trial)
  candidates=list(dict.fromkeys(candidates))
  if len(candidates)!=1:raise ValueError('registry_exact_field_roundtrip_not_unique:'+str((len(old),len(new),len(hits),len(candidates),r['source']==r['text'])))
  raw=candidates[0]
 return raw
