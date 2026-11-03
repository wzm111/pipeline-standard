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
import visual_evidence as V
import pipeline_cli as CLI


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
        cli("visual-view", "--receipt", "fixture", "--capture", captured, "--name", "cli-view", "--viewed")
        self.assertEqual(cli("visual-inspect", "--run-id", self.args[1], "--requirement", declaration)["status"], "view_declared")
        with self.assertRaises(ValueError):
            V.begin(*self.args, self.requirement, "v" + self.args[2])


if __name__ == "__main__":
    unittest.main()
