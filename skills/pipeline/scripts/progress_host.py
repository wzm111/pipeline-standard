"""Read one visible assistant message from a local host transcript."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import run_evidence as E


_CODEX_HEADER = re.compile(
    rb'^\s*\{\s*"timestamp"\s*:\s*"[^"]+"\s*,\s*(?:"ordinal"\s*:\s*-?\d+\s*,\s*)?'
    rb'"type"\s*:\s*"(session_meta|turn_context|response_item)"\s*,')
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")
_SESSION = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def _codex_records(stream, session, message_id, marker):
    cwd = None
    for index, line in enumerate(stream):
        header = _CODEX_HEADER.match(line)
        if index == 0:
            if not header or header[1] != b"session_meta":
                raise ValueError("Codex session metadata missing")
            identity = json.loads(line)["payload"]
            if identity.get("id") != session or identity.get("agent_path"):
                raise ValueError("Codex session identity mismatch")
            cwd = identity.get("cwd")
        elif header and header[1] == b"turn_context":
            cwd = json.loads(line)["payload"].get("cwd")
        elif header and header[1] == b"response_item" and marker in line:
            record = json.loads(line)
            message = record["payload"]
            if (message.get("id") == message_id and message.get("type") == "message"
                    and message.get("role") == "assistant" and message.get("phase") == "commentary"):
                yield record["timestamp"], cwd, message["content"], "output_text", line


def _claude_records(stream, session, message_id, marker):
    for line in stream:
        if marker not in line:
            continue
        record = json.loads(line)
        message = record.get("message") or {}
        if (record.get("uuid") == message_id and record.get("sessionId") == session
                and record.get("type") == "assistant" and record.get("isSidechain") is False
                and message.get("role") == "assistant"):
            yield record["timestamp"], record.get("cwd"), message["content"], "text", line


def read_message(root, evidence):
    """Return only the selected message and its host timestamp and fingerprint."""
    if (not isinstance(evidence, dict) or set(evidence) != {"transcript", "message_id"}
            or not isinstance(evidence["message_id"], str) or not _ID.fullmatch(evidence["message_id"])
            or not isinstance(evidence["transcript"], str)):
        raise ValueError("solution_returned requires host_message with transcript and message_id")
    home = Path.home().resolve()
    source = Path(evidence["transcript"])
    codex_dir, claude_dir = home / ".codex/sessions", home / ".claude/projects"
    try:
        if source.parent.parent == claude_dir:
            runtime, reader = "claude", _claude_records
            session = source.stem
            if not re.fullmatch(_SESSION, session):
                raise ValueError("invalid Claude session")
            current = os.environ.get("PIPELINE_CLAUDE_SESSION_ID")
            if not current or current != session or os.environ.get("CODEX_THREAD_ID"):
                raise ValueError("Claude session mismatch")
        elif source.is_relative_to(codex_dir):
            runtime, reader = "codex", _codex_records
            match = re.fullmatch(rf"rollout-.+-({_SESSION})\.jsonl", source.name)
            current = os.environ.get("CODEX_THREAD_ID")
            if not match or not current or current != match[1] or os.environ.get("PIPELINE_CLAUDE_SESSION_ID"):
                raise ValueError("Codex session mismatch")
            session = current
        else:
            raise ValueError("expected a local host transcript")
        source = E._local(home, str(source.relative_to(home)))
        if source.suffix != ".jsonl" or not stat.S_ISREG(source.stat().st_mode):
            raise ValueError("expected a regular host transcript")
        message_id = evidence["message_id"]
        marker = json.dumps(message_id).encode()
        with source.open("rb") as stream:
            records = reader(stream, session, message_id, marker)
            found = next(records, None)
            if found is None or next(records, None) is not None:
                raise ValueError("expected one visible assistant message")
        sent_at, cwd, blocks, text_type, line = found
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).resolve().is_relative_to(root):
            raise ValueError("assistant message belongs to a different worktree")
        text = "\n".join(block["text"] for block in blocks if block.get("type") == text_type)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        raise ValueError("solution_returned cannot verify a unique visible host message in this worktree") from None
    return text, {"runtime": runtime, "message_id": message_id,
                  "sent_at": sent_at, "record_sha256": hashlib.sha256(line).hexdigest()}
