# 在独立小项目中试用 / Try an isolated task

[中文首页](../README.md) · [English overview](README.en.md)

此示例使用合成文件和 Python 标准库。它展示如何提出一个有边界、可验收的需求，不是实际模型执行录像，也不承诺完成时长。先按首页安装完整仓库；无需业务账号或生产数据。

## 建立示例项目 / Create the fixture

在 Bash 或 Zsh 中执行。目录由系统新建，不覆盖已有项目；保留终端显示的路径，以便在编码智能体中打开。

```bash
PIPELINE_DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pipeline-demo.XXXXXX")"
cd "$PIPELINE_DEMO_DIR"
git init -q
cat > normalizer.py <<'PY'
def normalize_label(value):
    return value.strip()
PY
cat > test_normalizer.py <<'PY'
import unittest
from normalizer import normalize_label


class LabelTests(unittest.TestCase):
    def test_trims_edges(self):
        self.assertEqual(normalize_label("  Demo  "), "Demo")


if __name__ == "__main__":
    unittest.main()
PY
python3 -m unittest -v
pwd
```

基线应通过 1 项测试。接着在 Claude Code 或 Codex 中打开打印出的目录，使用以下请求。首次接入可能生成 `PIPELINE.md` 和被忽略的本地记录；不要在本示例中授权提交、推送或发布。

## 中文请求

Codex：

```text
$pipeline 修改 normalize_label：把连续空白合并为一个空格，并去掉首尾空白；空字符串和纯空白返回空字符串，保留大小写及其他字符内容。只修改 normalizer.py 和直接相关测试，不新增依赖、不联网、不做额外功能。允许为此示例建立项目契约和本地运行记录。用 python3 -m unittest -v 验证，展示当前动作、结果、剩余步骤和阻塞。不要提交、推送或发布。
```

Claude Code 将 `$pipeline` 换成 `/pipeline`，其余请求保持相同。

检查实际结果：

- `"  Demo   Label  "` → `"Demo Label"`。
- `"Demo\tLabel\n"` → `"Demo Label"`。
- `""` 和 `" \t\n "` → `""`。
- `"MiXeD 中文"` → `"MiXeD 中文"`。
- 旧的首尾空白测试仍通过；测试断言未被删掉；没有新增依赖或范围外功能。

如需了解过程，可说“列出已完成、当前、剩余步骤和阻塞”。结束后可说“预览本项目的运行报告”，查看已有记录；记录缺失或字段未知时应如实提示，不补造历史。进度和报告依赖当前会话实际采用相应入口，不能把示例文字当成已执行结果。

## English

Run the setup block above, then open the printed directory in your coding agent. The baseline should pass one test. For Codex, send:

```text
$pipeline Update normalize_label to collapse consecutive whitespace to one space and trim the edges. Empty and whitespace-only input should return an empty string. Preserve case and all other characters. Change only normalizer.py and directly related tests; add no dependencies, network calls, or unrelated features. You may create this fixture's project contract and local run records. Verify with python3 -m unittest -v. Show the current action, result, remaining steps, and blockers. Do not commit, push, or publish.
```

For Claude Code, replace `$pipeline` with `/pipeline`. Verify the input/output examples listed above, retain the original test, and inspect the actual diff. This is a walkthrough, not a recorded model run or a timing benchmark.

Ask for a progress summary during execution and a local run-report preview afterward. Missing records must remain missing, and unknown fields must remain unknown. Previewing through chat may send the preview text to your agent's host under its data policy.

## 反馈与清理 / Feedback and cleanup

试用结果可自愿提交到 [GitHub Issues](https://github.com/wzm111/pipeline-standard/issues)：注明运行时、系统、版本、预期、实际结果和合成复现。不要上传整个运行目录、私有归属文件或原始业务日志。

完成后可在文件管理器删除该独立临时目录。删除会同时移除本示例的代码和本地运行记录，不会卸载仓库链接。
