"""Read-only resource discovery anchored to the actual runner, never project cwd.

The supported distribution keeps skills/pipeline beside agents, templates,
hooks and init-project.sh. install.sh links the skill to that distribution;
resolving the runner first also handles those installations.
"""
from pathlib import Path


def discover(runner=None, *, project=None):
    runner = Path(runner or Path(__file__).with_name("pipeline_cli.py")).resolve()
    skill = runner.parent.parent
    distribution = skill.parent.parent
    files = {"skill": skill / "SKILL.md", "runner": runner,
             "release_runner": runner.with_name("release_checks.py"),
             "init_project": distribution / "init-project.sh",
             "project_template": distribution / "templates/PIPELINE.md",
             "single_agent_template": distribution / "templates/single-agent-pipeline.md",
             "hook": distribution / "hooks/pipeline-guard.sh"}
    for role in ("developer", "planner", "tester", "releaser", "scope-guardian"):
        files[role.replace("-", "_")] = distribution / f"agents/pipeline-{role}.md"
    available = {key: str(path.resolve()) for key, path in files.items() if path.is_file()}
    result = {"paths": available, "unavailable": [key for key in files if key not in available]}
    if project is not None:
        root = Path(project).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("project must be an existing directory")
        result["project_root"] = str(root)
    return result
