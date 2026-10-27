"""Task routing behavior without model/provider calls or user configuration writes."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("routing_models", Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/model_config.py")
MODELS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODELS)


class TaskRoutingTests(unittest.TestCase):
    def setUp(self):
        self.preference = {"model": "base", "effort": "high", "fallback": "inherit",
                           "task_routes": {"scoped_fix": {"model": "light", "effort": "medium"}}}

    def choose(self, preference=None, **context):
        return MODELS.select_task_route("developer", self.preference if preference is None else preference,
                                        **dict({"task_kind": "scoped_fix", "risk": "low", "complexity": "simple"}, **context))

    def test_low_simple_task_selects_independent_tuple_and_explains_selection(self):
        result = self.choose()
        self.assertEqual(result["preference"], {"model": "light", "effort": "medium"})
        self.assertEqual(result["routing"], {"task_kind": "scoped_fix", "risk": "low", "complexity": "simple",
                                             "applied": True, "reason": "matched", "base_model": "base"})
        self.assertNotIn("fallback", result["preference"])

    def test_all_role_task_pairs_and_cross_role_config_rejection(self):
        expected = {"routine_planning": {"planner"}, "routine_review": {"scope_guardian"},
                    "scoped_fix": {"developer"}, "ui_validation": {"tester"}, "release_summary": {"releaser"}}
        self.assertEqual(MODELS.TASK_KINDS, expected)
        for task_kind, roles in expected.items():
            role = next(iter(roles))
            config = {"model": "base", "task_routes": {task_kind: {"model": "light"}}}
            for other_role in MODELS.ROLES:
                document = {"schema_version": 1, "runtimes": {"codex": {other_role: config}}}
                if other_role == role:
                    MODELS.validate_document(document)
                    result = MODELS.select_task_route(role, config, task_kind=task_kind, risk="low", complexity="simple")
                    self.assertEqual(result["preference"], {"model": "light"})
                else:
                    with self.assertRaises(ValueError):
                        MODELS.validate_document(document)
                    with self.assertRaises(ValueError):
                        MODELS.select_task_route(other_role, {"model": "base"}, task_kind=task_kind, risk="low", complexity="simple")

    def test_unknown_medium_high_and_complex_tasks_keep_base(self):
        for risk in ("low", "medium", "high", "unknown"):
            for complexity in ("simple", "moderate", "complex", "unknown"):
                if risk == "low" and complexity == "simple":
                    continue
                with self.subTest(risk=risk, complexity=complexity):
                    result = self.choose(risk=risk, complexity=complexity)
                    self.assertEqual(result["preference"], {"model": "base", "effort": "high", "fallback": "inherit"})
                    self.assertFalse(result["routing"]["applied"])

    def test_missing_context_or_missing_route_keeps_base(self):
        result = MODELS.select_task_route("developer", self.preference)
        self.assertEqual(result["preference"]["model"], "base")
        self.assertEqual(result["routing"]["reason"], "task_kind_missing")
        result = self.choose({"model": "fixed", "effort": "high"})
        self.assertEqual(result["preference"], {"model": "fixed", "effort": "high"})
        self.assertEqual(result["routing"]["reason"], "route_not_configured")

    def test_no_input_mutation_or_output_aliasing(self):
        before = deepcopy(self.preference)
        selected = self.choose()
        selected["preference"]["model"] = "mutated-output"
        base = self.choose(risk="high")
        base["preference"]["effort"] = "mutated-base-output"
        self.assertEqual(self.preference, before)

    def test_old_config_and_unknown_extension_compatibility(self):
        preference = {"model": "fixed", "vendor": {"arbitrary": 7}}
        result = self.choose(preference)
        self.assertEqual(result["preference"], preference)
        result["preference"]["vendor"]["arbitrary"] = 9
        self.assertEqual(preference["vendor"]["arbitrary"], 7)

    def test_invalid_route_values_and_nested_routes_are_rejected(self):
        invalid = (None, [], "light", {"unknown_task": {"model": "light"}},
                   {"scoped_fix": "light"}, {"scoped_fix": {}}, {"scoped_fix": {"model": ""}},
                   {"scoped_fix": {"model": "light", "effort": None}},
                   {"scoped_fix": {"model": "light", "fallback": "automatic"}},
                   {"scoped_fix": {"model": "inherit", "effort": "medium"}},
                   {"scoped_fix": {"model": "light", "task_routes": {}}},
                   {"scoped_fix": {"model": "light", "risk": "low"}})
        for routes in invalid:
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                MODELS.validate_document({"schema_version": 1, "runtimes": {"codex": {
                    "developer": {"model": "base", "task_routes": routes}}}})

    def test_invalid_classifications_and_unknown_roles_are_rejected(self):
        for context in ({"risk": "small"}, {"risk": None}, {"risk": []}, {"complexity": "easy"},
                        {"complexity": True}, {"task_kind": "unknown"}, {"task_kind": []}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.choose(**context)
        with self.assertRaises(ValueError):
            MODELS.select_task_route("unknown-role", {"model": "fixed"})

    def test_route_without_effort_never_inherits_base_effort_or_fallback(self):
        self.preference["task_routes"]["scoped_fix"] = {"model": "light"}
        self.assertEqual(self.choose()["preference"], {"model": "light"})

    def test_explicit_route_inherit_and_base_inherit(self):
        self.preference["task_routes"]["scoped_fix"] = {"model": "inherit"}
        selected = self.choose()["preference"]
        self.assertEqual(selected, {"model": "inherit"})
        self.assertEqual(MODELS.codex_spawn_parameters(selected, {"agent_spawn": True})["parameters"], {})
        preference = {"model": "inherit", "task_routes": {"scoped_fix": {"model": "light"}}}
        self.assertEqual(self.choose(preference)["preference"], {"model": "light"})
        self.assertEqual(self.choose(preference, risk="unknown")["preference"], {"model": "inherit"})

    def test_model_change_clears_routes_and_explicit_replacement_is_validated(self):
        document = {"schema_version": 1, "runtimes": {"codex": {"developer": deepcopy(self.preference)}}}
        before = deepcopy(document)
        updated = MODELS.prepare_update(document, "codex", "developer", {"model": "new-base"})
        self.assertEqual(updated["runtimes"]["codex"]["developer"], {"model": "new-base"})
        replacement = {"scoped_fix": {"model": "new-light", "fallback": "inherit"}}
        updated = MODELS.prepare_update(document, "codex", "developer", {"model": "new-base", "task_routes": replacement})
        self.assertEqual(updated["runtimes"]["codex"]["developer"], {"model": "new-base", "task_routes": replacement})
        with self.assertRaises(ValueError):
            MODELS.prepare_update(document, "codex", "developer", {"task_routes": {"ui_validation": {"model": "wrong-role"}}})
        self.assertEqual(document, before)

    def test_effort_only_edit_keeps_routes_and_inherit_change_clears_them(self):
        document = {"schema_version": 1, "runtimes": {"codex": {"developer": deepcopy(self.preference)}}}
        updated = MODELS.prepare_update(document, "codex", "developer", {"effort": "medium"})
        self.assertEqual(updated["runtimes"]["codex"]["developer"]["task_routes"], self.preference["task_routes"])
        inherited = MODELS.prepare_update(document, "codex", "developer", {"model": "inherit"})
        self.assertEqual(inherited["runtimes"]["codex"]["developer"], {"model": "inherit"})

    def test_unavailable_or_unknown_permission_cannot_bypass_host_preflight(self):
        selected = self.choose()["preference"]
        host = {"agent_spawn": True, "model_override": True, "effort_override": True,
                "models_complete": True, "models": {"base": {"permission": True, "efforts": ["high"]},
                "light": {"permission": False, "efforts": ["medium"]}}}
        self.assertEqual(MODELS.assess_capability(selected, host)["state"], "unavailable")
        for permission in (False, None):
            host["models"]["light"]["permission"] = permission
            with self.assertRaises(ValueError):
                MODELS.codex_spawn_parameters(selected, host)
        selected["fallback"] = "inherit"
        plan = MODELS.codex_spawn_parameters(selected, host)
        self.assertTrue(plan["fallback"])
        self.assertEqual(plan["parameters"], {})

    def test_skill_personal_project_and_temporary_replace_entire_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            defaults, personal, project = (root / name for name in ("defaults.json", "personal.json", "project.json"))
            def save(path, preference):
                path.write_text(json.dumps({"schema_version": 1, "runtimes": {"codex": {"developer": preference}}}))
            def resolved(temporary=None):
                return MODELS.resolve("codex", personal, project, temporary, defaults=defaults)["roles"]["developer"]
            save(defaults, self.preference)
            self.assertEqual(self.choose(resolved()["preference"])["preference"]["model"], "light")
            save(personal, {"model": "personal-fixed"})
            self.assertEqual(self.choose(resolved()["preference"])["preference"], {"model": "personal-fixed"})
            personal_routed = {"model": "personal-base", "task_routes": {"scoped_fix": {"model": "personal-light"}}}
            save(personal, personal_routed)
            self.assertEqual(self.choose(resolved()["preference"])["preference"]["model"], "personal-light")
            save(project, {"model": "project-fixed"})
            self.assertFalse(self.choose(resolved()["preference"])["routing"]["applied"])
            self.assertEqual(resolved()["source"], "project")
            before = {path: path.read_bytes() for path in (defaults, personal, project)}
            temporary = {"developer": self.preference}
            self.assertEqual(resolved(temporary)["source"], "temporary")
            self.assertEqual(self.choose(resolved(temporary)["preference"])["preference"]["model"], "light")
            self.assertEqual(self.choose(resolved({"developer": {"model": "inherit"}})["preference"])["preference"], {"model": "inherit"})
            self.assertEqual(before, {path: path.read_bytes() for path in before})


if __name__ == "__main__":
    unittest.main()
