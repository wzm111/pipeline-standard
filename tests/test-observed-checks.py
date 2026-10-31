"""Host observation tests use only self-cleaning fixture projects/local HTTP."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import observed_checks as H
import run_checks as C
import run_evidence as E
import run_state as R


class Observations(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        state = R.start(self.root, {"stage": "implementation"})
        self.args = self.root, state["run_id"], state["owner_token"]
        (self.root / "input").write_text("before")
        (self.root / "host.log").write_text("actual host output")

    def begin(self, name="host", **changes):
        values = dict(command="fixture.host.inspect", scope=["input"],
                      environment={"host": "fixture", "private": "secret-value"})
        values.update(changes)
        return H.begin(*self.args, name, **values)

    def finish(self, name="host", **changes):
        values = dict(exit_code=0, log_path="host.log", host_stopped=True,
                      stop_evidence="fixture host returned")
        values.update(changes)
        return H.finish(*self.args, name, **values)

    def state(self):
        return R.status(self.root)["state"]

    def test_real_local_api_and_progress_restore(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"local API returned")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            ticket = self.begin()
            self.assertEqual((self.state()["stage"], self.state()["activity"]), ("checking", "testing"))
            with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
                (self.root / "host.log").write_bytes(response.read())
            path = self.finish()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        payload = E.read_report(self.root, self.args[1], path, kind="check")["payload"]
        self.assertEqual(payload["started_at"], ticket["started_at"])
        self.assertGreater(payload["seconds"], 0)
        self.assertTrue(payload["inputs_stable"])
        self.assertEqual(payload["result_class"], "success")
        self.assertEqual((self.state()["stage"], self.state()["activity"]), ("implementation", "execution"))
        self.assertNotIn("secret-value", json.dumps(self.state()))
        self.assertNotIn(self.args[2], json.dumps(payload))

    def test_same_ticket_idempotent_and_independent_identical_logs_count_twice(self):
        first = self.begin()
        self.assertEqual(self.begin(), first)
        path = self.finish()
        self.assertEqual(self.finish(), path)
        self.assertEqual(self.begin()["status"], "completed")
        self.begin("second")
        other = self.finish("second")
        self.assertNotEqual(other, path)
        self.assertEqual(C.check_summary(self.root)["executed"], 2)

    def test_changed_declaration_result_log_or_inputs_rejected(self):
        self.begin()
        for changes in ({"command": "different"}, {"environment": {"host": "different"}},
                        {"coverage": ["browser"]}, {"write_paths": ["dist"]}):
            with self.assertRaises(ValueError):
                self.begin(**changes)
        self.finish()
        for changes in ({"exit_code": 1}, {"stop_evidence": "different"}, {"log_path": "input"}):
            with self.assertRaises(ValueError):
                self.finish(**changes)
        (self.root / "host.log").write_text("changed")
        with self.assertRaises(ValueError):
            self.finish()
        (self.root / "input").write_text("changed")
        with self.assertRaises(ValueError):
            self.begin()

    def test_input_drift_is_truthful_and_cannot_be_reused(self):
        self.begin()
        (self.root / "input").write_text("changed")
        path = self.finish()
        payload = E.read_report(self.root, self.args[1], path)["payload"]
        self.assertFalse(payload["inputs_stable"])
        self.assertEqual(E.reusable_check(self.root, self.args[1], path, command=payload["command"],
                         environment=payload["environment"], impact_covered=True)["reason"],
                         "inputs_changed_during_check")

    def test_shared_resources_and_pending_run_close_are_blocked(self):
        self.begin(write_paths=["browser"])
        with self.assertRaises(ValueError):
            self.begin("other", write_paths=["browser/tab"])
        with self.assertRaises(ValueError):
            C._claim(*self.args, "command", None)
        with self.assertRaises(ValueError):
            R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="assertion alone")
        self.begin("isolated", write_paths=["separate"])
        self.finish("isolated")
        self.assertEqual(self.state()["activity"], "testing")
        self.finish()
        self.assertEqual(self.state()["stage"], "implementation")

    def test_failures_stop_confirmation_and_recovery_do_not_create_pass(self):
        self.begin()
        for changes in ({"host_stopped": False}, {"stop_evidence": ""},
                        {"exit_code": 0, "error": "interrupted"}):
            with self.assertRaises(ValueError):
                self.finish(**changes)
        self.assertEqual(C.check_summary(self.root)["executed"], 0)
        path = self.finish(exit_code=-1, error="interrupted")
        self.assertEqual(E.read_report(self.root, self.args[1], path)["payload"]["result_class"], "interrupted")
        self.begin("recovered")
        R.resume(*self.args, executors_stopped=True, stop_evidence="all fixture hosts stopped")
        with self.assertRaises(ValueError):
            self.finish("recovered")
        self.assertEqual(self.state()["observed_checks"]["recovered"]["status"], "closed")

    def test_owner_paths_symlinks_and_secrets(self):
        with self.assertRaises(ValueError):
            H.begin(self.root, self.args[1], "wrong", "host", command="fixture", scope=["input"], environment={"host": "fixture"})
        for changes in ({"command": self.args[2]}, {"environment": {"secret": self.args[2]}},
                        {"scope": ["../outside"]}, {"write_paths": ["../outside"]}):
            with self.assertRaises(ValueError):
                self.begin(**changes)
        self.begin()
        (self.root / "link").symlink_to(self.root / "host.log")
        for log in ("link", "../outside", str(self.root / "host.log"), "missing"):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.finish(log_path=log)
        self.assertEqual(self.state()["observed_checks"]["host"]["status"], "running")

    def test_registration_failure_retains_claim_and_retries_original_report(self):
        self.begin()
        atomic = R._atomic
        def fail_registration(path, value):
            if path.name == self.args[1] + ".json":
                raise OSError("fixture registration failure")
            return atomic(path, value)
        with patch.object(R, "_atomic", side_effect=fail_registration):
            with self.assertRaises(OSError):
                self.finish()
        self.assertEqual(self.state()["observed_checks"]["host"]["status"], "running")
        self.assertEqual(len(self.state()["running_checks"]), 1)
        self.assertEqual(C.check_summary(self.root)["executed"], 0)
        path = f"tmp/pipeline/runs/{self.args[1]}/artifacts/check/host.json"
        original = (self.root / path).read_bytes()
        self.assertEqual(self.finish(), path)
        self.assertEqual((self.root / path).read_bytes(), original)
        self.assertEqual(self.state()["running_checks"], [])

    def test_report_write_and_log_permission_failures_leave_no_completion(self):
        self.begin()
        read_bytes = Path.read_bytes
        def deny_log(path):
            if path == self.root / "host.log":
                raise PermissionError("fixture denied log")
            return read_bytes(path)
        with patch.object(Path, "read_bytes", deny_log):
            with self.assertRaises(PermissionError):
                self.finish()
        atomic = R._atomic
        def deny_report(path, value):
            if path.name == "host.json":
                raise PermissionError("fixture denied report")
            return atomic(path, value)
        with patch.object(R, "_atomic", side_effect=deny_report):
            with self.assertRaises(PermissionError):
                self.finish()
        self.assertEqual(self.state()["observed_checks"]["host"]["status"], "running")
        self.assertEqual(C.check_summary(self.root)["executed"], 0)
        self.assertEqual(len(self.state()["running_checks"]), 1)
        self.finish()

    def test_orphan_timing_must_remain_complete_aware_and_consistent(self):
        self.begin()
        atomic = R._atomic
        def fail_registration(path, value):
            if path.name == self.args[1] + ".json":
                raise OSError("fixture registration failure")
            return atomic(path, value)
        with patch.object(R, "_atomic", side_effect=fail_registration):
            with self.assertRaises(OSError):
                self.finish()
        relative = f"tmp/pipeline/runs/{self.args[1]}/artifacts/check/host.json"
        path = self.root / relative
        original = path.read_text()
        invalid = [
            ("seconds", -17), ("seconds", float("inf")), ("seconds", float("nan")),
            ("seconds", True), ("seconds", None), ("seconds", 999),
            ("finished_at", "2000-01-01T00:00:00+00:00"),
            ("finished_at", "2099-01-01T00:00:00+00:00"),
            ("finished_at", "2026-10-31T00:00:00"),
            ("finished_at", "2026-10-31T00:00:00Z"),
            ("finished_at", "unknown"), ("finished_at", None),
            ("recorded_at", "2001-01-01T00:00:00+00:00"),
        ]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                report = json.loads(original)
                target = report if field == "recorded_at" else report["payload"]
                target[field] = value
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    self.finish()
                self.assertEqual(self.state()["observed_checks"]["host"]["status"], "running")
                self.assertNotIn(relative, self.state()["artifacts"])
                self.assertEqual(len(self.state()["running_checks"]), 1)
        path.write_text(original)
        # Retry uses the existing end boundary, even if the wall clock advances.
        with patch.object(R, "_now", return_value="2099-01-01T00:00:00+00:00"):
            self.assertEqual(self.finish(), relative)
        self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
