"""Explicit local chat/task attribution; only a digest is stored, no chat access."""
import argparse
import hashlib
import json
from pathlib import Path
import re

import run_state as R
import run_policy
import run_tasks as T

NOTICE = 'Caller-declared association only, not host verification or acceptance. No historical attribution inferred.'


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid origin arguments')


def source_key(host_id, thread_id):
    if (not isinstance(host_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', host_id)
            or not isinstance(thread_id, str)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', thread_id)):
        raise ValueError('valid host and native chat identity required')
    return hashlib.sha256(json.dumps([host_id, thread_id], separators=(',', ':')).encode()).hexdigest()


def _boundary(state):
    tasks = T._tasks(state)
    return tasks[-1]['started_at'] if tasks else state['created_at']


def _bindings(state):
    values = state.get('task_origins', [])
    if not isinstance(values, list):
        raise ValueError('invalid task origins')
    seen = set()
    for item in values:
        if (not isinstance(item, dict) or set(item) != {'task_started_at', 'session_sha256', 'recorded_at'}
                or not isinstance(item['session_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', item['session_sha256'])
                or item['task_started_at'] in seen):
            raise ValueError('invalid task origin binding')
        at = R._timestamp(item['task_started_at'])
        if R._timestamp(item['recorded_at']) < at:
            raise ValueError('invalid origin time')
        seen.add(item['task_started_at'])
    return values


def bind(root, run_id, owner, *, host_id, thread_id):
    key = source_key(host_id, thread_id)
    with R._write_guard(root) as base:
        path, state = R._load_owned(base, run_id, owner)
        run_policy.require(root, state)
        if state['status'] != 'running' or (T._tasks(state) and T._tasks(state)[-1]['status'] != 'running'):
            raise ValueError('origin binds only the current executing task, never historical tasks')
        boundary = _boundary(state)
        prior = next((x for x in _bindings(state) if x['task_started_at'] == boundary), None)
        if prior:
            if prior['session_sha256'] != key:
                raise ValueError('current task already has a different declared origin')
            return {'associated': True, 'created': False, 'notice': NOTICE}
        now = R._now()
        state.setdefault('task_origins', []).append({'task_started_at': boundary,
            'session_sha256': key, 'recorded_at': now})
        state['updated_at'] = now
        R._atomic(path, state)
    return {'associated': True, 'created': True, 'notice': NOTICE}


def inspect(root, run_id, *, host_id, thread_id):
    key = source_key(host_id, thread_id)
    before = R.status(root, run_id)
    state = before['state']
    result = {'associated': False, 'matches_requested_source': None, 'matching_tasks': [], 'notice': NOTICE}
    if state:
        tasks = T._tasks(state)
        scopes = {t['started_at']: t.get('ended_at') for t in tasks} or {state['created_at']: None}
        origins = _bindings(state)
        current = next((x for x in origins if x['task_started_at'] == _boundary(state)), None)
        result.update(associated=current is not None,
                      matches_requested_source=current['session_sha256'] == key if current else None)
        result['matching_tasks'] = [{'started_at': x['task_started_at'], 'ended_at': scopes[x['task_started_at']]}
                                    for x in origins if x['session_sha256'] == key and x['task_started_at'] in scopes]
    if R.status(root, run_id) != before:
        raise ValueError('run changed while reading origin')
    return result


def scan(root, *, host_id, thread_id, limit=20):
    import run_audit
    source_key(host_id, thread_id)
    existing = run_audit.scan(root, limit)
    if existing['status'] != 'ok':
        return existing
    matches, unknown = [], 0
    for item in existing['runs']:
        origin = inspect(root, item['run_id'], host_id=host_id, thread_id=thread_id)
        if origin['matching_tasks']:
            # Do not attach whole-run totals: another chat or goal can share it.
            matches.append({'run_id': item['run_id'], 'matching_tasks': origin['matching_tasks']})
        elif not origin['associated']:
            unknown += 1
    return {'status': 'ok', 'matches': matches, 'unattributed_runs': unknown,
            'truncated': existing['truncated'], 'limit': limit, 'notice': NOTICE}


def main(argv=None):
    p = _Parser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('bind', 'inspect', 'scan'):
        c = sub.add_parser(action, allow_abbrev=False)
        c.add_argument('--project', default='.')
        c.add_argument('--host-id', required=True)
        c.add_argument('--thread-id', required=True)
        if action == 'bind': c.add_argument('--receipt', required=True)
        elif action == 'inspect': c.add_argument('--run-id', required=True)
        else: c.add_argument('--limit', type=int, default=20)
    try:
        args = p.parse_args(argv)
        root = Path(args.project).resolve(strict=True)
        options = {'host_id':args.host_id, 'thread_id':args.thread_id}
        if args.action == 'bind':
            from pipeline_cli import _receipt
            r = _receipt(root, args.receipt)
            result = bind(root, r['run_id'], r['owner_token'], **options)
        elif args.action == 'inspect': result = inspect(root, args.run_id, **options)
        else: result = scan(root, limit=args.limit, **options)
        code = 0 if result.get('status') != 'error' else 2
    except (OSError, ValueError, KeyError, TypeError):
        result, code = {'error':'origin_request_rejected', 'notice':NOTICE}, 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__': raise SystemExit(main())
