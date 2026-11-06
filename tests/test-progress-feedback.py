"""Actual supplied feedback formatting checks without messages or state writes."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/pipeline/scripts'
sys.path.insert(0, str(SCRIPTS))
import progress_feedback as F


class FeedbackTests(unittest.TestCase):
    def test_six_fixed_labels_and_body(self):
        for status, icon in F.ICONS.items():
            self.assertTrue(F.inspect_feedback([{'text': f'{icon} **[{status}｜测试]** 当前检查已返回；下一步复核。'}])['format_valid'])
        for text in ['正在处理', '🛠️ **已进入标准流水线**：开发中', '✅ 已完成',
                     '🔵 [进行中｜测试] 处理中', '✅ **[进行中]** 当前动作', '✅ **[已完成]**']:
            self.assertFalse(F.inspect_feedback([{'text':text}])['format_valid'])

    def test_no_body_text_or_private_values_in_output(self):
        value = F.inspect_feedback([{'text':'✅ **[已完成]** PRIVATE-REPORT 业务输出'}])
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertEqual(value['content_and_actual_execution'], 'requires_review')
        self.assertEqual(value['cadence']['status'], 'unknown')

    def test_unknown_timestamps_not_invented_and_known_gaps_are_only_candidates(self):
        v = F.inspect_feedback([{'text':'x','sent_at':'2026-11-04T10:00:00+08:00'},
                                {'text':'y','sent_at':'2026-11-04T10:02:00+08:00'}])
        self.assertEqual(v['cadence']['gaps_over_90s_after_indexes'], [1])
        for bad in ['2026-11-04T10:00:00', 'PRIVATE']:
            with self.assertRaises(ValueError): F.inspect_feedback([{'text':'x','sent_at':bad}])
        self.assertIsNone(F.inspect_feedback([])['format_valid'])

    def test_cli_only_stdin_no_echo_and_no_project_access(self):
        for text, code in [(json.dumps([{'text':'PRIVATE'}]), 1), ('PRIVATE INVALID', 2),
                           (json.dumps([{'text':'🔵 **[进行中｜测试]** 正在执行定向测试。'}]), 0)]:
            r = subprocess.run([sys.executable, '-B', str(SCRIPTS/'progress_feedback.py'), '--stdin'],
                               input=text, text=True, capture_output=True)
            self.assertEqual(r.returncode, code)
            self.assertNotIn('PRIVATE', r.stdout + r.stderr)
            json.loads(r.stdout)

    def test_invalid_cli_arguments_never_echo_values(self):
        r = subprocess.run([sys.executable,'-B',str(SCRIPTS/'progress_feedback.py'),
                            '--stdin','--PRIVATE-UNKNOWN'],input='[]',text=True,capture_output=True)
        self.assertEqual(r.returncode,2)
        self.assertNotIn('PRIVATE',r.stdout+r.stderr)

    def test_draft_cli_outputs_a_valid_message_and_rejects_malformed_labels(self):
        command = [sys.executable, '-B', str(SCRIPTS/'progress_feedback.py'), '--draft']
        draft = {'status': '等待中', 'step': '独立验收', 'body': '本地检查已通过，等待 tester 返回。'}
        result = subprocess.run(command, input=json.dumps(draft), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '⏳ **[等待中｜独立验收]** 本地检查已通过，等待 tester 返回。')
        self.assertTrue(F.inspect_feedback([{'text': result.stdout}])['format_valid'])
        result = subprocess.run(command, input=json.dumps({**draft, 'step': 'PRIVATE]'}),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('PRIVATE', result.stdout + result.stderr)


if __name__ == '__main__': unittest.main()
