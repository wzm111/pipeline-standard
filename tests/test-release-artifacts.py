"""Portable release layers tested with isolated local trees only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/release_artifacts.py"
SPEC = importlib.util.spec_from_file_location("release_artifacts", SCRIPT)
ARTIFACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARTIFACTS)


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tree = self.root / "upload"
        self.tree.mkdir()
        (self.tree / "empty").mkdir()
        self.file = self.tree / "app.py"
        self.file.write_bytes(b"source data\n")
        self.file.chmod(0o644)
        self.identity = {"build_id": "fixture-commit-abc", "platform": "source-portable"}

    def capture(self, base="upload", layer="source", identity=None):
        return ARTIFACTS.capture(self.root, base, layer=layer, identity=self.identity if identity is None else identity)

    def test_round_trip_and_relocated_identical_source(self):
        original = self.capture()
        restored = ARTIFACTS.loads_manifest(json.dumps(original))
        (self.root / "moved").mkdir()
        (self.root / "moved/empty").mkdir()
        (self.root / "moved/app.py").write_bytes(self.file.read_bytes())
        (self.root / "moved/app.py").chmod(0o644)
        result = ARTIFACTS.compare(restored, self.capture("moved"))
        self.assertEqual(result, {"complete": True, "added": [], "missing": [], "changed": []})
        self.assertEqual(original["entries"]["empty"], {"kind": "directory"})

    def test_all_differences_including_empty_directories(self):
        original = self.capture()
        self.file.write_bytes(b"changed")
        (self.tree / "empty").rmdir()
        (self.tree / "new-empty").mkdir()
        (self.tree / "generated.png").write_bytes(b"fixture image")
        result = ARTIFACTS.compare(original, self.capture())
        self.assertFalse(result["complete"])
        self.assertEqual(result["changed"], ["app.py"])
        self.assertEqual(result["missing"], ["empty"])
        self.assertEqual(result["added"], ["generated.png", "new-empty"])
        self.file.unlink()
        self.assertIn("app.py", ARTIFACTS.compare(original, self.capture())["missing"])

    def test_executable_permission_changes_and_file_directory_replacement(self):
        original = self.capture()
        self.file.chmod(0o744)
        self.assertEqual(ARTIFACTS.compare(original, self.capture())["changed"], ["app.py"])
        self.file.unlink()
        self.file.mkdir()
        self.assertEqual(ARTIFACTS.compare(original, self.capture())["changed"], ["app.py"])

    def test_source_and_platform_artifacts_cannot_impersonate_each_other(self):
        source = self.capture()
        for folder, data in (("mac", b"mac-binary"), ("linux", b"elf-binary")):
            (self.root / folder).mkdir()
            (self.root / folder / "app").write_bytes(data)
            (self.root / folder / "app").chmod(0o755)
        mac_id = {"build_id": "build-xyz", "platform": "darwin-arm64"}
        linux_id = {"build_id": "build-xyz", "platform": "linux-x86_64"}
        mac = self.capture("mac", "artifact", mac_id)
        linux = self.capture("linux", "artifact", linux_id)
        for base, manifest, identity in (("mac", mac, mac_id), ("linux", linux, linux_id)):
            self.assertTrue(ARTIFACTS.compare(manifest, self.capture(base, "artifact", identity))["complete"])
        for left, right in ((source, mac), (mac, linux),
                            (source, self.capture(layer="artifact")),
                            (linux, self.capture("linux", "artifact", dict(linux_id, build_id="build-other")))):
            with self.assertRaises(ValueError):
                ARTIFACTS.compare(left, right)

    def test_unsafe_paths_symlinks_and_special_files_rejected(self):
        (self.root / "alias").symlink_to(self.tree, target_is_directory=True)
        for base in ("../upload", str(self.tree), "upload/../upload", "alias", "alias/empty", "", "upload//empty"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.capture(base)
        link = self.tree / "link"
        link.symlink_to(self.file)
        with self.assertRaises(ValueError):
            self.capture()
        link.unlink()
        os.mkfifo(self.tree / "fifo")
        with self.assertRaises(ValueError):
            self.capture()

    def test_invalid_imported_manifests_fail_closed(self):
        original = self.capture()
        invalid = []
        for key, value in (("format", "unknown"), ("layer", "combined"), ("identity", {}), ("entries", [])):
            invalid.append(dict(original, **{key: value}))
        for path, entry in (("../escape", {"kind": "directory"}),
                            ("/absolute", {"kind": "directory"}),
                            ("missing-parent/file", original["entries"]["app.py"]),
                            ("app.py", {"kind": "file", "sha256": "bad", "executable": 0}),
                            ("app.py", {"kind": "file", "sha256": "0" * 64, "executable": True}),
                            ("app.py", {"kind": "symlink"})):
            candidate = copy.deepcopy(original)
            candidate["entries"][path] = entry
            invalid.append(candidate)
        invalid.append(dict(original, extra="unexpected"))
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                ARTIFACTS.compare(original, candidate)
        for text in ('{"format":1,"format":2}', '{"x":NaN}', '[]', '{}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ARTIFACTS.loads_manifest(text)

    def test_racing_file_write_and_symlink_replacement_rejected(self):
        original_read = os.read
        for action in ("write", "symlink"):
            if self.file.is_symlink():
                self.file.unlink()
            self.file.write_bytes(b"stable")
            mutated = False

            def racing_read(fd, size):
                nonlocal mutated
                data = original_read(fd, size)
                if data and not mutated:
                    mutated = True
                    if action == "write":
                        self.file.write_bytes(b"changed concurrently")
                    else:
                        self.file.unlink()
                        self.file.symlink_to(self.root / "outside")
                return data

            with self.subTest(action=action), patch.object(os, "read", racing_read):
                with self.assertRaises(ValueError):
                    self.capture()

    def test_already_read_file_change_detected_by_second_scan(self):
        (self.tree / "z.txt").write_bytes(b"last file")
        original_read = os.read
        mutated = False

        def racing_read(fd, size):
            nonlocal mutated
            data = original_read(fd, size)
            if data == b"last file" and not mutated:
                mutated = True
                self.file.write_bytes(b"changed after its first visit")
            return data

        with patch.object(os, "read", racing_read), self.assertRaises(ValueError):
            self.capture()


if __name__ == "__main__":
    unittest.main()
