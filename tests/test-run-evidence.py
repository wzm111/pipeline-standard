"""Real isolated files exercise ownership, stale evidence and write conflicts."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import run_evidence as E
import run_state as R


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = R.start(self.root)
        (self.root / "src").mkdir()
        (self.root / "src/a.py").write_text("answer = 42\n")
        (self.root / "log.txt").write_text("PASS\n")
        self.env = {"python": "fixture-runtime", "dependencies": "fixture-lock"}

    def report(self, name="final", **kwargs):
        return E.write_report(self.root, self.state["run_id"],
                              kwargs.pop("owner", self.state["owner_token"]), "qa", name, kwargs or {"gate": "PASS"})

    def check(self, name="unit", **changes):
        options = dict(command="python tests.py", scope=["src"], environment=self.env,
                       exit_code=0, log_path="log.txt", started_fingerprint=E.fingerprint(self.root, ["src"]), seconds=1.5)
        options.update(changes)
        return E.record_check(self.root, self.state["run_id"], self.state["owner_token"], name, **options)

    def reuse(self, relative, **changes):
        args = dict(command="python tests.py", environment=self.env, impact_covered=True)
        args.update(changes)
        return E.reusable_check(self.root, self.state["run_id"], relative, **args)

    def test_reports_are_run_bound_registered_immutable_and_owner_free(self):
        relative = self.report()
        self.assertIn(relative, R.status(self.root)["state"]["artifacts"])
        report = E.read_report(self.root, self.state["run_id"], relative, kind="qa")
        self.assertEqual(report["payload"], {"gate": "PASS"})
        self.assertEqual(report["run_id"], self.state["run_id"])
        self.assertNotIn(self.state["owner_token"], (self.root / relative).read_text())
        with self.assertRaises(FileExistsError):
            self.report()
        self.assertEqual(E.read_report(self.root, self.state["run_id"], relative), report)

    def test_legacy_cross_run_and_forged_metadata_cannot_be_accepted(self):
        (self.root / "tmp/pipeline/legacy.md").write_text("gate: PASS")
        with self.assertRaises(ValueError):
            E.read_report(self.root, self.state["run_id"], "tmp/pipeline/legacy.md")
        relative = self.report()
        other = "0" * 32
        with self.assertRaises(ValueError):
            E.read_report(self.root, other, relative)
        data = json.loads((self.root / relative).read_text())
        data["run_id"] = other
        (self.root / relative).write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            E.read_report(self.root, self.state["run_id"], relative)

    def test_writes_require_active_owner_and_never_leak_owner_in_payload(self):
        with self.assertRaises(ValueError):
            self.report(owner="wrong")
        with self.assertRaises(ValueError):
            self.report(payload={"nested": [{"owner_token": "hidden"}]})
        with self.assertRaises(ValueError):
            self.report(payload={"items": ({"owner_token": self.state["owner_token"]},)})
        with self.assertRaises(ValueError):
            self.report(payload={"summary": "accidental token: " + self.state["owner_token"]})
        R.finish(self.root, self.state["run_id"], self.state["owner_token"], "completed",
                 executors_stopped=True, stop_evidence="isolated fixture has no executors")
        with self.assertRaises(ValueError):
            self.report()

    def test_read_queries_and_reuse_do_not_mutate_files(self):
        relative = self.check()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(self.reuse(relative)["reusable"])
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_scope_changes_detect_file_edit_addition_deletion_and_mode(self):
        relative = self.check()
        path = self.root / "src/a.py"
        original = path.read_bytes()
        path.write_text("answer = 7")
        self.assertEqual(self.reuse(relative)["reason"], "inputs_changed")
        path.write_bytes(original)
        extra = self.root / "src/added.py"
        extra.write_text("new import")
        self.assertFalse(self.reuse(relative)["reusable"])
        extra.unlink()
        path.chmod(0o700)
        self.assertFalse(self.reuse(relative)["reusable"])
        path.unlink()
        self.assertFalse(self.reuse(relative)["reusable"])

    def test_environment_command_log_and_coverage_must_match(self):
        relative = self.check()
        self.assertFalse(self.reuse(relative, environment={"python": "other"})["reusable"])
        self.assertFalse(self.reuse(relative, command="different command")["reusable"])
        self.assertEqual(self.reuse(relative, impact_covered=False)["reason"], "impact_not_confirmed")
        (self.root / "log.txt").write_text("tampered")
        self.assertEqual(self.reuse(relative)["reason"], "log_changed")
        (self.root / "log.txt").unlink()
        self.assertFalse(self.reuse(relative)["reusable"])

    def test_failure_unknown_and_inflight_changes_cannot_be_reused(self):
        failed = self.check(exit_code=1)
        self.assertEqual(self.reuse(failed)["reason"], "check_not_passed")
        before = E.fingerprint(self.root, ["src"])
        (self.root / "src/a.py").write_text("changed during check")
        with self.assertRaises(ValueError):
            self.check("unit2", started_fingerprint=before)
        with self.assertRaises(ValueError):
            self.check("unit2", started_fingerprint=None)
        with self.assertRaises(ValueError):
            self.check("unit2", seconds=float("nan"))
        with self.assertRaises(ValueError):
            self.check("unit2", exit_code=True)

    def test_symlinks_traversal_and_unsafe_labels_are_rejected(self):
        (self.root / "src/link").symlink_to(self.root / "log.txt")
        with self.assertRaises(ValueError):
            E.fingerprint(self.root, ["src"])
        for scope in (["../outside"], ["/absolute"], ["."]):
            with self.assertRaises(ValueError):
                E.fingerprint(self.root, scope)
        for name in ("../escape", "with/slash", ""):
            with self.assertRaises(ValueError):
                self.report(name)
        (self.root / "tmp/pipeline/runs" / self.state["run_id"]).symlink_to(self.root / "src", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.report()

    def test_write_failure_preserves_owned_state_and_cleans_coordination_lock(self):
        before = R.status(self.root)["state"]
        with patch.object(R, "_atomic", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.report()
        self.assertEqual(R.status(self.root)["state"], before)
        self.assertFalse((self.root / ".pipeline.models.lock").exists())

    def test_nested_write_paths_conflict_and_unknown_sets_are_conservative(self):
        for a, b in [([".nuxt"], [".nuxt/types"]), (["dist"], ["dist"]), (None, ["dist"])]:
            self.assertTrue(E.write_paths_conflict(self.root, a, b))
        self.assertFalse(E.write_paths_conflict(self.root, ["isolated-a/.nuxt"], ["isolated-b/.nuxt"]))
        self.assertFalse(E.write_paths_conflict(self.root, [], ["dist"]))
        self.assertFalse(E.write_paths_conflict(self.root, ["dist"], ["distribution"]))


if __name__ == "__main__":
    unittest.main()
