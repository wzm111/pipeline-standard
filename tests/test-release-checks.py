"""Real isolated command counters exercise release ordering and recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import release_checks as F
import run_checks as C
import run_evidence as E
import run_state as R


class ReleaseChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("fixture release contract")
        (self.root / "source.py").write_text("fixture source")
        self.state = R.start(self.root)
        self.args = self.root, self.state["run_id"], self.state["owner_token"]
        self.build_argv = [sys.executable, "-c", "from pathlib import Path; p=Path('build-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); Path('artifact').write_text('verified-output')"]

    def execute(self, name, phase, argv=None, **kwargs):
        return F.execute(*self.args, name, phase=phase,
            argv=argv or [sys.executable,"-c","print('observed fixture check')"],
            scope=["source.py"], timeout=10, **kwargs)

    def prechecks(self):
        self.execute("preflight", "preflight")
        self.execute("compatibility", "compatibility")

    def build(self, name="build", **kwargs):
        return self.execute(name, "build", self.build_argv, artifact_paths=["artifact"], **kwargs)

    def activate(self, name="activate", success=True):
        return self.execute(name, "activate", [sys.executable,"-c",f"raise SystemExit({0 if success else 1})"])

    def smoke(self, name="smoke"):
        return self.execute(name,"smoke",coverage=["release_identity"])

    def plan(self):
        return R.status(self.root)["state"]["release_plan"]

    def test_rollout_blocker_prevents_activation_until_fresh_targeted_retest(self):
        self.prechecks()
        old = self.execute("before-finding", "preflight", coverage=["consent"])
        self.build()
        blocker = E.write_report(*self.args, "release_blocker", "consent-denied", {
            "action": "open", "coverage": "consent", "reason": "Denied events still emitted"})
        with self.assertRaisesRegex(ValueError, "unresolved release blockers"):
            self.activate()
        self.assertFalse(any(a["phase"] == "activate" for a in self.plan()["attempts"]))
        with self.assertRaisesRegex(ValueError, "unresolved release blockers"):
            C.execute_check(*self.args, "monolithic",
                            argv=[sys.executable, "-c", "from pathlib import Path; Path('live').touch()"],
                            scope=["source.py"], coverage=["release_activation"])
        self.assertFalse((self.root / "live").exists())
        with self.assertRaisesRegex(ValueError, "unresolved release blockers"):
            R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")
        resolution = {"action": "resolve", "blocker_ref": blocker, "check_ref": old["report"]}
        with self.assertRaisesRegex(ValueError, "fresh retest"):
            E.write_report(*self.args, "release_blocker", "stale", resolution)
        fresh = self.execute("retest", "preflight", [sys.executable, "-c", "print('consent retest')"],
                             coverage=["consent"])
        E.write_report(*self.args, "release_blocker", "resolved",
                       resolution | {"check_ref": fresh["report"]})
        self.assertEqual(self.activate()["exit_code"], 0)

    def test_activation_rechecks_review_under_lock_before_registering_attempt(self):
        self.prechecks()
        self.build()
        before = self.plan()["attempts"]
        with patch("review_control.validate_pre_activation", side_effect=[
                None, ValueError("verification changed during activation registration")]):
            with self.assertRaisesRegex(ValueError, "verification changed"):
                self.activate()
        self.assertEqual(self.plan()["attempts"], before)

    def declare_project_activation_gate(self):
        (self.root / "pipeline.release.json").write_text(
            json.dumps({"activation_guard": "guard.py", "inputs": ["guard-input"]}))
        (self.root / "guard-input").write_text("gate dependency")
        (self.root / "guard.py").write_text(
            "import hashlib, json, subprocess, sys\n"
            "ctx = json.loads(sys.argv[1])\n"
            "assert ctx['candidate_assets'] == ['artifact'] and sys.argv[2] == '--'\n"
            "assert ctx['candidate_asset_sha256']['artifact'] == hashlib.sha256(b'verified-output').hexdigest()\n"
            "assert ctx['project_guard_input_sha256']['guard-input'] == hashlib.sha256(b'gate dependency').hexdigest()\n"
            "assert ctx['environment']['pipeline_release_candidate'] == ctx['candidate_id']\n"
            "if ctx['environment'].get('test_decision') != 'allow':\n"
            "    raise SystemExit(2)\n"
            "raise SystemExit(subprocess.run(sys.argv[3:], check=False).returncode)\n")

    def test_project_gate_wraps_real_activation_and_blocks_other_runner_paths(self):
        self.declare_project_activation_gate()
        self.prechecks()
        self.build()
        command = [sys.executable, "-c", "from pathlib import Path; Path('switched').touch()"]
        with self.assertRaisesRegex(ValueError, "requires release_checks activate"):
            C.execute_check(*self.args, "direct", argv=command,
                            scope=["source.py"], coverage=["release_activation"])
        import observed_checks
        with self.assertRaisesRegex(ValueError, "requires release_checks activate"):
            observed_checks.begin(*self.args, "native", command="fixture-native", scope=["source.py"],
                                  environment={"scope": "test"}, coverage=["release_activation"])
        self.assertFalse((self.root / "switched").exists())
        rejected = self.execute("guard-rejected", "activate", command, environment={"test_decision": "deny"})
        self.assertFalse(rejected["release_accepted"])
        self.assertFalse((self.root / "switched").exists())
        fingerprint = E.fingerprint
        opened = Path.open
        dependency_reads = []

        def count_dependency_reads(path, mode="r", *args, **kwargs):
            if path == self.root / "guard-input" and mode == "rb":
                dependency_reads.append(path)
            return opened(path, mode, *args, **kwargs)

        with patch.object(E, "fingerprint", wraps=fingerprint) as observed, patch.object(Path, "open", count_dependency_reads):
            accepted = self.execute("guard-approved", "activate", command, environment={"test_decision": "allow"})
        self.assertEqual(sum(call.args[1] == ["artifact"] for call in observed.call_args_list), 2)
        self.assertEqual(len(dependency_reads), 2)
        self.assertTrue(accepted["release_accepted"])
        self.assertTrue((self.root / "switched").exists())

    def declare_readiness_probe(self):
        self.declare_project_activation_gate()
        declaration = self.root / "pipeline.release.json"
        declared = json.loads(declaration.read_text()) | {"readiness_probe": "readiness.py"}
        declaration.write_text(json.dumps(declared))
        (self.root / "readiness.py").write_text(
            "from pathlib import Path\n"
            "if not Path('driver-ready').exists(): raise SystemExit(20)\n"
            "if not Path('permission-ready').exists(): raise SystemExit(22)\n")

    def test_readiness_probe_runs_first_and_reports_missing_driver_or_permissions(self):
        self.declare_readiness_probe()
        preflight = [sys.executable, "-c", "from pathlib import Path; Path('full-preflight-ran').touch()"]
        blocked = self.execute("unbound-driver", "preflight", preflight)
        self.assertEqual(blocked["reason_code"], "production_driver_unbound")
        self.assertFalse((self.root / "full-preflight-ran").exists())
        (self.root / "driver-ready").touch()
        denied = self.execute("denied-permissions", "preflight", preflight)
        self.assertEqual(denied["reason_code"], "production_permission_denied")
        self.assertFalse((self.root / "full-preflight-ran").exists())
        self.assertIn("release_readiness", F.public_status(*self.args)["missing_before"])
        (self.root / "permission-ready").touch()
        self.assertTrue(self.execute("permitted", "preflight", preflight,
                                     environment={"release_target": "production-a"})["release_accepted"])
        self.assertTrue((self.root / "full-preflight-ran").exists())
        self.assertEqual([a["phase"] for a in self.plan()["attempts"]],
                         ["readiness", "readiness", "readiness", "preflight"])
        self.execute("compatibility", "compatibility")
        self.build()
        with self.assertRaises(F.PreflightIncomplete) as rejected:
            self.execute("wrong-target", "activate", environment={"release_target": "production-b"})
        self.assertEqual(rejected.exception.missing, ["release_readiness_target"])
        self.assertFalse(any(a["phase"] == "activate" for a in self.plan()["attempts"]))

    def test_activation_rechecks_readiness_after_registration(self):
        self.declare_readiness_probe()
        for path in ("driver-ready", "permission-ready"):
            (self.root / path).touch()
        self.prechecks()
        self.build()
        original = C.execute_check

        def revoke_before_claim(*args, **kwargs):
            if args[3] == "activate":
                (self.root / "permission-ready").unlink()
                self.assertEqual(self.execute("revoked", "preflight")["reason_code"],
                                 "production_permission_denied")
            return original(*args, **kwargs)

        with patch.object(C, "execute_check", side_effect=revoke_before_claim):
            with self.assertRaises(F.PreflightIncomplete):
                self.execute("activate", "activate", environment={"test_decision": "allow"})
        attempt = next(a for a in self.plan()["attempts"] if a["name"] == "activate")
        self.assertFalse(attempt["launch_possible"])
        self.assertNotIn("recovery_hold", attempt)

    def test_new_readiness_cannot_be_paired_with_old_preflight(self):
        self.declare_readiness_probe()
        for path in ("driver-ready", "permission-ready"):
            (self.root / path).touch()
        self.prechecks()
        self.build()
        with self.assertRaises(ValueError):
            self.execute("invalid-preflight", "preflight", coverage="bad")
        self.assertIn("release_preflight", F.public_status(*self.args)["missing_before"])
        with self.assertRaises(F.PreflightIncomplete):
            self.execute("unsafe-launch", "activate")

    def test_activation_rechecks_compatibility_after_registration(self):
        self.declare_readiness_probe()
        for path in ("driver-ready", "permission-ready"):
            (self.root / path).touch()
        self.prechecks()
        self.build()
        original = C.execute_check

        def fail_compatibility_before_claim(*args, **kwargs):
            if args[3] == "activate":
                rejected = self.execute("new-failure", "compatibility", [sys.executable, "-c", "raise SystemExit(1)"])
                self.assertFalse(rejected["release_accepted"])
            return original(*args, **kwargs)

        with patch.object(C, "execute_check", side_effect=fail_compatibility_before_claim):
            with self.assertRaises(F.PreflightIncomplete) as rejected:
                self.execute("activate", "activate", environment={"test_decision": "allow"})
        self.assertEqual(rejected.exception.missing, ["runner_compatibility"])
        self.assertFalse(self.plan()["attempts"][-2]["launch_possible"])

    def test_closeout_accepts_a_fresh_preflight_for_the_same_activated_target(self):
        self.declare_readiness_probe()
        for path in ("driver-ready", "permission-ready"):
            (self.root / path).touch()
        target = {"release_target": "production-a"}
        self.execute("preflight", "preflight", environment=target)
        self.execute("compatibility", "compatibility")
        self.build()
        result = self.execute("activate", "activate", environment=target | {"test_decision": "allow"})
        self.assertTrue(result["release_accepted"])
        self.smoke()
        self.assertIn("report", F.closeout(*self.args))
        updated = self.execute("refreshed-preflight", "preflight", environment=target)
        self.assertTrue(updated["release_accepted"])
        self.assertIn("report", F.closeout(*self.args))

    def test_activation_claim_rejects_a_new_same_command_failure(self):
        self.declare_readiness_probe()
        for path in ("driver-ready", "permission-ready"):
            (self.root / path).touch()
        command = [sys.executable, "-c",
                   "from pathlib import Path; raise SystemExit(Path('compat-fail').exists())"]
        self.execute("preflight", "preflight")
        self.execute("compatibility", "compatibility", command)
        self.build()
        original = C.execute_check

        def run_same_command_before_claim(*args, **kwargs):
            if args[3] == "activate":
                (self.root / "compat-fail").touch()
                failed = original(*self.args, "concurrent-failure", argv=command, scope=["source.py"], intent="check")
                self.assertEqual(failed["exit_code"], 1)
            return original(*args, **kwargs)

        with patch.object(C, "execute_check", side_effect=run_same_command_before_claim):
            with self.assertRaises(F.PreflightIncomplete) as rejected:
                self.execute("activate", "activate", environment={"test_decision": "allow"})
        self.assertEqual(rejected.exception.missing, ["runner_compatibility"])

    def test_valid_local_compatibility_reuses_latest_release_evidence(self):
        command = [sys.executable, "-c",
                   "from pathlib import Path; p=Path('compat-count'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
        first = self.execute("first-compat", "compatibility", command)
        reused = self.execute("retry-compat", "compatibility", command)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["report"], first["report"])
        self.assertEqual((self.root / "compat-count").read_text(), "x")
        changed = self.execute("changed-compat", "compatibility", command, environment={"scope": "changed runner"})
        self.assertFalse(changed["reused"])
        self.assertEqual((self.root / "compat-count").read_text(), "xx")
        failed = self.execute("failed-compat", "compatibility", [sys.executable, "-c", "raise SystemExit(1)"])
        self.assertFalse(failed["release_accepted"])
        observed = self.execute("fresh-compat", "compatibility", command)
        self.assertFalse(observed["reused"])
        self.assertEqual((self.root / "compat-count").read_text(), "xxx")

    def test_compatibility_selects_latest_attempt_under_lock(self):
        self.prechecks()
        original = F._execute_prepared

        def fail_after_prepare(*args, **kwargs):
            if args[3] == "retry":
                failed = self.execute("concurrent-failure", "compatibility", [sys.executable, "-c", "raise SystemExit(1)"])
                self.assertFalse(failed["release_accepted"])
            return original(*args, **kwargs)

        with patch.object(F, "_execute_prepared", side_effect=fail_after_prepare):
            result = self.execute("retry", "compatibility")
        self.assertFalse(result["reused"])
        self.assertTrue(result["release_accepted"])

    def test_last_guard_check_rejects_candidate_change_after_attempt_registration(self):
        self.declare_project_activation_gate()
        self.prechecks()
        self.build()
        execute_check = C.execute_check

        def changed_before_launch(*args, **kwargs):
            (self.root / "artifact").write_text("changed after release preflight")
            return execute_check(*args, **kwargs)

        command = [sys.executable, "-c", "from pathlib import Path; Path('switched').touch()"]
        with patch.object(C, "execute_check", side_effect=changed_before_launch):
            with self.assertRaisesRegex(ValueError, "candidate changed before activation"):
                self.execute("activate", "activate", command, environment={"test_decision": "allow"})
        self.assertFalse((self.root / "switched").exists())
        self.assertNotIn("recovery_hold", self.plan()["attempts"][-1])

    def test_missing_or_changed_project_gate_is_not_allowed_to_activate(self):
        self.declare_project_activation_gate()
        with self.assertRaisesRegex(ValueError, "was not prepared"):
            C.execute_check(*self.args, "unprepared", argv=self.build_argv,
                            scope=["source.py"], coverage=["release_activation"])
        self.prechecks()
        self.build()
        (self.root / "guard-input").write_text("changed")
        with self.assertRaisesRegex(ValueError, "gate changed after release preparation"):
            self.activate()
        (self.root / "pipeline.release.json").unlink()
        with self.assertRaisesRegex(ValueError, "gate changed after release preparation"):
            self.activate()
        self.assertFalse(any(a["phase"] == "activate" for a in self.plan()["attempts"]))

    def test_fail_fast_before_build_when_runner_compatibility_has_not_passed(self):
        self.execute("preflight", "preflight")
        bad=self.execute("compatibility", "compatibility", [sys.executable,"-c","raise SystemExit(1)"])
        self.assertEqual(bad["exit_code"],1)
        with self.assertRaisesRegex(ValueError,"prechecks"):
            self.build()
        self.assertFalse((self.root/"build-count").exists())

    def test_valid_candidate_is_reused_after_activation_failure_without_rebuilding(self):
        self.prechecks()
        self.build()
        self.activate("first",False)
        reused=self.build("reuse")
        self.assertTrue(reused["reused"])
        self.assertEqual((self.root/"build-count").read_text(),"1")
        self.assertEqual(self.activate("recovery")["exit_code"],0)
        self.smoke()
        self.assertFalse(F.closeout(*self.args)["reused"])

    def test_missing_or_changed_artifact_forces_actual_build(self):
        self.prechecks()
        self.build()
        (self.root/"artifact").unlink()
        self.assertFalse(self.build("missing")["reused"])
        (self.root/"artifact").write_text("damaged")
        self.assertFalse(self.build("changed")["reused"])
        self.assertEqual((self.root/"build-count").read_text(),"3")

    def test_changed_process_environment_or_requested_command_cannot_reuse_candidate(self):
        self.prechecks()
        self.build()
        with patch.dict(os.environ,{"PIPELINE_TEST_TOOL_ID":"changed"}):
            self.assertFalse(self.build("environment")["reused"])
        command=[*self.build_argv[:2],self.build_argv[2]+"; print('different build command')"]
        changed=self.execute("command","build",command,artifact_paths=["artifact"])
        self.assertFalse(changed["reused"])
        self.assertEqual((self.root/"build-count").read_text(),"3")

    def test_source_change_invalidates_prechecks_and_candidate(self):
        self.prechecks()
        self.build()
        (self.root/"source.py").write_text("changed source")
        with self.assertRaisesRegex(ValueError,"prechecks"):
            self.activate()
        with self.assertRaisesRegex(ValueError,"prechecks"):
            self.build("changed")

    def test_two_activation_failures_stop_and_names_or_same_candidate_cannot_reset_limit(self):
        self.prechecks()
        self.build()
        self.activate("first",False)
        self.activate("recovery",False)
        old_id=self.plan()["candidate"]["id"]
        # Even a genuinely rebuilt identical output cannot reset recovery.
        (self.root/"artifact").unlink()
        self.build("rebuilt")
        self.assertEqual(self.plan()["candidate"]["id"],old_id)
        with self.assertRaisesRegex(ValueError,"limit"):
            self.activate("renamed")

    def test_successful_activation_is_not_repeated_to_generate_another_report(self):
        self.prechecks()
        self.build()
        self.activate()
        with self.assertRaisesRegex(ValueError,"already activated"):
            self.activate("duplicate")
        self.assertEqual(len([a for a in self.plan()["attempts"] if a["phase"]=="activate"]),1)

    def test_live_preflight_and_smoke_are_not_cached_from_old_observations(self):
        self.prechecks()
        self.assertFalse(self.execute("fresh-preflight","preflight")["reused"])
        self.build()
        self.activate()
        self.smoke()
        self.assertFalse(self.smoke("fresh-smoke")["reused"])

    def test_closeout_is_single_report_and_external_outcomes_stay_pending(self):
        self.prechecks()
        self.build()
        self.activate()
        self.smoke()
        first=F.closeout(*self.args,external_pending=["search_indexing","ai_recommendation"])
        before={p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        second=F.closeout(*self.args,external_pending=["ai_recommendation","search_indexing"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["report"],second["report"])
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        payload=E.read_report(self.root,self.state["run_id"],first["report"])["payload"]
        self.assertEqual(payload["external_pending"],["ai_recommendation","search_indexing"])
        self.assertNotIn("search_indexing",payload["coverage"]["covered"])

    def test_pending_external_result_cannot_erase_contract_requirement(self):
        F.prepare(*self.args,required_after=["search_indexing"])
        self.prechecks()
        self.build()
        self.activate()
        self.smoke()
        with self.assertRaisesRegex(ValueError,"incomplete"):
            F.closeout(*self.args,external_pending=["search_indexing"])

    def test_health_identity_and_activation_are_required_before_closeout(self):
        self.prechecks()
        self.build()
        with self.assertRaises(ValueError):
            self.smoke()
        self.activate()
        self.execute("partial-smoke","smoke")
        with self.assertRaises(ValueError):
            F.closeout(*self.args)
        self.smoke()
        self.assertTrue(F.closeout(*self.args)["report"])

    def test_latest_failure_blocks_old_pass_and_failed_build_cannot_certify_output(self):
        self.prechecks()
        result=self.build()
        old=E.read_report(self.root,self.state["run_id"],result["report"])["payload"]
        failed=deepcopy(old)
        failed.update(exit_code=1,coverage=["other-label"])
        E.write_report(*self.args,"check","latest-failure",failed)
        with self.assertRaises(ValueError):
            self.activate()

    def test_successful_process_with_missing_output_is_not_accepted_as_candidate(self):
        self.prechecks()
        with self.assertRaisesRegex(ValueError,"produce"):
            self.execute("empty-build","build",artifact_paths=["missing-artifact"])
        self.assertEqual(self.plan()["attempts"][-1]["status"],"failed")
        self.assertNotIn("candidate",self.plan())

    def test_wrong_owner_and_outside_artifacts_are_rejected(self):
        with self.assertRaises(ValueError):
            F.prepare(self.root,self.state["run_id"],"wrong")
        with self.assertRaises(ValueError):
            self.execute("outside","build",artifact_paths=["../outside"])

    def test_new_candidate_requires_its_own_post_activation_identity_and_smoke(self):
        self.prechecks()
        self.build()
        self.activate()
        self.smoke()
        F.closeout(*self.args)
        other=[*self.build_argv[:2],self.build_argv[2]+"; Path('artifact').write_text('candidate-b')"]
        self.execute("build-b","build",other,artifact_paths=["artifact"])
        self.activate("activate-b")
        with self.assertRaisesRegex(ValueError,"incomplete"):
            F.closeout(*self.args)
        self.smoke("smoke-b")
        packet=F.closeout(*self.args)
        self.assertIn("smoke-b",json.dumps(E.read_report(self.root,self.state["run_id"],packet["report"])["payload"]["coverage"]["sources"]["release_smoke"]))

    def test_finish_requires_current_closeout_and_keeps_active_on_missing_smoke(self):
        self.prechecks()
        self.build()
        self.activate()
        with self.assertRaisesRegex(ValueError,"closeout"):
            R.finish(*self.args,"completed",executors_stopped=True,stop_evidence="fixture commands ended")
        self.assertIsNotNone(R.status(self.root)["active"])
        self.smoke()
        F.closeout(*self.args)
        final=R.finish(*self.args,"completed",executors_stopped=True,stop_evidence="fixture commands ended")
        self.assertEqual(final["status"],"completed")

    def test_failed_new_build_spec_never_silently_activates_previous_candidate(self):
        self.prechecks()
        self.build()
        result=self.execute("failed-new-spec","build",[sys.executable,"-c","raise SystemExit(5)"],artifact_paths=["artifact"])
        self.assertEqual(result["exit_code"],5)
        with self.assertRaisesRegex(ValueError,"candidate"):
            self.activate()
        self.build("explicit-successful-rebuild")
        self.assertEqual(self.activate()["exit_code"],0)

    def test_new_run_must_refresh_preflight_and_runtime_even_with_inherited_checks(self):
        self.prechecks()
        self.build()
        self.activate()
        self.smoke()
        F.closeout(*self.args)
        R.finish(*self.args,"completed",executors_stopped=True,stop_evidence="fixture commands ended")
        self.state=R.start(self.root,{"delivery_from":"latest"})
        self.args=self.root,self.state["run_id"],self.state["owner_token"]
        self.assertTrue(self.state["delivery_status"]["adopted"])
        with self.assertRaisesRegex(ValueError,"prechecks"):
            self.build("must-not-build")
        self.prechecks()
        self.assertEqual(self.build("new-build")["exit_code"],0)

    def test_later_preflight_or_smoke_failure_blocks_old_success_with_different_command(self):
        self.prechecks()
        self.execute("new-failed-preflight","preflight",[sys.executable,"-c","raise SystemExit(7)"])
        with self.assertRaisesRegex(ValueError,"prechecks"):
            self.build()
        self.execute("restored-preflight","preflight")
        self.build()
        self.activate()
        self.smoke()
        F.closeout(*self.args)
        self.execute("later-smoke-failed","smoke",[sys.executable,"-c","raise SystemExit(8)"],coverage=["release_identity"])
        with self.assertRaisesRegex(ValueError,"incomplete"):
            F.closeout(*self.args)


if __name__=="__main__":
    unittest.main()
