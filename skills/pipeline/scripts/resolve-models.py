#!/usr/bin/env python3
"""Read-only role preference resolver. Never starts models or saves files."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_config import (ROLES, claude_role_parameters, codex_spawn_parameters,
                          read_document, read_layer, resolve, select_task_route)  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("codex", "claude"), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--personal", type=Path, default=Path.home() / ".config/pipeline-standard/models.json")
    parser.add_argument("--temporary", help="session-only role object as JSON; never persisted")
    parser.add_argument("--read", action="store_true", help="read project document and revision instead of resolving")
    parser.add_argument("--role", choices=sorted(ROLES), help="return only the role being delegated")
    parser.add_argument("--compact", action="store_true", help="compact JSON for agent handoff")
    parser.add_argument("--host-evidence", type=Path, help="current host capability JSON; requires --role; previews parameters only")
    parser.add_argument("--task-kind", help="task type for an explicitly configured role route")
    parser.add_argument("--risk", choices=("low", "medium", "high", "unknown"), default="unknown")
    parser.add_argument("--complexity", choices=("simple", "moderate", "complex", "unknown"), default="unknown")
    args = parser.parse_args()
    try:
        project = args.project / "pipeline.models.json"
        routing_requested = args.task_kind is not None or args.risk != "unknown" or args.complexity != "unknown"
        if routing_requested and (args.role is None or args.task_kind is None):
            raise ValueError("task routing requires both --role and --task-kind")
        if args.host_evidence is not None and args.role is None:
            raise ValueError("--host-evidence requires --role")
        if args.read and (args.role is not None or args.host_evidence is not None):
            raise ValueError("--read cannot select or plan a role")
        if args.read:
            if args.temporary is not None:
                raise ValueError("--read does not accept --temporary")
            document, revision = read_document(project)
            result = {"document": document, "revision": revision, "path": str(project)}
        else:
            temporary = json.loads(args.temporary) if args.temporary is not None else None
            if args.temporary is not None and not isinstance(temporary, dict):
                raise ValueError("temporary roles must be an object")
            result = resolve(args.runtime, args.personal, project, temporary)
            if args.role is not None:
                if args.role not in result["roles"]:
                    raise ValueError(f"no preference for role {args.role}")
                result = {"runtime": args.runtime, "role": args.role, **result["roles"][args.role]}
                if routing_requested:
                    result.update(select_task_route(args.role, result["preference"], task_kind=args.task_kind,
                                                    risk=args.risk, complexity=args.complexity))
                if args.host_evidence is not None:
                    host = json.loads(args.host_evidence.read_text(encoding="utf-8"))
                    if not isinstance(host, dict):
                        raise ValueError("host evidence must be an object")
                    convert = codex_spawn_parameters if args.runtime == "codex" else claude_role_parameters
                    result["launch_plan"] = convert(result["preference"], host)
    except (OSError, ValueError) as error:
        print(f"模型配置未加载：{error}", file=sys.stderr)
        return 1
    options = {"separators": (",", ":")} if args.compact else {"indent": 2}
    print(json.dumps(result, ensure_ascii=False, **options))
    return 0


if __name__ == "__main__":
    sys.exit(main())
