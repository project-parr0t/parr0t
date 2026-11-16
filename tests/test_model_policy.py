from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.errors import ConfigurationError
from game_l10n.model_policy import NATIVE_ROLE_ROUTES, apply_native_role_models
from game_l10n.native_runtime import (
    RUNTIME_PATH, configure_native_runtime, launch_contract, native_runtime,
)
from game_l10n.provider_policy import MODE_LOCK_PATH
from game_l10n.review_policy import configure_review_policy, review_policy_status
from game_l10n.workspace import _write_config, initialize_workspace, load_config


class PromptModelPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sdk = self.root / "sdk"
        self.sdk.mkdir()
        self.work = self.root / "workspace"
        with patch("game_l10n.workspace.inventory_sdk_branches", return_value={
            "source": "fixture", "fingerprint": "fixture", "candidates": [],
        }):
            initialize_workspace(self.work, self.sdk, git_choice="disabled")
        establish_control_plane_entry(self.work)
        self.path = self.work / "game-l10n.json"
        self.model = "fixture-gateway/exact-review@fixture-route:high"

    def bind(self, **kwargs):
        return configure_native_runtime(self.work, "omp", {}, omp_isolation="prompt", **kwargs)

    def test_explicit_fallback_replaces_default_judge_without_changing_connector_or_mode(self):
        before = load_config(self.work)
        lock = (self.work / MODE_LOCK_PATH).read_bytes()
        self.bind()
        result = configure_review_policy(
            self.work, model=self.model, max_calls=4, user_requested=True,
        )
        after = load_config(self.work)
        self.assertEqual(result["status"], "configured")
        self.assertEqual(after["routing"]["reviewers"], ["native-sol", "native-review-fallback"])
        self.assertEqual(after["routing"]["review_attempt_limits"]["native-review-fallback"], 4)
        self.assertEqual(after["providers"], before["providers"])
        self.assertEqual(after["routing"]["translators"], before["routing"]["translators"])
        self.assertEqual(after["routing"]["rewriters"], before["routing"]["rewriters"])
        self.assertEqual((self.work / MODE_LOCK_PATH).read_bytes(), lock)
        contract = launch_contract(self.work, "native-review-fallback", ["fixture-task"])
        self.assertEqual(contract["model"], self.model)
        self.assertFalse(contract["inherit_model_allowed"])
        self.assertEqual(contract["model_authorization"]["status"], "approved")
        self.assertEqual(contract["model_authorization"]["source"], "user_prompt")
        self.assertFalse(contract["model_authorization"]["additional_allowlist_required"])

    def test_request_and_four_call_limit_survive_until_native_host_is_bound(self):
        result = configure_review_policy(
            self.work, model=self.model, max_calls=4, user_requested=True,
        )
        self.assertEqual(result["status"], "native_binding_required")
        self.assertEqual(result["native_binding_pending"], ["native-review-fallback"])
        self.assertEqual(result["model"], self.model)
        self.assertEqual(result["review_attempt_limits"]["native-review-fallback"], 4)
        authorization = result["model_authorizations"]["native-review-fallback"]
        self.assertEqual(authorization["status"], "approved")
        self.assertEqual(authorization["scope"], "all_sdks")
        self.assertEqual(authorization["model"], self.model)
        self.assertFalse((self.work / RUNTIME_PATH).exists())
        self.bind()
        policy = review_policy_status(load_config(self.work), self.work)
        self.assertEqual(policy["status"], "configured")
        self.assertEqual(launch_contract(self.work, "native-review-fallback", ["fixture"])["model"], self.model)
        self.assertEqual(policy["review_attempt_limits"]["native-review-fallback"], 4)

    def test_independent_role_models_win_over_defaults_and_survive_rebinding(self):
        models = {role: f"fixture/{role}-exact@route:high" for role in NATIVE_ROLE_ROUTES}
        before = load_config(self.work)
        self.bind(role_models=models, user_requested_models=True)
        self.bind()
        after = load_config(self.work)
        for role, route in NATIVE_ROLE_ROUTES.items():
            with self.subTest(role=role):
                self.assertEqual(launch_contract(self.work, route, ["fixture"])["model"], models[role])
        self.assertEqual(after["routing"]["translator_retries"], {"native-translation": 5, "native-sol": 3})
        self.assertEqual(after["routing"]["context_analyzers"], ["native-context", "native-luna"])
        self.assertEqual(after["providers"], before["providers"])
        self.assertEqual(after["model_policy"]["native_roles"], models)

    def test_partial_role_update_preserves_previous_user_choice_and_other_operator_bindings(self):
        configure_native_runtime(self.work, "omp", {"native-sol": "fixture/operator-sol"}, omp_isolation="prompt")
        self.bind(role_models={"translation": "fixture/translator"}, user_requested_models=True)
        self.bind(role_models={"triage": "fixture/triager"}, user_requested_models=True)
        runtime = native_runtime(self.work)
        self.assertEqual(runtime["models"]["native-sol"], "fixture/operator-sol")
        self.assertEqual(runtime["models"]["native-translation"], "fixture/translator")
        self.assertEqual(runtime["models"]["native-triage"], "fixture/triager")
        self.assertEqual(load_config(self.work)["routing"]["reviewers"], ["native-sol", "deepseek"])

    def test_role_selection_needs_explicit_user_authorization(self):
        self.bind()
        before = self.path.read_bytes()
        runtime = (self.work / RUNTIME_PATH).read_bytes()
        with self.assertRaisesRegex(ValueError, "MODEL_POLICY_USER_REQUEST_REQUIRED"):
            self.bind(role_models={"triage": "fixture/unapproved"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.work / RUNTIME_PATH).read_bytes(), runtime)

    def test_generic_route_override_cannot_replace_a_recorded_prompt_choice(self):
        self.bind(role_models={"translation": "fixture/chosen"}, user_requested_models=True)
        before = (self.work / RUNTIME_PATH).read_bytes()
        with self.assertRaisesRegex(ValueError, "MODEL_POLICY_BINDING_CONFLICT"):
            configure_native_runtime(self.work, "omp", {"native-translation": "fixture/wrong"})
        self.assertEqual((self.work / RUNTIME_PATH).read_bytes(), before)
        self.assertEqual(launch_contract(self.work, "native-translation", ["fixture"])["model"], "fixture/chosen")

    def test_sol_limit_cannot_be_evaded_with_a_new_role_name(self):
        self.bind()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "SOL_REVIEW_CALL_LIMIT"):
            configure_review_policy(self.work, model="fixture/gpt-6.1-sol:high", max_calls=3, user_requested=True)
        self.assertEqual(self.path.read_bytes(), before)
        result = configure_review_policy(self.work, model="fixture/gpt-6.1-sol:high", max_calls=2, user_requested=True)
        self.assertEqual(result["review_attempt_limits"]["native-review-fallback"], 2)

    def test_a_stale_native_binding_cannot_launch_the_wrong_requested_model(self):
        self.bind(role_models={"review-fallback": "fixture/old"}, user_requested_models=True)
        updated = apply_native_role_models(load_config(self.work), {"review-fallback": "fixture/new"})
        _write_config(self.work, updated)
        self.assertEqual(review_policy_status(updated, self.work)["status"], "native_binding_required")
        with self.assertRaisesRegex(ValueError, "MODEL_POLICY_BINDING_REQUIRED"):
            launch_contract(self.work, "native-review-fallback", ["fixture"])
        self.bind()
        self.assertEqual(launch_contract(self.work, "native-review-fallback", ["fixture"])["model"], "fixture/new")

    def test_invalid_role_or_transport_does_not_mutate_workspace(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ConfigurationError, "MODEL_POLICY_INVALID"):
            self.bind(role_models={"unknown-role": "fixture/model"}, user_requested_models=True)
        self.assertEqual(self.path.read_bytes(), before)
        config = json.loads(before)
        config["provider_preset"] = "openai-deepseek"
        with self.assertRaisesRegex(ConfigurationError, "MODEL_POLICY_TRANSPORT_UNSUPPORTED"):
            apply_native_role_models(config, {"translation": "fixture/model"})


if __name__ == "__main__":
    unittest.main()
