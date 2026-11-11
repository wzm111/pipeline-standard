"""Cross-agent checkpoint through the CLI with an isolated Git worktree."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import agent_handoff as H
import run_evidence as E
import run_state as R


class HandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / ".gitignore").write_text("/tmp/\n")
        (self.root / "source").write_text("baseline")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "fixture")
        self.state = R.start(self.root)
        self.run = self.state["run_id"]
        self.owner = self.state["owner_token"]
        self.receipt = self.root / "tmp/pipeline/runs" / self.run / "receipt.json"
        R._atomic(self.receipt, {"schema_version": 1, "project_root": str(self.root), "run_id": self.run, "owner_token": self.owner})

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def cli(self, *args, payload=None, code=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args],
                                cwd=self.root, input=json.dumps(payload) if payload else None,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn(self.owner, result.stdout)
        return json.loads(result.stdout)

    def test_cli_preserves_context_and_detects_same_path_content_drift(self):
        ref = E.write_report(self.root, self.run, self.owner, "qa", "prior", {"gate": "CONCERNS"})
        untracked = self.root / "new"
        untracked.write_text("first")
        path = self.cli("handoff-save", "--receipt", str(self.receipt), payload={
            "goal": "Finish verification", "decisions": ["Reuse the existing fixture"],
            "next": "Run targeted regression", "evidence": [ref]})["handoff"]
        checkpoint = self.root / path
        before = checkpoint.read_bytes()
        self.assertNotIn(self.owner.encode(), before)
        self.assertEqual(checkpoint.stat().st_mode & 0o077, 0)
        result = self.cli("handoff-read", "--run-id", self.run)
        self.assertEqual(result["git_status"], "matched")
        self.assertEqual(result["evidence"], {ref: "matched"})
        untracked.write_text("second")
        result = self.cli("handoff-read", "--run-id", self.run)
        self.assertEqual(result["git_status"], "changed")
        self.assertEqual(result["notes"]["next"], "Run targeted regression")
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_over_target_summary_survives_and_details_are_read_on_demand(self):
        goal = "保留完整目标" * 600
        details = {"design": "extended context " * 900, "reproduction": "specific steps"}
        saved = self.cli("handoff-save", "--receipt", str(self.receipt),
                         payload={"goal": goal, "next": "Check evidence", "details": details})
        self.assertTrue(saved["summary_size"]["over_target"])
        summary = self.cli("handoff-read", "--run-id", self.run)
        self.assertEqual(summary["notes"]["goal"], goal)
        self.assertNotIn("details", summary["notes"])
        self.assertEqual(summary["detail_sections"], ["design", "reproduction"])
        detail = self.cli("handoff-read", "--run-id", self.run, "--detail", "design")
        self.assertEqual(detail["text"], details["design"])
        self.assertNotIn("notes", detail)

    def test_project_below_git_root_hashes_changed_file_content(self):
        project = self.root / "app"
        project.mkdir()
        changed = project / "new"
        changed.write_text("first")
        first = H._git_state(project)
        changed.write_text("second")
        second = H._git_state(project)
        self.assertTrue(second["content_complete"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_large_dirty_tree_is_reported_as_partial(self):
        for index in range(65):
            (self.root / f"untracked-{index}").write_text("fixture")
        self.cli("handoff-save", "--receipt", str(self.receipt),
                 payload={"goal": "Fixture", "next": "Inspect remaining changes"})
        result = self.cli("handoff-read", "--run-id", self.run)
        self.assertEqual(result["git_status"], "partial")

    def test_save_rejects_ownership_text_and_unregistered_references(self):
        for notes in ({"next": self.owner}, {"evidence": ["receipt.json"]}):
            self.cli("handoff-save", "--receipt", str(self.receipt), code=2,
                     payload={"goal": "Fixture", "next": "Check evidence", **notes})
        self.assertFalse(self.receipt.with_name("handoff.json").exists())


if __name__ == "__main__":
    unittest.main()
