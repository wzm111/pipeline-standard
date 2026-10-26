import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("metrics", Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/workflow_metrics.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class MetricsTests(unittest.TestCase):
    def record(self, arm, task="T1", repeat=1, **values):
        return dict(task_id=task, arm=arm, repeat=repeat, base_commit="abc", model="model", effort="medium",
                    runtime="codex", environment_id="env", acceptance_id="checks", status="passed", acceptance_passed=True,
                    **dict(dict(wall_seconds=100, human_wait_seconds=0, token_usage=None), **values))

    def test_pair_ratio_and_unknown_tokens(self):
        result = M.paired_summary([self.record("A"), self.record("B", wall_seconds=130)])
        self.assertEqual(result["median_ratio"], 1.3)
        self.assertIsNone(result["performance_gate"])
        self.assertIsNone(result["token_median_ratio"])

    def test_failures_and_mismatches_are_visible(self):
        b = self.record("B")
        b["model"] = "other"
        result = M.paired_summary([self.record("A"), b])
        self.assertEqual(result["task_count"], 0)
        self.assertEqual(len(result["excluded"]), 1)
        b["model"], b["status"] = "model", "failed"
        self.assertIn("failure", M.paired_summary([self.record("A"), b])["excluded"][0]["reason"])

    def test_repeated_samples_required_and_task_weighting(self):
        rows = [self.record(arm, f"T{i}", repeat) for i in range(12) for repeat in (1, 2) for arm in ("A", "B")]
        result = M.paired_summary(rows)
        self.assertTrue(result["performance_gate"])
        self.assertEqual(result["label"], "publishable_sample")
        with_c = M.paired_summary(rows + [self.record("C")])
        self.assertTrue(with_c["performance_gate"])
        self.assertEqual(len(with_c["separate"]), 1)

    def test_bad_measurements_fail(self):
        for invalid in ({}, [1], None):
            with self.assertRaises(ValueError):
                M.paired_summary(invalid)
        for bad in (-1, float("nan"), True):
            with self.assertRaises(ValueError):
                M.paired_summary([self.record("A"), self.record("B", wall_seconds=bad)])
        with self.assertRaises(ValueError):
            M.paired_summary([self.record("A"), self.record("A")])

    def test_intervals_do_not_double_count(self):
        self.assertEqual(M.interval_seconds([(0, 10), (5, 15), (20, 25)]), 20)
        with self.assertRaises(ValueError):
            M.interval_seconds([(3, 1)])

    def test_evidence_invalidation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "src").mkdir()
            (root / "src/a").write_text("a")
            first = M.fingerprint(root, ["src"], "env1")
            output = root / "result.log"
            output.write_text("ok")
            proof = dict(exit_code=0, fingerprint=first, command="test", finished_at="now", output_path=str(output))
            proof["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertTrue(M.reusable(proof, first, "test", True))
            self.assertFalse(M.reusable(proof, first, "test", False))
            output.write_text("replaced")
            self.assertFalse(M.reusable(proof, first, "test", True))
            (root / "src/b").write_text("new")
            second = M.fingerprint(root, ["src"], "env1")
            self.assertNotEqual(first, second)
            self.assertFalse(M.reusable(proof, second, "test", True))
            self.assertNotEqual(second, M.fingerprint(root, ["src"], "env2"))
            with self.assertRaises(ValueError):
                M.fingerprint(root, ["../other"], "env1")
            (root / "src/link").symlink_to(output)
            with self.assertRaises(ValueError):
                M.fingerprint(root, ["src"], "env1")


if __name__ == "__main__":
    unittest.main()
