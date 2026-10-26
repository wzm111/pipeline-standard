#!/usr/bin/env python3
"""Read-only role preference resolver. Never starts models or saves files."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_config import read_document, read_layer, resolve  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("codex", "claude"), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--personal", type=Path, default=Path.home() / ".config/pipeline-standard/models.json")
    parser.add_argument("--temporary", help="session-only role object as JSON; never persisted")
    parser.add_argument("--read", action="store_true", help="read project document and revision instead of resolving")
    args = parser.parse_args()
    try:
        project = args.project / "pipeline.models.json"
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
    except (OSError, ValueError) as error:
        print(f"模型配置未加载：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
