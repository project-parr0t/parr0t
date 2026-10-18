"""Preserve catalog identities while rebinding a rebuilt UnityFS bundle."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import struct
import zlib
from UnityPy.files import BundleFile
from UnityPy.streams import EndianBinaryReader


def block_data(raw):
 # Parse only the UnityFS container; no managed assembly is loaded or executed.
 reader=EndianBinaryReader(raw)
 signature=reader.read_string_to_null();version=reader.read_u_int();vp=reader.read_string_to_null();ve=reader.read_string_to_null()
 assert signature=='UnityFS'
 obj=object.__new__(BundleFile);obj.signature=signature;obj.version=version;obj.version_player=vp;obj.version_engine=ve
 nodes,blocks=obj.read_fs(reader);blocks.Position=0;return blocks.read_bytes(blocks.Length)

def crc(raw):return zlib.crc32(block_data(raw))

def entries(catalog):
 raw=base64.b64decode(catalog['m_EntryDataString'],validate=True);n=struct.unpack_from('<i',raw)[0]
 assert len(raw)==4+28*n
 return [list(struct.unpack_from('<7i',raw,4+28*i)) for i in range(n)],raw

def options_at(extra,offset):
 p=offset;assert extra[p]==7;p+=1
 n=extra[p];p+=1;assembly=extra[p:p+n].decode('ascii');p+=n
 n=extra[p];p+=1;typ=extra[p:p+n].decode('ascii');p+=n
 assert assembly.startswith('Unity.ResourceManager,') and typ=='UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions'
 length_pos=p;n=struct.unpack_from('<i',extra,p)[0];p+=4
 assert n>0 and n%2==0 and p+n<=len(extra)
 data=json.loads(extra[p:p+n].decode('utf-16-le'))
 return data,length_pos,p+n

def patch_catalog(raw,bundle_suffix,bundle):
 catalog=json.loads(raw.decode('utf-8-sig'));before=copy.deepcopy(catalog);rows,entry_raw=entries(catalog);extra=base64.b64decode(catalog['m_ExtraDataString'],validate=True)
 found=[(i,r) for i,r in enumerate(rows) if catalog['m_InternalIds'][r[0]].replace('\\','/').endswith('/'+bundle_suffix)]
 assert len(found)==1
 index,row=found[0];assert catalog['m_ProviderIds'][row[1]]=='UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider'
 offset=row[4];old,length_pos,end=options_at(extra,offset);new=copy.deepcopy(old)
 # The hash is a cache identity in Addressables, not an additional AssetBundle CRC.
 # Content-addressing it avoids reusing an old cached download after this rebuild.
 new.update(m_Crc=crc(bundle),m_BundleSize=len(bundle),m_Hash=hashlib.md5(bundle).hexdigest())
 assert new['m_Crc']!=0
 option_bytes=json.dumps(new,ensure_ascii=False,separators=(',',':')).encode('utf-16-le')
 fresh=extra[:length_pos]+struct.pack('<i',len(option_bytes))+option_bytes+extra[end:]
 delta=len(fresh)-len(extra);new_rows=copy.deepcopy(rows)
 for r in new_rows:
  if r[4]>offset:r[4]+=delta
 entry_fresh=struct.pack('<i',len(new_rows))+b''.join(struct.pack('<7i',*r) for r in new_rows)
 replacements={'m_ExtraDataString':base64.b64encode(fresh).decode('ascii')}
 if entry_fresh!=entry_raw:replacements['m_EntryDataString']=base64.b64encode(entry_fresh).decode('ascii')
 result=raw
 for key,value in replacements.items():
  old_quoted=json.dumps(before[key]).encode('ascii');new_quoted=json.dumps(value).encode('ascii');assert result.count(old_quoted)==1;result=result.replace(old_quoted,new_quoted)
 after=json.loads(result.decode('utf-8-sig'));expected=copy.deepcopy(before);expected.update(replacements);assert after==expected
 arows,_=entries(after);aextra=base64.b64decode(after['m_ExtraDataString']);checked,_,_=options_at(aextra,arows[index][4]);assert checked==new
 # All catalog keys, labels, paths, providers, dependencies and resource identities stay exact.
 assert all(after[k]==before[k] for k in before if k not in replacements)
 for i,(r0,r1) in enumerate(zip(rows,arows)):
  assert all(r0[k]==r1[k] for k in range(7) if k!=4)
  if r0[4]>=0:
   if i==index:continue
   try:o0,_,_=options_at(extra,r0[4]);o1,_,_=options_at(aextra,r1[4]);assert o0==o1
   except AssertionError:
    # Non-option data records may exist; ensure their bytes are unchanged as slices.
    next0=min([r[4] for r in rows if r[4]>r0[4]]+[len(extra)]);next1=min([r[4] for r in arows if r[4]>r1[4]]+[len(aextra)]);assert extra[r0[4]:next0]==aextra[r1[4]:next1]
 return result,{'entry_index':index,'original':old,'replacement':new,'extra_offset_delta':delta,'catalog_keys_ids_urls_and_other_records_preserved':True}
