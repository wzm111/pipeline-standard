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
