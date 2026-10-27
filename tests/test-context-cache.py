"""Real file fixtures for context reuse and invalidation, no model calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import context_cache as C
import run_state as R


class ContextCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("allowed fixture scope")
        self.state = R.start(self.root)

    def lookup(self, sources=None):
        return C.lookup(self.root, self.state["run_id"], self.state["owner_token"], "base", sources or ["PIPELINE.md"])

    def save(self, ticket, content="summary of the actual fixture"):
        return C.save(self.root, self.state["run_id"], self.state["owner_token"], ticket, content)

    def test_warm_lookup_emits_reference_without_repeating_source_or_summary(self):
        saved = self.save(self.lookup())
        hit = self.lookup()
        self.assertEqual(hit["status"], "unchanged")
        self.assertEqual(hit["reference"], saved["reference"])
        self.assertFalse(hit["content_included"])
        self.assertNotIn("summary of", json.dumps(hit))
        self.assertNotIn("content", hit)
        self.assertEqual(C.load(self.root, hit["reference"])["content"], "summary of the actual fixture")

    def test_changed_added_deleted_and_redeclared_sources_require_refresh(self):
        saved = self.save(self.lookup(["PIPELINE.md", "AGENTS.md"]))
        (self.root / "AGENTS.md").write_text("new instructions")
        self.assertEqual(self.lookup(["PIPELINE.md", "AGENTS.md"])["status"], "refresh_required")
        with self.assertRaises(ValueError):
            C.load(self.root, saved["reference"])
        self.save(self.lookup())
        (self.root / "PIPELINE.md").unlink()
        self.assertEqual(self.lookup()["status"], "refresh_required")

    def test_changed_during_summary_is_never_certified(self):
        ticket = self.lookup()
        (self.root / "PIPELINE.md").write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.save(ticket)
        self.assertFalse((self.root / "tmp/pipeline/context/cache/base.json").exists())

    def test_base_automatically_tracks_contract_and_new_agent_instructions(self):
        (self.root / "notes.md").write_text("extra context")
        ticket = self.lookup(["notes.md"])
        self.assertTrue({"PIPELINE.md", "AGENTS.md", "CLAUDE.md"}.issubset(ticket["sources"]))
        self.save(ticket)
        (self.root / "PIPELINE.md").write_text("new contract")
        self.assertEqual(self.lookup(["notes.md"])["status"], "refresh_required")
        self.save(self.lookup(["notes.md"]))
        (self.root / "CLAUDE.md").write_text("new local constraint")
        self.assertEqual(self.lookup(["notes.md"])["status"], "refresh_required")

    def test_summary_and_reference_tampering_invalidate_cache(self):
        saved = self.save(self.lookup())
        artifact = self.root / saved["reference"]["path"]
        artifact.write_text(artifact.read_text() + " ")
        self.assertEqual(self.lookup()["status"], "refresh_required")
        with self.assertRaises(ValueError):
            C.load(self.root, saved["reference"])

    def test_next_run_can_reuse_only_unchanged_same_project_context(self):
        saved = self.save(self.lookup())
        R.finish(self.root, self.state["run_id"], self.state["owner_token"], "completed",
                 executors_stopped=True, stop_evidence="fixture has no executors")
        self.state = R.start(self.root)
        self.assertEqual(self.lookup()["reference"], saved["reference"])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError, OSError)):
                C.load(directory, saved["reference"])

    def test_ownership_symlink_and_scope_protection(self):
        with self.assertRaises(ValueError):
            C.lookup(self.root, self.state["run_id"], "wrong", "base", ["PIPELINE.md"])
        (self.root / "alias").symlink_to(self.root / "PIPELINE.md")
        for sources in (["alias"], ["../outside"], ["."]):
            with self.assertRaises(ValueError):
                self.lookup(sources)
        with self.assertRaises(ValueError):
            self.save(self.lookup(), self.state["owner_token"])

    def test_cold_load_is_read_only_and_duplicate_save_is_idempotent(self):
        ticket = self.lookup()
        saved = self.save(ticket)
        again = self.save(ticket)
        self.assertEqual(saved, again)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        C.load(self.root, saved["reference"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
