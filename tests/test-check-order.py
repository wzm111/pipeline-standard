"""Critical quick checks precede heavy gates without weakening reuse or coverage."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import check_plan as P
import run_evidence as E
import run_state as R


class CheckOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "PIPELINE.md").write_text("isolated scope")
        (self.root / "input.txt").write_text("initial")
        state = R.start(self.root)
        self.args = self.root, state["run_id"], state["owner_token"]

    def check(self, label, *, layer=None, fail=False):
        code = ("import json;from pathlib import Path;p=Path('trace.json');"
                "a=json.loads(p.read_text()) if p.exists() else [];"
                f"a.append({label!r});p.write_text(json.dumps(a));" + ("raise SystemExit(3)" if fail else ""))
        value = {"argv": [sys.executable, "-c", code], "scope": ["input.txt"],
                 "coverage": [label], "timeout": 10}
        return {**value, **({"layer": layer} if layer is not None else {})}

    def test_boundary_failure_prevents_heavy_command_and_never_passes_missing_gate(self):
        plan = P.save_plan(*self.args, "order", {"a-heavy": self.check("heavy"),
                           "z-boundary": self.check("boundary", layer="quick", fail=True)})
        result = P.verify_plan(*self.args, plan, "first")
        self.assertEqual(json.loads((self.root / "trace.json").read_text()), ["boundary"])
        self.assertEqual(result["results"][0]["check_id"], "z-boundary")
        self.assertEqual(result["skipped_checks"], ["a-heavy"])
        self.assertNotEqual(result["gate"], "PASS")
        self.assertIn("heavy", result["missing_checks"])
        self.assertEqual(R.status(self.root)["state"]["status"], "running")

    def test_quick_then_final_retains_all_original_requirements(self):
        plan = P.save_plan(*self.args, "order", {"a-heavy": self.check("heavy", layer="final"),
                           "z-boundary": self.check("boundary", layer="quick")})
        result = P.verify_plan(*self.args, plan, "first")
        self.assertEqual(result["gate"], "PASS")
        self.assertEqual(result["required_checks"], ["boundary", "heavy"])
        self.assertEqual(json.loads((self.root / "trace.json").read_text()), ["boundary", "heavy"])

    def test_changing_only_layer_reuses_original_identity_and_changed_input_reruns(self):
        checks = {"a-heavy": self.check("heavy"), "z-boundary": self.check("boundary", layer="quick")}
        old = P.save_plan(*self.args, "old", checks)
        self.assertEqual(P.verify_plan(*self.args, old, "first")["gate"], "PASS")
        checks["a-heavy"]["layer"] = "quick"
        checks["z-boundary"]["layer"] = "final"
        new = P.save_plan(*self.args, "new", checks)
        result = P.verify_plan(*self.args, new, "second")
        self.assertTrue(all(item["reused"] for item in result["results"]))
        self.assertEqual(result["gate"], "PASS")
        self.assertEqual(json.loads((self.root / "trace.json").read_text()), ["boundary", "heavy"])
        (self.root / "input.txt").write_text("changed")
        changed = P.verify_plan(*self.args, new, "third")
        self.assertFalse(any(item["reused"] for item in changed["results"]))
        self.assertEqual(changed["gate"], "PASS")
        self.assertEqual(json.loads((self.root / "trace.json").read_text()),
                         ["boundary", "heavy", "heavy", "boundary"])

    def test_old_plan_without_layers_and_invalid_layers_remain_safe(self):
        old = P.save_plan(*self.args, "old", {"first": self.check("one"), "second": self.check("two")})
        self.assertNotIn("layer", P.read_plan(*self.args, old)["checks"]["first"])
        self.assertEqual(P.verify_plan(*self.args, old, "first")["gate"], "PASS")
        for bad in (True, [], "fastest", None):
            with self.assertRaises(ValueError):
                P.save_plan(*self.args, "bad", {"quick": {**self.check("one"), "layer": bad}})

    def test_reclassifying_layer_does_not_hide_newer_same_identity_failure(self):
        check = self.check("boundary", layer="quick")
        old = P.save_plan(*self.args, "old", {"edge": check})
        result = P.verify_plan(*self.args, old, "first")["results"][0]
        proof = E.read_report(self.root, self.args[1], result["report"], kind="check")["payload"]
        (self.root / "observed.log").write_text("external same-identity failure")
        E.record_check(*self.args, "newer-failure", command=proof["command"],
                       scope=proof["scope"], environment=proof["environment"], exit_code=1,
                       log_path="observed.log", coverage=proof["coverage"],
                       started_fingerprint=E.fingerprint(self.root, proof["scope"]))
        new = P.save_plan(*self.args, "new", {"edge": {**check, "layer": "final"}})
        repeated = P.verify_plan(*self.args, new, "second")
        self.assertFalse(repeated["results"][0]["reused"])
        self.assertEqual(json.loads((self.root / "trace.json").read_text()), ["boundary", "boundary"])


if __name__ == "__main__":
    unittest.main()
