# pipeline-standard

English · [简体中文](../README.md)

[![CI](https://github.com/wzm111/pipeline-standard/actions/workflows/test.yml/badge.svg)](https://github.com/wzm111/pipeline-standard/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](../LICENSE)

**A conversational development workflow for Claude Code and Codex: bounded scope, visible progress, and verifiable delivery evidence.**

Describe a task in your coding-agent session. Pipeline helps the agent define scope and acceptance, implement the change, run appropriate checks, and summarize the result. Small tasks stay in the current session; complex work uses the necessary roles. The agent maintains a project-specific `PIPELINE.md` contract.

- **Bounded scope:** unrelated findings are recorded instead of becoming extra development work.
- **Visible progress:** see the current action, last meaningful result, remaining steps, and blockers.
- **Evidence-aware checks:** reuse results only while command, working directory, inputs, environment, logs, and provenance remain valid.
- **Local diagnostics:** preview local run records without an automatic report-upload service.

Use it for feature work, fixes with meaningful acceptance checks, and development-to-release handoffs. For a one-line edit or a simple query, a direct agent request may be sufficient. No fixed speedup or token reduction is promised.

## Install the complete repository

Requirements: Git, Python 3, and an existing Claude Code or Codex installation. Repository CI runs on Ubuntu and macOS with Python 3.11. The installer uses Bash and symbolic links; native Windows installation is not claimed here.

```bash
git clone https://github.com/wzm111/pipeline-standard.git
cd pipeline-standard
bash install.sh
```

Install only one runtime with `bash install.sh --codex` or `bash install.sh --claude`. These commands install the current default branch. For a fixed release, clone with `--branch v3.13.0 --depth 1` instead.

Review the installer before running it. It creates skill links and local model defaults; for Claude Code it also links agents and hooks. Keep the checkout at its installed location. Existing model choices are preserved on reinstall. Hook activation for a project is part of project setup, not a promise that every host operation is intercepted.

This package depends on resources outside the skill folder. Copying only `SKILL.md` or `skills/pipeline/` is not a complete installation. See [distribution status](DISTRIBUTION.md) before advertising third-party one-command installation.

## Try a task

In a Codex project session:

```text
$pipeline Add search to the list. Verify empty results and clearing the search. Keep this task scoped to the affected list and tests.
```

In Claude Code, use `/pipeline` with the same request.

For a reproducible starting point, follow the [isolated Python walkthrough](TRY-PIPELINE.md#english). It uses synthetic files and the Python standard library, with no external account or production data.

Continue naturally:

| Goal | Example |
| --- | --- |
| See progress | `$pipeline Show completed, current, remaining steps and blockers.` |
| Resume | `$pipeline Continue the last task and reuse checks that are still valid.` |
| Inspect local records | `$pipeline Preview this project's local run report.` |
| Commit | `$pipeline Commit the accepted changes.` |

Commit, push, and release permissions are distinct. The workflow does not turn a commit request into permission to deploy or modify another project.

## What you can verify

The tools validate evidence at integrated entry points and retain failed checks. A rule acknowledgement does not prove that a model understood every instruction. Registered `running` state alone does not prove host health. A successful test does not prove deployment or downstream platform acceptance.

Progress scripts produce read-only summaries; the executing agent must actually use them in the conversation. Long host calls can delay visible updates. Claude hooks and Codex's workflow checks have different enforcement boundaries; neither is described as a complete sandbox.

Diagnostics are recorded under the project's ignored `tmp/pipeline/` directory. Preview output is restricted to technical metadata; raw logs and private ownership files are not public reporting artifacts. When an agent displays a preview in chat, that text is handled under the host's data policy. Local diagnostic code does not make the whole host offline.

## Releases and details

Current release: [v3.13.0 — readiness checks, reuse diagnostics, and truthful progress](releases/v3.13.0.md). The complete Chinese guide covers [project setup](../skills/pipeline/references/interaction.md), [check plans](../skills/pipeline/references/check-plan.md), [delivery](../skills/pipeline/references/delivery.md), and [local records](../skills/pipeline/references/audit.md).

Maintainers run `bash tests/run-all.sh` from the repository root. Local tests and CI are implementation evidence; actual project adoption and performance require separate measurements.

## Feedback

Use [GitHub Issues](https://github.com/wzm111/pipeline-standard/issues) for questions and reproducible failures. Include the runtime, operating system, version, expected behavior, and a minimal synthetic reproduction. Reports are voluntary. Do not paste private project logs, credentials, customer data, or the entire `tmp/pipeline/` directory.

## License

[MIT](../LICENSE).
