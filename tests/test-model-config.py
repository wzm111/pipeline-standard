"""Resolver tests run entirely in temporary directories, without model calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import sys

SPEC = importlib.util.spec_from_file_location("models", Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/resolve-models.py")
MODELS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODELS)
CORE = sys.modules["model_config"]


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.personal = self.root / "personal.json"
        self.project = self.root / "pipeline.models.json"

    def save(self, path, runtimes):
        path.write_text(json.dumps({"schema_version": 1, "runtimes": runtimes}), encoding="utf-8")

    def test_missing_preserves_runtime_default(self):
        self.assertEqual(MODELS.resolve("codex", self.personal, self.project)["roles"], {})
        self.assertFalse(self.project.exists())

    def test_project_replaces_entire_role_without_stale_effort(self):
        self.save(self.personal, {"codex": {"developer": {"model": "old", "effort": "high"}, "tester": {"model": "test"}}})
        self.save(self.project, {"codex": {"developer": {"model": "new"}}})
        result = MODELS.resolve("codex", self.personal, self.project)["roles"]
        self.assertEqual(result["developer"]["preference"], {"model": "new"})
        self.assertEqual(result["tester"]["source"], "personal")

    def test_inherit_masks_personal_and_providers_are_isolated(self):
        self.save(self.personal, {"codex": {"tester": {"model": "custom"}}})
        self.save(self.project, {"codex": {"tester": {"model": "inherit"}}, "claude": {"tester": {"model": "claude-choice"}}})
        self.assertEqual(MODELS.resolve("codex", self.personal, self.project)["roles"]["tester"]["preference"]["model"], "inherit")
        self.assertEqual(MODELS.resolve("claude", self.personal, self.project)["roles"]["tester"]["preference"]["model"], "claude-choice")

    def test_invalid_config_is_not_overwritten(self):
        for content in ('{broken', '{"schema_version": 2}', '{"schema_version": true}'):
            self.project.write_text(content)
            with self.assertRaises(ValueError):
                MODELS.resolve("codex", self.personal, self.project)
            self.assertEqual(self.project.read_text(), content)

    def test_invalid_role_settings(self):
        for config in ({"model": ""}, {"model": "inherit", "effort": "high"}, {"model": "m", "fallback": "silent"}, {"model": "m", "effort": None}):
            self.save(self.project, {"codex": {"developer": config}})
            with self.assertRaises(ValueError):
                MODELS.resolve("codex", self.personal, self.project)

    def test_schema_validates_both_runtimes_and_preserves_extensions(self):
        data = {"schema_version": 1, "extra": {"x": 7}, "runtimes": {
            "future": {"opaque": True}, "codex": {"developer": {"model": "a", "vendor": 3}},
            "claude": {"tester": {"model": "c"}}}}
        self.project.write_text(json.dumps(data))
        original, revision = CORE.read_document(self.project)
        updated = CORE.prepare_update(original, "codex", "developer", {"model": "b"})
        CORE.save_project(self.root, updated, revision)
        saved = json.loads(self.project.read_text())
        self.assertEqual(saved["extra"], data["extra"])
        self.assertEqual(saved["runtimes"]["future"], data["runtimes"]["future"])
        self.assertEqual(saved["runtimes"]["claude"], data["runtimes"]["claude"])
        self.assertEqual(saved["runtimes"]["codex"]["developer"]["vendor"], 3)
        self.assertEqual(original, data)
        saved["runtimes"]["claude"]["tester"] = {"model": "inherit", "effort": "high"}
        with self.assertRaises(ValueError):
            CORE.validate_document(saved)

    def test_temporary_overrides_do_not_write(self):
        self.save(self.personal, {"codex": {"tester": {"model": "personal"}}})
        self.save(self.project, {"codex": {"tester": {"model": "project"}}})
        before = self.project.read_bytes()
        result = CORE.resolve("codex", self.personal, self.project, {"tester": {"model": "inherit"}})
        self.assertEqual(result["roles"]["tester"]["source"], "temporary")
        self.assertEqual(self.project.read_bytes(), before)
        with self.assertRaises(ValueError):
            CORE.resolve("codex", self.personal, self.project, {"not-a-role": {"model": "x"}})

    def test_inherit_and_delete_have_different_effects(self):
        self.save(self.personal, {"codex": {"tester": {"model": "personal"}}})
        document, revision = CORE.read_document(self.project)
        inherited = CORE.prepare_update(document, "codex", "tester", {"model": "inherit"})
        revision = CORE.save_project(self.root, inherited, revision)
        self.assertEqual(CORE.resolve("codex", self.personal, self.project)["roles"]["tester"]["preference"]["model"], "inherit")
        removed = CORE.prepare_update(inherited, "codex", "tester", remove=True)
        CORE.save_project(self.root, removed, revision)
        self.assertEqual(CORE.resolve("codex", self.personal, self.project)["roles"]["tester"]["preference"]["model"], "personal")

    def test_effort_only_and_model_change(self):
        document = {"schema_version": 1, "runtimes": {}}
        updated = CORE.prepare_update(document, "codex", "tester", {"effort": "high"}, effective={"model": "a", "effort": "low"})
        self.assertEqual(updated["runtimes"]["codex"]["tester"], {"model": "a", "effort": "high"})
        updated = CORE.prepare_update(updated, "codex", "tester", {"model": "b"})
        self.assertEqual(updated["runtimes"]["codex"]["tester"], {"model": "b"})
        with self.assertRaises(ValueError):
            CORE.prepare_update(document, "codex", "tester", {"effort": "high"})

    def test_save_refuses_corruption_unknown_version_and_symlinks(self):
        valid = {"schema_version": 1, "runtimes": {}}
        for content in ('{broken', '{"schema_version": 99}', '{"schema_version": true}'):
            self.project.write_text(content)
            with self.assertRaises(ValueError):
                CORE.save_project(self.root, valid, None)
            self.assertEqual(self.project.read_text(), content)
        self.project.unlink()
        self.personal.write_text(json.dumps(valid))
        self.project.symlink_to(self.personal)
        with self.assertRaises(ValueError):
            CORE.save_project(self.root, valid, None)
        self.assertTrue(self.project.is_symlink())
        self.assertEqual(json.loads(self.personal.read_text()), valid)

    def test_save_refuses_active_run_and_concurrent_writer(self):
        document, revision = CORE.read_document(self.project)
        active = self.root / "tmp/pipeline/.active"
        active.parent.mkdir(parents=True)
        active.write_text("run")
        with self.assertRaisesRegex(ValueError, "active"):
            CORE.save_project(self.root, document, revision)
        active.unlink()
        self.save(self.project, {"codex": {"tester": {"model": "concurrent"}}})
        with self.assertRaisesRegex(ValueError, "changed"):
            CORE.save_project(self.root, document, revision)
        self.assertEqual(json.loads(self.project.read_text())["runtimes"]["codex"]["tester"]["model"], "concurrent")
        lock = self.root / ".pipeline.models.lock"
        lock.write_text("busy")
        with self.assertRaises(FileExistsError):
            CORE.save_project(self.root, document, revision)
        self.assertEqual(lock.read_text(), "busy")

    def test_mid_save_edit_detected_and_temp_cleaned(self):
        document, revision = CORE.read_document(self.project)
        def external_edit(_fd):
            self.save(self.project, {"codex": {"tester": {"model": "external"}}})
        with patch.object(CORE.os, "fsync", side_effect=external_edit):
            with self.assertRaisesRegex(ValueError, "changed"):
                CORE.save_project(self.root, document, revision)
        self.assertEqual(CORE.read_layer(self.project, "codex")["tester"]["model"], "external")
        self.assertEqual(list(self.root.glob(".pipeline.models.*")), [])

    def test_atomic_replace_failure_leaves_original_and_cleans_temps(self):
        self.save(self.project, {"codex": {"tester": {"model": "old"}}})
        document, revision = CORE.read_document(self.project)
        before = self.project.read_bytes()
        updated = CORE.prepare_update(document, "codex", "tester", {"model": "new"})
        with patch.object(CORE.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                CORE.save_project(self.root, updated, revision)
        self.assertEqual(self.project.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".pipeline.models.*")), [])

    def test_post_replace_readback_failure_is_reported_without_blind_rollback(self):
        document, revision = CORE.read_document(self.project)
        updated = CORE.prepare_update(document, "codex", "tester", {"model": "new"})
        real_read = CORE.read_document
        calls = 0
        def unreadable_after_replace(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("readback denied")
            return real_read(path)
        with patch.object(CORE, "read_document", side_effect=unreadable_after_replace):
            with self.assertRaisesRegex(OSError, "readback denied"):
                CORE.save_project(self.root, updated, revision)
        self.assertEqual(real_read(self.project)[0], updated)
        self.assertEqual(list(self.root.glob(".pipeline.models.*")), [])

    def test_project_directory_alias_allowed_but_control_symlink_refused(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        document, revision = CORE.read_document(self.project)
        CORE.save_project(alias, document, revision)
        control = self.root / "tmp"
        control.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            CORE.save_project(alias, document, CORE.read_document(self.project)[1])

    def test_active_run_appearing_during_save_is_not_removed(self):
        document, revision = CORE.read_document(self.project)
        active = self.root / "tmp/pipeline/.active"
        def start_run(_fd):
            active.parent.mkdir(parents=True)
            active.write_text("new-run")
        with patch.object(CORE.os, "fsync", side_effect=start_run):
            with self.assertRaisesRegex(ValueError, "active"):
                CORE.save_project(self.root, document, revision)
        self.assertFalse(self.project.exists())
        self.assertEqual(active.read_text(), "new-run")
        self.assertEqual(list(self.root.glob(".pipeline.models.*")), [])

    def test_cli_read_resolve_and_errors(self):
        command = [sys.executable, str(Path(MODELS.__file__)), "--runtime", "codex", "--project", str(self.root), "--personal", str(self.personal)]
        result = subprocess.run(command + ["--temporary", '{"tester":{"model":"inherit"}}'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["roles"]["tester"]["source"], "temporary")
        self.assertFalse(self.project.exists())
        read = subprocess.run(command + ["--read"], capture_output=True, text=True)
        self.assertIsNone(json.loads(read.stdout)["revision"])
        for extra in (["--temporary", "null"], ["--temporary", "{"], ["--read", "--temporary", "{}"]):
            failed = subprocess.run(command + extra, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 1)


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.preference = {"model": "host-supplied-model", "effort": "medium"}
        self.host = {"agent_spawn": True, "model_override": True, "effort_override": True,
                     "models": {"host-supplied-model": {"permission": True, "efforts": ["medium"]}}}

    def test_four_states_and_no_visibility_as_permission(self):
        self.assertEqual(CORE.assess_capability(self.preference, self.host)["state"], "available_precheck")
        self.host["models"]["host-supplied-model"].pop("permission")
        unknown = CORE.assess_capability(self.preference, self.host)
        self.assertEqual(unknown["state"], "unverifiable")
        self.assertFalse(unknown["verified"])
        self.host["models"]["host-supplied-model"]["permission"] = False
        self.assertEqual(CORE.assess_capability(self.preference, self.host)["state"], "unavailable")
        self.host["model_override"] = False
        self.assertEqual(CORE.assess_capability(self.preference, self.host)["state"], "host_unsupported")

    def test_absent_list_model_and_effort(self):
        for host, expected in (({}, "unverifiable"), ({"agent_spawn": True, "model_override": True}, "unverifiable"),
                               ({"agent_spawn": True, "model_override": True, "models": {}, "models_complete": True}, "unavailable")):
            self.assertEqual(CORE.assess_capability(self.preference, host)["state"], expected)
        self.host["models"]["host-supplied-model"]["efforts"] = ["low"]
        self.assertEqual(CORE.assess_capability(self.preference, self.host)["state"], "unavailable")
        self.host["effort_override"] = False
        self.assertEqual(CORE.assess_capability(self.preference, self.host)["state"], "host_unsupported")

    def test_spawn_conversion_fork_and_explicit_fallback(self):
        plan = CORE.codex_spawn_parameters(self.preference, self.host)
        self.assertEqual(plan["parameters"], {"model": "host-supplied-model", "reasoning_effort": "medium", "fork_turns": "none"})
        self.assertFalse(plan["fallback"])
        self.assertEqual(CORE.codex_spawn_parameters({"model": "inherit"}, {"agent_spawn": True})["parameters"], {})
        with self.assertRaises(ValueError):
            CORE.codex_spawn_parameters(self.preference, {})
        fallback = CORE.codex_spawn_parameters(dict(self.preference, fallback="inherit"), {"agent_spawn": True})
        self.assertTrue(fallback["fallback"])
        self.assertEqual(fallback["parameters"], {})
        self.assertIsNotNone(fallback["fallback_reason"])

    def test_inherit_still_requires_agent_startup_support(self):
        inherited = {"model": "inherit"}
        self.assertEqual(CORE.assess_capability(inherited, {})["state"], "unverifiable")
        self.assertEqual(CORE.assess_capability(inherited, {"agent_spawn": False})["state"], "host_unsupported")
        with self.assertRaises(ValueError):
            CORE.codex_spawn_parameters(dict(self.preference, fallback="inherit"), {"agent_spawn": False})

    def test_actual_model_is_only_reported_evidence(self):
        plan = CORE.codex_spawn_parameters(self.preference, self.host)
        record = CORE.execution_record("codex", "tester", self.preference, plan)
        self.assertEqual(record["actual_model"], "unknown")
        self.assertEqual(record["actual_effort"], "unknown")
        record = CORE.execution_record("codex", "tester", self.preference, plan, {"actual_model": "reported-model"})
        self.assertEqual(record["actual_model"], "reported-model")
        self.assertEqual(record["requested_model"], self.preference["model"])

    def test_startup_failure_is_recorded_without_inventing_model_feedback(self):
        plan = CORE.codex_spawn_parameters(self.preference, self.host)
        for reason in ("network", "rate_limit", "native_binary_missing"):
            record = CORE.execution_record("codex", "tester", self.preference, plan,
                                           {"startup_status": "failed", "error": reason})
            self.assertEqual(record["startup_status"], "failed")
            self.assertEqual(record["error"], reason)
            self.assertEqual(record["actual_model"], "unknown")
            self.assertFalse(record["fallback"])

    def test_claude_frontmatter_mapping_and_explicit_inherit(self):
        host = dict(self.host, role_frontmatter=True, role_inherit=True)
        plan = CORE.claude_role_parameters(self.preference, host)
        self.assertEqual(plan["parameters"], {"model": "host-supplied-model", "effort": "medium"})
        inherited = CORE.claude_role_parameters({"model": "inherit"}, host)
        source_frontmatter = {"model": "old-fixed-model", "effort": "high", "description": "preserved"}
        for key, value in inherited["parameters"].items():
            if value is None:
                source_frontmatter.pop(key, None)
            else:
                source_frontmatter[key] = value
        self.assertEqual(source_frontmatter, {"model": "inherit", "description": "preserved"})
        no_effort = CORE.claude_role_parameters({"model": "host-supplied-model"}, host)
        self.assertIsNone(no_effort["parameters"]["effort"])

    def test_claude_unknown_permission_effort_and_host_support_refuse_application(self):
        host = dict(self.host, role_frontmatter=True, role_inherit=True)
        for changed in ({"role_frontmatter": None}, {"effort_override": False},
                        {"effort_override": None}, {"models": {"host-supplied-model": {"efforts": ["medium"]}}},
                        {"models": {"host-supplied-model": {"permission": True, "efforts": ["low"]}}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                CORE.claude_role_parameters(self.preference, dict(host, **changed))
        with self.assertRaises(ValueError):
            CORE.claude_role_parameters({"model": "inherit"}, dict(host, role_inherit=None))

    def test_claude_fallback_is_explicit_and_requires_working_inheritance(self):
        host = {"agent_spawn": True, "role_frontmatter": True, "role_inherit": True}
        with self.assertRaises(ValueError):
            CORE.claude_role_parameters(self.preference, host)
        allowed = dict(self.preference, fallback="inherit")
        plan = CORE.claude_role_parameters(allowed, host)
        self.assertTrue(plan["fallback"])
        self.assertEqual(plan["parameters"], {"model": "inherit", "effort": None})
        for changed in ({"agent_spawn": False}, {"role_inherit": False}):
            with self.assertRaises(ValueError):
                CORE.claude_role_parameters(allowed, dict(host, **changed))


if __name__ == "__main__":
    unittest.main()
