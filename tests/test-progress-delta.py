"""Single bounded progress read, caller-held deltas and private metadata."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/pipeline/scripts'
sys.path.insert(0, str(SCRIPTS))
import run_state as R
import run_evidence as E
import run_progress as G
import run_tasks as T
import operation_metrics as O
import observed_checks as H


class ProgressDeltaTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.root = Path(t.name).resolve()
        (self.root / 'input').write_text('fixture')
        s = R.start(self.root); self.args = self.root, s['run_id'], s['owner_token']

    def test_one_snapshot_includes_pending_operations_host_calls_and_recorded_qa_only(self):
        O.begin(*self.args, 'network', 'candidate-upload')
        H.begin(*self.args, 'browser-smoke', command='private-browser-command', scope=['input'],
                environment={'secret': 'PRIVATE-ENV'}, write_paths=[])
        E.write_report(*self.args, 'qa', 'concerns', {'gate':'CONCERNS', 'summary':'PRIVATE-SUMMARY'})
        before = {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(E, 'fingerprint', side_effect=AssertionError('no source hashing')), \
             patch.object(E, 'reusable_check', side_effect=AssertionError('not acceptance')):
            v = G.snapshot(self.root)
        self.assertEqual(v['current']['operations'][0]['step'], 'candidate-upload')
        self.assertEqual(v['current']['observations'][0]['step'], 'browser-smoke')
        self.assertEqual(v['latest_qa']['gate'], 'CONCERNS')
        self.assertEqual(v['latest_qa']['acceptance'], 'not_revalidated')
        self.assertNotIn('PRIVATE', json.dumps(v)); self.assertNotIn(self.args[2], json.dumps(v))
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertIn('candidate-upload', G.render(v))

    def test_prior_task_operations_and_qa_are_not_current_progress(self):
        O.begin(*self.args, 'read', 'previous-read')
        E.write_report(*self.args, 'qa', 'old', {'gate':'PASS'})
        T.start(*self.args, 'current', 'goal', ['acceptance'])
        v = G.snapshot(self.root)
        self.assertEqual(v['current']['operations'], [])
        self.assertIsNone(v['latest_qa'])

    def test_delta_never_repeats_full_payload_for_same_milestone(self):
        first = G.snapshot(self.root)
        same = G.delta(G.snapshot(self.root), first['milestone_id'])
        self.assertFalse(same['changed']); self.assertNotIn('current', same)
        self.assertNotIn('task_id', same)
        O.begin(*self.args, 'read', 'new-read')
        updated = G.delta(G.snapshot(self.root), first['milestone_id'])
        self.assertTrue(updated['changed'])
        with self.assertRaises(ValueError): G.delta(updated, 'invalid')

    def test_after_task_end_host_work_does_not_leak_through_check_claims(self):
        T.start(*self.args, 'current', 'goal', ['acceptance'])
        E.write_report(*self.args, 'qa', 'accepted', {'gate': 'PASS'})
        T.end(*self.args, 'current')
        H.begin(*self.args, 'after-end', command='private-host-action', scope=['input'],
                environment={'host': 'fixture'}, write_paths=[])
        v = G.snapshot(self.root)
        self.assertEqual(v['current']['checks'], [])
        self.assertEqual(v['current']['observations'], [])
        self.assertEqual(v['latest_qa']['gate'], 'PASS')
        self.assertNotIn('after-end', json.dumps(v))

    def test_cli_delta_is_readonly_and_safe(self):
        def cli(*args):
            return subprocess.run([sys.executable, '-B', str(SCRIPTS/'pipeline_cli.py'),
                'progress', '--project', str(self.root), *args], capture_output=True, text=True)
        first = cli(); self.assertEqual(first.returncode, 0)
        cursor = json.loads(first.stdout)['milestone_id']
        second = cli('--after-milestone', cursor)
        self.assertEqual(second.returncode, 0)
        self.assertFalse(json.loads(second.stdout)['changed'])
        self.assertNotIn(self.args[2], second.stdout)
        text = cli('--after-milestone', cursor, '--format', 'text')
        self.assertIn('无新的已登记里程碑', text.stdout)


if __name__ == '__main__': unittest.main()
