"""Private binding-plan interface for the bounded Unity 6000.4 profile."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from unity_serialized import (aligned_behaviour_node, apply_serialized,
                              bundle_members, rebuild_bundle)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def within(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise ValueError('PATH_OUTSIDE_BOUNDARY')
    return path


def relative(root, value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('RELATIVE_PATH_INVALID')
    p = Path(value)
    if p.is_absolute() or '..' in p.parts or ':' in value:
        raise ValueError('RELATIVE_PATH_INVALID')
    return within(Path(root) / p, root)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def configuration(source, output):
    value = os.environ.get('GAME_L10N_WORKDIR')
    plan_value = os.environ.get('GAME_L10N_BINDINGS')
    if not value or not plan_value:
        raise ValueError('WORKSPACE_AND_BINDINGS_REQUIRED')
    workspace = Path(value).resolve()
    output = within(output, workspace)
    source = Path(source).resolve()
    repository = Path(__file__).resolve().parents[3]
    if (not workspace.is_dir() or output.exists() or output.is_relative_to(source)
            or workspace.is_relative_to(source) or source.is_relative_to(workspace)
            or workspace.is_relative_to(repository) or repository.is_relative_to(workspace)
            or output.is_relative_to(repository)):
        raise ValueError('SOURCE_WORKSPACE_SEPARATION_REQUIRED')
    plan = read_json(within(plan_value, workspace))
    if (plan.get('schema_version') != 1 or plan.get('engine') != 'unity'
            or not re.fullmatch(r'6000\.4\.\d+[abfp]\d+', plan.get('engine_build', ''))
            or plan.get('backend') != 'Mono'):
        raise ValueError('PROFILE_IDENTITY_NOT_BOUND')
    files = plan.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('BINDINGS_EMPTY')
    paths, ids = set(), set()
    for file in files:
        path = relative(source, file['path'])
        if path in paths or file.get('kind') not in {'unityfs', 'managed'}:
            raise ValueError('SOURCE_FILE_BINDING_AMBIGUOUS')
        paths.add(path)
        if not path.is_file() or digest(path.read_bytes()) != file.get('sha256'):
            raise ValueError('SOURCE_FILE_CHANGED')
        if not file.get('bindings'):
            raise ValueError('FILE_BINDINGS_EMPTY')
        for binding in file['bindings']:
            if (not isinstance(binding.get('id'), str) or not binding['id']
                    or binding['id'] in ids or not isinstance(binding.get('source'), str)):
                raise ValueError('TARGET_ID_BINDING_AMBIGUOUS')
            ids.add(binding['id'])
    return workspace, plan


def generator(workspace, plan):
    path = plan.get('typetree_dll_directory')
    if path is None:
        return None
    from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
    directory = relative(workspace, path)
    if not directory.is_dir():
        raise ValueError('TYPE_TREE_ASSEMBLY_DIRECTORY_MISSING')
    result = TypeTreeGenerator(plan['engine_build'])
    result.load_local_dll_folder(str(directory))
    return result


def node_bindings(file):
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    return {int(oid): (aligned_behaviour_node(record['records']) if record.get('align_enabled')
            else TypeTreeNode.from_list(record['records']))
            for oid, record in file.get('nodes', {}).items()}


def apply_members(raw, file, targets, gen):
    _env, members = bundle_members(raw)
    by_member = {}
    for binding in file['bindings']:
        row = dict(binding, target=targets[binding['id']])
        by_member.setdefault(binding['member'], []).append(row)
    changed = {}
    for name, rows in by_member.items():
        if name not in members:
            raise ValueError('BUNDLE_MEMBER_MISSING')
        changed[name] = apply_serialized(members[name], rows, generator=gen,
            nodes=node_bindings(file.get('member_options', {}).get(name, {})))
    return members, changed


def managed(input_path, output_path, file, targets, workspace, *, inspect=False):
    writer = os.environ.get('GAME_L10N_MANAGED_WRITER')
    if not writer or not Path(writer).is_file():
        raise ValueError('MANAGED_WRITER_REQUIRED')
    request = {'schema_version': 1, 'input': str(input_path), 'output': str(output_path),
        'sha256': file['sha256'], 'protected_methods': file.get('protected_methods', []),
        'operation': 'inspect' if inspect else 'apply',
        'literals': [dict(row, target=targets[row['id']]) for row in file['bindings']]}
    with tempfile.TemporaryDirectory(prefix='sdk-managed-', dir=workspace) as directory:
        request_path = Path(directory)/'request.json'
        write_json(request_path, request)
        completed = subprocess.run([str(Path(writer).resolve()), str(request_path)],
            capture_output=True, shell=False, check=False)
        if completed.returncode:
            raise ValueError('MANAGED_BINDING_OR_READBACK_FAILED')


def extract_inventory(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    workspace, plan = configuration(source, output)
    gen = generator(workspace, plan)
    rows = [dict(id=b['id'], source=b['source'], order=i,
                 metadata={'sdk_profile': 'sdk-unity-6000.4', 'binding_required': True})
            for i, b in enumerate(b for f in plan['files'] for b in f['bindings'])]
    targets = {row['id']: row['source'] for row in rows}
    for file in plan['files']:
        path = relative(source, file['path'])
        if file['kind'] == 'unityfs':
            apply_members(path.read_bytes(), file, targets, gen)
        else:
            managed(path, workspace/'synthetic-unused-inspection-output.dll', file,
                    targets, workspace, inspect=True)
        if digest(path.read_bytes()) != file['sha256']:
            raise ValueError('SOURCE_CHANGED_DURING_INVENTORY')
    output.mkdir(parents=True)
    write_json(output/'segments.json', {'segments': rows})
    write_json(output/'coverage.json', {'static_complete': False, 'runtime_verified': False,
        'scope': 'explicit_bindings_only', 'independent_inventory_required': True})
    return {'status': 'bound', 'segments': len(rows), 'static_complete': False}


def translations(plan, result_path):
    from game_l10n.validators import protected_tokens
    text = Path(result_path).read_text(encoding='utf-8-sig')
    try:
        records = json.loads(text)
    except json.JSONDecodeError:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(records, dict):
        records = records.get('results')
    bindings = {b['id']: b for f in plan['files'] for b in f['bindings']}
    if not isinstance(records, list) or len(records) != len(bindings):
        raise ValueError('RESULT_COVERAGE_NOT_EXACT')
    targets = {}
    for row in records:
        identity = row.get('segment_id')
        if identity not in bindings or identity in targets:
            raise ValueError('RESULT_ID_AMBIGUOUS')
        source, target = bindings[identity]['source'], row.get('text')
        if (row.get('source') != source or not isinstance(target, str)
                or row.get('status') not in {'reviewed', 'source_preserved'}
                or (row['status'] == 'source_preserved' and target != source)):
            raise ValueError('RESULT_SOURCE_OR_STATUS_CHANGED')
        if (protected_tokens(source, [r'\{\d+\}']) != protected_tokens(target, [r'\{\d+\}'])
                or any(source.count(c) != target.count(c) for c in '\r\n\t')):
            raise ValueError('RESULT_PROTECTED_TOKENS_CHANGED')
        targets[identity] = target
    return targets


def build_payload(result_path, output):
    from game_l10n.tooling import _require_pack_input_binding, record_verified_pack_artifact
    if os.environ.get('GAME_L10N_STAGE') != 'pack':
        raise ValueError('PACK_STAGE_REQUIRED')
    workspace_value = os.environ.get('GAME_L10N_WORKDIR', '')
    if not workspace_value:
        raise ValueError('WORKSPACE_AND_BINDINGS_REQUIRED')
    workspace = Path(workspace_value).resolve()
    result_path = within(result_path, workspace)
    _require_pack_input_binding(workspace, result_path)
    plan = read_json(within(os.environ.get('GAME_L10N_BINDINGS', ''), workspace_value))
    source = Path(plan['game_root']).resolve()
    workspace, plan = configuration(source, output)
    output = Path(output).resolve()
    targets, gen = translations(plan, result_path), generator(workspace, plan)
    # Reuse the core's own preflight; its public verified-pack API repeats the
    # binding check after reconstruction before creating valid pack state.
    output.mkdir(parents=True)
    for file in plan['files']:
        path, destination = relative(source, file['path']), relative(output, file['path'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = path.read_bytes()
        if file['kind'] == 'managed':
            managed(path, destination, file, targets, workspace)
        else:
            members, changed = apply_members(raw, file, targets, gen)
            if file.get('font_groups'):
                from unity_fonts import integrate
                groups = []
                for record in file['font_groups']:
                    record = dict(record)
                    font_path = relative(workspace, record.pop('font_path'))
                    record['font_bytes'] = font_path.read_bytes()
                    if digest(record['font_bytes']) != record.pop('font_sha256'):
                        raise ValueError('FONT_SOURCE_CHANGED')
                    groups.append(record)
                affected = {g['member'] for g in groups} | {b['member'] for b in file.get('fallbacks', [])}
                prepared = {name: changed.get(name, members[name]) for name in affected}
                changed.update(integrate(prepared, groups, file.get('fallbacks', [])))
            destination.write_bytes(rebuild_bundle(raw, changed))
        if digest(path.read_bytes()) != file['sha256']:
            raise ValueError('SOURCE_CHANGED_DURING_PACK')
    state = record_verified_pack_artifact(input_path=result_path, output_path=output,
        work_dir=workspace, verifier_fingerprint='sdk-unity-6000.4:bound-roundtrip-v1')
    return {'status': state['status'], 'files': len(plan['files']), 'runtime_verified': False}
