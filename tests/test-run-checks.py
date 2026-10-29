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

    def test_environment_diagnostics_are_private_and_strict_drift_blocks_reuse(self):
        previous = {"PATH": "private-path", "SECRET_FIXTURE": "private-secret"}
        current = dict(previous, PATH="changed-path", SECRET_FIXTURE="changed-secret")
        diagnostic = C.environment_diagnostics(previous)
        encoded = json.dumps(diagnostic)
        for private in ("private-path", "private-secret", "SECRET_FIXTURE"):
            self.assertNotIn(private, encoded)
        self.assertNotIn(__import__("hashlib").sha256(json.dumps("private-secret").encode()).hexdigest(), encoded)
        self.assertEqual(C.environment_changes(diagnostic, C.environment_diagnostics(current)),
                         {"status": "known", "changed_keys": ["PATH"], "other_environment_changed": True})
        self.assertEqual(C.environment_changes(None, diagnostic), {"status": "unknown"})
        with patch.dict(os.environ, {"NODE_ENV": "fixture-a", "SECRET_FIXTURE": "private-secret"}):
            first = self.execute("diagnostic-first")
            self.assertTrue(self.execute("diagnostic-hit", impact_covered=True)["reused"])
        with patch.dict(os.environ, {"NODE_ENV": "fixture-b", "SECRET_FIXTURE": "changed-secret"}):
            changed = self.execute("diagnostic-drift", impact_covered=True)
            self.assertFalse(changed["reused"])
            delta = changed["reuse_decision"]["environment_changes"]
            self.assertIn("NODE_ENV", delta["changed_keys"])
            self.assertTrue(delta["other_environment_changed"])
            self.assertEqual(changed["reuse_decision"]["reason"], "command_or_environment_changed")
        self.assertIn("observed_environment_diagnostics", self.payload(first))

    def test_legacy_diagnostics_are_optional_and_explicit_superseded_drift_is_explained(self):
        with patch.dict(os.environ, {"NODE_ENV": "legacy"}):
            first = self.execute("legacy-original")
            proof = self.payload(first)
            legacy = E.record_check(self.root, self.state["run_id"], self.state["owner_token"],
                                    "legacy-proof", command=proof["command"], scope=proof["scope"],
                                    environment=proof["environment"], exit_code=0, log_path=proof["log_path"],
                                    started_fingerprint=proof["fingerprint"])
            self.assertTrue(self.execute("legacy-hit", reuse_from=legacy, impact_covered=True)["reused"])
        with patch.dict(os.environ, {"NODE_ENV": "new"}):
            changed = self.execute("legacy-drift", impact_covered=True)
            self.assertEqual(changed["reuse_decision"]["environment_changes"], {"status": "unknown"})
        with patch.dict(os.environ, {"NODE_ENV": "legacy"}):
            superseded = self.execute("superseded-drift", reuse_from=first["report"], impact_covered=True)
            self.assertFalse(superseded["reused"])
            decision = superseded["reuse_decision"]
            self.assertEqual(decision["reason"], "superseded_observation")
            self.assertEqual(decision["latest_reason"], "command_or_environment_changed")
            self.assertEqual(decision["environment_changes"]["changed_keys"], ["NODE_ENV"])

    def test_actual_package_cwd_is_used_and_prevents_cross_package_reuse(self):
        for package in ("one", "two"):
            (self.root / package).mkdir()
        source = "from pathlib import Path; Path('counter').write_text('ran')"
        first = self.execute("package-one", source, cwd="one", impact_covered=True)
        same = self.execute("package-one-again", source, cwd="one", impact_covered=True)
        other = self.execute("package-two", source, cwd="two", impact_covered=True)
        self.assertTrue(same["reused"])
        self.assertFalse(other["reused"])
        self.assertEqual(self.payload(first)["cwd"], "one")
        self.assertTrue((self.root / "one/counter").exists())
        self.assertTrue((self.root / "two/counter").exists())
        self.assertFalse((self.root / "counter").exists())

    def test_runner_preflight_is_read_only_and_invalid_cwd_never_executes(self):
        (self.root / "package").mkdir()
        before = sorted(str(p) for p in self.root.rglob("*"))
        runner = C.inspect_runner(self.root, [sys.executable, "-c", "raise SystemExit(1)"], cwd="package")
        self.assertTrue(runner["available"])
        self.assertEqual(runner["cwd"], "package")
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        self.assertFalse(C.inspect_runner(self.root, ["/nonexistent/fixture-tool"])["available"])
        for cwd in ("../outside", "missing", str(self.root)):
            with self.assertRaises(ValueError):
                self.execute("bad-cwd", "open('bad','w').close()", cwd=cwd)
        (self.root / "alias").symlink_to(self.root / "package", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.execute("bad-link", cwd="alias")
        self.assertFalse((self.root / "bad").exists())

    def test_cache_decision_explains_reexecution_without_dumping_environment(self):
        first = self.execute("baseline")
        self.assertEqual(first["reuse_decision"]["reason"], "impact_not_confirmed")
        hit = self.execute("hit", impact_covered=True)
        self.assertTrue(hit["reused"])
        self.assertEqual(hit["reuse_decision"]["source"], first["report"])
        (self.root / "input.txt").write_text("new input")
        changed = self.execute("input-change", impact_covered=True)
        self.assertEqual(changed["reuse_decision"]["reason"], "inputs_changed")
        private_context = {"external_revision": "fixture-private-environment-value"}
        env = self.execute("env-change", impact_covered=True, environment=private_context)
        self.assertEqual(env["reuse_decision"]["reason"], "command_or_environment_changed")
        self.assertNotIn(private_context["external_revision"], json.dumps(env["reuse_decision"]))
        (self.root / self.payload(env)["log_path"]).write_text("changed log")
        log = self.execute("log-change", impact_covered=True, environment=private_context)
        self.assertEqual(log["reuse_decision"]["reason"], "log_changed")
        self.assertEqual(self.payload(log)["reuse_decision"], log["reuse_decision"])
        labels = self.execute("labels-change", impact_covered=True, environment=private_context, coverage=["unit"])
        self.assertEqual(labels["reuse_decision"]["reason"], "coverage_changed")
        other = self.execute("other-command", "print('a different check')", impact_covered=True)
        self.assertEqual(other["reuse_decision"]["reason"], "no_matching_check")

    def test_latest_failure_in_another_environment_blocks_older_success(self):
        source = "import os; raise SystemExit(int(os.environ['FIXTURE_EXIT']))"
        with patch.dict(os.environ, {"FIXTURE_EXIT": "0"}):
            first = self.execute("env-pass", source, impact_covered=True)
        with patch.dict(os.environ, {"FIXTURE_EXIT": "1"}):
            failed = self.execute("env-fail", source, impact_covered=True)
            self.assertFalse(failed["reused"])
            self.assertEqual(failed["exit_code"], 1)
        with patch.dict(os.environ, {"FIXTURE_EXIT": "0"}):
            retry = self.execute("env-retry", source, impact_covered=True)
        self.assertEqual(first["exit_code"], 0)
        self.assertFalse(retry["reused"])
        self.assertEqual(retry["exit_code"], 0)
        self.assertEqual(retry["reuse_decision"], {"source": failed["report"], "reason": "check_not_passed"})

    def test_explicit_old_pass_cannot_bypass_newer_manual_failure_or_invalid_log(self):
        for index, observation in enumerate(("legacy_failure", "invalid_log")):
            with self.subTest(observation=observation):
                source = f"from pathlib import Path; p=Path('counter{index}'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
                first = self.execute(f"first{index}", source, coverage=["unit"])
                if observation == "legacy_failure":
                    proof = self.payload(first)
                    log = f"manual{index}.log"
                    (self.root / log).write_text("actual failure")
                    latest = E.record_check(self.root, self.state["run_id"], self.state["owner_token"],
                                             f"manual{index}", command=proof["command"], scope=proof["scope"],
                                             environment=proof["environment"], exit_code=1, log_path=log,
                                             started_fingerprint=E.fingerprint(self.root, proof["scope"]),
                                             coverage=["different_label"])
                    reason = "check_not_passed"
                else:
                    newer = self.execute(f"newer{index}", source, coverage=["unit"])
                    latest = newer["report"]
                    (self.root / self.payload(newer)["log_path"]).write_text("replaced log")
                    reason = "log_changed"
                retry = self.execute(f"retry{index}", source, reuse_from=first["report"],
                                     impact_covered=True, coverage=["unit"])
                self.assertFalse(retry["reused"])
                self.assertEqual(retry["reuse_decision"]["reason"], "superseded_observation")
                self.assertEqual(retry["reuse_decision"]["latest_source"], latest)
                self.assertEqual(retry["reuse_decision"]["latest_reason"], reason)
                self.assertEqual((self.root / f"counter{index}").read_text(), "2" if index == 0 else "3")

    def test_explicit_reference_respects_latest_failure_in_another_environment(self):
        source = "import os; raise SystemExit(int(os.environ['FIXTURE_EXIT']))"
        with patch.dict(os.environ, {"FIXTURE_EXIT": "0"}):
            first = self.execute("explicit-env-pass", source)
        with patch.dict(os.environ, {"FIXTURE_EXIT": "1"}):
            latest = self.execute("explicit-env-fail", source)
        with patch.dict(os.environ, {"FIXTURE_EXIT": "0"}):
            retry = self.execute("explicit-env-retry", source, reuse_from=first["report"], impact_covered=True)
        self.assertFalse(retry["reused"])
        self.assertEqual(retry["exit_code"], 0)
        self.assertEqual(retry["reuse_decision"]["latest_source"], latest["report"])

    def test_explicit_valid_reference_survives_other_cwd_failure_and_newer_valid_pass(self):
        for directory in ("one", "two"):
            (self.root / directory).mkdir()
        source = "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); raise SystemExit(int(Path.cwd().name=='two'))"
        first = self.execute("one-pass", source, cwd="one", coverage=["unit"])
        self.execute("two-fail", source, cwd="two", coverage=["unit"])
        retry = self.execute("one-reuse", source, cwd="one", coverage=["unit"],
                             reuse_from=first["report"], impact_covered=True)
        self.assertTrue(retry["reused"])
        self.execute("one-new-pass", source, cwd="one", coverage=["unit"])
        self.assertTrue(self.execute("one-valid-reuse", source, cwd="one", coverage=["unit"],
                                    reuse_from=first["report"], impact_covered=True)["reused"])
        self.assertEqual((self.root / "one/counter").read_text(), "2")

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
    def test_timeout_does_not_signal_the_already_cleaned_process_group_twice(self):
        original = C.os.killpg
        signals = []
        def signal_once(pid, sig):
            signals.append((pid, sig))
            if len(signals) > 1:
                raise PermissionError("fixture: group no longer owned after cleanup")
            return original(pid, sig)
        with patch.object(C.os, "killpg", side_effect=signal_once):
            result = self.execute("one-cleanup", "import time; time.sleep(20)", timeout=0.05)
        self.assertEqual(result["result_class"], "timeout")
        self.assertEqual(len(signals), 1)

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
