#!/usr/bin/env python3
"""Read-only role preference resolver. Availability is checked by the runtime."""
import argparse
import json
from pathlib import Path
import sys

ROLES = {"developer", "planner", "scope_guardian", "tester", "releaser"}


def read_layer(path, runtime):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError(f"{path}: unsupported schema_version")
    runtimes = data.get("runtimes", {})
    if not isinstance(runtimes, dict):
        raise ValueError(f"{path}: runtimes must be an object")
    roles = runtimes.get(runtime, {})
    if not isinstance(roles, dict):
        raise ValueError(f"{path}: {runtime} must be an object")
    for role, config in roles.items():
        if role not in ROLES:
            raise ValueError(f"{path}: unknown role {role}")
        if not isinstance(config, dict):
            raise ValueError(f"{path}: {role} must be an object")
        model = config.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{path}: {role} requires model")
        effort = config.get("effort")
        if "effort" in config and (not isinstance(effort, str) or not effort.strip() or model == "inherit"):
            raise ValueError(f"{path}: invalid effort for {role}")
        if "fallback" in config and config["fallback"] != "inherit":
            raise ValueError(f"{path}: fallback must be inherit when supplied")
    return roles


def resolve(runtime, personal, project):
    result = {}
    for source, path in (("personal", personal), ("project", project)):
        for role, config in read_layer(path, runtime).items():
            result[role] = {"preference": config, "source": source, "path": str(path)}
    return {"runtime": runtime, "roles": result, "unconfigured": "runtime_default"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("codex", "claude"), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--personal", type=Path, default=Path.home() / ".config/pipeline-standard/models.json")
    args = parser.parse_args()
    try:
        result = resolve(args.runtime, args.personal, args.project / "pipeline.models.json")
    except (OSError, ValueError) as error:
        print(f"模型配置未加载：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
