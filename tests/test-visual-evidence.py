"""Synthetic image structure checks do not claim native capture or visual QA."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import check_plan as P
import observed_checks as H
import run_evidence as E
import run_policy as Q
import run_state as R
import run_tasks as T
import run_checks as C
import visual_evidence as V
import pipeline_cli as CLI
import role_dispatch as D
import work_allocation as W


def png():
    def chunk(tag, value):
        return struct.pack(">I", len(value)) + tag + value + struct.pack(">I", zlib.crc32(tag + value))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + b"\x80" * 9 + b"\0" + b"\x80" * 9)) + chunk(b"IEND", b""))


def jpeg():
    # SOF/SOS/entropy framing only; no promise that this fixture decodes pixels.
    return (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00"
            b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x7f\xff\x00\x7f\xff\xd9")


class VisualTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "PIPELINE.md").write_text("synthetic fixture contract")
        (self.root / "source").write_text("v1")
        state = R.start(self.root, managed=True)
        self.args = self.root, state["run_id"], state["owner_token"]
        Q.acknowledge(*self.args, Q.snapshot(self.root)["revision"])
        self.surface = dict(target="checkout", viewport={"width": 1280, "height": 720}, state="ready", version="v1.2.0")
        self.requirement = V.declare(*self.args, "checkout", scope=["source"], **self.surface)

    def begin(self, name="capture", image_format="jpeg"):
        return V.begin(*self.args, self.requirement, name, image_format=image_format)

    def developer_request(self, alias):
        W.decide(*self.args, "serial", "shared_write_paths",
                 [{"id": "fixture-work", "executor_id": alias, "write_paths": [], "depends_on": []}])
        return D.begin(*self.args, "developer", work_item="fixture-work")

    def image(self, ticket, data):
        path = self.root / ticket["image_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def finish(self, name="capture", **changes):
        return V.finish(*self.args, self.requirement, name, observed=changes.get("observed", self.surface),
                         host_stopped=changes.get("host_stopped", True), stop_evidence="fixture host returned")

    def inspect(self):
        return V.inspect(self.root, self.args[1], self.requirement)

    def captured_view(self, name, reviewer=None):
        self.image(self.begin(name), jpeg())
        capture = self.finish(name)
        viewed = V.view(*self.args, capture, name + "-view", viewed=True, reviewer_dispatch=reviewer)
        return capture, viewed

    def judgment(self, name, capture, viewed, gate="PASS", findings=None, **options):
        return V.review(*self.args, capture, viewed, name, gate=gate, findings=findings or [], **options)

    def formal_qa(self, name="unit", *, reuse=False):
        check = C.execute_check(*self.args, name, argv=[sys.executable, "-B", "-c", "print('actual unit fixture')"],
                                scope=["source"], coverage=["unit"], write_paths=[], impact_covered=reuse)
        ref = E.write_report(*self.args, "qa", name, {"gate": "PASS", "checks": [check["report"]], "required_checks": ["unit"]})
        return E.read_report(self.root, self.args[1], ref)["payload"]

    def completed(self):
        return R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture executors returned")

    def adopted(self):
        state = R.start(self.root, {"delivery_from": "latest"}, managed=True)
        self.args = self.root, state["run_id"], state["owner_token"]
        Q.acknowledge(*self.args, Q.snapshot(self.root)["revision"])
        return state

    def test_png_and_jpeg_actual_bytes_dimensions_timing_viewing_and_readonly_status(self):
        self.assertEqual(self.inspect()["status"], "capture_pending")
        for kind, data in (("jpeg", jpeg()), ("png", png())):
            with self.subTest(kind=kind):
                ticket = self.begin(kind, kind)
                self.assertTrue(ticket["created"])
                self.assertFalse(self.begin(kind, kind)["created"])
                self.image(ticket, data)
                ref = self.finish(kind)
                capture = E.read_report(self.root, self.args[1], ref)["payload"]
                self.assertEqual((capture["image"]["width"], capture["image"]["height"]), (3, 2))
                self.assertEqual(capture["image"]["mime"], "image/" + kind)
                observation = E.read_report(self.root, self.args[1], capture["observation"])["payload"]
                self.assertEqual(observation["intent"], "visual")
                self.assertGreaterEqual(observation["seconds"], 0)
                self.assertEqual(self.inspect()["status"], "view_pending")
                with self.assertRaises(ValueError):
                    V.view(*self.args, ref, "no-view")
                V.view(*self.args, ref, kind + "-view", viewed=True)
                before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
                result = self.inspect()
                self.assertEqual(result["status"], "view_declared")
                self.assertTrue(result["viewed"])
                self.assertFalse(result["native_proof"])
                self.assertNotIn(self.args[2], json.dumps(result))
                self.assertNotIn("source", json.dumps(result))
                self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
                with self.assertRaises(FileExistsError):
                    self.finish(kind)

    def test_missing_truncated_corrupt_oversize_and_wrong_format_images_refuse(self):
        ticket = self.begin()
        with self.assertRaises(ValueError):
            self.finish()
        for data in (b"not-image", jpeg()[:-1], png()[:-1], png()[:35] + b"bad", png()):
            self.image(ticket, data)
            with self.assertRaises(ValueError):
                self.finish()
        self.image(ticket, jpeg())
        with patch.object(V, "MAX_BYTES", 5):
            with self.assertRaises(ValueError):
                self.finish()
        self.assertEqual(self.inspect()["status"], "capture_pending")

    def test_source_changes_during_capture_and_after_capture_never_stay_current(self):
        ticket = self.begin()
        self.image(ticket, jpeg())
        (self.root / "source").write_text("v2")
        with self.assertRaisesRegex(ValueError, "changed during capture"):
            self.finish()
        ticket = self.begin("fresh")
        image = self.image(ticket, jpeg())
        ref = self.finish("fresh")
        self.assertEqual(self.inspect()["status"], "view_pending")
        (self.root / "source").write_text("v3")
        self.assertEqual(self.inspect()["status"], "stale_or_unconfirmed")
        with self.assertRaises(ValueError):
            V.view(*self.args, ref, "stale", viewed=True)
        (self.root / "source").write_text("v2")
        image.write_bytes(jpeg() + b"trailing")
        self.assertEqual(self.inspect()["status"], "stale_or_unconfirmed")

    def test_wrong_surface_stopping_and_preexisting_image_refuse(self):
        path = self.root / V._image_path(self.args[1], "old", "jpeg")
        path.parent.mkdir(parents=True)
        path.write_bytes(jpeg())
        with self.assertRaises(ValueError):
            self.begin("old")
        ticket = self.begin()
        self.image(ticket, jpeg())
        for key, value in (("state", "loading"), ("version", "v2"), ("target", "other"),
                           ("viewport", {"width": 320, "height": 640})):
            with self.assertRaises(ValueError):
                self.finish(observed={**self.surface, key: value})
        with self.assertRaises(ValueError):
            self.finish(host_stopped=False)
        self.finish()

    def test_old_task_cross_run_unsafe_paths_credentials_and_wrong_owner_reject(self):
        ticket = self.begin()
        self.image(ticket, jpeg())
        self.finish()
        T.start(*self.args, "new", "new goal", ["observe"])
        with self.assertRaises(ValueError):
            self.begin("wrong-task")
        with self.assertRaises(ValueError):
            self.inspect()
        for scope in (["../source"], ["/source"], ["tmp/pipeline"]):
            with self.assertRaises(ValueError):
                V.declare(*self.args, "unsafe", scope=scope, **self.surface)
        (self.root / "link").symlink_to(self.root / "source")
        with self.assertRaises(ValueError):
            V.declare(*self.args, "link", scope=["link"], **self.surface)
        with self.assertRaises(ValueError):
            V.declare(*self.args, "secret", scope=["source"], **{**self.surface, "version": self.args[2]})
        with self.assertRaises(ValueError):
            V.begin(self.root, self.args[1], "wrong-owner", self.requirement, "no-owner")
        with self.assertRaises((ValueError, FileNotFoundError)):
            V.inspect(self.root, "a" * 32, self.requirement)

    def test_symlink_image_and_tampered_requirement_or_observation_refuse(self):
        ticket = self.begin()
        image = self.root / ticket["image_path"]
        image.parent.mkdir(parents=True)
        (self.root / "outside.jpg").write_bytes(jpeg())
        image.symlink_to(self.root / "outside.jpg")
        with self.assertRaises(ValueError):
            self.finish()
        image.unlink()
        image.write_bytes(jpeg())
        ref = self.finish()
        payload = E.read_report(self.root, self.args[1], ref)["payload"]
        proof_path = self.root / payload["observation"]
        proof = json.loads(proof_path.read_text())
        proof["payload"]["log_sha256"] = "0" * 64
        proof_path.write_text(json.dumps(proof))
        self.assertEqual(self.inspect()["status"], "stale_or_unconfirmed")

    def test_visual_capture_cannot_be_formal_acceptance_plan_or_reuse(self):
        ticket = self.begin()
        self.image(ticket, jpeg())
        ref = self.finish()
        capture = E.read_report(self.root, self.args[1], ref)["payload"]
        proof = E.read_report(self.root, self.args[1], capture["observation"])["payload"]
        self.assertEqual(E.reusable_check(self.root, self.args[1], capture["observation"], command=proof["command"],
            environment=proof["environment"], impact_covered=True)["reason"], "visual_not_acceptance")
        check = dict(argv=[sys.executable, "-B", "-c", "print('check')"], scope=["source"], coverage=["unit"], intent="visual")
        with self.assertRaises(ValueError):
            P.save_plan(*self.args, "invalid", {"unit": check})
        # Native capture remains outside the formal test/check identity set.
        import managed_qa
        self.assertEqual(managed_qa._checks(self.root, R.status(self.root)["state"]), (set(), {}))

    def test_new_pending_or_failed_capture_does_not_revive_old_view(self):
        ticket = self.begin()
        self.image(ticket, jpeg())
        ref = self.finish()
        V.view(*self.args, ref, "view", viewed=True)
        self.assertEqual(self.inspect()["status"], "view_declared")
        newer = self.begin("newer")
        self.assertEqual(self.inspect()["status"], "capture_pending")
        self.image(newer, b"failed fixture capture")
        H.finish(*self.args, "newer", exit_code=1, log_path=newer["image_path"],
                 host_stopped=True, stop_evidence="fixture failure returned")
        self.assertEqual(self.inspect()["status"], "capture_pending")

    def test_cli_roundtrip_no_credentials_and_safe_status(self):
        receipt = {"run_id": self.args[1], "owner_token": self.args[2]}
        def cli(action, *options):
            args = CLI.parser().parse_args([action, "--project", str(self.root), *options])
            with patch.object(CLI, "_receipt", return_value=receipt):
                result, code = CLI.dispatch(args)
            self.assertEqual(code, 0)
            self.assertNotIn(self.args[2], json.dumps(result))
            return result
        declaration = cli("visual-declare", "--receipt", "fixture", "--name", "cli", "--json",
                          json.dumps({"scope": ["source"], **self.surface}))["report"]
        begun = cli("visual-begin", "--receipt", "fixture", "--requirement", declaration, "--name", "cli")
        self.image(begun, jpeg())
        captured = cli("visual-finish", "--receipt", "fixture", "--requirement", declaration, "--name", "cli",
                       "--json", json.dumps(self.surface), "--host-stopped", "--stop-evidence", "fixture host returned")["report"]
        viewed = cli("visual-view", "--receipt", "fixture", "--capture", captured, "--name", "cli-view", "--viewed")["report"]
        self.assertEqual(cli("visual-inspect", "--run-id", self.args[1], "--requirement", declaration)["status"], "view_declared")
        cli("visual-review", "--receipt", "fixture", "--capture", captured, "--view", viewed,
            "--name", "cli-review", "--json", '{"gate":"PASS","findings":[]}')
        self.assertEqual(cli("visual-inspect", "--run-id", self.args[1], "--requirement", declaration)["review"], "PASS")
        with self.assertRaises(ValueError):
            V.begin(*self.args, self.requirement, "v" + self.args[2])

    def test_small_ui_no_findings_one_self_review_without_extra_role(self):
        capture, viewed = self.captured_view("small")
        ref = self.judgment("review", capture, viewed)
        result = self.inspect()
        self.assertEqual((result["status"], result["review"]), ("review_declared", "PASS"))
        self.assertFalse(result["native_proof"])
        self.assertFalse(R.status(self.root)["state"].get("role_dispatches"))
        with self.assertRaises(FileExistsError):
            self.judgment("review", capture, viewed)
        before = (self.root / ref).read_bytes()
        (self.root / "source").write_text("changed")
        self.assertEqual(self.inspect()["status"], "stale_or_unconfirmed")
        self.assertEqual(before, (self.root / ref).read_bytes())

    def test_find_fix_recapture_review_preserves_failure_and_previous_refs(self):
        capture, viewed = self.captured_view("before")
        finding = {"id": "overlap", "summary": "Primary action overlaps adjacent text in target viewport"}
        failed = self.judgment("failed", capture, viewed, gate="FAIL", findings=[finding])
        original = (self.root / failed).read_bytes()
        self.assertEqual(self.inspect()["review"], "FAIL")
        with self.assertRaisesRegex(ValueError, "retain or explicitly resolve"):
            self.judgment("quiet-drop", capture, viewed)
        with self.assertRaisesRegex(ValueError, "new screenshot"):
            self.judgment("same-image", capture, viewed, resolved=["overlap"])
        (self.root / "source").write_text("scoped layout correction")
        revised, new_view = self.captured_view("fixed")
        self.assertEqual(self.inspect()["review"], "pending")
        with self.assertRaises(ValueError):
            self.judgment("wrong-view", revised, viewed, resolved=["overlap"])
        accepted = self.judgment("fixed-review", revised, new_view, resolved=["overlap"])
        proof = E.read_report(self.root, self.args[1], accepted)["payload"]
        self.assertEqual(proof["previous_review"], failed)
        self.assertEqual(self.inspect()["review"], "PASS")
        self.assertEqual(original, (self.root / failed).read_bytes())
        failed_report = json.loads(original)
        failed_report["payload"]["findings"][0]["summary"] = "tampered history"
        (self.root / failed).write_text(json.dumps(failed_report))
        self.assertEqual(self.inspect()["status"], "stale_or_unconfirmed")

    def test_latest_concerns_cannot_revive_prior_pass_or_ignore_unresolved_findings(self):
        capture, viewed = self.captured_view("one")
        self.judgment("old-pass", capture, viewed)
        finding = {"id": "spacing", "summary": "Actual target spacing is inconsistent with approved neighbor"}
        self.judgment("concerns", capture, viewed, gate="CONCERNS", findings=[finding])
        self.assertEqual(self.inspect()["review"], "CONCERNS")
        next_capture, next_view = self.captured_view("next")
        with self.assertRaises(ValueError):
            self.judgment("unresolved", next_capture, next_view)
        self.judgment("still-concerns", next_capture, next_view, gate="CONCERNS", findings=[finding])
        self.assertEqual(self.inspect()["review"], "CONCERNS")
        with self.assertRaises(ValueError):
            self.judgment("old-image", capture, viewed, gate="CONCERNS", findings=[finding])

    def test_independent_review_requires_actual_current_tester_and_their_view(self):
        self.requirement = V.declare(*self.args, "important", scope=["source"], **self.surface, review="independent")
        capture, self_view = self.captured_view("important")
        with self.assertRaisesRegex(ValueError, "independent"):
            self.judgment("self-is-not-independent", capture, self_view)
        with self.assertRaises(ValueError):
            V.view(*self.args, capture, "fake", viewed=True, reviewer_dispatch="not-a-dispatch")
        developer = self.developer_request("developer")
        D.accepted(*self.args, developer["id"], "developer", host_executor_id="host-dev")
        with self.assertRaises(ValueError):
            V.view(*self.args, capture, "developer-review", viewed=True, reviewer_dispatch=developer["id"])
        tester = D.begin(*self.args, "tester")
        with self.assertRaises(ValueError):
            V.view(*self.args, capture, "not-accepted", viewed=True, reviewer_dispatch=tester["id"])
        D.accepted(*self.args, tester["id"], "tester", host_executor_id="host-tester")
        with self.assertRaises(ValueError):
            self.judgment("wrong-observer", capture, self_view, reviewer_dispatch=tester["id"])
        tester_view = V.view(*self.args, capture, "tester-view", viewed=True, reviewer_dispatch=tester["id"])
        self.judgment("independent", capture, tester_view, reviewer_dispatch=tester["id"])
        self.assertEqual(self.inspect()["review"], "PASS")
        self.assertFalse(self.inspect()["native_proof"])

    def test_review_requires_current_untampered_view_and_truthful_findings(self):
        capture, viewed = self.captured_view("one")
        for gate, findings in (("PASS", [{"id": "actual", "summary": "Still unresolved"}]),
                               ("FAIL", []), ("score-100", [])):
            with self.assertRaises(ValueError):
                self.judgment("bad-judgment", capture, viewed, gate=gate, findings=findings)
        self.judgment("valid", capture, viewed)
        path = self.root / viewed
        report = json.loads(path.read_text())
        report["payload"]["viewed"] = False
        path.write_text(json.dumps(report))
        self.assertEqual(self.inspect()["status"], "stale_or_unconfirmed")
        T.start(*self.args, "next", "new UI task", ["observe"])
        with self.assertRaises(ValueError):
            self.judgment("old-task", capture, viewed)

    def test_old_task_developer_can_review_new_task_but_current_implementer_cannot(self):
        T.start(*self.args, "old", "old UI", ["observe"])
        old = self.developer_request("old-dev")
        D.accepted(*self.args, old["id"], "old-dev", host_executor_id="host-reused")
        D.complete(*self.args, old["id"], executors_stopped=True, stop_evidence="fixture old task stopped")
        T.end(*self.args, "old")
        T.start(*self.args, "current", "current UI", ["observe"])
        self.requirement = V.declare(*self.args, "current", scope=["source"], **self.surface, review="independent")
        capture, _ = self.captured_view("current")
        tester = D.begin(*self.args, "tester")
        D.accepted(*self.args, tester["id"], "current-tester", host_executor_id="host-reused")
        viewed = V.view(*self.args, capture, "current-tester-view", viewed=True, reviewer_dispatch=tester["id"])
        self.judgment("current-review", capture, viewed, reviewer_dispatch=tester["id"])
        developer = self.developer_request("current-dev")
        D.accepted(*self.args, developer["id"], "current-dev", host_executor_id="host-reused")
        with self.assertRaisesRegex(ValueError, "implementation executor"):
            V.view(*self.args, capture, "same-host", viewed=True, reviewer_dispatch=tester["id"])
        # Executor identity alone is also disallowed when a host mapping is unknown.
        state = R.status(self.root)["state"]
        state["role_dispatches"][-1].pop("host_executor_id")
        state["role_dispatches"][-1]["executor_id"] = "current-tester"
        R._atomic(self.root / "tmp/pipeline/runs" / (self.args[1] + ".json"), state)
        with self.assertRaisesRegex(ValueError, "implementation executor"):
            V.view(*self.args, capture, "same-executor", viewed=True, reviewer_dispatch=tester["id"])

    def test_managed_ui_requires_capture_view_and_review_before_qa_and_finish(self):
        self.assertEqual(self.formal_qa("missing")["gate"], "CONCERNS")
        with self.assertRaises(ValueError):
            self.completed()
        self.image(self.begin(), jpeg())
        capture = self.finish()
        self.assertEqual(self.formal_qa("no-view")["gate"], "CONCERNS")
        viewed = V.view(*self.args, capture, "view", viewed=True)
        self.assertEqual(self.formal_qa("no-review")["gate"], "CONCERNS")
        self.judgment("actual-review", capture, viewed)
        qa = self.formal_qa("accepted")
        self.assertEqual(qa["gate"], "PASS")
        self.assertTrue(qa["visual"]["complete"])
        final = self.completed()
        self.assertTrue(final["delivery_status"]["sealed"])
        self.assertEqual(final["delivery_status"]["visual"][0]["target"], "checkout")

    def test_finish_revalidates_source_image_view_review_and_current_failure(self):
        capture, viewed = self.captured_view("ready")
        review = self.judgment("ready-review", capture, viewed)
        self.assertEqual(self.formal_qa()["gate"], "PASS")
        payload = E.read_report(self.root, self.args[1], capture)["payload"]
        for path in (self.root / "source", self.root / payload["image_path"], self.root / viewed, self.root / review):
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                self.completed()
            path.write_bytes(original)
        finding = {"id": "actual-problem", "summary": "Actual target alignment still needs correction"}
        self.judgment("later-failure", capture, viewed, gate="FAIL", findings=[finding])
        with self.assertRaises(ValueError):
            self.completed()
        self.assertEqual(self.formal_qa("failed-qa")["gate"], "CONCERNS")

    def test_renaming_cannot_drop_failed_requirement_explicit_update_retains_findings(self):
        old = self.requirement
        capture, viewed = self.captured_view("before")
        finding = {"id": "overlap", "summary": "Target action overlaps the heading"}
        self.judgment("failed", capture, viewed, gate="CONCERNS", findings=[finding])
        renamed = V.declare(*self.args, "renamed", scope=["source"], **self.surface)
        self.requirement = renamed
        captured, viewed = self.captured_view("renamed")
        self.judgment("renamed-pass", captured, viewed)
        self.assertEqual(self.formal_qa("not-erased")["gate"], "CONCERNS")
        self.surface = {**self.surface, "version": "v2", "state": "updated", "viewport": {"width": 720, "height": 800}}
        self.requirement = V.declare(*self.args, "updated", scope=["source"], **self.surface, supersedes=old)
        with self.assertRaises(ValueError):
            V.declare(*self.args, "branch", scope=["source"], **self.surface, supersedes=old)
        (self.root / "source").write_text("scoped correction")
        corrected, new_view = self.captured_view("corrected")
        with self.assertRaises(ValueError):
            self.judgment("drop-finding", corrected, new_view)
        self.judgment("fixed", corrected, new_view, resolved=["overlap"])
        # The separate renamed requirement also needs a current capture after the edit.
        self.requirement, original_surface = renamed, self.surface
        self.surface = {**self.surface, "version": "v1.2.0", "state": "ready", "viewport": {"width": 1280, "height": 720}}
        renamed_capture, renamed_view = self.captured_view("renamed-current")
        self.judgment("renamed-current-pass", renamed_capture, renamed_view)
        self.assertEqual(self.formal_qa("updated-qa")["gate"], "PASS")
        self.assertTrue(self.completed()["delivery_status"]["sealed"])

    def test_prior_unresolved_task_cannot_be_dropped_but_passed_ui_does_not_pollute_non_ui(self):
        capture, viewed = self.captured_view("old")
        finding = {"id": "contrast", "summary": "Actual target contrast remains insufficient"}
        self.judgment("old-fail", capture, viewed, gate="FAIL", findings=[finding])
        old = self.requirement
        T.start(*self.args, "next", "next task", ["observe"])
        qa = self.formal_qa("pending-prior")
        self.assertEqual(qa["gate"], "CONCERNS")
        self.assertEqual(qa["visual"]["requirements"][0]["scope"], "unresolved_prior_task")
        self.requirement = V.declare(*self.args, "current", scope=["source"], **self.surface, supersedes=old)
        capture, viewed = self.captured_view("current")
        self.judgment("current-pass", capture, viewed, resolved=["contrast"])
        self.assertEqual(self.formal_qa("current-qa")["gate"], "PASS")
        T.end(*self.args, "next")
        T.start(*self.args, "non-ui", "unrelated non UI task", ["unit"])
        (self.root / "source").write_text("unrelated task input")
        qa = self.formal_qa("non-ui")
        self.assertEqual(qa["gate"], "PASS")
        self.assertNotIn("visual", qa)

    def test_explicit_delivery_keeps_unchanged_visual_chain_and_changed_source_pending(self):
        capture, viewed = self.captured_view("original")
        self.judgment("original-pass", capture, viewed)
        self.formal_qa("source-qa")
        self.completed()
        original_requirement = self.requirement
        adopted = self.adopted()
        self.assertTrue(adopted["delivery_status"]["adopted"])
        self.assertTrue(adopted["inherited_visual"])
        qa = self.formal_qa("commit-check", reuse=True)
        self.assertEqual(qa["gate"], "PASS")
        self.assertEqual(qa["visual"]["requirements"][0]["scope"], "inherited")
        self.assertTrue(self.completed()["delivery_status"]["sealed"])
        self.adopted()
        (self.root / "source").write_text("changed source candidate")
        self.assertEqual(self.formal_qa("changed-source")["gate"], "CONCERNS")
        with self.assertRaises(ValueError):
            self.completed()
        self.surface = {**self.surface, "version": "v2"}
        self.requirement = V.declare(*self.args, "replacement", scope=["source"], **self.surface, supersedes=original_requirement)
        capture, viewed = self.captured_view("new-candidate")
        self.judgment("new-candidate-review", capture, viewed)
        self.assertEqual(self.formal_qa("rechecked")["gate"], "PASS")
        self.assertTrue(self.completed()["delivery_status"]["sealed"])

    def test_delivery_missing_image_and_invalid_source_approval_do_not_erase_ui_gap(self):
        capture, viewed = self.captured_view("original")
        self.judgment("original-pass", capture, viewed)
        self.formal_qa("source-qa")
        final = self.completed()
        payload = E.read_report(self.root, self.args[1], capture)["payload"]
        (self.root / payload["image_path"]).unlink()
        adopted = self.adopted()
        self.assertTrue(adopted["delivery_status"]["adopted"])
        self.assertEqual(self.formal_qa("missing-image")["gate"], "CONCERNS")
        R.finish(*self.args, "aborted", executors_stopped=True, stop_evidence="fixture stopped")
        # Explicit immutable source ref still identifies UI obligations even on failed adoption.
        reference = final["delivery_status"]["reference"]
        import delivery
        packet = delivery._read_reference(self.root, reference, "delivery")
        approval_path = self.root / packet["approvals"][0]["path"]
        approval_path.write_bytes(approval_path.read_bytes() + b" ")
        state = R.start(self.root, {"delivery_from": reference}, managed=True)
        self.args = self.root, state["run_id"], state["owner_token"]
        Q.acknowledge(*self.args, Q.snapshot(self.root)["revision"])
        self.assertFalse(state["delivery_status"]["adopted"])
        self.assertTrue(state["inherited_visual"])
        self.assertEqual(self.formal_qa("invalid-source")["gate"], "CONCERNS")

    def test_plan_qa_and_existing_release_mode_both_preserve_visual_concerns(self):
        import release_checks as F
        F.prepare(*self.args)
        for phase in ("preflight", "compatibility", "build", "activate", "smoke"):
            source = "from pathlib import Path; Path('artifact').write_text('candidate')" if phase == "build" else f"print({phase!r})"
            options = {"artifact_paths": ["artifact"]} if phase == "build" else {}
            if phase == "smoke":
                options["coverage"] = ["release_identity"]
            F.execute(*self.args, phase, phase=phase, argv=[sys.executable, "-B", "-c", source], scope=["source"], **options)
        concerns = F.closeout(*self.args)
        self.assertEqual(concerns["gate"], "CONCERNS")
        with self.assertRaises(ValueError):
            self.completed()
        capture, viewed = self.captured_view("candidate")
        self.judgment("candidate-review", capture, viewed)
        accepted = F.closeout(*self.args)
        payload = E.read_report(self.root, self.args[1], accepted["report"])["payload"]
        self.assertEqual(payload["machine_validation"]["mode"], "release")
        self.assertEqual(payload["gate"], "PASS")
        self.assertTrue(payload["visual"]["complete"])
        self.assertTrue(self.completed()["delivery_status"]["sealed"])

    def test_plan_cannot_override_missing_ui_or_latest_visual_failure(self):
        plan = P.save_plan(*self.args, "plan", {"unit": {"argv": [sys.executable, "-B", "-c", "print('unit')"],
                           "scope": ["source"], "coverage": ["unit"], "write_paths": []}})
        P.run_planned(*self.args, plan, "unit", "planned")
        qa = P.qa_from_plan(*self.args, plan, "missing-ui")
        self.assertEqual(E.read_report(self.root, self.args[1], qa)["payload"]["gate"], "CONCERNS")
        capture, viewed = self.captured_view("current")
        self.judgment("actual-failure", capture, viewed, gate="FAIL", findings=[{"id": "actual", "summary": "Observed target issue remains"}])
        qa = P.qa_from_plan(*self.args, plan, "failed-ui")
        self.assertEqual(E.read_report(self.root, self.args[1], qa)["payload"]["gate"], "CONCERNS")
        with self.assertRaises(ValueError):
            self.completed()

    def test_changed_delivery_contract_keeps_pending_and_allows_fresh_explicit_replacement(self):
        capture, viewed = self.captured_view("original")
        self.judgment("original-pass", capture, viewed)
        self.formal_qa("source-qa")
        self.completed()
        old = self.requirement
        (self.root / "PIPELINE.md").write_text("new current contract; old PASS is not current acceptance")
        state = self.adopted()
        self.assertFalse(state["delivery_status"]["adopted"])
        self.assertTrue(state["inherited_visual"])
        self.assertEqual(self.formal_qa("new-context")["gate"], "CONCERNS")
        with self.assertRaises(ValueError):
            self.completed()
        self.surface = {**self.surface, "version": "v2"}
        self.requirement = V.declare(*self.args, "new-context", scope=["source"], **self.surface, supersedes=old)
        capture, viewed = self.captured_view("new-context-capture")
        self.judgment("new-context-review", capture, viewed)
        self.assertEqual(self.formal_qa("new-context-accepted")["gate"], "PASS")
        self.assertTrue(self.completed()["delivery_status"]["sealed"])

    def test_known_release_candidate_change_requires_current_visual_capture(self):
        capture, viewed = self.captured_view("original")
        self.judgment("original-pass", capture, viewed)
        state = R.status(self.root)["state"]
        state["release_plan"] = {"candidate": {"id": "changed-candidate"}}
        self.assertFalse(V.judgment(self.root, state)["complete"])


if __name__ == "__main__":
    unittest.main()
