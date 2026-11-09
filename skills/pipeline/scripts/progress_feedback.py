"""Inspect supplied user-visible feedback; never intercept, store or send it."""
import argparse
from datetime import datetime
import json
import re
import sys

ICONS = {'进行中': '🔵', '已完成': '✅', '修复中': '🟠', '等待中': '⏳',
         '需你处理': '🔴', '待核实': '🔎'}
LABEL = re.compile(r'^\s*(🔵|✅|🟠|⏳|🔴|🔎)\s*\*\*\[(进行中|已完成|修复中|等待中|需你处理|待核实)(?:[｜|][^\]\n]{1,40})?\]\*\*')
MAX_BYTES = 262144
MILESTONES = ("start", "solution_returned", "verification_started", "blocked")


def milestone_audit(required, reports):
    samples = {report.get("milestone"): report.get("format_valid") for report in reports}
    return {"missing": [step for step in required if step not in samples],
            "invalid": [step for step in required if step in samples and samples[step] is not True],
            "passed": [step for step in required if samples.get(step) is True],
            "sample_format_passed": sum(report.get("format_valid") is True for report in reports),
            "sample_format_invalid": sum(report.get("format_valid") is False for report in reports),
            "untagged_samples": sum("milestone" not in report for report in reports),
            "basis": "caller_supplied_visible_messages"}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid feedback arguments')


def inspect_feedback(messages):
    if not isinstance(messages, list) or len(messages) > 100:
        raise ValueError('feedback requires at most 100 supplied messages')
    items, times = [], []
    for index, item in enumerate(messages, 1):
        if (not isinstance(item, dict) or set(item) - {'text', 'sent_at'}
                or not isinstance(item.get('text'), str) or len(item['text']) > 20000):
            raise ValueError('invalid supplied feedback')
        text, issues = item['text'], []
        match = LABEL.match(text)
        if match is None:
            issues.append('missing_fixed_bold_status_label')
        else:
            if match[1] != ICONS[match[2]]:
                issues.append('status_icon_mismatch')
            if not text[match.end():].strip():
                issues.append('missing_concrete_feedback_body')
        if '\x1b' in text:
            issues.append('terminal_color_escape_not_supported')
        stamp = None
        if item.get('sent_at') is not None:
            try:
                stamp = datetime.fromisoformat(item['sent_at'])
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('feedback timestamps require explicit timezone') from None
        times.append(stamp)
        items.append({'index': index, 'format_valid': not issues, 'issues': issues})
    comparable = bool(messages) and all(t is not None for t in times)
    if comparable and any(b < a for a, b in zip(times, times[1:])):
        raise ValueError('feedback timestamps must be chronological')
    gaps = [i for i, (a, b) in enumerate(zip(times, times[1:]), 1)
            if a is not None and b is not None and (b - a).total_seconds() > 90]
    return {'basis': 'caller_supplied_messages_not_host_interception', 'checked': len(items),
            'format_valid': all(i['format_valid'] for i in items) if items else None,
            'items': items, 'cadence': {'status': 'observed_timestamps_only' if comparable else 'unknown',
                                      'gaps_over_90s_after_indexes': gaps},
            'content_and_actual_execution': 'requires_review',
            'notice': 'Format and supplied time gaps only; no proof of truthful content, health or full compliance. No text stored or sent.'}


def draft_feedback(value):
    """Render one ready-to-send update from caller-supplied facts."""
    if not isinstance(value, dict) or set(value) - {'status', 'step', 'body'}:
        raise ValueError('invalid feedback draft')
    status, step, body = value.get('status'), value.get('step', ''), value.get('body')
    if (not isinstance(status, str) or status not in ICONS
            or not isinstance(step, str) or (step and not re.fullmatch(r'[^\[\]*｜|\s](?:[^\[\]*｜|\r\n]{0,39})', step))
            or not isinstance(body, str) or not body.strip()):
        raise ValueError('invalid feedback draft')
    label = f'{status}｜{step}' if step else status
    message = f'{ICONS[status]} **[{label}]** {body.strip()}'
    if not inspect_feedback([{'text': message}])['format_valid']:
        raise ValueError('invalid feedback draft')
    return message


def main(argv=None):
    p = _Parser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--stdin', action='store_true',
                   help='JSON array of actual visible messages: text and optional trustworthy sent_at')
    mode.add_argument('--draft', action='store_true',
                      help='read a JSON object with status, optional step and body from stdin')
    try:
        args = p.parse_args(argv)
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('oversized input')
        value = json.loads(raw)
        if args.draft:
            print(draft_feedback(value))
            return 0
        result = inspect_feedback(value)
        code = 0 if result['format_valid'] is True else 1
    except (ValueError, TypeError, OSError):
        result, code = {'error': 'invalid_feedback_input', 'notice': 'No input text stored or echoed.'}, 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
