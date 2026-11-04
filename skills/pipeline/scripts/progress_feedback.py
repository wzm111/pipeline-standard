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


def main(argv=None):
    p = _Parser(description=__doc__)
    p.add_argument('--stdin', action='store_true', required=True,
                   help='JSON array of actual visible messages: text and optional trustworthy sent_at')
    try:
        p.parse_args(argv)
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('oversized input')
        result = inspect_feedback(json.loads(raw))
        code = 0 if result['format_valid'] is True else 1
    except (ValueError, TypeError, OSError):
        result, code = {'error': 'invalid_feedback_input', 'notice': 'No input text stored or echoed.'}, 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
