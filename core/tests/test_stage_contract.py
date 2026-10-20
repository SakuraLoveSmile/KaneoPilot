"""Stage contract boundary tests: no database, harness, or service access."""
import copy
import unittest
from unittest.mock import patch

from kanpilot import stage_contract as contract, util


def plan_result(**changes):
    value = {"status": "ready", "plan_markdown": "# 冻结计划", "questions": [], "evidence": []}
    value.update(changes)
    return value


def question():
    return {"id": "scope", "title": "范围？", "description": "选择范围",
            "options": [{"id": "a", "label": "本机", "description": "仅本机"}],
            "recommended_option_id": "a", "allow_custom": True, "required": True}


class StageContractTests(unittest.TestCase):
    def make(self, **changes):
        args = {"stage": "plan", "task_id": "task-1", "request_id": "request-1",
                "run_id": "run-1", "harness_id": "fixture.harness",
                "workspace": "/tmp/contract-worktree",
                "inputs": {"plan": {"sha256": "a" * 64}, "source_sha256": "b" * 64},
                "config": {"model": "fixture", "api_key_env": "FIXTURE_API_KEY"},
                "verification_commands": ["python3 -m unittest tests.test_stage_contract"],
                "allowed_actions": ["read_workspace"]}
        args.update(changes)
        return contract.make_contract(**args)

    def rejected(self, function, *args, **kwargs):
        with self.assertRaises(util.KanpilotError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, "validation_failed")
        return raised.exception.message

    def test_snapshot_and_digest_freeze_all_inputs(self):
        inputs = {"plan": {"sha256": "a" * 64}}
        original = self.make(inputs=inputs)
        inputs["plan"]["sha256"] = "c" * 64
        self.assertEqual(original["inputs"]["plan"]["sha256"], "a" * 64)
        self.assertNotEqual(original["contract_sha256"], self.make(inputs=inputs)["contract_sha256"])
        changed = copy.deepcopy(original)
        changed["inputs"]["plan"]["sha256"] = "d" * 64
        self.rejected(contract.validate_contract, changed)
        for field, replacement in (("workspace", "/tmp/another-worktree"),
                                   ("verification_commands", ["different command"]),
                                   ("allowed_actions", ["write_workspace"]),
                                   ("config", {"model": "another"})):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = replacement
                self.rejected(contract.validate_contract, changed)

    def test_canonical_order_and_detached_validation(self):
        first = self.make(inputs={"a": 1, "b": [2]})
        second = self.make(inputs={"b": [2], "a": 1})
        self.assertEqual(first, second)
        checked = contract.validate_contract(first, expected={"task_id": "task-1", "inputs": {"b": [2], "a": 1}})
        checked["inputs"]["b"].append(3)
        self.assertEqual(first["inputs"]["b"], [2])
        self.rejected(contract.validate_contract, first, expected={"task_id": "other"})
        self.rejected(contract.validate_contract, first, expected={"unknown_field": None})
        self.rejected(contract.validate_contract, first, expected={"inputs": {"a": True, "b": [2]}})

    def test_identity_stage_path_and_version_are_validated(self):
        for changes in ({"task_id": ""}, {"run_id": None}, {"harness_id": "with space"},
                        {"stage": "unknown"}, {"workspace": "relative"},
                        {"workspace": "/tmp/../tmp/work"}, {"workspace": "/tmp/work/"},
                        {"workspace": "/tmp/work\x00"}):
            with self.subTest(changes=changes):
                self.rejected(self.make, **changes)
        value = self.make()
        for field, replacement in (("version", "kp-stage-v999"), ("output_contract", "wrong")):
            with self.subTest(field=field):
                changed = dict(value, **{field: replacement})
                self.rejected(contract.validate_contract, changed)

    def test_missing_and_extra_contract_fields_rejected(self):
        value = self.make()
        for field in value:
            changed = dict(value)
            del changed[field]
            self.rejected(contract.validate_contract, changed)
        self.rejected(contract.validate_contract, dict(value, extra="unfrozen"))

    def test_credentials_rejected_recursively_without_leaking_values(self):
        for key in ("apiKey", "access_token", "KaneoToken", "client_secret", "password",
                    "Authorization", "private_key", "credentials", "session_cookie"):
            for field in ("inputs", "config"):
                with self.subTest(key=key, field=field):
                    error = self.rejected(self.make, **{field: {"nested": [{key: "do-not-leak-this"}]}})
                    self.assertNotIn("do-not-leak-this", error)
        self.make(config={"api_key_env": "API_KEY", "credentials_path": "/safe/config",
                          "token_ref": "credential-store:fixture", "max_tokens": 1024})

    def test_json_limits_and_types(self):
        for item in (float("nan"), float("inf"), {1: "key"}, ("not", "JSON"), object(), "\ud800"):
            with self.subTest(item=repr(item)):
                self.rejected(self.make, inputs={"value": item})
        nested = {}
        for _ in range(contract.MAX_DEPTH + 1):
            nested = {"child": nested}
        self.rejected(self.make, inputs=nested)
        self.rejected(self.make, inputs={"large": "x" * contract.MAX_BYTES})
        self.rejected(self.make, verification_commands="not a command list")
        self.rejected(self.make, allowed_actions=[None])

    def test_result_is_bound_to_contract_and_copies_payload(self):
        frozen = self.make()
        payload = plan_result()
        result = contract.make_result(frozen, payload, evidence=[{"artifact": "report.json"}])
        payload["plan_markdown"] = "changed later"
        self.assertEqual(contract.validate_result(result, frozen)["payload"], plan_result())
        for field, replacement in (("task_id", "other"), ("request_id", "other"),
                                   ("run_id", "other"), ("harness_id", "other"),
                                   ("stage", "review"), ("version", "kp-stage-v2"),
                                   ("contract_sha256", "0" * 64)):
            with self.subTest(field=field):
                self.rejected(contract.validate_result, dict(result, **{field: replacement}), frozen)
        self.rejected(contract.validate_result, result, self.make(config={"model": "changed"}))

    def test_result_envelope_rejects_missing_extra_and_wrong_types(self):
        frozen = self.make()
        result = contract.make_result(frozen, plan_result())
        for field in result:
            changed = dict(result)
            del changed[field]
            self.rejected(contract.validate_result, changed, frozen)
        for changes in ({"extra": True}, {"outcome": "finished"}, {"payload": []}, {"evidence": {}}):
            self.rejected(contract.validate_result, dict(result, **changes), frozen)

    def test_succeeded_plan_validator_cannot_be_bypassed(self):
        frozen = self.make()
        result = contract.make_result(frozen, {"status": "ready"})
        self.rejected(contract.validate_result, result, frozen, payload_validator=lambda _: {})
        seen = []
        result = contract.make_result(frozen, plan_result())
        contract.validate_result(result, frozen, payload_validator=lambda payload: seen.append(payload))
        self.assertEqual(seen, [plan_result()])

    def test_review_reuses_shared_validator(self):
        from kanpilot import review_settle
        frozen = self.make(stage="review")
        payload = {"conclusion": "pass", "report_markdown": "# Review", "fix": None}
        result = contract.make_result(frozen, payload)
        with patch.object(review_settle, "validate_result", wraps=review_settle.validate_result) as validator:
            contract.validate_result(result, frozen)
        validator.assert_called_once_with(payload)
        result["payload"]["fix"] = {"title": "must not exist"}
        self.rejected(contract.validate_result, result, frozen, payload_validator=lambda _: {})

    def test_non_success_results_have_shared_strict_error_payload(self):
        frozen = self.make()
        for outcome in ("failed", "blocked", "partial", "cancelled"):
            with self.subTest(outcome=outcome):
                payload = {"code": "transport_unknown", "detail": "等待只读核对"}
                result = contract.make_result(frozen, payload, outcome=outcome,
                                              evidence=[{"partial_artifact": "draft.md"}])
                self.assertEqual(contract.validate_result(result, frozen)["payload"], payload)
                for invalid in ({"code": "missing_detail"}, dict(payload, retryable=True),
                                {"code": " ", "detail": "error"}, plan_result()):
                    result["payload"] = invalid
                    self.rejected(contract.validate_result, result, frozen)

    def test_execution_delivery_require_business_validation_at_caller(self):
        for stage in ("execution", "delivery"):
            frozen = self.make(stage=stage)
            result = contract.make_result(frozen, {"adapter_claim": "not proof"})
            self.assertEqual(contract.validate_result(result, frozen), result)
            def reject_unproven(_payload):
                raise util.KanpilotError("validation_failed", "missing trusted evidence")
            self.rejected(contract.validate_result, result, frozen, payload_validator=reject_unproven)


class PlanResultTests(unittest.TestCase):
    def rejected(self, value):
        with self.assertRaises(util.KanpilotError):
            contract.validate_plan_result(value)

    def test_ready_and_needs_input_preserve_original_values(self):
        for value in (plan_result(), plan_result(status="needs_input", plan_markdown="",
                                               questions=[" 历史文本问题 ", question()])):
            self.assertEqual(contract.validate_plan_result(value), value)

    def test_top_level_schema_and_status_invariants(self):
        for value in (plan_result(status="unknown"), plan_result(plan_markdown=" "),
                      plan_result(questions=["尚有问题"]), plan_result(status="needs_input"),
                      plan_result(plan_markdown=None), plan_result(evidence=[{}]),
                      plan_result(questions="question"), plan_result(extra="unexpected")):
            with self.subTest(value=value):
                self.rejected(value)
        for field in plan_result():
            value = plan_result()
            del value[field]
            self.rejected(value)

    def test_questions_are_not_repaired_or_silently_discarded(self):
        cases = [" ", {}, dict(question(), extra=True), dict(question(), required=1),
                 dict(question(), recommended_option_id="missing"),
                 dict(question(), options=[{"id": "custom", "label": "reserved", "description": ""}]),
                 dict(question(), options=[{"id": "a", "label": "missing description"}])]
        for field in question():
            invalid = question()
            del invalid[field]
            cases.append(invalid)
        for invalid in cases:
            with self.subTest(question=invalid):
                self.rejected(plan_result(status="needs_input", questions=[invalid]))
        self.rejected(plan_result(status="needs_input", questions=[question(), question()]))
        duplicated = question()
        duplicated["options"] *= 2
        self.rejected(plan_result(status="needs_input", questions=[duplicated]))


if __name__ == "__main__":
    unittest.main()
