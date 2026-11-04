"""Opt-in host metadata removal changes real execution, never stored env replay."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import run_checks as C
import run_evidence as E
import run_state as R


STABLE = {"pipeline_execution_profile": "stable_host"}


class StableEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = R.start(self.root, {"stage": "implementation"})
        (self.root / "input.txt").write_text("known input")
        self.argv = [sys.executable, "-c", "print('PASS')"]

    def execute(self, name, **changes):
        args = dict(argv=self.argv, scope=["input.txt"], environment=STABLE)
        args.update(changes)
        return C.execute_check(self.root, self.state["run_id"], self.state["owner_token"], name, **args)

    def inspect(self, **changes):
        args = dict(argv=self.argv, scope=["input.txt"], environment=STABLE, impact_covered=True)
        args.update(changes)
        return C.inspect_reuse(self.root, self.state["run_id"], **args)

    def test_actual_child_only_removes_tool_call_id_when_opted_in(self):
        with patch.dict(os.environ, {"CODEX_TOOL_CALL_ID": "private-invocation",
                                     "UNKNOWN_KEEP": "private-secret"}):
            for name, context, condition in (("stable", STABLE, "not in"), ("strict", None, "in")):
                argv = [sys.executable, "-c", "import os; "
                        f"assert 'CODEX_TOOL_CALL_ID' {condition} os.environ; "
                        "assert 'UNKNOWN_KEEP' in os.environ; print('PASS')"]
                result = self.execute(name, argv=argv, environment=context)
                self.assertEqual(result["exit_code"], 0)
                proof = E.read_report(self.root, self.state["run_id"], result["report"])["payload"]
                self.assertEqual((self.root / proof["log_path"]).read_text().strip(), "PASS")
                self.assertTrue(proof["inputs_stable"])

    def test_different_invocations_reuse_same_optin_and_inspection_agrees(self):
        with patch.dict(os.environ, {"CODEX_TOOL_CALL_ID": "private-call-one"}):
            first = self.execute("first")
        with patch.dict(os.environ, {"CODEX_TOOL_CALL_ID": "private-call-two"}):
            with patch.object(C.subprocess, "Popen", side_effect=AssertionError("must reuse")):
                self.assertTrue(self.inspect()["reusable"])
                self.assertTrue(self.execute("reused", impact_covered=True)["reused"])
        proof = E.read_report(self.root, self.state["run_id"], first["report"])["payload"]
        self.assertEqual(proof["environment"]["context:pipeline_execution_profile"], "stable_host")
        for secret in ("private-call-one", "private-call-two"):
            self.assertNotIn(secret, json.dumps(proof))

    def test_default_strict_invocation_change_invalidates(self):
        with patch.dict(os.environ, {"CODEX_TOOL_CALL_ID": "one"}):
            self.execute("strict-first", environment=None)
        with patch.dict(os.environ, {"CODEX_TOOL_CALL_ID": "two"}):
            self.assertFalse(self.inspect(environment=None)["reusable"])
            self.assertFalse(self.execute("strict-second", environment=None, impact_covered=True)["reused"])

    def test_strict_proof_never_upgrades_even_when_tool_id_was_absent(self):
        with patch.dict(os.environ):
            os.environ.pop("CODEX_TOOL_CALL_ID", None)
            self.execute("legacy-strict", environment=None)
            self.assertFalse(self.inspect()["reusable"])
            self.assertFalse(self.execute("optin-new", impact_covered=True)["reused"])
            self.assertFalse(self.inspect(environment=None)["reusable"])

    def test_significant_and_unknown_environment_drift_remain_strict_and_private(self):
        for index, key in enumerate(("PATH", "NODE_ENV", "NODE_OPTIONS", "PYTHONPATH", "SHLVL", "UNKNOWN_PRIVATE_KEY")):
            with self.subTest(key=key), patch.dict(os.environ):
                os.environ[key] = os.environ.get(key, "") + "original-private-value"
                self.execute(f"original-{index}")
                os.environ[key] += "changed-private-value"
                result = self.inspect()
                self.assertFalse(result["reusable"])
                self.assertEqual(result["reason"], "command_or_environment_changed")
                if key == "UNKNOWN_PRIVATE_KEY":
                    self.assertTrue(result["environment_changes"]["other_environment_changed"])
                for secret in ("original-private-value", "changed-private-value", "UNKNOWN_PRIVATE_KEY",
                               self.state["owner_token"]):
                    self.assertNotIn(secret, json.dumps(result))
                self.assertFalse(self.execute(f"changed-{index}", impact_covered=True)["reused"])

    def test_helper_preserves_every_other_environment_value_and_filters_owner(self):
        original = {"CODEX_TOOL_CALL_ID": "tool-id", "PATH": "path", "PYTHONPATH": "python",
                    "NODE_OPTIONS": "node", "SHLVL": "3", "UNKNOWN": "private-secret",
                    "PIPELINE_OWNER_TOKEN": "credential", "ALIAS": "prefix-owner-suffix"}
        with patch.dict(os.environ, original, clear=True):
            strict = {k: v for k, v in original.items() if k not in {"PIPELINE_OWNER_TOKEN", "ALIAS"}}
            self.assertEqual(C.child_environment("owner"), strict)
            self.assertEqual(C.child_environment("owner", STABLE),
                             {k: v for k, v in strict.items() if k != "CODEX_TOOL_CALL_ID"})
            self.assertEqual(dict(os.environ), original)

    def test_unsupported_profile_rejected_before_execution_or_reuse(self):
        context = {"pipeline_execution_profile": "private-unsupported-profile"}
        before = R.status(self.root, self.state["run_id"])
        with patch.object(C.subprocess, "Popen", side_effect=AssertionError("must reject")):
            for call in (lambda: self.execute("invalid", environment=context),
                         lambda: self.inspect(environment=context),
                         lambda: C.inspect_runner(self.root, self.argv, environment=context)):
                with self.assertRaisesRegex(ValueError, "^unsupported pipeline execution profile$"):
                    call()
        # execute_check records its failed operation, but never claims or produces a check.
        state = R.status(self.root, self.state["run_id"])["state"]
        self.assertFalse(state.get("running_checks"))
        self.assertEqual(state["artifacts"], before["state"]["artifacts"])


if __name__ == "__main__":
    unittest.main()
