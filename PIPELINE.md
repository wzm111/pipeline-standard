# pipeline-standard 项目契约

## ① 需求基线

docs/ROADMAP.md；本次用户授权保存所议模型配置、逐期开发并各自中文提交，持续检查流程开销。

## ② 架构与规范

Python 标准库、Shell、Markdown；保持 Claude/Codex 双端兼容。配置与验证脚本不得隐式安装模型或调用付费接口。修改 skill 使用 skill-creator 指导，运行时事实按当前工具能力及官方文档确认。保护已有用户改动，禁止把未验证能力报告为已验收。

## ③ 测试命令

在项目根执行受影响 tests/test-*.py（PYTHONDONTWRITEBYTECODE=1），以及相关 Shell 冒烟测试。终验运行全部 Python 测试、bash tests/test-codex-smoke.sh、bash -n install.sh init-project.sh hooks/pipeline-guard.sh、git diff --check。真实模型/外部项目试点与本地测试分开记录，不可相互替代。

## ④ 上线方式

本次用户明确授权每期独立中文提交到当前分支；由调度员执行精确文件列表的 add/commit，无需增加仅负责提交的 agent。未授权自动 push、tag、发布或外部项目变更。每期提交包括实际验收状态，不伪造完成。

## ⑤ 禁区与边界

允许修改本仓库 skills/、agents/、hooks/、tests/、templates/、docs/、tools/、README.md、PIPELINE.md、pipeline.models.json、install.sh、init-project.sh、.gitignore、.github/ 及 tmp/pipeline/。不得修改用户全局配置、密钥、其他项目与已有 git 历史。临时结果放 tmp/pipeline/，测试夹具放系统临时目录并自清理。

<!-- pipeline-guard:allow-write
skills/
agents/
hooks/
tests/
templates/
docs/
tools/
README.md
PIPELINE.md
pipeline.models.json
install.sh
init-project.sh
.gitignore
.github/
tmp/
-->

## ⑥ 执行效率策略

按阶段拆成内聚批次，默认 standard；本路线图和执行授权已确认，不重复要求确认相同范围。高风险变更独立审查。模型按 pipeline.models.json 选取，无授权回退。主会话模型不能由该文件切换。只记录真实耗时，token 不可见留空。测试成功证据在相关内容未变时复用。
