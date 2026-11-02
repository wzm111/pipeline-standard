"""Real CLI processes; fixture runs/receipts/logs live only in system temp."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import observed_checks as H
import run_checks as C
import run_evidence as E
import run_state as R


class ObserveCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "input").write_text("fixture input")
        (self.root / "host.log").write_text("fixture host returned")
        self.declaration = {"command": "fixture.host.inspect", "scope": ["input"],
                            "environment": {"host": "fixture-private-context"}, "coverage": ["browser"]}
        self.receipt = self.cli("start")["receipt"]
        self.private = json.loads((self.root / self.receipt).read_text())

    def command(self, *args):
        return [sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args]

    def cli(self, *args, expected=0):
        result = subprocess.run(self.command(*args), cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertFalse(result.stderr, result.stderr)
        if hasattr(self, "private"):
            for secret in (self.private["owner_token"], "fixture-private-context", self.declaration["command"]):
                self.assertNotIn(secret, result.stdout)
        return json.loads(result.stdout)

    def begin_args(self, name="host", declaration=None):
        return ("observe-begin", "--receipt", self.receipt, "--id", name,
                "--json", json.dumps(self.declaration if declaration is None else declaration))

    def finish_args(self, name="host", code=0, log="host.log"):
        return ("observe-finish", "--receipt", self.receipt, "--id", name,
                "--exit-code", str(code), "--log-path", log, "--host-stopped",
                "--stop-evidence", "fixture host returned")

    def owned(self):
        return self.root, self.private["run_id"], self.private["owner_token"]

    def test_short_atomic_created_and_idempotent_finish(self):
        first = self.cli(*self.begin_args())
        self.assertEqual(set(first), {"observation_id", "status", "started_at", "created"})
        self.assertTrue(first["created"])
        again = self.cli(*self.begin_args())
        self.assertEqual(again, first | {"created": False})
        completed = self.cli(*self.finish_args())
        self.assertEqual(set(completed), {"observation_id", "status", "report"})
        self.assertEqual(self.cli(*self.finish_args()), completed)
        self.assertFalse(self.cli(*self.begin_args())["created"])
        encoded = json.dumps([first, again, completed])
        for private in (self.private["owner_token"], "fixture-private-context", self.declaration["command"]):
            self.assertNotIn(private, encoded)
        self.assertEqual(C.check_summary(self.root)["executed"], 1)

    def test_concurrent_processes_same_id_authorize_exactly_one_invocation(self):
        processes = [subprocess.Popen(self.command(*self.begin_args()), cwd=self.root,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        self.addCleanup(lambda: [p.kill() for p in processes if p.poll() is None])
        results = []
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertFalse(err, err)
            payload = json.loads(out)
            if process.returncode == 0:
                results.append(payload)
            else:
                # Existing exclusive write guard fails closed while another
                # process owns it. The CLI never retries a host invocation.
                self.assertEqual(process.returncode, 2, out)
                self.assertEqual(payload["error"], "FileExistsError", out)
                self.assertNotIn("created", payload)
        self.assertEqual(sum(item["created"] for item in results), 1)
        self.assertFalse(self.cli(*self.begin_args())["created"])
        self.assertEqual(len(R.status(self.root)["state"]["running_checks"]), 1)

    def test_different_ids_same_log_remain_independent(self):
        refs = []
        for name in ("one", "two"):
            self.assertTrue(self.cli(*self.begin_args(name))["created"])
            refs.append(self.cli(*self.finish_args(name))["report"])
        self.assertNotEqual(*refs)
        self.assertEqual(C.check_summary(self.root)["executed"], 2)

    def test_legacy_python_ticket_and_event_view_are_compatible(self):
        original = H.begin(*self.owned(), "python", **self.declaration)
        self.assertIn("declaration", original)
        self.assertNotIn("created", original)
        self.assertFalse(H.begin(*self.owned(), "python", **self.declaration, event_view=True)["created"])
        self.assertEqual(H.begin(*self.owned(), "python", **self.declaration), original)

    def test_bad_ownership_receipt_permissions_and_declarations(self):
        wrong = self.private | {"owner_token": "fixture-wrong-owner"}
        path = self.root / "wrong.json"
        path.write_text(json.dumps(wrong))
        path.chmod(0o600)
        args = list(self.begin_args())
        args[2] = "wrong.json"
        self.cli(*args, expected=2)
        path.chmod(0o644)
        self.cli(*args, expected=2)
        for declaration in ({}, [], self.declaration | {"event_view": False},
                            self.declaration | {"command": {"fixture.host.inspect": "bad type"}},
                            self.declaration | {"environment": {"host": ["fixture-private-context"]}},
                            self.declaration | {"scope": ["../outside"]},
                            self.declaration | {"command": self.private["owner_token"]}):
            self.cli(*self.begin_args(declaration=declaration), expected=2)
        self.cli("observe-begin", "--receipt", self.receipt, "--id", "host", "--json",
                 '{"fixture.host.inspect": "fixture-private-context",', expected=2)
        self.assertFalse(R.status(self.root)["state"].get("observed_checks"))

    def test_stop_failure_interruption_and_path_rejections(self):
        self.cli(*self.begin_args())
        args = list(self.finish_args())
        args.remove("--host-stopped")
        self.cli(*args, expected=2)
        (self.root / "link").symlink_to(self.root / "host.log")
        for log in ("../outside", "link", "missing"):
            self.cli(*self.finish_args(log=log), expected=2)
        self.assertEqual(C.check_summary(self.root)["executed"], 0)
        self.cli(*self.finish_args(code=-1), "--error", "interrupted")
        self.assertEqual(C.check_summary(self.root)["by_result_class"], {"interrupted": 1})
        self.cli(*self.begin_args("failure"))
        self.cli(*self.finish_args("failure", code=1))
        self.assertEqual(C.check_summary(self.root)["by_result_class"]["test_failure"], 1)

    def test_invalid_exit_and_error_do_not_echo_private_values(self):
        self.cli(*self.begin_args())
        for flag in ("--exit-code", "--error"):
            args = list(self.finish_args())
            if flag == "--exit-code":
                args[args.index(flag) + 1] = "fixture-private-context"
            else:
                args.extend([flag, "fixture-private-context"])
            result = self.cli(*args, expected=2)
            self.assertEqual(result["error"], "ValueError")
            self.assertNotIn("created", result)
        self.assertEqual(C.check_summary(self.root)["executed"], 0)
        self.assertEqual(len(R.status(self.root)["state"]["running_checks"]), 1)

    def test_orphan_registration_retry_and_corrupt_timing_remain_guarded(self):
        self.cli(*self.begin_args())
        original = R._atomic
        def fail_state(path, value):
            if path.name == self.private["run_id"] + ".json":
                raise OSError("fixture registration failure")
            return original(path, value)
        with patch.object(R, "_atomic", side_effect=fail_state):
            with self.assertRaises(OSError):
                H.finish(*self.owned(), "host", exit_code=0, log_path="host.log",
                         host_stopped=True, stop_evidence="fixture host returned")
        report = self.root / f"tmp/pipeline/runs/{self.private['run_id']}/artifacts/check/host.json"
        valid = report.read_bytes()
        corrupt = json.loads(valid)
        corrupt["payload"]["seconds"] = -17
        report.write_text(json.dumps(corrupt))
        self.cli(*self.finish_args(), expected=2)
        self.assertEqual(len(R.status(self.root)["state"]["running_checks"]), 1)
        report.write_bytes(valid)
        self.cli(*self.finish_args())
        self.assertEqual(report.read_bytes(), valid)


if __name__ == "__main__":
    unittest.main()
