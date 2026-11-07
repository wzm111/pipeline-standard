"""Compact CLI output preserves execution, ownership and complete saved evidence."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import pipeline_cli as CLI
import run_evidence as E
import run_state as R


class CompactCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("isolated authorized fixture")
        (self.root / "input").write_text("stable")
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        self.check = {"argv": [sys.executable, "-B", "-c",
            "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
            "scope": ["input"], "coverage": ["unit"], "write_paths": []}

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args],
            cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.output = result.stdout
        return json.loads(result.stdout)

    def start(self, compact=True):
        started = self.cli("start", "--review-risk", "low", "--review-reason", "fixture scoped change", *(["--compact"] if compact else []))
        self.receipt = started["receipt"]
        self.run = started["run_id"]
        self.cli("policy-ack", "--receipt", self.receipt, "--revision", started["policy"]["revision"])
        return started

    def verify(self, name, *options, check=None, expected=0):
        return self.cli("verify", "--receipt", self.receipt, "--name", name,
            "--json", json.dumps({"checks": {"unit": check or self.check}}), *options, expected=expected)

    def finish(self, *options, expected=0):
        return self.cli("finish", "--receipt", self.receipt, "--direct", "--stopped",
            "--stop-evidence", "fixture host observed stopped", *options, expected=expected)

    def test_compact_start_receipt_policy_ack_and_saved_audit(self):
        started = self.start()
        self.assertEqual(set(started), {"run_id", "status", "stage", "activity", "receipt", "policy", "next_action"})
        receipt = Path(self.receipt)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(json.loads(receipt.read_text())["owner_token"], json.dumps(started))
        self.assertEqual(self.cli("policy")["caller_acknowledged"], True)
        self.assertNotEqual(self.cli("audit", "--run-id", self.run)["status"], "error")
        self.assertLess(len(json.dumps(started).encode()), len(json.dumps({**started, **CLI.paths()}).encode()))

    def test_default_start_and_verify_keep_original_full_response(self):
        started = self.start(compact=False)
        self.assertIn("audit", started)
        for key in CLI.paths():
            self.assertIn(key, started)
        self.assertIn("pending", started)
        full_bytes = len((json.dumps(started, ensure_ascii=False, allow_nan=False) + "\n").encode())
        compact = CLI._compact_response("start", started, 0)
        compact_bytes = len((json.dumps(compact, ensure_ascii=False, allow_nan=False) + "\n").encode())
        self.assertLess(compact_bytes, full_bytes)
        verified = self.verify("full")
        self.assertIn("results", verified)
        self.assertEqual(verified["skipped_checks"], [])
        self.assertNotIn("counts", verified)
        final = self.finish()
        self.assertIn("audit", final)

    def test_compact_pass_saves_full_evidence_and_executes_once(self):
        self.start()
        result = self.verify("compact", "--compact")
        compact_bytes = len(self.output.encode())
        self.assertEqual(result["gate"], "PASS")
        self.assertEqual(result["counts"], {"executed": 1, "reused": 0, "failed": 0, "skipped": 0})
        self.assertNotIn("results", result)
        self.assertEqual(result["required_checks"], ["unit"])
        self.assertEqual(result["missing_checks"], [])
        qa = E.read_report(self.root, self.run, result["report"], kind="qa")["payload"]
        self.assertEqual(qa["gate"], "PASS")
        self.assertEqual(qa["plan_ref"], result["plan_ref"])
        state = R.status(self.root)["state"]
        checks = [ref for ref in state["artifacts"] if "/artifacts/check/" in ref]
        self.assertEqual(len(checks), 1)
        proof = E.read_report(self.root, self.run, checks[0])["payload"]
        self.assertEqual(proof["command"], shlex.join(self.check["argv"]))
        self.assertTrue((self.root / proof["log_path"]).is_file())
        full = self.verify("full")
        self.assertLess(compact_bytes, len(self.output.encode()))
        self.assertTrue(full["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_compact_reuse_counts_preserve_prior_artifacts(self):
        self.start()
        first = self.verify("first")
        saved = (self.root / first["report"]).read_bytes()
        result = self.verify("second", "--compact")
        self.assertEqual(result["counts"], {"executed": 0, "reused": 1, "failed": 0, "skipped": 0})
        self.assertEqual((self.root / "counter").read_text(), "1")
        self.assertEqual((self.root / first["report"]).read_bytes(), saved)

    def test_compact_verify_finish_preserves_delivery_and_report_refs(self):
        self.start()
        result = self.verify("seal", "--compact", "--finish", "--direct", "--stopped",
            "--stop-evidence", "fixture host observed stopped")
        self.assertEqual(result["gate"], "PASS")
        completion = result["completion"]
        self.assertEqual(completion["status"], "completed")
        self.assertTrue(completion["delivery_sealed"])
        self.assertTrue(completion["coverage_complete"])
        self.assertEqual(completion["coverage_missing"], [])
        self.assertEqual(completion["coverage_issues"], {})
        self.assertNotIn("audit", completion)
        self.assertTrue((self.root / result["report"]).is_file())
        self.assertTrue((self.root / result["plan_ref"]).is_file())
        self.assertIsNone(R.status(self.root)["active"])

    def test_compact_failure_and_preflight_concerns_remain_full(self):
        self.start()
        for name, argv in (("missing", ["pipeline-fixture-no-runner"]),
                           ("failed", [sys.executable, "-B", "-c", "raise SystemExit(7)"])):
            result = self.verify(name, "--compact", check={**self.check, "argv": argv}, expected=1)
            self.assertEqual(result["gate"], "CONCERNS")
            self.assertIn("results", result)
            self.assertIn("skipped_checks", result)
            self.assertIn("environment_diagnostics", result)
            self.assertNotIn("counts", result)
            self.assertIs(CLI._compact_response("verify", result, 1), result)
            self.assertIs(CLI._compact_response("verify", result, 0), result)
        self.assertIsNotNone(R.status(self.root)["active"])

    def test_compact_stop_and_ownership_errors_are_unchanged(self):
        self.start()
        for args in (("verify", "--receipt", self.receipt, "--name", "blocked", "--finish",
                      "--json", json.dumps({"checks": {"unit": self.check}})),
                     ("finish", "--receipt", "tmp/pipeline/.active", "--stop-evidence", "observed")):
            full = self.cli(*args, expected=2)
            compact = self.cli(*args, "--compact", expected=2)
            self.assertEqual(compact, full)
        self.assertFalse((self.root / "counter").exists())
        self.assertIsNotNone(R.status(self.root)["active"])

    def test_compact_finish_cannot_bypass_missing_qa_or_changed_evidence(self):
        self.start()
        self.cli("plan", "--receipt", self.receipt, "--name", "planned", "--json",
                 json.dumps({"checks": {"unit": self.check}}))
        full = self.finish(expected=2)
        self.assertEqual(self.finish("--compact", expected=2), full)
        self.verify("qa")
        (self.root / "input").write_text("changed after QA")
        full = self.finish(expected=2)
        self.assertEqual(self.finish("--compact", expected=2), full)
        self.assertIsNotNone(R.status(self.root)["active"])

    def test_legacy_compact_finish_retains_missing_proof_diagnostics(self):
        state = R.start(self.root, managed=False)
        self.run = state["run_id"]
        self.receipt = str(self.root / "legacy-receipt.json")
        R._atomic(Path(self.receipt), {"project_root": str(self.root),
            **{key: state[key] for key in ("schema_version", "run_id", "owner_token")}})
        self.cli("check", "--receipt", self.receipt, "--name", "metadata", "--scope", "input",
                 "--coverage", "metadata", "--", sys.executable, "-B", "-c", "print('metadata fixture')")
        self.cli("report", "--receipt", self.receipt, "--kind", "qa", "--name", "legacy", "--json",
                 json.dumps({"gate": "PASS", "required_checks": ["unit"]}))
        final = self.finish("--compact")
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["coverage_complete"])
        self.assertEqual(final["coverage_missing"], ["unit"])
        self.assertEqual(final["coverage_issues"], {"unit": ["no_valid_evidence"]})
        self.assertIn("delivery_reason", final)
        self.assertIn("delivery_detail", final)
        self.assertNotIn("audit", final)

    def test_compact_finish_without_evidence_keeps_unsealed_reason_and_unknown_coverage(self):
        self.start()
        final = self.finish("--compact")
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["delivery_sealed"])
        self.assertIsNotNone(final["delivery_reason"])
        self.assertIn("accepted", final["delivery_detail"])
        self.assertIsNone(final["coverage_complete"])
        self.assertIsNone(final["coverage_missing"])
        self.assertEqual(final["coverage_issues"], {})


if __name__ == "__main__":
    unittest.main()
