"""Local preview does not transmit, write, inspect raw content or invent facts."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import audit_preview as V
import run_audit as A
import run_evidence as E
import run_state as R
import run_tasks as T


class AuditPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def start(self):
        state = R.start(self.root)
        return state, (self.root, state["run_id"], state["owner_token"])

    def files(self):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}

    def test_empty_project_preview_does_not_create_state_or_report(self):
        value = V.preview(self.root)
        self.assertEqual(value["snapshot_status"], "none")
        self.assertEqual(value["selection"], "none")
        self.assertIsNone(value["task_count"])
        self.assertIsNone(value["timing"]["tracked_seconds"])
        self.assertIn("未知", V.render(value))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_current_run_and_last_completed_selection_keep_unknown_model_and_no_ids(self):
        state, args = self.start()
        current = V.preview(self.root)
        self.assertEqual(current["selection"], "active")
        self.assertEqual(current["run_status"], "running")
        R.finish(*args, "completed", executors_stopped=True, stop_evidence="fixture direct process stopped")
        completed = V.preview(self.root)
        self.assertEqual(completed["selection"], "latest")
        self.assertEqual(completed["run_status"], "completed")
        for value in (current, completed):
            text = json.dumps(value)
            for private in (state["run_id"], state["owner_token"], str(self.root), "source_state_hash", "implementation_hash"):
                self.assertNotIn(private, text)
            self.assertIsNone(value["actual_model"])
            self.assertIsNone(value["tokens"])
            self.assertTrue(value["local_only"])

    def test_stale_snapshot_and_live_view_are_distinct_and_readonly(self):
        state, args = self.start()
        R.update(*args, {"stage": "PRIVATE", "activity": "human_wait"})
        before = self.files()
        stored = V.preview(self.root, state["run_id"])
        live = V.preview(self.root, state["run_id"], live=True)
        self.assertEqual(stored["snapshot_status"], "stale")
        self.assertEqual(stored["source"], "stored")
        self.assertEqual(live["snapshot_status"], "stale")
        self.assertEqual(live["source"], "live")
        self.assertNotEqual(stored["snapshot_at"], live["snapshot_at"])
        self.assertEqual(before, self.files())
        self.assertNotIn("PRIVATE", json.dumps(live))

    def test_no_network_process_raw_file_read_source_hash_or_lineage_validation(self):
        state, args = self.start()
        T.start(*args, "private-task", "PRIVATE goal", ["PRIVATE acceptance"])
        T.end(*args, "private-task")
        (self.root / "source.py").write_text("PRIVATE source")
        (self.root / "raw.log").write_text("PRIVATE log")
        E.write_report(*args, "check", "check", {"command": "PRIVATE command", "scope": ["source.py"],
            "environment": {"PRIVATE": "PRIVATE credential"}, "log_path": "raw.log", "result_class": "fixture_failure"})
        before = self.files()
        original = Path.open
        def guarded(path, *a, **kw):
            if path in {self.root / "source.py", self.root / "raw.log"}:
                raise AssertionError("raw file opened")
            return original(path, *a, **kw)
        with patch.object(Path, "open", guarded), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("process")), \
             patch.object(E, "fingerprint", side_effect=AssertionError("source scan")), \
             patch.object(E, "validate_lineage", side_effect=AssertionError("not acceptance")):
            value = V.preview(self.root, live=True)
            text = V.render(value) + json.dumps(value)
        self.assertEqual(value["checks"]["by_result_class"]["fixture_failure"], 1)
        for private in ("PRIVATE", "private-task", state["owner_token"], state["run_id"], str(self.root)):
            self.assertNotIn(private, text)
        self.assertEqual(before, self.files())

    def test_unknown_active_never_falls_back_to_old_success_or_clears_marker(self):
        _, args = self.start()
        R.finish(*args, "completed", executors_stopped=True, stop_evidence="fixture stopped")
        (self.root / "tmp/pipeline/.active").write_text("PRIVATE legacy marker")
        before = self.files()
        value = V.preview(self.root)
        self.assertEqual(value["snapshot_status"], "error")
        self.assertEqual(value["error"], "active_owner_unrecognized")
        self.assertEqual(value["source"], "none")
        self.assertEqual(before, self.files())
        self.assertNotIn("PRIVATE", V.render(value))

    def test_active_owner_mismatch_is_rejected_without_leaking_private_content(self):
        _, args = self.start()
        marker = self.root / "tmp/pipeline/.active"
        value = json.loads(marker.read_text());value["owner_token"] = "PRIVATE wrong owner";marker.write_text(json.dumps(value))
        before = self.files()
        result = V.preview(self.root)
        self.assertEqual(result["snapshot_status"], "error")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(before, self.files())

    def test_missing_legacy_summary_not_recreated_and_live_optin_is_memory_only(self):
        state, _ = self.start()
        target = self.root / f"tmp/pipeline/runs/{state['run_id']}/audit.json"
        target.unlink()
        before = self.files()
        self.assertEqual(V.preview(self.root)["snapshot_status"], "missing")
        live = V.preview(self.root, live=True)
        self.assertEqual(live["source"], "live")
        self.assertEqual(live["adoption"]["mode"], "legacy_untracked")
        self.assertEqual(before, self.files())

    def test_tampered_and_symlink_audits_refuse_without_overwrite(self):
        state, _ = self.start()
        target = self.root / f"tmp/pipeline/runs/{state['run_id']}/audit.json"
        target.write_text('{"PRIVATE":"PRIVATE"}')
        before = self.files()
        value = V.preview(self.root)
        self.assertEqual(value["snapshot_status"], "error")
        self.assertNotIn("PRIVATE", V.render(value))
        self.assertEqual(before, self.files())
        target.unlink();outside = self.root / "outside.json";outside.write_text("PRIVATE external")
        target.symlink_to(outside)
        self.assertEqual(V.preview(self.root)["error"], "unsafe_path")
        self.assertEqual(outside.read_text(), "PRIVATE external")

    def test_explicit_invalid_identity_never_echoes_user_input(self):
        value = V.preview(self.root, "PRIVATE invalid ID")
        self.assertEqual(value["snapshot_status"], "error")
        self.assertNotIn("PRIVATE", json.dumps(value) + V.render(value))

    def test_latest_scan_is_bounded_and_retains_missing_selected_record(self):
        first, args = self.start()
        R.finish(*args, "completed", executors_stopped=True, stop_evidence="fixture stopped")
        second, args = self.start()
        R.finish(*args, "aborted", executors_stopped=True, stop_evidence="fixture stopped")
        target = self.root / f"tmp/pipeline/runs/{second['run_id']}/audit.json"
        target.unlink()
        newest = self.root / f"tmp/pipeline/runs/{second['run_id']}.json"
        older = self.root / f"tmp/pipeline/runs/{first['run_id']}.json"
        os.utime(newest, ns=(older.stat().st_mtime_ns + 1000000000,) * 2)
        value = V.preview(self.root)
        self.assertEqual(value["snapshot_status"], "missing")
        self.assertTrue(value["selection_truncated"])
        explicit = V.preview(self.root, first["run_id"])
        self.assertEqual(explicit["run_status"], "completed")

    def test_known_key_nested_identifiers_and_bad_count_types_are_not_projected(self):
        state, _ = self.start()
        target = self.root / f"tmp/pipeline/runs/{state['run_id']}/audit.json"
        audit = json.loads(target.read_text())
        audit["checks"]["executed"] = {"run_id": "a" * 32}
        audit["checks"]["reused"] = True
        audit["checks"]["inherited"] = 1.5
        target.write_text(json.dumps(audit))
        value = V.preview(self.root)
        self.assertEqual(value["snapshot_status"], "current")
        self.assertIsNone(value["checks"]["executed"])
        self.assertIsNone(value["checks"]["reused"])
        self.assertIsNone(value["checks"]["inherited"])
        self.assertNotIn("a" * 32, json.dumps(value))

    def test_installed_cli_defaults_to_text_and_json_is_explicit(self):
        state, _ = self.start()
        before = self.files()
        for runtime in (".claude", ".codex"):
            with tempfile.TemporaryDirectory() as folder:
                scripts = Path(folder) / runtime / "skills/pipeline/scripts"
                shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
                command = [sys.executable, "-B", str(scripts / "pipeline_cli.py"), "preview", "--project", str(self.root)]
                result = subprocess.run(command, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("本地运行报告", result.stdout)
                self.assertNotIn(state["owner_token"], result.stdout)
                result = subprocess.run(command + ["--format", "json", "--live"], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["local_only"])
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
