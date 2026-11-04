"""Persistent operation identity must not replay side effects or old environments."""
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import delivery as D
import release_checks as F
import run_checks as C
import run_evidence as E
import run_state as R
import run_tasks as T


class OperationProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("fixture contract")
        (self.root / "source").write_text("source")
        self.state = R.start(self.root)
        self.args = self.root, self.state["run_id"], self.state["owner_token"]
        self.target = {"release_target": "private-production-target"}
        for phase in ("preflight", "compatibility"):
            self.execute(phase, phase)
        self.execute("build", "build", "from pathlib import Path; Path('artifact').write_text('candidate')",
                     artifact_paths=["artifact"])
        self.execute("activate", "activate", "from pathlib import Path; Path('mutation-count').write_text('1')")
        self.live = self.smoke("smoke")
        self.proof = F.capture_operation_proof(*self.args, "operation", target_environment=self.target)["proof"]

    def execute(self, name, phase, code=None, **options):
        return F.execute(*self.args, name, phase=phase, argv=[sys.executable, "-c", code or f"print('{name}')"],
                         scope=["source"], environment=self.target, timeout=10, **options)

    def smoke(self, name, code=None):
        return self.execute(name, "smoke", code, coverage=["release_identity"])

    def inspect(self, **options):
        return F.inspect_operation_proof(self.root, self.state["run_id"], proof=self.proof,
                                         target_environment=self.target, **options)

    def cli(self, action, declaration, *, owned=False, expected=0):
        argv = [action, "--project", str(self.root), "--json", json.dumps(declaration)]
        if owned:
            receipt = self.root / "receipt.json"
            receipt.write_text(json.dumps({"schema_version": 1, "project_root": str(self.root), **{
                k: self.state[k] for k in ("run_id", "owner_token")}}))
            receipt.chmod(0o600)
            argv += ["--receipt", str(receipt)]
        else:
            argv += ["--run-id", self.state["run_id"]]
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(F.main(argv), expected)
        result = json.loads(stream.getvalue())
        self.assertNotIn(self.state["owner_token"], stream.getvalue())
        self.assertNotIn(self.target["release_target"], stream.getvalue())
        return result

    def test_cli_capture_owned_and_inspect_without_receipt(self):
        captured = self.cli("operation-capture", {"name": "cli-operation", "target_environment": self.target}, owned=True)
        self.assertEqual(captured["acceptance_status"], "CONCERNS")
        result = self.cli("operation-inspect", {"proof": captured["proof"], "target_environment": self.target})
        self.assertTrue(result["valid_operation"])
        self.assertEqual(result["acceptance_status"], "CONCERNS")
        fresh = self.smoke("cli-fresh-smoke")
        result = self.cli("operation-inspect", {"proof": self.proof, "target_environment": self.target,
                         "readback": D._reference(self.root, self.state["run_id"], fresh["report"])})
        self.assertTrue(result["current_readback_valid"])

    def test_cli_invalid_binding_is_exit_one_and_bad_declaration_exit_two(self):
        invalid = {**self.proof, "sha256": "0" * 64}
        self.assertFalse(self.cli("operation-inspect", {"proof": invalid, "target_environment": self.target},
                                 expected=1)["valid_operation"])
        for declaration in ({}, {"proof": [], "target_environment": self.target},
                            {"proof": self.proof, "target_environment": self.target, "unexpected": True},
                            {"proof": self.proof, "target_environment": self.target, "readback": []}):
            self.assertIn("error", self.cli("operation-inspect", declaration, expected=2))

    def test_cli_capture_requires_private_owned_receipt(self):
        import pipeline_cli
        with patch.object(pipeline_cli, "_receipt", side_effect=ValueError("private-value")):
            self.assertIn("error", self.cli("operation-capture", {
                "name": "not-owned", "target_environment": self.target}, owned=True, expected=2))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
            F.main(["operation-capture", "--project", str(self.root), "--json", "{}"])
        self.assertEqual(context.exception.code, 2)

    def test_valid_identity_is_readonly_diagnostic_and_never_acceptance(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(C, "execute_check", side_effect=AssertionError("must not execute")):
            result = self.inspect()
        self.assertTrue(result["valid_operation"])
        self.assertEqual(result["acceptance_status"], "CONCERNS")
        self.assertEqual(result["next_action"], "validate_original_strict_qa_contract")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        public = json.dumps(result)
        for private in (self.state["owner_token"], self.target["release_target"], self.proof["path"]):
            self.assertNotIn(private, public)

    def test_changed_process_preserves_history_but_requires_fresh_live_readback(self):
        with patch.dict(os.environ, {"PIPELINE_OPERATION_ENV": "changed-private-value"}):
            result = self.inspect()
            self.assertTrue(result["historical_mutation_valid"])
            self.assertFalse(result["current_readback_valid"])
            self.assertFalse(result["valid_operation"])
            fresh = self.smoke("fresh-smoke")
            reference = D._reference(self.root, self.state["run_id"], fresh["report"])
            self.assertTrue(self.inspect(readback=reference)["valid_operation"])
        self.assertEqual((self.root / "mutation-count").read_text(), "1")

    def test_tampered_proof_or_activation_or_log_is_rejected(self):
        saved = E.read_report(self.root, self.state["run_id"], self.proof["path"])["payload"]
        mutation = E.read_report(self.root, self.state["run_id"], saved["activation"]["path"])["payload"]
        for path in (self.proof["path"], saved["activation"]["path"], saved["readback"]["path"], mutation["log_path"]):
            location = self.root / path
            original = location.read_bytes()
            location.write_bytes(original + b" ")
            self.assertFalse(self.inspect()["valid_operation"])
            location.write_bytes(original)
        self.assertTrue(self.inspect()["valid_operation"])

    def test_candidate_and_source_changes_invalidate_proof(self):
        for name in ("artifact", "source"):
            path = self.root / name
            original = path.read_bytes()
            path.write_text("changed")
            self.assertFalse(self.inspect()["valid_operation"])
            path.write_bytes(original)

    def test_target_and_run_mismatch_are_rejected_without_values(self):
        result = F.inspect_operation_proof(self.root, self.state["run_id"], proof=self.proof,
                                          target_environment={"release_target": "other"})
        self.assertFalse(result["valid_operation"])
        other = {**self.proof, "run_id": "0" * 32}
        self.assertFalse(F.inspect_operation_proof(self.root, self.state["run_id"], proof=other,
                                                 target_environment=self.target)["valid_operation"])
        for invalid in (None, [], "private-invalid-reference"):
            self.assertFalse(F.inspect_operation_proof(self.root, self.state["run_id"], proof=invalid,
                                                     target_environment=self.target)["valid_operation"])

    def test_new_task_cannot_adopt_previous_task_operation_as_current(self):
        T.start(*self.args, "next-task", "another task", ["actual current task evidence"])
        self.assertFalse(self.inspect()["valid_operation"])

    def test_successful_new_candidate_requires_its_own_operation(self):
        self.execute("build-other", "build", "from pathlib import Path; Path('artifact').write_text('other')",
                     artifact_paths=["artifact"])
        self.assertFalse(self.inspect()["valid_operation"])

    def test_current_readback_keeps_strict_full_environment_requirement(self):
        fresh = self.smoke("fresh-same-environment")
        reference = D._reference(self.root, self.state["run_id"], fresh["report"])
        with patch.dict(os.environ, {"PIPELINE_UNRELATED_VARIABLE": "still-identity-relevant"}):
            result = self.inspect(readback=reference)
        self.assertTrue(result["historical_mutation_valid"])
        self.assertFalse(result["valid_operation"])

    def test_later_smoke_failure_invalidates_old_success(self):
        self.smoke("failed-smoke", "raise SystemExit(3)")
        self.assertFalse(self.inspect()["valid_operation"])
        with self.assertRaises(ValueError):
            F.capture_operation_proof(*self.args, "cannot-capture", target_environment=self.target)

    def test_later_same_command_failure_with_other_labels_is_not_hidden(self):
        saved = E.read_report(self.root, self.state["run_id"], self.proof["path"])["payload"]
        payload = deepcopy(E.read_report(self.root, self.state["run_id"], saved["activation"]["path"])["payload"])
        payload.update(exit_code=1, coverage=["unrelated"])
        E.write_report(*self.args, "check", "later-failure", payload)
        self.assertFalse(self.inspect()["valid_operation"])

    def test_capture_requires_matching_observed_target_and_current_environment(self):
        with self.assertRaises(ValueError):
            F.capture_operation_proof(*self.args, "wrong-target", target_environment={"release_target": "other"})
        with patch.dict(os.environ, {"PIPELINE_OPERATION_ENV": "different"}):
            with self.assertRaises(ValueError):
                F.capture_operation_proof(*self.args, "old-live", target_environment=self.target)


if __name__ == "__main__":
    unittest.main()
