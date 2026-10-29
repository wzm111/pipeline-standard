"""Release holds use real local command counters and isolated temporary runs."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import delivery as D
import release_checks as F
import run_evidence as E
import run_state as R


class ReleaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "source").write_text("fixture")
        self.state = R.start(self.root)
        self.args = self.root, self.state["run_id"], self.state["owner_token"]
        self.execute("preflight", "preflight")
        self.execute("compatibility", "compatibility")

    def execute(self, name, phase, code=0, *, argv=None, timeout=5):
        script = ("from pathlib import Path; "
                  "p=Path('command-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
                  "Path('artifact').write_text('candidate'); "
                  f"raise SystemExit({code})")
        return F.execute(*self.args, name, phase=phase,
                         argv=argv or [sys.executable, "-c", script], scope=["source"], timeout=timeout,
                         **({"artifact_paths": ["artifact"]} if phase == "build" else {}))

    def plan(self):
        return R.status(self.root)["state"]["release_plan"]

    def attempt(self, name):
        return next(a for a in self.plan()["attempts"] if a["name"] == name)

    def count(self):
        return int((self.root / "command-count").read_text())

    def evidence(self, name, label, **changes):
        payload = {"attempt": name, "executors_stopped": True, "stop_evidence": "caller observed executor exit and target rollback",
                   "remote_state": "not_activated", **changes}
        relative = E.write_report(*self.args, "release_recovery", label, payload)
        return D._reference(self.root, self.state["run_id"], relative)

    def reconcile(self, name, label):
        return F.reconcile(*self.args, name, evidence=self.evidence(name, label))

    def test_unknown_build_outcome_blocks_renamed_commands_closeout_and_finish_but_allows_probe(self):
        result = self.execute("lost-build", "build", 255)
        self.assertEqual(result["exit_code"], 255)
        self.assertEqual(result["result_class"], "command_failure")
        self.assertFalse(result["release_accepted"])
        self.assertTrue(result["recovery_blocked"])
        count = self.count()
        with self.assertRaisesRegex(ValueError, "hold"):
            self.execute("renamed-build", "build")
        with self.assertRaises(ValueError):
            self.execute("renamed-activation", "activate")
        self.assertEqual(self.count(), count)
        self.execute("onsite-probe", "preflight")
        self.assertEqual(self.count(), count + 1)
        with self.assertRaises(ValueError):
            F.closeout(*self.args)
        with self.assertRaises(ValueError):
            R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="local process stopped")
        self.assertEqual(R.status(self.root)["state"]["status"], "running")
        self.assertFalse(self.reconcile("lost-build", "fresh-safe")["recovery_blocked"])
        self.assertEqual(self.attempt("lost-build")["status"], "failed")
        self.assertTrue(self.execute("new-build", "build")["release_accepted"])

    def test_recovery_requires_fresh_registered_attempt_bound_safe_immutable_owned_evidence(self):
        self.execute("build", "build")
        old = self.evidence("activation", "old")
        self.execute("activation", "activate", 255)
        bad = [old,
               self.evidence("activation", "unknown", remote_state="unknown"),
               self.evidence("activation", "activated", remote_state="activated"),
               self.evidence("activation", "active", executors_stopped=False),
               self.evidence("another-attempt", "wrong-attempt"),
               self.evidence("activation", "empty-stop", stop_evidence=" ")]
        good = self.evidence("activation", "good")
        bad.extend([{**good, "run_id": "0" * 32}, {**good, "sha256": "0" * 64}])
        unregistered = self.root / good["path"].replace("good.json", "unregistered.json")
        unregistered.write_bytes((self.root / good["path"]).read_bytes())
        bad.append(D._reference(self.root, self.state["run_id"], unregistered.relative_to(self.root).as_posix()))
        for evidence in bad:
            with self.subTest(evidence=evidence["path"]):
                with self.assertRaises(ValueError):
                    F.reconcile(*self.args, "activation", evidence=evidence)
        with self.assertRaises(ValueError):
            F.reconcile(self.root, self.state["run_id"], "wrong-owner", "activation", evidence=good)
        before = self.count()
        with self.assertRaises(ValueError):
            self.execute("still-blocked", "activate")
        self.assertEqual(self.count(), before)
        F.reconcile(*self.args, "activation", evidence=good)
        self.assertEqual(self.attempt("activation")["result"]["exit_code"], 255)
        self.assertTrue(self.execute("retry", "activate")["release_accepted"])

    def test_shell_signal_and_timeout_codes_hold_without_reclassification(self):
        for code in (124, 130, 137, 143, 255):
            name = f"build-{code}"
            result = self.execute(name, "build", code)
            self.assertEqual(result["exit_code"], code)
            self.assertEqual(result["result_class"], "command_failure")
            self.assertTrue(result["recovery_blocked"])
            self.reconcile(name, f"safe-{code}")
        timeout = self.execute("timeout", "build", argv=[sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.02)
        self.assertEqual(timeout["result_class"], "timeout")
        self.assertLess(timeout["exit_code"], 0)
        self.assertTrue(timeout["recovery_blocked"])
        self.reconcile("timeout", "safe-timeout")
        signal = self.execute("signal", "build", argv=[sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)"])
        self.assertLess(signal["exit_code"], 0)
        self.assertTrue(signal["recovery_blocked"])

    def test_execution_and_post_command_exceptions_persist_holds(self):
        with patch.object(F.C, "execute_check", side_effect=RuntimeError("local observation interrupted")):
            with self.assertRaises(RuntimeError):
                self.execute("exception", "build")
        self.assertEqual(self.attempt("exception")["status"], "failed")
        self.reconcile("exception", "safe-exception")
        original = F.E.fingerprint
        def unavailable(root, paths):
            if paths == ["artifact"]:
                raise OSError("artifact observation unavailable")
            return original(root, paths)
        with patch.object(F.E, "fingerprint", side_effect=unavailable):
            with self.assertRaises(OSError):
                self.execute("post-exception", "build")
        attempt = self.attempt("post-exception")
        self.assertEqual(attempt["result"]["exit_code"], 0)
        self.assertEqual(attempt["recovery_hold"]["status"], "held")
        self.assertNotIn("candidate", self.plan())

    def test_late_cancel_preserves_raw_exit_zero_without_passing_build_or_activation(self):
        original = F.C.execute_check
        def cancel_after_command(*args, **kwargs):
            result = original(*args, **kwargs)
            F.cancel_attempt(*self.args, args[3], reason="caller cancelled while adapter was executing")
            return result
        with patch.object(F.C, "execute_check", side_effect=cancel_after_command):
            result = self.execute("cancelled-build", "build")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["result_class"], "success")
        self.assertFalse(result["release_accepted"])
        self.assertTrue(result["recovery_blocked"])
        self.assertEqual(self.attempt("cancelled-build")["status"], "failed")
        self.assertNotIn("candidate", self.plan())
        self.reconcile("cancelled-build", "build-safe")
        self.execute("build", "build")
        with patch.object(F.C, "execute_check", side_effect=cancel_after_command):
            result = self.execute("cancelled-activation", "activate")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["release_accepted"])
        self.assertEqual(self.attempt("cancelled-activation")["status"], "failed")
        self.assertFalse(F._activated(self.plan(), self.plan()["candidate"]))
        report = E.read_report(self.root, self.state["run_id"], result["report"], kind="check")
        self.assertEqual(report["payload"]["exit_code"], 0)

    def test_running_attempt_mutual_exclusion_and_reconcile_never_run_an_extra_command(self):
        original = F.C.execute_check
        def while_running(*args, **kwargs):
            before = self.count()
            with self.assertRaisesRegex(ValueError, "running"):
                self.execute("competing-build", "build")
            self.assertEqual(self.count(), before)
            F.cancel_attempt(*self.args, args[3], reason="caller requested cancellation")
            evidence = self.evidence(args[3], args[3] + "-too-soon")
            with self.assertRaisesRegex(ValueError, "running"):
                F.reconcile(*self.args, args[3], evidence=evidence)
            return original(*args, **kwargs)
        with patch.object(F.C, "execute_check", side_effect=while_running):
            self.execute("running-build", "build")
        self.reconcile("running-build", "stopped-build")
        self.execute("built", "build")
        with patch.object(F.C, "execute_check", side_effect=while_running):
            self.execute("running-activation", "activate")

    def test_resolved_hash_is_rechecked_and_new_cancel_cannot_use_old_reference(self):
        self.execute("build", "build")
        self.execute("activation", "activate", 255)
        reference = self.evidence("activation", "first-safe")
        F.reconcile(*self.args, "activation", evidence=reference)
        F.cancel_attempt(*self.args, "activation", reason="new uncertainty after original recovery")
        with self.assertRaises(ValueError):
            F.reconcile(*self.args, "activation", evidence=reference)
        second = self.evidence("activation", "second-safe")
        F.reconcile(*self.args, "activation", evidence=second)
        target = self.root / second["path"]
        target.write_text(target.read_text() + " ")
        before = self.count()
        with self.assertRaisesRegex(ValueError, "hold"):
            self.execute("changed-proof", "activate")
        with self.assertRaisesRegex(ValueError, "hold"):
            F.closeout(*self.args)
        self.assertEqual(self.count(), before)

    def test_transport_hold_then_cancel_requires_evidence_after_cancellation(self):
        self.execute("build", "build")
        self.execute("activation", "activate", 255)
        reference = self.evidence("activation", "before-cancellation")
        held_at = self.attempt("activation")["recovery_hold"]["held_at"]
        cancelled = F.cancel_attempt(*self.args, "activation", reason="caller cancelled after transport loss")
        self.assertEqual(cancelled["recovery_hold"]["held_at"], held_at)
        self.assertGreater(cancelled["cancel_requested_at"], E.read_report(
            self.root, self.state["run_id"], reference["path"], kind="release_recovery")["recorded_at"])
        repeated = F.cancel_attempt(*self.args, "activation", reason="same unresolved cancellation")
        self.assertEqual(repeated["cancel_requested_at"], cancelled["cancel_requested_at"])
        with self.assertRaises(ValueError):
            F.reconcile(*self.args, "activation", evidence=reference)
        before = self.count()
        with self.assertRaisesRegex(ValueError, "hold"):
            self.execute("old-proof-blocked", "activate")
        self.assertEqual(self.count(), before)
        self.assertFalse(self.reconcile("activation", "after-cancellation")["recovery_blocked"])
        self.assertTrue(self.execute("fresh-proof-retry", "activate")["release_accepted"])

    def test_two_activation_limit_survives_recovery_and_post_success_cancel_preserves_record(self):
        self.execute("build", "build")
        self.execute("first", "activate", 255)
        self.reconcile("first", "safe-first")
        self.execute("second", "activate", 255)
        self.reconcile("second", "safe-second")
        before = self.count()
        with self.assertRaisesRegex(ValueError, "limit"):
            self.execute("renamed-third", "activate")
        self.assertEqual(self.count(), before)
        # A completed successful build retains its outcome when a later safety
        # cancellation records new uncertainty; the new hold blocks acceptance.
        old = deepcopy(self.attempt("build"))
        candidate = deepcopy(self.plan()["candidate"])
        F.cancel_attempt(*self.args, "build", reason="late safety stop")
        after = self.attempt("build")
        for key in ("status", "report", "result", "finished_at"):
            self.assertEqual(after[key], old[key])
        self.assertEqual(self.plan()["candidate"], candidate)
        self.assertEqual(after["recovery_hold"]["status"], "held")


if __name__ == "__main__":
    unittest.main()
