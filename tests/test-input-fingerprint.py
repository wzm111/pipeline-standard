"""Shared hashes exercised against isolated real files and mutation failures."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
SPEC = importlib.util.spec_from_file_location("fingerprint_metrics", SCRIPTS / "workflow_metrics.py")
METRICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METRICS)
sys.path.insert(0, str(SCRIPTS))
import run_evidence as EVIDENCE
import input_fingerprint as SHARED


class InputFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "src"
        self.source.mkdir()
        self.file = self.source / "a.py"
        self.file.write_text("answer = 42\n")
        self.file.chmod(0o644)
        self.environment = {"python": "fixture", "dependencies": "lock-v1"}

    def both(self, scope=None):
        scope = ["src"] if scope is None else scope
        return (METRICS.fingerprint(self.root, scope, self.environment), EVIDENCE.fingerprint(self.root, scope))

    def test_both_wrappers_use_single_shared_implementation(self):
        shared_path = str(SCRIPTS / "input_fingerprint.py")
        self.assertEqual(METRICS._input_fingerprint.__code__.co_filename, shared_path)
        self.assertEqual(EVIDENCE._input_fingerprint.__code__.co_filename, shared_path)
        metrics, evidence = self.both()
        self.assertEqual(metrics, SHARED.fingerprint(self.root, ["src"], self.environment))
        self.assertEqual(evidence, SHARED.fingerprint(self.root, ["src"], narrow=True))

    def test_established_digest_encodings_remain_compatible(self):
        content_hash = hashlib.sha256(self.file.read_bytes()).hexdigest()
        metrics_inputs = {"src": {"directory": True}, "src/a.py": {"sha256": content_hash, "mode": 0o644},
                          "missing.lock": {"missing": True}}
        evidence_inputs = {"src": "directory", "src/a.py": [content_hash, 0o644], "missing.lock": "missing"}
        expected_metrics = hashlib.sha256(json.dumps({"inputs": metrics_inputs, "environment": self.environment}, sort_keys=True).encode()).hexdigest()
        expected_evidence = hashlib.sha256(json.dumps(evidence_inputs, sort_keys=True).encode()).hexdigest()
        self.assertEqual(self.both(["src", "missing.lock"]), (expected_metrics, expected_evidence))

    def test_content_addition_deletion_mode_and_missing_appearance_invalidate_both(self):
        original = self.both(["src", "missing.lock"])
        self.file.write_text("answer = 7\n")
        self.assertTrue(all(a != b for a, b in zip(original, self.both(["src", "missing.lock"]))))
        self.file.write_text("answer = 42\n")
        self.assertEqual(original, self.both(["src", "missing.lock"]))
        added = self.source / "new.py"
        added.write_text("new import")
        self.assertTrue(all(a != b for a, b in zip(original, self.both(["src", "missing.lock"]))))
        added.unlink()
        self.assertEqual(original, self.both(["src", "missing.lock"]))
        self.file.chmod(0o700)
        self.assertTrue(all(a != b for a, b in zip(original, self.both(["src", "missing.lock"]))))
        self.file.chmod(0o644)
        self.assertEqual(original, self.both(["src", "missing.lock"]))
        self.file.unlink()
        self.assertTrue(all(a != b for a, b in zip(original, self.both(["src", "missing.lock"]))))
        self.file.write_text("answer = 42\n")
        self.file.chmod(0o644)
        (self.root / "missing.lock").write_text("now present")
        self.assertTrue(all(a != b for a, b in zip(original, self.both(["src", "missing.lock"]))))

    def test_environment_only_changes_workflow_digest_and_is_required(self):
        old = METRICS.fingerprint(self.root, ["src"], self.environment)
        self.assertNotEqual(old, METRICS.fingerprint(self.root, ["src"], dict(self.environment, dependencies="lock-v2")))
        self.assertNotEqual(METRICS.fingerprint(self.root, ["src"], "env-v1"), METRICS.fingerprint(self.root, ["src"], "env-v2"))
        for missing in (None, "", {}):
            with self.assertRaises(ValueError):
                METRICS.fingerprint(self.root, ["src"], missing)

    def test_scope_order_duplicates_and_nested_directory_content(self):
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "module.py").write_text("nested content")
        for fingerprint in (lambda scope: METRICS.fingerprint(self.root, scope, self.environment),
                            lambda scope: EVIDENCE.fingerprint(self.root, scope)):
            with self.subTest(fingerprint=fingerprint):
                self.assertEqual(fingerprint(["src", "absent"]), fingerprint(["absent", "src", "src/a.py"]))
                before = fingerprint(["src"])
                (nested / "module.py").write_text("changed nested content")
                self.assertNotEqual(before, fingerprint(["src"]))
                (nested / "module.py").write_text("nested content")

    def test_symlinks_traversal_and_absolute_paths_rejected_by_both(self):
        target = self.root / "outside.txt"
        target.write_text("private")
        (self.root / "alias").symlink_to(self.source, target_is_directory=True)
        for scope in (["../outside"], [str(target)], ["alias/a.py"], ["src/../../outside"]):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.both(scope)
        (self.source / "link").symlink_to(target)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must reject before reading")):
            for fingerprint in (lambda: METRICS.fingerprint(self.root, ["src/link"], self.environment),
                                lambda: EVIDENCE.fingerprint(self.root, ["src/link"])):
                with self.assertRaises(ValueError):
                    fingerprint()
        with self.assertRaises(ValueError):
            EVIDENCE.fingerprint(self.root, ["src"])
        with self.assertRaises(ValueError):
            METRICS.fingerprint(self.root, ["src"], self.environment)

    def test_run_evidence_root_scope_stays_rejected_and_metrics_root_remains_supported(self):
        self.assertIsInstance(METRICS.fingerprint(self.root, ["."], self.environment), str)
        for scope in (["."], [""], [], None, "src"):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                EVIDENCE.fingerprint(self.root, scope)
        with self.assertRaises(ValueError):
            METRICS.fingerprint(self.root, [], self.environment)

    def test_read_time_content_mode_and_directory_changes_are_rejected(self):
        original_read = Path.read_bytes
        for interface in ("metrics", "evidence"):
            for change in ("content", "mode", "directory"):
                self.file.write_text("answer = 42\n")
                self.file.chmod(0o644)
                added = self.source / "added-during-read.py"
                added.unlink(missing_ok=True)
                def changing_read(path):
                    value = original_read(path)
                    if path == self.file:
                        if change == "content":
                            path.write_text("mutated while reading")
                        elif change == "mode":
                            path.chmod(0o700)
                        else:
                            added.write_text("new child")
                    return value
                with self.subTest(interface=interface, change=change), patch.object(Path, "read_bytes", changing_read):
                    with self.assertRaisesRegex(ValueError, "changed during fingerprint"):
                        if interface == "metrics":
                            METRICS.fingerprint(self.root, ["src"], self.environment)
                        else:
                            EVIDENCE.fingerprint(self.root, ["src"])

    def test_importlib_metrics_loading_without_scripts_on_sys_path(self):
        code = ("import importlib.util; "
                f"s=importlib.util.spec_from_file_location('isolated_metrics', {str(SCRIPTS / 'workflow_metrics.py')!r}); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                f"print(m.fingerprint({str(self.root)!r}, ['src'], 'env'))")
        result = subprocess.run([sys.executable, "-B", "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), METRICS.fingerprint(self.root, ["src"], "env"))

    def test_fingerprinting_is_read_only(self):
        before = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        self.both()
        after = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_overlapping_scopes_read_each_file_once_and_validate_all_declarations(self):
        original = Path.read_bytes
        reads = []
        def counted(path):
            reads.append(path)
            return original(path)
        expected = self.both()
        for index, interface in enumerate((lambda scope: METRICS.fingerprint(self.root, scope, self.environment),
                                           lambda scope: EVIDENCE.fingerprint(self.root, scope))):
            for scope in (["src", "src/a.py", "src/.", "src"], ["src/a.py", "src"]):
                reads.clear()
                with patch.object(Path, "read_bytes", counted):
                    self.assertEqual(interface(scope), expected[index])
                self.assertEqual(reads, [self.file])
            with self.assertRaises(ValueError):
                interface(["src", "src/../src/a.py"])
            (self.source / "alias").symlink_to(self.file)
            with self.assertRaises(ValueError):
                interface(["src/a.py", "src/alias"])
            (self.source / "alias").unlink()

    def test_later_sibling_mutation_of_cached_or_missing_input_is_rejected(self):
        second = self.root / "second"
        second.write_text("second")
        original = Path.read_bytes
        for first in ("src/a.py", "missing"):
            def changing(path):
                value = original(path)
                if path == second:
                    (self.root / first).write_text("changed")
                return value
            with patch.object(Path, "read_bytes", changing), self.assertRaisesRegex(ValueError, "changed during fingerprint"):
                SHARED.fingerprint(self.root, [first, "second", first])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX fifo")
    def test_special_files_fail_without_attempting_blocking_read(self):
        os.mkfifo(self.source / "pipe")
        for fingerprint in (lambda: METRICS.fingerprint(self.root, ["src"], self.environment),
                            lambda: EVIDENCE.fingerprint(self.root, ["src"])):
            with self.assertRaises(ValueError):
                fingerprint()


if __name__ == "__main__":
    unittest.main()
