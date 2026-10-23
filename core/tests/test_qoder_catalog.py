"""Qoder live catalog control protocol; isolated settings/DB and fake CLI only."""
import json
import os
import subprocess
import sys
import tempfile
import time
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import cli, db, qoder_cli, util


BUILTINS = [{"displayName": "Auto", "source": "system", "priceFactor": .5},
            {"displayName": "DeepSeek-Flash", "source": "system", "priceFactor": 1}]


def response(request_id, data=None):
    return {"type": "control_response", "response": {
        "subtype": "success", "request_id": request_id, "response": data or {}}}


def model(name, current=None, original=None, billing="qoder_credits"):
    return {"id": name, "name": name, "billing_type": billing,
            "credit_multiplier": current, "original_credit_multiplier": original}


def catalog(models):
    return {"models": models, "multiplier_source": "qoder_cli_live"}


class QoderCatalogTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.settings = self.root / "qoder-settings" / "settings.json"
        self.settings.parent.mkdir()
        self.binary = self.root / "qoder-catalog-fake"
        self.observed = self.root / "catalog-observed.json"
        self.env = {"QODER_CONFIG_DIR": str(self.settings.parent)}
        for patcher in (
            mock.patch.object(qoder_cli.constants, "qoder_bin", return_value=str(self.binary)),
            mock.patch.object(qoder_cli, "child_env", return_value=self.env),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake_cli()

    def fake_cli(self, models=None, *, init=None, reply=None, raw=None,
                 suffix="", stderr="", exit_code=0, prefix=""):
        init = response("catalog-init") if init is None else init
        reply = response("catalog-models", {"models": BUILTINS if models is None else models}) if reply is None else reply
        code = ("#!" + sys.executable + "\nimport json, os, sys, time, subprocess\nfrom pathlib import Path\n"
            + "observed=Path(" + repr(str(self.observed)) + ")\n"
            + "seen={'argv':sys.argv[1:],'frames':[],'cwd':os.getcwd(),'pid':os.getpid()}\n"
            + "def save(): observed.write_text(json.dumps(seen))\n"
            + "save()\n" + prefix
            + "seen['frames'].append(json.loads(sys.stdin.readline())); save()\n")
        if raw is not None:
            code += "sys.stdout.write(" + repr(raw) + "); sys.stdout.flush()\n"
        else:
            code += ("print(" + repr(json.dumps(init)) + ",flush=True)\n"
                + "seen['frames'].append(json.loads(sys.stdin.readline())); save()\n"
                + "print(" + repr(json.dumps(reply)) + ",flush=True)\n"
                + suffix
                + "seen['remainder']=sys.stdin.read(); save()\n")
        code += "sys.stderr.write(" + repr(stderr) + ")\nsys.exit(" + str(exit_code) + ")\n"
        self.binary.write_text(code, encoding="utf-8")
        self.binary.chmod(0o755)

    def write_settings(self, data):
        self.settings.write_text(json.dumps(data), encoding="utf-8")

    def snapshot_db(self):
        with db.connect(self.db_path) as conn:
            return list(conn.iterdump())

    def assert_catalog_error(self, *, timeout=None):
        with self.assertRaises(util.KanpilotError) as raised:
            if timeout is None:
                self.call("qoder-model-catalog")
            else:
                qoder_cli.model_catalog(timeout=timeout)
        self.assertEqual(raised.exception.code, "validation_failed")
        self.assertNotIn("leaked-secret", raised.exception.message)
        self.assertNotIn("private-endpoint", raised.exception.message)
        return raised.exception

    def test_exact_two_control_frames_flags_and_isolated_cwd(self):
        self.fake_cli(init=response("catalog-init", {
            "account": {"token": "leaked-secret"},
            "models": [{"displayName": "stale-cache", "priceFactor": 99}],
            "capabilities": {"endpoint": "https://private-endpoint.invalid"}}))
        self.assertEqual(self.call("qoder-model-catalog"), catalog([
            model("Auto", .5), model("DeepSeek-Flash", 1)]))
        seen = json.loads(self.observed.read_text())
        self.assertEqual(seen["frames"], [
            {"type": "control_request", "request_id": "catalog-init", "request": {"subtype": "initialize"}},
            {"type": "control_request", "request_id": "catalog-models", "request": {"subtype": "get_models", "fetchStrategy": "live"}},
        ])
        self.assertEqual(seen["remainder"], "")
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(tempfile.gettempdir()))
        argv = seen["argv"]
        self.assertEqual(argv[:15], ["--print", "--input-format", "stream-json", "--output-format", "stream-json",
            "--no-session-persistence", "--tools", "", "--permission-mode", "dont_ask",
            "--strict-mcp-config", "--setting-sources", "", "--settings", argv[14]])
        self.assertEqual(json.loads(argv[14]), {
            "general": {"enableAutoUpdate": False}, "disableAllHooks": True,
            "skills": {"enabled": False, "loadFromAgentsDirectory": False}})
        self.assertEqual(argv[15:], ["--max-model-request-retries", "0"])
        self.assertNotIn("--list-models", argv)
        self.assertNotIn("--model", argv)
        with self.assertRaises(ProcessLookupError):
            os.kill(seen["pid"], 0)

    def test_current_original_promotion_and_explicit_zero(self):
        self.fake_cli([
            {"displayName": "Auto", "source": "system", "priceFactor": .5},
            {"displayName": "Ultimate", "source": "system", "priceFactor": .4,
             "originalPriceFactor": 3, "promotion": {"active": True, "before_promotion_price_factor": 2, "discount_factor": .2}},
            {"displayName": "Qwen3.8-Flash", "source": "system", "priceFactor": 0, "originalPriceFactor": .1, "isFree": True},
            {"displayName": "NotActive", "source": "system", "priceFactor": 1, "originalPriceFactor": 2,
             "promotion": {"active": False, "before_promotion_price_factor": 3}},
            {"displayName": "Same", "source": "system", "priceFactor": 1, "originalPriceFactor": 1},
            {"displayName": "Lower", "source": "system", "priceFactor": 2, "originalPriceFactor": 1},
            {"displayName": "FreeFlagOnly", "source": "system", "isFree": True},
        ])
        self.assertEqual(self.call("qoder-model-catalog"), catalog([
            model("Auto", .5), model("Ultimate", .4, 2), model("Qwen3.8-Flash", 0, .1),
            model("NotActive", 1, 2), model("Same", 1), model("Lower", 2), model("FreeFlagOnly")]))

    def test_missing_and_invalid_multipliers_are_null_without_guessing(self):
        invalid = [None, True, False, "0.5", -1, float("inf"), float("-inf"), float("nan"), 10 ** 400]
        self.fake_cli([{"displayName": "Invalid" + str(i), "source": "system", "priceFactor": value,
                        "originalPriceFactor": 3, "promotion": {"discount_factor": .1}, "isFree": True}
                       for i, value in enumerate(invalid)])
        self.assertEqual(self.call("qoder-model-catalog"), catalog([model("Invalid" + str(i)) for i in range(len(invalid))]))
        self.fake_cli([{"displayName": "Original" + str(i), "source": "system", "priceFactor": .5,
                        "originalPriceFactor": value} for i, value in enumerate(invalid)])
        self.assertEqual(self.call("qoder-model-catalog"), catalog([model("Original" + str(i), .5) for i in range(len(invalid))]))

    def test_custom_full_ids_duplicate_names_and_no_auth_resolution(self):
        self.write_settings({"providers": {
            "selected": {"baseUrl": "https://private-endpoint.invalid/v1", "apiKey": "${UNSET_CATALOG_TOKEN}",
                "model": "unused-default", "priceFactor": 9,
                "models": [{"model": "org/nested/model", "displayName": "Auto", "secret": "hidden-model-secret", "priceFactor": 0},
                           {"model": "another", "displayName": "Auto"}, {"model": "no-display"}]},
            "without-models": {"apiKey": "unrelated-secret", "model": "not-a-directory-entry"}},
            "hooks": {"dangerous": "must-not-run"}, "mcpServers": {"unused": {"command": "must-not-run"}}})
        original = self.settings.read_bytes()
        result = self.call("qoder-model-catalog")
        self.assertEqual(result["models"][2:], [
            dict(model("Auto", billing="external"), id="selected/org/nested/model"),
            dict(model("Auto", billing="external"), id="selected/another"),
            dict(model("no-display", billing="external"), id="selected/no-display")])
        encoded = json.dumps(result) + self.observed.read_text()
        for private in ("private-endpoint", "UNSET_CATALOG_TOKEN", "unrelated-secret", "hidden-model-secret", "unused-default", "must-not-run"):
            self.assertNotIn(private, encoded)
        self.assertEqual(self.settings.read_bytes(), original)

    def test_missing_settings_empty_directory_and_custom_only(self):
        self.fake_cli([])
        self.assertEqual(self.call("qoder-model-catalog"), catalog([]))
        self.assertFalse(self.settings.exists())
        self.write_settings({})
        self.assertEqual(self.call("qoder-model-catalog"), catalog([]))
        self.write_settings({"providers": {"p": {"models": [{"model": "org/model"}]}}})
        self.assertEqual(self.call("qoder-model-catalog"), catalog([
            dict(model("org/model", billing="external"), id="p/org/model")]))

    def test_unknown_source_invalid_identity_and_duplicate_model_fail(self):
        for entries in ([{}], [None], [{"displayName": "Auto", "source": "custom"}],
                        [{"displayName": "Auto", "source": "future"}],
                        [{"displayName": "-unsafe", "source": "system"}],
                        [{"displayName": "bad\nname", "source": "system"}], [BUILTINS[0], BUILTINS[0]]):
            with self.subTest(entries=entries):
                self.fake_cli(entries)
                self.assert_catalog_error()

    def test_query_preserves_database_events_configuration_and_settings(self):
        manual = {"model": "offline/manual/model", "reasoning_effort": "inherit"}
        self.call("set-planner-config", {"backend": "qoder", "qoder": manual})
        self.call("set-review-config", {"mode": "custom", "backend": "qoder", "qoder": manual})
        self.write_settings({"providers": {}})
        settings_before, before = self.settings.read_bytes(), self.snapshot_db()
        self.call("qoder-model-catalog")
        self.assertEqual(self.snapshot_db(), before)
        self.assertEqual(self.settings.read_bytes(), settings_before)
        self.assertEqual(self.call("get-planner-config")["config"]["qoder"], manual)
        self.assertEqual(self.call("get-review-config")["config"]["qoder"], manual)

    def test_failed_protocol_does_not_return_custom_only_or_raw_output(self):
        self.write_settings({"providers": {"p": {"models": [{"model": "custom"}]}}})
        self.fake_cli(init={"type": "control_response", "response": {"request_id": "catalog-init", "subtype": "error",
                      "error": "leaked-secret https://private-endpoint.invalid"}}, stderr="leaked-secret")
        before = self.snapshot_db()
        code, stdout, stderr = self.call_main(["qoder-model-catalog", "--json", "{}", "--db", str(self.db_path)])
        self.assertEqual(code, 1)
        error = json.loads(stdout)
        self.assertEqual(error["code"], "validation_failed")
        self.assertNotIn("models", error)
        self.assertNotIn("leaked-secret", stdout + stderr)
        self.assertNotIn("private-endpoint", stdout + stderr)
        self.assertEqual(self.snapshot_db(), before)

    def test_malformed_and_unmatched_or_inference_protocol_rejected(self):
        frames = [[], {}, response("wrong-id"), {"type": "control_request"},
                  {"type": "user", "message": "leaked-secret"}, {"type": "assistant"}, {"type": "result"}]
        for raw in ("", "not-json: leaked-secret", "MODEL\n", *[json.dumps(f) + "\n" for f in frames]):
            with self.subTest(raw=raw):
                self.fake_cli(raw=raw)
                self.assert_catalog_error()
        for reply in (response("wrong-id"), response("catalog-models", {"models": {}}),
                      response("catalog-models"), {"type": "assistant"}):
            with self.subTest(reply=reply):
                self.fake_cli(reply=reply)
                self.assert_catalog_error()

    def test_system_status_frames_and_missing_initialize_payload(self):
        # Initialize success needs no payload; it only gates the second request.
        init = {"type": "control_response", "response": {"subtype": "success", "request_id": "catalog-init"}}
        prefix = "\n".join("print(json.dumps({'type':'system','subtype':" + repr(subtype) + "}),flush=True)"
                           for subtype in ("init", "artifacts_update", "status")) + "\n"
        self.fake_cli(init=init, prefix=prefix)
        self.assertEqual(self.call("qoder-model-catalog"), catalog([model("Auto", .5), model("DeepSeek-Flash", 1)]))

    def test_nonzero_exit_after_success_is_failure(self):
        self.fake_cli(exit_code=2, stderr="leaked-secret https://private-endpoint.invalid")
        self.assertIn("失败", self.assert_catalog_error().message)

    def test_timeout_stops_and_reaps_process(self):
        self.fake_cli(prefix="time.sleep(30)\n")
        started = time.monotonic()
        self.assertIn("超时", self.assert_catalog_error(timeout=.2).message)
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaises(ProcessLookupError):
            os.kill(json.loads(self.observed.read_text())["pid"], 0)

    def test_timeout_after_both_output_pipes_close_is_still_bounded(self):
        self.fake_cli(suffix="os.close(1)\nos.close(2)\ntime.sleep(30)\n")
        started = time.monotonic()
        self.assertIn("超时", self.assert_catalog_error(timeout=.2).message)
        self.assertLess(time.monotonic() - started, 2)

    def test_timeout_cleans_descendant_after_success_and_exited_leader(self):
        forbidden = self.root / "forbidden"
        child = "import time; time.sleep(.7); open(" + repr(str(forbidden)) + ",'w').write('bad')"
        self.fake_cli(suffix="subprocess.Popen([sys.executable,'-c'," + repr(child) + "])\nsys.exit(0)\n")
        self.assertIn("超时", self.assert_catalog_error(timeout=.2).message)
        time.sleep(.8)
        self.assertFalse(forbidden.exists())

    def test_post_response_exit_wait_is_bounded_to_five_seconds(self):
        self.fake_cli(suffix="time.sleep(30)\n")
        started = time.monotonic()
        self.assertIn("超时", self.assert_catalog_error().message)
        self.assertLess(time.monotonic() - started, 6.5)

    def test_output_limit_and_protocol_failure_cleanup_are_explicit(self):
        self.fake_cli(raw="leaked-secret" * 200)
        with mock.patch.object(qoder_cli, "_CATALOG_OUTPUT_LIMIT", 100):
            self.assertIn("限制", self.assert_catalog_error().message)
        self.fake_cli(raw="bad-protocol")
        with mock.patch.object(qoder_cli, "_catalog_stop", wraps=qoder_cli._catalog_stop) as stop:
            self.assert_catalog_error()
        self.assertEqual(stop.call_count, 1)
        self.fake_cli(raw="bad-protocol")
        real_stop = qoder_cli._catalog_stop
        def fail_cleanup(proc):
            real_stop(proc)
            raise PermissionError("leaked-secret private-endpoint")
        with mock.patch.object(qoder_cli, "_catalog_stop", side_effect=fail_cleanup):
            self.assertIn("清理失败", self.assert_catalog_error().message)

    def test_missing_cli_and_execution_error_are_explicit(self):
        self.binary.unlink()
        self.assertIn("未找到 Qoder CLI", self.assert_catalog_error().message)
        self.fake_cli()
        with mock.patch.object(qoder_cli.subprocess, "Popen", side_effect=OSError("leaked-secret private-endpoint")):
            self.assertIn("无法执行", self.assert_catalog_error().message)

    def test_invalid_settings_fail_before_process_launch(self):
        invalid = [[], {"providers": []}, {"providers": {"p": []}}, {"providers": {"p": {"models": {}}}},
            {"providers": {"p": {"models": ["raw-secret"]}}}, {"providers": {"p": {"models": [{"model": ""}]}}},
            {"providers": {"-unsafe": {"models": []}}}, {"providers": {"p/ambiguous": {"models": []}}},
            {"providers": {"p": {"models": [{"model": "control\nmodel"}]}}},
            {"providers": {"p": {"models": [{"model": "ok", "displayName": 12}]}}},
            {"providers": {"p": {"models": [{"model": "duplicate"}, {"model": "duplicate"}]}}}]
        for data in invalid:
            with self.subTest(data=data):
                self.write_settings(data)
                with mock.patch.object(qoder_cli.subprocess, "Popen") as launch:
                    self.assert_catalog_error()
                launch.assert_not_called()
        for raw in ("{leaked-secret private-endpoint", '{"providers":NaN}'):
            self.settings.write_text(raw)
            self.assert_catalog_error()

    def test_command_registration_and_payload_rejection(self):
        self.assertIn("qoder-model-catalog", cli.COMMANDS)
        error = self.call_err("qoder-model-catalog", {"model": "must-not-run"})
        self.assertEqual(error.code, "bad_request")
        self.assertFalse(self.observed.exists())
