"""Actual CLI behavior, explicit run receipts and no runtime/global mutations."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
CLI = SCRIPTS / "pipeline_cli.py"
sys.path.insert(0, str(SCRIPTS))
import run_state as R


class PipelineCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("authorized isolated fixture")
        (self.root / "source.py").write_text("fixture input")

    def cli(self, *args, expected=0, root=None):
        r = subprocess.run([sys.executable, str(CLI), *args], cwd=root or self.root,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE":"1"}, capture_output=True, text=True)
        self.assertEqual(r.returncode, expected, r.stdout+r.stderr)
        return json.loads(r.stdout)

    def start(self):
        s = self.cli("start")
        self.receipt = s["receipt"]
        return s

    def check(self, name, command=None, expected=0, coverage="unit-fixture"):
        command = command or "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        return self.cli("check", "--receipt", self.receipt, "--name", name, "--scope", "source.py",
            "--coverage", coverage, "--impact-covered", "--timeout", "10", "--", sys.executable, "-c", command, expected=expected)

    def test_paths_and_status_are_read_only_and_resolve_existing_names(self):
        before = sorted(self.root.iterdir())
        p = self.cli("paths")["paths"]
        self.assertTrue(all(Path(v).is_file() for v in p.values()))
        self.assertEqual(Path(p["developer"]).name, "pipeline-developer.md")
        self.assertEqual(self.cli("status")["active"], False)
        self.assertEqual(before, sorted(self.root.iterdir()))

    def test_cli_without_environment_flag_does_not_generate_source_caches(self):
        scripts = self.root / "isolated-skill/scripts"
        shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        result = subprocess.run([sys.executable, str(scripts / "pipeline_cli.py"), "paths"],
                                cwd=self.root, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(scripts.rglob("*.pyc")))
        self.assertFalse(list(scripts.rglob("__pycache__")))

    def test_start_records_early_stage_without_printing_credentials(self):
        s = self.start()
        receipt = json.loads(Path(self.receipt).read_text())
        self.assertEqual(Path(self.receipt).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("owner_token", json.dumps(s))
        self.assertNotIn(receipt["owner_token"], json.dumps(s))
        self.assertEqual(s["stage"], "execution")
        before = Path(self.receipt).read_bytes()
        self.assertEqual(self.cli("start", expected=2)["error"], "ValueError")
        self.assertEqual(Path(self.receipt).read_bytes(), before)

    def test_attempt_names_change_but_stable_semantic_coverage_reuses_real_check(self):
        self.start()
        first = self.check("first")
        second = self.check("followup-r2")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual((self.root / "counter").read_text(), "1")
        self.assertNotIn("owner_token", json.dumps(second))

    def test_changed_source_reruns_and_coverage_is_not_silently_relabelled(self):
        self.start()
        self.check("first")
        (self.root / "source.py").write_text("changed")
        self.assertFalse(self.check("changed")["reused"])
        self.assertFalse(self.check("different-purpose", coverage="e2e-fixture")["reused"])
        self.assertEqual((self.root / "counter").read_text(), "3")

    def test_nonzero_retains_actual_result_and_never_generates_qa_pass(self):
        self.start()
        result = self.check("failed", "raise SystemExit(7)", expected=1)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["result_class"], "test_failure")
        state = R.status(self.root)["state"]
        self.assertFalse(any("/qa/" in p for p in state["artifacts"]))

    def test_report_then_stopped_direct_finish_preserves_delivery_evidence(self):
        self.start()
        self.check("unit")
        self.cli("update", "--receipt", self.receipt, "--stage", "acceptance", "--activity", "testing")
        self.cli("report", "--receipt", self.receipt, "--kind", "qa", "--name", "final", "--json",
                 json.dumps({"gate":"PASS", "required_checks":["unit-fixture"]}))
        blocked = self.cli("finish", "--receipt", self.receipt, "--direct", "--stop-evidence", "fixture commands ended", expected=2)
        self.assertEqual(blocked["error"], "ValueError")
        self.assertIsNotNone(R.status(self.root)["active"])
        final = self.cli("finish", "--receipt", self.receipt, "--direct", "--stopped", "--stop-evidence", "fixture commands ended")
        self.assertTrue(final["delivery_sealed"])
        self.assertTrue(final["coverage_complete"])
        self.assertIsNone(R.status(self.root)["active"])

    def test_finish_explains_missing_reusable_categories_without_promoting_qa(self):
        self.start()
        self.check("original", coverage="unit")
        (self.root / "source.py").write_text("changed release input")
        (self.root / "metadata.py").write_text("release fixture")
        self.cli("check", "--receipt", self.receipt, "--name", "metadata", "--scope", "metadata.py",
                 "--coverage", "metadata", "--", sys.executable, "-c", "print('fixture metadata PASS')")
        self.cli("report", "--receipt", self.receipt, "--kind", "qa", "--name", "final", "--json",
                 json.dumps({"gate": "PASS", "required_checks": ["unit", "metadata", "lint"]}))
        final = self.cli("finish", "--receipt", self.receipt, "--direct", "--stopped", "--stop-evidence", "fixture commands ended")
        self.assertTrue(final["delivery_sealed"])
        self.assertFalse(final["coverage_complete"])
        self.assertEqual(final["coverage_missing"], ["lint", "unit"])
        self.assertEqual(final["coverage_issues"], {"unit": ["inputs_changed"], "lint": ["no_valid_evidence"]})
        self.assertNotIn("owner_token", json.dumps(final))

    def test_explicit_receipt_cannot_be_replaced_by_an_active_marker_or_wrong_owner(self):
        self.start()
        marker = "tmp/pipeline/.active"
        denied = self.cli("update", "--receipt", marker, "--stage", "changed", expected=2)
        self.assertEqual(denied["error"], "ValueError")
        receipt = json.loads(Path(self.receipt).read_text())
        receipt["owner_token"] = "wrong"
        Path(self.receipt).write_text(json.dumps(receipt))
        self.assertEqual(self.cli("update", "--receipt", self.receipt, "--stage", "changed", expected=2)["error"], "ValueError")
        self.assertEqual(R.status(self.root)["state"]["stage"], "execution")

    def test_receipt_symlink_public_permissions_and_cross_project_are_rejected(self):
        self.start()
        alias = self.root / "alias.json"
        alias.symlink_to(self.receipt)
        self.cli("update", "--receipt", str(alias), "--stage", "changed", expected=2)
        Path(self.receipt).chmod(0o644)
        self.cli("update", "--receipt", self.receipt, "--stage", "changed", expected=2)
        Path(self.receipt).chmod(0o600)
        with tempfile.TemporaryDirectory() as d:
            other = Path(d).resolve()
            fake = other / "receipt.json"
            fake.write_bytes(Path(self.receipt).read_bytes())
            fake.chmod(0o600)
            self.cli("update", "--receipt", str(fake), "--stage", "changed", expected=2, root=other)
        self.assertEqual(R.status(self.root)["state"]["stage"], "execution")

    def test_report_rejects_owner_credentials(self):
        self.start()
        result = self.cli("report", "--receipt", self.receipt, "--kind", "qa", "--name", "bad", "--json",
                          '{"gate":"PASS","owner_token":"not-public"}', expected=2)
        self.assertEqual(result["error"], "ValueError")


if __name__ == "__main__":
    unittest.main()
