"""Explicit dependency metadata identities use real isolated JSON files."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("dependency_fingerprints", Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/input_fingerprint.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class DependencyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.package = {"name": "fixture", "version": "1.0.0", "scripts": {"test": "node test.js"},
                        "engines": {"node": ">=20"}, "dependencies": {"dep": "^2.0.0"},
                        "devDependencies": {"tool": "^1"}, "config": {"strict": True}}
        self.lock = {"name": "fixture", "version": "1.0.0", "lockfileVersion": 3,
                     "packages": {"": {"name": "fixture", "version": "1.0.0", "dependencies": {"dep": "^2.0.0"}},
                                  "node_modules/dep": {"version": "2.0.1", "resolved": "https://example.invalid/dep.tgz", "integrity": "fixture-hash"}},
                     "dependencies": {"dep": {"version": "2.0.1", "dependencies": {"transitive": {"version": "3.0.0"}}}}}
        self.paths = ["package.json", "package-lock.json"]
        self.write()

    def write(self, package=None, lock=None):
        (self.root / "package.json").write_text(json.dumps(self.package if package is None else package))
        (self.root / "package-lock.json").write_text(json.dumps(self.lock if lock is None else lock))

    def identity(self, paths=None):
        return M.dependency_identity(self.root, self.paths if paths is None else paths)

    def test_only_three_root_version_positions_are_ignored(self):
        expected = self.identity()
        for location in ("package", "lock", "root-entry", "all"):
            package, lock = deepcopy(self.package), deepcopy(self.lock)
            if location in {"package", "all"}:
                package["version"] = "9.0.0"
            if location in {"lock", "all"}:
                lock["version"] = "9.0.0"
            if location in {"root-entry", "all"}:
                lock["packages"][""]["version"] = "9.0.0"
            self.write(package, lock)
            self.assertEqual(self.identity(), expected)
        package, lock = deepcopy(self.package), deepcopy(self.lock)
        package.pop("version")
        lock.pop("version")
        lock["packages"][""].pop("version")
        self.write(package, lock)
        self.assertEqual(self.identity(), expected)

    def test_dependency_scripts_engines_lock_format_and_nested_versions_all_participate(self):
        expected = self.identity()
        mutations = [
            ("package", ("dependencies", "dep"), "^3.0.0"),
            ("package", ("devDependencies", "tool"), "^2"),
            ("package", ("scripts", "test"), "node other-test.js"),
            ("package", ("engines", "node"), ">=22"),
            ("package", ("config", "strict"), False),
            ("package", ("name",), "changed-name"),
            ("lock", ("lockfileVersion",), 2),
            ("lock", ("packages", "", "dependencies", "dep"), "^4"),
            ("lock", ("packages", "node_modules/dep", "version"), "2.0.2"),
            ("lock", ("packages", "node_modules/dep", "resolved"), "https://example.invalid/new.tgz"),
            ("lock", ("packages", "node_modules/dep", "integrity"), "different-hash"),
            ("lock", ("dependencies", "dep", "dependencies", "transitive", "version"), "4.0.0"),
        ]
        for document, parts, value in mutations:
            package, lock = deepcopy(self.package), deepcopy(self.lock)
            target = package if document == "package" else lock
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            self.write(package, lock)
            with self.subTest(document=document, parts=parts):
                self.assertNotEqual(self.identity(), expected)
        package = deepcopy(self.package)
        package["custom"] = {"version": "meaningful-nested-version"}
        self.write(package)
        first = self.identity()
        package["custom"]["version"] = "other-nested-version"
        self.write(package)
        self.assertNotEqual(self.identity(), first)

    def test_format_order_and_explicit_scope_have_deterministic_identity(self):
        expected = self.identity()
        (self.root / "package.json").write_text(json.dumps(self.package, sort_keys=True, indent=4))
        (self.root / "package-lock.json").write_text(json.dumps(self.lock, sort_keys=True, indent=2))
        self.assertEqual(self.identity(list(reversed(self.paths))), expected)
        self.assertNotEqual(self.identity(["package.json"]), expected)
        self.assertEqual(self.identity(["package.json", "package.json"]), self.identity(["package.json"]))
        child = self.root / "workspace"
        child.mkdir()
        (child / "package.json").write_text(json.dumps(self.package))
        self.assertIsInstance(self.identity(["workspace/package.json"]), str)

    def test_regular_fingerprint_still_detects_version_changes(self):
        before = M.fingerprint(self.root, self.paths, {"runtime": "fixture"})
        identity = self.identity()
        package = dict(self.package, version="2.0.0")
        self.write(package)
        self.assertEqual(self.identity(), identity)
        self.assertNotEqual(M.fingerprint(self.root, self.paths, {"runtime": "fixture"}), before)

    def test_invalid_json_duplicates_constants_and_root_shapes_are_rejected(self):
        for content in ("{broken", "[]", "null", '"text"', '{"version":"1", "version":"2"}',
                        '{"config":{"duplicate":1,"duplicate":2}}', '{"config":NaN}', '{"config":Infinity}', '{"config":1e999}'):
            with self.subTest(content=content):
                (self.root / "package.json").write_text(content)
                with self.assertRaises(ValueError):
                    self.identity()
        self.write()
        for content in ("{broken", "[]", "null", '{"packages":{"":{},"":{}}}', '{"packages":{"":null}}'):
            (self.root / "package-lock.json").write_text(content)
            with self.assertRaises(ValueError):
                self.identity()

    def test_malformed_known_dependency_metadata_is_rejected_before_normalizing(self):
        for changes in ({"version": 1}, {"version": ""}, {"dependencies": []}, {"dependencies": {"dep": 3}},
                        {"scripts": None}, {"engines": {"node": True}}):
            with self.subTest(changes=changes):
                self.write(dict(self.package, **changes))
                with self.assertRaises(ValueError):
                    self.identity()
        for changes in ({"version": None}, {"lockfileVersion": True}, {"lockfileVersion": 0},
                        {"packages": []}, {"packages": {"": "bad"}},
                        {"packages": {"": {"version": 1}}},
                        {"packages": {"node_modules/dep": {"version": None}}},
                        {"dependencies": {"dep": "not-a-record"}},
                        {"dependencies": {"dep": {"dependencies": []}}}):
            with self.subTest(changes=changes):
                self.write(lock=dict(self.lock, **changes))
                with self.assertRaises(ValueError):
                    self.identity()

    def test_paths_unknown_files_missing_files_and_symlinks_are_rejected(self):
        for paths in (None, [], "package.json", ["README.md"], ["."], ["../package.json"], [str(self.root / "package.json")], [None]):
            with self.subTest(paths=paths), self.assertRaises((ValueError, OSError)):
                M.dependency_identity(self.root, paths)
        (self.root / "package-lock.json").unlink()
        with self.assertRaises(OSError):
            self.identity()
        (self.root / "package-lock.json").mkdir()
        with self.assertRaises(ValueError):
            self.identity()
        (self.root / "package-lock.json").rmdir()
        (self.root / "package-lock.json").symlink_to(self.root / "package.json")
        with self.assertRaises(ValueError):
            self.identity()
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.identity(["alias/package.json"])

    def test_read_mutations_and_earlier_input_changed_during_later_read_are_rejected(self):
        original_read = Path.read_bytes
        package_path, lock_path = (self.root / name for name in self.paths)
        for change in ("content", "mode", "earlier-input"):
            self.write()
            package_path.chmod(0o644)
            def unstable_read(path):
                value = original_read(path)
                if change == "content" and path == package_path:
                    path.write_text(json.dumps(dict(self.package, version="7.0.0")))
                elif change == "mode" and path == package_path:
                    path.chmod(0o700)
                elif change == "earlier-input" and path == lock_path:
                    package_path.write_text(json.dumps(dict(self.package, version="8.0.0")))
                return value
            with self.subTest(change=change), patch.object(Path, "read_bytes", unstable_read):
                with self.assertRaisesRegex(ValueError, "changed during dependency identity"):
                    self.identity()

    def test_dependency_identity_is_read_only(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.iterdir()}
        self.identity()
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.iterdir()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
