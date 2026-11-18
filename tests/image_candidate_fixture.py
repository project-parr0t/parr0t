from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import zlib

from game_l10n.data_plane_task import prepare_data_plane_task
from game_l10n.extractor_preparation import apply_extractor_requirements, check_extractor_preparation
from game_l10n.image_localization import record_image_localization_policy
from tests.test_extractor_preparation import ExtractorPreparationTests


RUNNER = '''import argparse, hashlib, json, struct
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--preparation-request', required=True)
p.add_argument('--preparation-result', required=True)
a = p.parse_args()
r = json.loads(Path(a.preparation_request).read_text(encoding='utf-8'))
for c in r['check']['cases']:
    data = Path(c['input_path']).read_bytes()
    if r['check']['format'] == 'image-font-resources':
        assert data[:4] == b'PIMG'
        size = struct.unpack('<I', data[4:8])[0]
        pixels = data[8:]
        assert len(pixels) == size and pixels.startswith(b'\\x89PNG\\r\\n\\x1a\\n')
        asset = Path('inputs/image-assets/synthetic-decoded.png').resolve()
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(pixels)
        result = {
            'schema': 'parr0t.image-inspection-candidates.v1',
            'requested_scope': 'player_visible_ui_text_assets',
            'bound_local_assets': [{'path': str(asset), 'sha256': hashlib.sha256(pixels).hexdigest()}],
            'unexamined_sources': [],
            'source_bindings': [{'source_root_index': 0, 'relative_path': 'packed.pak',
                                 'sha256': hashlib.sha256(data).hexdigest(), 'status': 'complete'}],
        }
    else:
        size = struct.unpack('<I', data[:4])[0]
        assert len(data) == size + 4
        result = [data[4:].decode('utf-8')]
    Path(c['actual_path']).write_bytes(json.dumps(result, ensure_ascii=False).encode('utf-8'))
Path(a.preparation_result).write_text(json.dumps({
    'schema': 'parr0t.extractor-preparation-check.v1',
    'request_id': r['request_id'], 'cases': r['check']['cases'],
}), encoding='utf-8')
'''


def ready_image_candidate_fixture(test_case):
    fixture = ExtractorPreparationTests('test_cli_exposes_bound_prepare_and_worker_check')
    fixture.setUp()
    test_case.addCleanup(fixture.doCleanups)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))

    pixels = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 32, 8, 6, 0, 0, 0))
    pixels += chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff\xff\xff\xff' * 64) * 32)) + chunk(b'IEND', b'')
    package = b'PIMG' + struct.pack('<I', len(pixels)) + pixels
    (fixture.game / 'packed.pak').write_bytes(package)
    record_image_localization_policy(fixture.work, requested=True, user_requested=True)
    initial = prepare_data_plane_task(
        fixture.work, fixture.work / '.game-l10n/data-plane/image_ui_inventory',
        worker_role='image_ui_inventory', config=fixture.config, sdk_activation=fixture.activation,
    )
    test_case.assertEqual(initial['next_action'], 'prepare_and_validate_extractors')
    test_case.assertFalse(initial['dispatch_allowed'])
    requirements = apply_extractor_requirements(
        fixture.work, Path(initial['extractor_requirements_path']), initial['extractor_requirements_sha256'],
    )
    fixture.config['extractor_requirements'] = requirements
    fixture.requirements = deepcopy(requirements)
    fixture._stage()
    (fixture.work / 'fixtures/image-package.bin').write_bytes(package)
    asset = (fixture.work / 'inputs/image-assets/synthetic-decoded.png').resolve()
    expected = {
        'schema': 'parr0t.image-inspection-candidates.v1',
        'requested_scope': 'player_visible_ui_text_assets',
        'bound_local_assets': [{'path': str(asset), 'sha256': hashlib.sha256(pixels).hexdigest()}],
        'unexamined_sources': [],
        'source_bindings': [{'source_root_index': 0, 'relative_path': 'packed.pak',
                             'sha256': hashlib.sha256(package).hexdigest(), 'status': 'complete'}],
    }
    (fixture.work / 'fixtures/image-expected.json').write_bytes(json.dumps(expected, ensure_ascii=False).encode('utf-8'))
    fixture.profile['checks'].append({
        'id': 'image-resources', 'format': 'image-font-resources',
        'capabilities': ['bounded-image-font-candidate-discovery'], 'runner': 'tools/check.py', 'arguments': [],
        'cases': [{'id': 'one-image', 'input_path': 'fixtures/image-package.bin',
                   'expected_path': 'fixtures/image-expected.json', 'actual_path': 'inputs/image-inspection-candidates.json'}],
    })
    (fixture.work / 'tools/check.py').write_text(RUNNER, encoding='utf-8')
    (fixture.work / 'tools/extractor-preparation.json').write_text(json.dumps(fixture.profile), encoding='utf-8')
    relay = fixture._prepare()
    checked = check_extractor_preparation(fixture.work, relay)
    test_case.assertEqual(checked['status'], 'ready')
    fixture.activation_patch.stop()
    return fixture, initial, expected
