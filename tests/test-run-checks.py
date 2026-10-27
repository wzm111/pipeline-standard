"""Foreground checks execute only fixture commands in self-cleaning projects."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import run_checks as C
import run_evidence as E
import run_state as R


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = R.start(self.root, {"stage": "implementation"})
        (self.root / "input.txt").write_text("known input")

    def execute(self, name="test", source="print('PASS')", **changes):
        args = dict(argv=[sys.executable, "-c", source], scope=["input.txt"])
        args.update(changes)
        return C.execute_check(self.root, self.state["run_id"], self.state["owner_token"], name, **args)

    def payload(self, result):
        return E.read_report(self.root, self.state["run_id"], result["report"], kind="check")["payload"]

    def test_executes_captures_observed_result_and_restores_phase(self):
        result = self.execute()
        payload = self.payload(result)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["result_class"], "success")
        self.assertGreater(payload["seconds"], 0)
        self.assertTrue(payload["inputs_stable"])
        self.assertEqual((self.root / payload["log_path"]).read_text().strip(), "PASS")
        state = R.status(self.root)["state"]
        self.assertEqual(state["stage"], "implementation")
        self.assertEqual(state["activity"], "execution")
        self.assertEqual([e["stage"] for e in state["history"]], ["implementation", "checking", "implementation"])
        self.assertEqual(C.check_summary(self.root)["by_result_class"], {"success": 1})

    def test_failure_classification_does_not_make_a_qa_gate(self):
        for index, (intent, code, expected) in enumerate([
            ("search", 1, "search_no_match"), ("search", 2, "command_failure"),
            ("locate", 1, "path_lookup_failure"), ("reproduce", 1, "reproduction_nonzero"),
            ("test", 1, "test_failure"), ("check", 1, "command_failure")]):
            row = self.execute(f"case{index}", f"raise SystemExit({code})", intent=intent)
            self.assertEqual(row["result_class"], expected)
            self.assertNotIn("gate", self.payload(row))
        summary = C.check_summary(self.root)
        self.assertEqual(summary["executed"], 6)
        self.assertEqual(summary["by_result_class"]["test_failure"], 1)

    def test_search_and_locate_keep_execution_phase_instead_of_test_phase(self):
        for index, intent in enumerate(("search", "locate")):
            source = ("import json; from pathlib import Path; "
                      f"s=json.loads(Path('tmp/pipeline/runs/{self.state['run_id']}.json').read_text()); "
                      "assert s['stage']=='implementation' and s['activity']=='execution'")
            self.assertEqual(self.execute(f"explore{index}", source, intent=intent)["exit_code"], 0)

    def test_successful_reuse_skips_execution_and_is_counted(self):
        source = "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        first = self.execute("first", source)
        reused = self.execute("second", source, reuse_from=first["report"], impact_covered=True)
        self.assertTrue(reused["reused"])
        self.assertEqual((self.root / "counter").read_text(), "1")
        self.assertEqual(C.check_summary(self.root)["reused"], 1)
        self.assertEqual(C.check_summary(self.root)["executed"], 1)

    def test_reasoned_failure_diagnosis_preserves_raw_result_and_exit(self):
        failed = self.execute("failed", "raise SystemExit(1)")
        C.review_check(self.root, self.state["run_id"], self.state["owner_token"], "diagnosis",
                       failed["report"], "fixture_failure", "isolated test fixture uses a stale selector")
        summary = C.check_summary(self.root)
        self.assertEqual(summary["by_result_class"], {"fixture_failure": 1})
        self.assertEqual(summary["by_raw_result_class"], {"test_failure": 1})
        self.assertEqual(self.payload(failed)["exit_code"], 1)
        self.assertEqual(summary["reviews"], 1)

    def test_diagnosis_cannot_relabel_success_or_invent_unsupported_causes(self):
        passed = self.execute("passed")
        failed = self.execute("failed", "raise SystemExit(1)")
        for source, category, reason in [(passed["report"], "fixture_failure", "why"),
                                          (failed["report"], "success", "why"),
                                          (failed["report"], "regression_failure", "")]:
            with self.assertRaises(ValueError):
                C.review_check(self.root, self.state["run_id"], self.state["owner_token"], "bad", source, category, reason)

    def test_changed_environment_scope_and_old_run_report_cannot_skip_execution(self):
        first = self.execute("first")
        changed = self.execute("changed", reuse_from=first["report"], impact_covered=True, environment={"browser": "new"})
        self.assertFalse(changed["reused"])
        (self.root / "other").write_text("another dependency")
        changed = self.execute("scope", scope=["input.txt", "other"], reuse_from=first["report"], impact_covered=True)
        self.assertFalse(changed["reused"])
        forged = first["report"].replace(self.state["run_id"], "0" * 32)
        self.assertFalse(self.execute("old", reuse_from=forged, impact_covered=True)["reused"])

    @unittest.skipUnless(os.name == "posix", "fixture uses POSIX executable scripts")
    def test_relative_and_empty_path_entries_use_child_cwd_and_tool_changes_invalidate(self):
        for index, entry in enumerate(("bin", "")):
            directory = self.root / entry
            directory.mkdir(exist_ok=True)
            executable = directory / "fixture-check"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"PATH": entry + os.pathsep + os.defpath}):
                first = self.execute(f"tool{index}", argv=["fixture-check"])
                self.assertNotEqual(self.payload(first)["environment"]["executable_sha256"], "unavailable")
                executable.write_text("#!/bin/sh\nexit 1\n")
                second = self.execute(f"toolchanged{index}", argv=["fixture-check"], reuse_from=first["report"], impact_covered=True)
                self.assertFalse(second["reused"])
                self.assertEqual(second["exit_code"], 1)

    def test_unknown_executable_identity_cannot_be_cached(self):
        with patch.object(C, "_environment", return_value={"executable_sha256": "unavailable"}):
            first = self.execute("unknown")
            self.assertFalse(first["inputs_stable"])
            second = self.execute("again", reuse_from=first["report"], impact_covered=True)
            self.assertFalse(second["reused"])

    def test_inflight_mutation_remains_unusable_even_after_input_reverted(self):
        result = self.execute(source="from pathlib import Path; Path('input.txt').write_text('changed')")
        self.assertFalse(result["inputs_stable"])
        (self.root / "input.txt").write_text("known input")
        payload = self.payload(result)
        reuse = E.reusable_check(self.root, self.state["run_id"], result["report"], command=payload["command"],
                                 environment=payload["environment"], impact_covered=True)
        self.assertEqual(reuse["reason"], "inputs_changed_during_check")

    def test_no_shell_expansion_and_owner_not_in_child_environment(self):
        old = os.environ.get("PIPELINE_OWNER_TOKEN")
        self.addCleanup(lambda: os.environ.pop("PIPELINE_OWNER_TOKEN", None) if old is None else os.environ.__setitem__("PIPELINE_OWNER_TOKEN", old))
        os.environ["PIPELINE_OWNER_TOKEN"] = self.state["owner_token"]
        source = "import os,sys; assert 'PIPELINE_OWNER_TOKEN' not in os.environ; print(sys.argv[1])"
        result = self.execute(argv=[sys.executable, "-c", source, "$(touch should-not-exist)"])
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse((self.root / "should-not-exist").exists())
        self.assertNotIn(self.state["owner_token"], json.dumps(self.payload(result)))

    def test_missing_command_is_recorded_and_timeout_restores_state(self):
        missing = self.execute("missing", argv=["/nonexistent/pipeline-check-command"])
        self.assertEqual(missing["result_class"], "command_unavailable")
        result = self.execute("timeout", "import time; time.sleep(20)", timeout=0.05)
        self.assertEqual(result["result_class"], "timeout")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(R.status(self.root)["state"]["running_checks"], [])

    @unittest.skipUnless(os.name == "posix", "process group cleanup is POSIX-specific")
    def test_timeout_cleans_our_descendant_process_group(self):
        source = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); Path('child-pid').write_text(str(p.pid)); time.sleep(20)"
        row = self.execute("tree", source, timeout=0.4)
        self.assertEqual(row["result_class"], "timeout")
        pid = int((self.root / "child-pid").read_text())
        observed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True, capture_output=True)
        self.assertTrue(not observed.stdout.strip() or observed.stdout.strip().startswith("Z"), observed.stdout)

    def test_conflicts_are_rejected_and_declared_isolation_is_allowed(self):
        C._claim(self.root, self.state["run_id"], self.state["owner_token"], "held", ["dist"])
        try:
            with self.assertRaises(ValueError):
                self.execute("blocked", write_paths=["dist/assets"])
            isolated = self.execute("isolated", write_paths=["isolated/dist"])
            self.assertEqual(isolated["exit_code"], 0)
        finally:
            C._release(self.root, self.state["run_id"], self.state["owner_token"], "held")
        self.assertEqual(R.status(self.root)["state"]["stage"], "implementation")

    def test_wrong_owner_duplicate_names_and_bad_arguments_never_start(self):
        with self.assertRaises(ValueError):
            C.execute_check(self.root, self.state["run_id"], "wrong", "wrong", argv=[sys.executable, "-c", "open('bad','w').close()"], scope=["input.txt"])
        self.assertFalse((self.root / "bad").exists())
        self.execute()
        with self.assertRaises(FileExistsError):
            self.execute(source="open('bad','w').close()")
        for changes in ({"argv": "echo bad"}, {"timeout": float("nan")}, {"intent": "invented"}):
            with self.assertRaises(ValueError):
                self.execute("invalid", **changes)
        self.assertFalse((self.root / "bad").exists())

    def test_explicit_recovery_clears_stale_claim_after_confirmed_stop(self):
        C._claim(self.root, self.state["run_id"], self.state["owner_token"], "stale", None)
        R.resume(self.root, self.state["run_id"], self.state["owner_token"],
                 executors_stopped=True, stop_evidence="fixture did not create a process")
        self.assertNotIn("running_checks", R.status(self.root)["state"])
        self.assertEqual(self.execute("recovered")["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
