from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from game_l10n.cli import build_parser, main
from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.data_plane_task import prepare_data_plane_task, record_data_plane_failure, seal_data_plane_task
from game_l10n.image_localization import record_image_localization_policy
from game_l10n.errors import ConfigurationError
from game_l10n.extractor_preparation import (
    ACTION, ATTESTATION_PATH, PROFILE_PATH, PURPOSE, check_extractor_preparation,
    apply_extractor_requirements,
    preparation_required, preparation_state, require_extractor_preparation,
)
from game_l10n.native_runtime import configure_native_runtime, native_launch_summary, record_native_launch
from game_l10n.store import stable_hash
from game_l10n.workspace import initialize_workspace, load_config, record_sdk_selection


# A genuine bounded binary-format check, not echoing the expected result.
RUNNER = '''import argparse, json, struct
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--preparation-request', required=True)
p.add_argument('--preparation-result', required=True)
a = p.parse_args()
r = json.loads(Path(a.preparation_request).read_text())
for c in r['check']['cases']:
    data = Path(c['input_path']).read_bytes()
    size = struct.unpack('<I', data[:4])[0]
    assert len(data) == size + 4
    text = data[4:].decode('utf-8')
    Path(c['actual_path']).write_bytes(json.dumps([text], ensure_ascii=False).encode('utf-8'))
Path(a.preparation_result).write_text(json.dumps({
    'schema': 'parr0t.extractor-preparation-check.v1',
    'request_id': r['request_id'], 'cases': r['check']['cases'],
}))
'''


class ExtractorPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.work, self.sdk, self.game = root / 'work', root / 'sdk', root / 'game'
        self.sdk.mkdir()
        self.game.mkdir()
        initialize_workspace(self.work, self.sdk, game_roots=[self.game], git_choice='disabled')
        establish_control_plane_entry(self.work)
        record_sdk_selection(self.work, 'private-new', target_engine='synthetic', target_version='1')
        self.config = load_config(self.work)
        self.requirements = {
            'required_capabilities': ['ordered-strings', 'unicode'],
            'required_formats': ['length-prefixed-utf8'],
            'source_metadata': {'engine': 'synthetic', 'version': '1', 'format_signatures': ['lp32-utf8']},
        }
        self.config['extractor_requirements'] = deepcopy(self.requirements)
        self._save_config()
        self.activation = {'selection_fingerprint': stable_hash(self.config['sdk_selection']),
                           'mode': 'private-new', 'target': {'engine': 'synthetic', 'version': '1'},
                           'runtime': {'content_fingerprint': 'synthetic-runtime-v1'}}
        self.activation_patch = patch('game_l10n.workspace.require_sdk_activation', side_effect=lambda *a, **kw: deepcopy(self.activation))
        self.activation_patch.start()
        self.addCleanup(self.activation_patch.stop)
        self.output = self.work / '.game-l10n/data-plane/extractor_preparation'

    def _save_config(self) -> None:
        (self.work / 'game-l10n.json').write_text(json.dumps(self.config), encoding='utf-8')

    def _stage(self) -> None:
        fixture = self.work / 'fixtures'
        fixture.mkdir(exist_ok=True)
        text = 'Synthetic café \u96ea'
        data = text.encode('utf-8')
        (fixture / 'input.bin').write_bytes(struct.pack('<I', len(data)) + data)
        (fixture / 'expected.json').write_bytes(json.dumps([text], ensure_ascii=False).encode('utf-8'))
        (self.work / 'tools/check.py').write_text(RUNNER, encoding='utf-8')
        self.profile = {
            'schema': 'parr0t.extractor-preparation-profile.v1', **deepcopy(self.requirements),
            'extractor_paths': ['tools/check.py'],
            'checks': [{'id': 'binary-unicode', 'format': 'length-prefixed-utf8',
                        'capabilities': ['ordered-strings', 'unicode'], 'runner': 'tools/check.py', 'arguments': [],
                        'cases': [{'id': 'unicode', 'input_path': 'fixtures/input.bin',
                                   'expected_path': 'fixtures/expected.json',
                                   'actual_path': '.game-l10n/preparation-output/actual.json'}]}],
        }
        (self.work / PROFILE_PATH).write_text(json.dumps(self.profile), encoding='utf-8')

    def _prepare(self) -> Path:
        result = prepare_data_plane_task(self.work, self.output, worker_role='source_probe',
                                         purpose=PURPOSE, config=self.config, sdk_activation=self.activation)
        return self.work / result['relay_path']

    def _ready(self) -> Path:
        self._stage()
        relay = self._prepare()
        result = check_extractor_preparation(self.work, relay)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['counts'], {'checks': 1, 'cases': 1})
        return relay

    def _cli(self, command: str, *arguments: str) -> dict:
        stream = io.StringIO()
        with patch('game_l10n.cli.require_sdk_activation', return_value=deepcopy(self.activation)):
            with redirect_stdout(stream):
                self.assertEqual(main([command, '--workspace', str(self.work), *arguments]), 0)
        return json.loads(stream.getvalue())

    def _cli_prepare(self) -> Path:
        prepared = self._cli('prepare-data-plane-task', '--worker-role', 'source_probe', '--purpose', PURPOSE)
        self.assertEqual(prepared['status'], 'ready')
        relay = self.work / prepared['relay_path']
        self.assertTrue(relay.is_file())
        self.assertTrue(relay.resolve().is_relative_to(self.work.resolve()))
        return relay

    def _start(self, relay_path: Path) -> None:
        relay = json.loads(relay_path.read_text())
        record_native_launch(self.work, relay_path, event='capability-available')
        record_native_launch(self.work, relay_path, event='worker-started', task='worker-0',
                             worker_id='synthetic-' + relay['task_id'], model=relay['launch_contract']['model'])

    def _success_envelope(self, relay_path: Path, artifact: str) -> Path:
        relay = json.loads(relay_path.read_text())
        envelope_path = self.work / relay['expected_envelope']
        envelope_path.write_text(json.dumps({
            'schema': 'parr0t.control-envelope.v2', 'worker_role': 'source_probe',
            'stage': 'discovery', 'status': 'success', 'target_ids': [],
            'counts': {'total': 1, 'success': 1, 'pending': 0}, 'issue_codes': [],
            'artifact_paths': [artifact], 'queue_transitions': [], 'route_evidence_path': '',
        }), encoding='utf-8')
        return envelope_path

    def _completed_discovery(self) -> dict[Path, bytes]:
        output = self.work / '.game-l10n/data-plane/source_probe'
        prepared = prepare_data_plane_task(self.work, output, worker_role='source_probe', config=self.config)
        relay = self.work / prepared['relay_path']
        envelope = self._success_envelope(relay, 'game-l10n.json')
        sealed = seal_data_plane_task(self.work, relay, worker_ended=True)
        self.assertEqual(sealed['status'], 'success')
        return {path: path.read_bytes() for path in (relay, output / 'task.json', envelope, output / 'receipt.json')}

    def test_additive_image_repair_invalidates_old_readiness_without_changing_other_policy(self) -> None:
        self._ready()
        before = load_config(self.work)
        requirements = deepcopy(self.requirements)
        requirements["required_capabilities"].append("bounded-image-font-candidate-discovery")
        requirements["required_formats"].append("image-font-resources")
        requirements["source_metadata"]["image_candidate_discovery"] = {
            "path": ".game-l10n/image-candidates.json",
            "sha256": "a" * 64,
            "requested_scope": "player_visible_text_assets",
            "output_manifest": "inputs/image-inspection-candidates.json",
            "asset_root": "inputs/image-assets",
        }
        path = self.work / ".game-l10n/image-extractor-requirements.json"
        path.write_text(json.dumps(requirements), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        apply_extractor_requirements(self.work, path, digest)
        updated = load_config(self.work)
        self.assertEqual(updated, {**before, "extractor_requirements": requirements})
        self.assertEqual(preparation_state(self.work, updated, self.activation)["status"], "pending")
        self.assertTrue((self.work / ATTESTATION_PATH).is_file())

    def test_image_repair_rejects_stale_or_narrowed_requirements_without_config_mutation(self) -> None:
        config_path = self.work / "game-l10n.json"
        original = config_path.read_bytes()
        path = self.work / ".game-l10n/image-extractor-requirements.json"
        for narrowed in (False, True):
            with self.subTest(narrowed=narrowed):
                requirements = deepcopy(self.requirements)
                if narrowed:
                    requirements["required_capabilities"] = ["bounded-image-font-candidate-discovery"]
                path.write_text(json.dumps(requirements), encoding="utf-8")
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if narrowed else "0" * 64
                with self.assertRaises(ConfigurationError):
                    apply_extractor_requirements(self.work, path, digest)
                self.assertEqual(config_path.read_bytes(), original)

    def test_opaque_image_cli_applies_repair_without_dispatching_unchanged_inventory(self) -> None:
        self._ready()
        configure_native_runtime(self.work, "codex", {})
        record_image_localization_policy(self.work, requested=True, user_requested=True)
        (self.game / "packed.pak").write_bytes(b"SYNTHETIC_OPAQUE_IMAGE_CONTAINER")
        first = self._cli("prepare-data-plane-task", "--worker-role", "image_ui_inventory")
        self.assertEqual(first["status"], "pending")
        self.assertIs(first["dispatch_allowed"], False)
        self.assertEqual(first["next_action"], ACTION)
        self.assertIs(first["extractor_requirements_applied"], True)
        self.assertEqual(first["data_plane_task"]["worker_role"], "source_probe")
        self.assertEqual(first["data_plane_task"]["purpose"], PURPOSE)
        updated = load_config(self.work)
        self.assertTrue(set(self.requirements["required_capabilities"]) <= set(
            updated["extractor_requirements"]["required_capabilities"],
        ))
        self.assertEqual(preparation_state(self.work, updated, self.activation)["status"], "pending")
        repeated = self._cli("prepare-data-plane-task", "--worker-role", "image_ui_inventory")
        self.assertIs(repeated["dispatch_allowed"], False)
        self.assertEqual(repeated["extractor_requirements_sha256"], first["extractor_requirements_sha256"])
        self.assertEqual(repeated["data_plane_task"]["purpose"], PURPOSE)

    def test_image_requirement_revisions_preserve_prior_exact_contexts(self) -> None:
        path = self.work / ".game-l10n/image-extractor-requirements.json"

        def apply(requirements: dict) -> None:
            path.write_text(json.dumps(requirements), encoding="utf-8")
            apply_extractor_requirements(
                self.work, path, hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        requirements = deepcopy(self.requirements)
        first = {"path": ".game-l10n/image-candidates.json", "sha256": "a" * 64}
        requirements["source_metadata"]["image_candidate_discovery"] = first
        apply(requirements)
        original = (self.work / "game-l10n.json").read_bytes()
        second = {**first, "sha256": "b" * 64}
        requirements["source_metadata"]["image_candidate_discovery"] = second
        with self.assertRaisesRegex(ConfigurationError, "retained history"):
            apply(requirements)
        self.assertEqual((self.work / "game-l10n.json").read_bytes(), original)
        requirements["source_metadata"]["image_candidate_discovery_history"] = [first]
        apply(requirements)
        retained = (self.work / "game-l10n.json").read_bytes()
        third = {**first, "sha256": "c" * 64}
        requirements["source_metadata"]["image_candidate_discovery"] = third
        requirements["source_metadata"]["image_candidate_discovery_history"] = [second]
        with self.assertRaisesRegex(ConfigurationError, "retained history"):
            apply(requirements)
        self.assertEqual((self.work / "game-l10n.json").read_bytes(), retained)
        requirements["source_metadata"]["image_candidate_discovery_history"] = [first, second]
        apply(requirements)
        self.assertEqual(
            load_config(self.work)["extractor_requirements"]["source_metadata"],
            {**self.requirements["source_metadata"],
             "image_candidate_discovery": third,
             "image_candidate_discovery_history": [first, second]},
        )

    def test_failed_preparation_cli_retry_keeps_discovery_and_recovers_own_status(self) -> None:
        discovery = self._completed_discovery()
        configure_native_runtime(self.work, 'codex', {})
        relay = self._cli_prepare()
        self._start(relay)
        first_task = json.loads((self.output / 'task.json').read_text())
        ledger = next(self.output.glob('*-launch.json'))
        ledger_bytes = ledger.read_bytes()
        record_data_plane_failure(self.work, relay, failure_code='provider_operational_error')
        status = self._cli('status')
        self.assertEqual(status['next_actions'], [ACTION])
        self.assertEqual(status['data_plane_task']['failure_action'], 'cancel_owned_child_then_seal_before_replacement')
        self.assertTrue(status['data_plane_task']['operational_failure']['cancellation_required'])
        sealed = self._cli('seal-data-plane-task', '--relay', relay.relative_to(self.work).as_posix(), '--worker-ended')
        self.assertEqual(sealed['receipt_path'], '.game-l10n/data-plane/extractor_preparation/receipt.json')
        self.assertIn('operational_error', sealed['issue_codes'])
        status = self._cli('status')
        self.assertEqual(status['next_actions'], [ACTION])
        self.assertEqual(status['data_plane_task']['last_receipt']['issue_codes'], sealed['issue_codes'])
        previous_task = (self.output / 'task.json').read_bytes()
        previous_receipt = (self.output / 'receipt.json').read_bytes()
        blocked = self._cli('prepare-data-plane-task', '--worker-role', 'source_probe', '--purpose', PURPOSE)
        self.assertEqual(blocked['status'], 'pending')
        self.assertEqual(blocked['next_action'], 'repair_native_runtime')
        self.assertFalse(blocked['dispatch_allowed'])
        self.assertEqual((self.output / 'task.json').read_bytes(), previous_task)
        original_model = json.loads(relay.read_text(encoding='utf-8'))['launch_contract']['model']
        configure_native_runtime(self.work, 'codex', {
            first_task['worker_route']: original_model,
            'native-unrelated-repair': 'synthetic/unrelated-model',
        })
        unrelated = self._cli('prepare-data-plane-task', '--worker-role', 'source_probe', '--purpose', PURPOSE)
        self.assertEqual(unrelated['status'], 'pending')
        self.assertEqual(unrelated['next_action'], 'repair_native_runtime')
        self.assertFalse(unrelated['dispatch_allowed'])
        self.assertEqual((self.output / 'task.json').read_bytes(), previous_task)
        configure_native_runtime(
            self.work, 'codex', {first_task['worker_route']: 'synthetic/approved-repaired-model'},
        )
        fresh_relay = self._cli_prepare()
        retried = json.loads((self.output / 'task.json').read_text())
        self.assertEqual(retried['attempt'], first_task['attempt'])
        self.assertGreater(retried['task_revision'], first_task['task_revision'])
        self.assertNotEqual(retried['task_id'], first_task['task_id'])
        self.assertEqual(native_launch_summary(self.work, fresh_relay)['started_workers'], 0)
        archive = self.output / 'history' / first_task['task_id']
        self.assertEqual((archive / 'task.json').read_bytes(), previous_task)
        self.assertEqual((archive / 'receipt.json').read_bytes(), previous_receipt)
        self.assertEqual(ledger.read_bytes(), ledger_bytes)
        for path, original in discovery.items():
            self.assertEqual(path.read_bytes(), original)

    def test_stale_success_cli_retry_binds_changed_requirements(self) -> None:
        configure_native_runtime(self.work, 'codex', {})
        self._stage()
        relay = self._cli_prepare()
        self._start(relay)
        check_extractor_preparation(self.work, relay)
        envelope = self._success_envelope(relay, ATTESTATION_PATH)
        sealed = self._cli('seal-data-plane-task', '--relay', relay.relative_to(self.work).as_posix(), '--worker-ended')
        self.assertEqual(sealed['status'], 'success')
        self.assertEqual(sealed['receipt_path'], '.game-l10n/data-plane/extractor_preparation/receipt.json')
        with self.assertRaisesRegex(ConfigurationError, 'success is already sealed'):
            record_data_plane_failure(self.work, relay, failure_code='provider_operational_error')
        first = json.loads((self.output / 'task.json').read_text())
        original_envelope = envelope.read_bytes()
        self.config['extractor_requirements']['source_metadata']['version'] = '2'
        self._save_config()
        self.assertEqual(self._cli('status')['next_actions'], [ACTION])
        self._cli_prepare()
        retried = json.loads((self.output / 'task.json').read_text())
        self.assertNotEqual(retried['task_id'], first['task_id'])
        self.assertEqual(retried['attempt'], first['attempt'] + 1)
        self.assertEqual(retried['extractor_requirements'], self.config['extractor_requirements'])
        self.assertEqual(envelope.read_bytes(), original_envelope)
        self.assertFalse((self.work / '.game-l10n/data-plane/source_probe/receipt.json').exists())
        self.profile['source_metadata'] = deepcopy(self.config['extractor_requirements']['source_metadata'])
        (self.work / PROFILE_PATH).write_text(json.dumps(self.profile), encoding='utf-8')
        self._start(relay)
        self.assertEqual(check_extractor_preparation(self.work, relay)['status'], 'ready')
        self._success_envelope(relay, ATTESTATION_PATH)
        self.assertEqual(self._cli('seal-data-plane-task', '--relay', relay.relative_to(self.work).as_posix(), '--worker-ended')['status'], 'success')
        for role in ('primary_inventory', 'independent_inventory'):
            self.assertEqual(self._inventory(role)['status'], 'ready')

    def test_preparation_runtime_repair_uses_own_receipt_and_preserves_discovery(self) -> None:
        discovery = self._completed_discovery()
        configure_native_runtime(self.work, 'codex', {})
        relay = self._cli_prepare()
        original_task = (self.output / 'task.json').read_bytes()
        record_native_launch(self.work, relay, event='model-unavailable', failure_code='host_model_unavailable')
        seal_data_plane_task(self.work, relay, worker_ended=True)
        original_receipt = (self.output / 'receipt.json').read_bytes()
        status = self._cli('status')
        self.assertEqual(status['next_actions'], [ACTION])
        configure_native_runtime(self.work, 'codex', {'native-sol': 'synthetic/approved-repaired-model'})
        self._cli_prepare()
        repaired = json.loads((self.output / 'task.json').read_text())
        original = json.loads(original_task)
        self.assertNotEqual(repaired['task_id'], original['task_id'])
        self.assertEqual(repaired['attempt'], original['attempt'])
        self.assertGreater(repaired['task_revision'], original['task_revision'])
        for field in ('source_roots', 'purpose', 'sdk_activation', 'extractor_requirements'):
            self.assertEqual(repaired[field], original[field])
        self.assertNotEqual(repaired['output_envelope'], original['output_envelope'])
        archive = self.output / 'history' / original['task_id']
        self.assertEqual((archive / 'task.json').read_bytes(), original_task)
        self.assertEqual((archive / 'receipt.json').read_bytes(), original_receipt)
        self.assertEqual(native_launch_summary(self.work, relay)['started_workers'], 0)
        for path, original in discovery.items():
            self.assertEqual(path.read_bytes(), original)

    def _inventory(self, role: str):
        return prepare_data_plane_task(self.work, self.work / '.game-l10n/data-plane' / role,
                                       worker_role=role, config=self.config, sdk_activation=self.activation)

    def test_deficient_sdk_blocks_both_inventory_admissions(self) -> None:
        for role in ('primary_inventory', 'independent_inventory'):
            with self.subTest(role=role), self.assertRaisesRegex(ConfigurationError, 'E_EXTRACTOR_PREPARATION_PENDING'):
                self._inventory(role)
            self.assertFalse((self.work / '.game-l10n/data-plane' / role / 'task.json').exists())
        self.assertEqual(preparation_state(self.work, self.config, self.activation)['next_action'], ACTION)

    def test_selection_modes_and_partial_or_declared_deficiency_need_proof(self) -> None:
        for mode in ('extend', 'derive-new', 'private-new'):
            config = {'sdk_selection': {'decision': {'mode': mode}}}
            self.assertTrue(preparation_required(self.work, config))
        config = {'sdk_selection': {'decision': {'mode': 'reuse', 'branch': 'sdk-synthetic-1'},
                                  'evaluations': {'sdk-synthetic-1': {'result': 'partial'}}}}
        self.assertTrue(preparation_required(self.work, config))
        config['sdk_selection']['evaluations'] = {}
        self.assertFalse(preparation_required(self.work, config))
        config['extractor_requirements'] = {'deficient': True}
        self.assertTrue(preparation_required(self.work, config))

    def test_exact_active_sdk_is_required_before_preparation(self) -> None:
        with patch('game_l10n.workspace.require_sdk_activation', side_effect=ConfigurationError('inactive exact SDK')):
            with self.assertRaisesRegex(ConfigurationError, 'inactive exact SDK'):
                self._prepare()
        with self.assertRaisesRegex(ConfigurationError, 'SDK_ACTIVATION'):
            prepare_data_plane_task(self.work, self.output, worker_role='source_probe', purpose=PURPOSE,
                                    config=self.config, sdk_activation={'wrong': True})
        self.assertFalse((self.output / 'task.json').exists())

    def test_success_unlocks_both_independent_inventory_tasks_without_coverage_claim(self) -> None:
        self._ready()
        for role in ('primary_inventory', 'independent_inventory'):
            result = self._inventory(role)
            self.assertEqual(result['status'], 'ready')
            task = json.loads((self.work / result['relay_path']).with_name('task.json').read_text())
            self.assertEqual(task['worker_role'], role)
            self.assertIn('counterpart_inventory_forbidden', task)
        self.assertFalse((self.work / 'inputs/primary-inventory.json').exists())
        self.assertFalse((self.work / 'inputs/independent-inventory.json').exists())
        self.assertFalse((self.work / '.game-l10n/inventory-attestation.json').exists())
        self.assertNotIn('static_complete', require_extractor_preparation(self.work, self.config, self.activation))
        self.assertNotIn('runtime_complete', require_extractor_preparation(self.work, self.config, self.activation))

    def test_tool_capability_selection_metadata_and_fixture_changes_invalidate(self) -> None:
        for mutation in ('tool', 'capability', 'selection', 'metadata', 'fixture', 'runtime', 'output'):
            with self.subTest(mutation=mutation):
                self._ready()
                original_config, original_activation = deepcopy(self.config), deepcopy(self.activation)
                if mutation == 'tool':
                    (self.work / 'tools/check.py').write_text(RUNNER + '\n# changed\n')
                elif mutation == 'capability':
                    self.config['extractor_requirements']['required_capabilities'].append('new-capability')
                elif mutation == 'selection':
                    self.config['sdk_selection']['decision']['target']['version'] = '2'
                elif mutation == 'metadata':
                    self.config['extractor_requirements']['source_metadata']['version'] = '2'
                elif mutation == 'fixture':
                    (self.work / 'fixtures/input.bin').write_bytes(b'changed')
                elif mutation == 'runtime':
                    self.activation['runtime']['content_fingerprint'] = 'changed'
                else:
                    (self.work / '.game-l10n/preparation-output/actual.json').write_bytes(b'changed')
                self.assertEqual(preparation_state(self.work, self.config, self.activation)['status'], 'pending')
                self.config, self.activation = original_config, original_activation
                self._save_config()

    def test_forged_or_unbound_attestation_never_admits(self) -> None:
        self._ready()
        path = self.work / ATTESTATION_PATH
        original = json.loads(path.read_text())
        for key, value in (('task_id', 'forged'), ('relay_path', 'missing.json'), ('binding', {}), ('checks', [])):
            with self.subTest(field=key):
                path.write_text(json.dumps({**original, key: value}))
                with self.assertRaisesRegex(ConfigurationError, 'E_EXTRACTOR_PREPARATION_PENDING'):
                    self._inventory('primary_inventory')
        path.write_text(json.dumps(original))
        result_path = self.work / original['checks'][0]['result_path']
        result = json.loads(result_path.read_text())
        result['request_id'] = 'unbound'
        result_path.write_text(json.dumps(result))
        self.assertEqual(preparation_state(self.work, self.config, self.activation)['status'], 'pending')

    def test_failed_check_clears_old_readiness_and_cannot_reuse_stale_output(self) -> None:
        relay = self._ready()
        (self.work / 'fixtures/expected.json').write_bytes(b'not the extracted text')
        with self.assertRaisesRegex(ConfigurationError, 'E_EXTRACTOR_PREPARATION_PENDING'):
            check_extractor_preparation(self.work, relay)
        self.assertEqual(json.loads((self.work / ATTESTATION_PATH).read_text())['status'], 'pending')
        self.assertEqual(preparation_state(self.work, self.config, self.activation)['status'], 'pending')

    def test_missing_format_or_capability_case_cannot_prove_readiness(self) -> None:
        self._stage()
        relay = self._prepare()
        self.profile['checks'][0]['capabilities'] = ['unicode']
        (self.work / PROFILE_PATH).write_text(json.dumps(self.profile))
        with self.assertRaisesRegex(ConfigurationError, 'E_EXTRACTOR_PREPARATION_PENDING'):
            check_extractor_preparation(self.work, relay)

    def test_bound_worker_contains_implementation_stage_not_inventory_repair(self) -> None:
        self._prepare()
        task = json.loads((self.output / 'task.json').read_text())
        self.assertEqual(task['worker_role'], 'source_probe')
        self.assertEqual(task['purpose'], PURPOSE)
        self.assertEqual(task['stage'], 'discovery')
        self.assertEqual(task['output_artifact'], ATTESTATION_PATH)
        self.assertEqual(task['extractor_requirements'], self.requirements)
        self.assertNotIn('inventory_gate', task)

    def test_status_prioritizes_activation_then_preparation_and_keeps_parent_text_free(self) -> None:
        for active in (False, True):
            stream = io.StringIO()
            with patch('game_l10n.cli.require_sdk_activation', side_effect=(lambda *a: self.activation) if active else ConfigurationError('inactive')):
                with redirect_stdout(stream):
                    self.assertEqual(main(['status', '--workspace', str(self.work)]), 0)
            result = json.loads(stream.getvalue())
            self.assertEqual(result['next_actions'], [ACTION if active else 'activate_selected_sdk_implementation'])
            if active:
                self.assertEqual(result['data_plane_task']['purpose'], PURPOSE)
                self.assertIn('--purpose extractor_preparation', result['data_plane_task']['prepare_command'])
                self.assertEqual(result['extractor_preparation']['status'], 'pending')
            self.assertNotIn('Synthetic café', stream.getvalue())

    def test_cli_exposes_bound_prepare_and_worker_check(self) -> None:
        args = build_parser().parse_args(['prepare-data-plane-task', '--workspace', str(self.work),
                                         '--worker-role', 'source_probe', '--purpose', PURPOSE])
        self.assertEqual(args.purpose, PURPOSE)
        args = build_parser().parse_args(['check-extractor-preparation', '--workspace', str(self.work), '--relay', 'relay.json'])
        self.assertEqual(args.func.__name__, 'cmd_check_extractor_preparation')

    def test_worker_success_seals_only_current_core_validated_preparation(self) -> None:
        relay_path = self._ready()
        relay = json.loads(relay_path.read_text())
        envelope_path = self.work / relay['expected_envelope']
        envelope_path.write_text(json.dumps({
            'schema': 'parr0t.control-envelope.v2', 'worker_role': 'source_probe',
            'stage': 'discovery', 'status': 'success', 'target_ids': [],
            'counts': {'total': 1, 'success': 1, 'pending': 0}, 'issue_codes': [],
            'artifact_paths': [ATTESTATION_PATH], 'queue_transitions': [],
            'route_evidence_path': '',
        }))
        receipt = seal_data_plane_task(self.work, relay_path, worker_ended=True)
        self.assertEqual(receipt['status'], 'success')
        self.assertEqual(receipt['worker_role'], 'source_probe')
        self.assertNotIn('artifact_paths', receipt)
        sealed = json.loads((self.work / receipt['receipt_path']).read_text())
        self.assertEqual(sealed['artifact_paths'], [ATTESTATION_PATH])
        self.assertNotIn('static_complete', receipt)
        (self.work / 'tools/check.py').write_text(RUNNER + '\n# stale implementation\n')
        with self.assertRaises(ConfigurationError):
            require_extractor_preparation(self.work, self.config, self.activation)
