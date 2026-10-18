"""Offline metadata schemas for the Unity 2021.1 Mono compatibility line."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path


def normalize_generated_schema(generated):
    """Clone generated nodes and disambiguate primitive collection wrappers.

    A scalar string has Array<char>; a generated string collection instead
    has Array<string>. Use the helper's vector dispatch for collections so
    both its reader and writer preserve their count and element boundaries.
    """
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    from UnityPy.helpers.TypeTreeHelper import FUNCTION_READ_MAP
    rows = []
    for current in generated.traverse():
        row = {k: (getattr(current, k) if getattr(current, k) is not None else 0)
               for k in ('m_Level', 'm_Type', 'm_Name', 'm_ByteSize', 'm_Version', 'm_MetaFlag')}
        rows.append(row)
        children = current.m_Children
        if current.m_Type not in FUNCTION_READ_MAP or len(children) != 1 or children[0].m_Type != 'Array':
            continue
        array = children[0]
        if (len(array.m_Children) != 2 or array.m_Children[0].m_Type != 'int'
                or array.m_Children[0].m_Name != 'size'):
            raise ValueError('schema_generated_array_invalid')
        if current.m_Type == 'string' and array.m_Children[1].m_Type == 'char':
            continue
        row['m_Type'] = 'vector'
    # Construct with the normalized type: the optional accelerated parser
    # caches primitive dispatch when each node is created.
    return TypeTreeNode.from_list(rows)

def plain_reference_schema(generated, native):
    """Remove only the generator's verified artificial native root prefix.

    This is called exclusively for a registry instance, never for a native
    MonoBehaviour object or an authoritative embedded reference schema.
    """
    node = normalize_generated_schema(generated)
    prefix = native.m_Children
    if ([(n.m_Name, n.m_Type) for n in prefix]
            != [(n.m_Name, n.m_Type) for n in node.m_Children[:len(prefix)]]):
        raise ValueError('schema_reference_native_prefix_mismatch')
    node.m_Children = node.m_Children[len(prefix):]
    return node

def reference_schema(typ, assetsfile):
    """Prefer an embedded registry schema, then bound offline metadata."""
    if not isinstance(typ, dict) or any(not isinstance(typ.get(k), str) for k in ('class', 'ns', 'asm')):
        raise ValueError('managed_reference_type_invalid')
    if typ['class'] == '':
        return None
    for candidate in getattr(assetsfile, 'ref_types', ()) or ():
        if (candidate.m_ClassName, candidate.m_NameSpace, candidate.m_AssemblyName) == (
                typ['class'], typ['ns'], typ['asm']):
            embedded = getattr(candidate, 'nodes', None)
            if embedded is None:
                embedded = getattr(candidate, 'node', None)
            if embedded is not None:
                return embedded
    generator = getattr(getattr(assetsfile, 'environment', None), 'typetree_generator', None)
    if generator is None or not hasattr(generator, 'get_reference_nodes'):
        raise ValueError('managed_reference_schema_invalid')
    return generator.get_reference_nodes(typ['asm'], '.'.join(filter(None, (typ['ns'], typ['class']))))

def native_prefix_schema(generated, native):
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    def clone(root):
        return TypeTreeNode.from_list([
            {k:(getattr(n,k) if getattr(n,k) is not None else 0) for k in ('m_Level','m_Type','m_Name','m_ByteSize','m_Version','m_MetaFlag')}
            for n in root.traverse()])
    node, native = clone(generated), clone(native)
    prefix = native.m_Children
    if [n.m_Name for n in node.m_Children[:len(prefix)]] != [n.m_Name for n in prefix]:
        raise ValueError('schema_native_prefix_mismatch')
    node.m_Children[:len(prefix)] = prefix
    return node

class ManagedSchemas:
    def __init__(self, version, managed_roots, allowed_roots):
        if not version.startswith('2021.1.'):
            raise ValueError('schema_target_version_mismatch')
        from . import require_dependencies
        require_dependencies()
        from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
        self.generator = TypeTreeGenerator(version)
        self.version = tuple(map(int, re.findall(r'\d+', version)))
        self.cache = {}
        self.references = {}
        seen = {}
        for directory in managed_roots:
            directory = Path(directory).resolve()
            if not directory.is_dir() or not any(directory.is_relative_to(p) for p in allowed_roots):
                raise ValueError('schema_reference_root_invalid')
            for path in sorted(directory.glob('*.dll')):
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if path.name in seen and seen[path.name] != digest:
                    raise ValueError('schema_reference_name_collision')
                self.references[path] = digest
                if path.name not in seen:
                    self.generator.load_dll(raw)
                    seen[path.name] = digest
        if not seen:
            raise ValueError('schema_references_missing')

    def get_nodes_up(self, assembly, fullname):
        # UnityPy emits a leading period for types in the global namespace.
        key = (assembly, fullname.lstrip('.'))
        if key not in self.cache:
            from UnityPy.helpers.Tpk import get_typetree_node
            # The generator's MonoBehaviour prefix aligns PPtr after reading
            # it; the release serializer aligns m_Enabled before that PPtr.
            # Use the versioned native release schema as the authority.
            self.cache[key] = native_prefix_schema(normalize_generated_schema(self.generator.get_nodes_up(*key)),
                                                   get_typetree_node(114, self.version))
        return self.cache[key]

    def get_reference_nodes(self, assembly, fullname):
        # Keep registry-instance and native-object roots in separate caches.
        key = ('reference', assembly, fullname.lstrip('.'))
        if key not in self.cache:
            from UnityPy.helpers.Tpk import get_typetree_node
            self.cache[key] = plain_reference_schema(self.generator.get_nodes_up(*key[1:]),
                                                      get_typetree_node(114, self.version))
        return self.cache[key]

    def verify_unchanged(self):
        if any(hashlib.sha256(p.read_bytes()).hexdigest() != value for p, value in self.references.items()):
            raise ValueError('schema_reference_changed')
