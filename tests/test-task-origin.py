"""Bounded explicit source attribution without chat contents or historical guesses."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/pipeline/scripts'
sys.path.insert(0, str(SCRIPTS))
import task_origin as O
import run_state as R
import run_tasks as T

CHAT_A = '01a17b00-f728-7b50-bbc0-a5ec0fa64638'
CHAT_B = '01a1392d-a9fc-7550-8702-caf86e509676'


class OriginTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.root = Path(t.name).resolve()
        s = R.start(self.root); self.args = self.root, s['run_id'], s['owner_token']
        self.source = {'host_id':'local','thread_id':CHAT_A}

    def test_idempotent_current_binding_stores_digest_not_identity(self):
        self.assertTrue(O.bind(*self.args, **self.source)['created'])
        before = R.status(self.root)['state']
        self.assertFalse(O.bind(*self.args, **self.source)['created'])
        self.assertEqual(before, R.status(self.root)['state'])
        self.assertTrue(O.inspect(self.root, self.args[1], **self.source)['matches_requested_source'])
        body = json.dumps(R.status(self.root)['state'])
        self.assertNotIn(CHAT_A, body); self.assertNotIn('"host_id"', body)
        with self.assertRaises(ValueError): O.bind(*self.args, host_id='local', thread_id=CHAT_B)
        with self.assertRaises(ValueError): O.bind(self.root,self.args[1],'wrong',**self.source)

    def test_mixed_run_attribution_keeps_individual_task_boundaries(self):
        T.start(*self.args,'first','goal',['criteria']); O.bind(*self.args,**self.source)
        T.end(*self.args,'first')
        with self.assertRaises(ValueError): O.bind(*self.args,**self.source)
        T.start(*self.args,'second','another goal',['criteria'])
        self.assertIsNone(O.inspect(self.root,self.args[1],**self.source)['matches_requested_source'])
        O.bind(*self.args,host_id='local',thread_id=CHAT_B)
        result=O.scan(self.root,**self.source)
        self.assertEqual(len(result['matches']),1)
        self.assertEqual(len(result['matches'][0]['matching_tasks']),1)
        self.assertNotIn('audit',result['matches'][0]);self.assertNotIn('wall_seconds',json.dumps(result))

    def test_readonly_unknown_does_not_create_logs_or_assume_no_pipeline(self):
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        value=O.scan(self.root,**self.source)
        self.assertEqual(value['matches'],[]);self.assertEqual(value['unattributed_runs'],1)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        with self.assertRaises(ValueError): O.source_key('local','quote')

    def test_cli_inspect_readonly_without_receipt(self):
        O.bind(*self.args,**self.source)
        r=subprocess.run([sys.executable,'-B',str(SCRIPTS/'task_origin.py'),'inspect','--project',str(self.root),
            '--run-id',self.args[1],'--host-id','local','--thread-id',CHAT_A],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr+r.stdout)
        self.assertTrue(json.loads(r.stdout)['matches_requested_source'])
        self.assertNotIn(CHAT_A,r.stdout);self.assertNotIn(self.args[2],r.stdout)

    def test_invalid_cli_arguments_never_echo_values(self):
        for extra in [ ['--limit','PRIVATE-MALFORMED-LIMIT'], ['--PRIVATE-UNKNOWN'] ]:
            r=subprocess.run([sys.executable,'-B',str(SCRIPTS/'task_origin.py'),'scan',
                '--project',str(self.root),'--host-id','local','--thread-id',CHAT_A,*extra],
                capture_output=True,text=True)
            self.assertEqual(r.returncode,2)
            self.assertNotIn('PRIVATE',r.stdout+r.stderr)


if __name__=='__main__':unittest.main()
