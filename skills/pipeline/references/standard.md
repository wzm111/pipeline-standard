# standard 流程

## 计划与闸口

1. 初始化 state.md(status/stage/batches/pending/next)，创建 planner，传 `context/base.md` 与 `context/planner.md`。
2. planner 发现实质歧义时只返回带建议答案的问题；无歧义时写 `plan.md`。任务 >4、预计开发 >40 分钟或天然分层时按依赖/文件归属拆为 2–4 批，通常每批 2–5 个任务；没有并行或提前 QA 收益时保持单批。
3. plan 只记录任务、验收、涉及文件、来源定位、快速/终验测试层和基于 metrics 的 ETA，不复制基线正文。
4. 用户给 `--auto` 或契约允许低风险自动进入时，发送计划简报后继续；新增依赖、禁区、公开接口/数据、权限/支付/安全、范围不确定时必须等待闸口 1。
5. 涉及 >5 个文件或上述风险信号时创建 guardian 评审 plan，最多 2 轮；保留同一 guardian 供终验复用。无风险时不创建。

## 开发、QA 与收口

1. 创建一个持久 developer；进入测试前读 `qa-loop.md`，创建一个持久 qa。后续批次只用 follow-up 发送 context 差量。
2. qa 完成所有批次后只读 `qa/index.md`，运行终验层一次并写 `qa/final.md`。若已有 guardian，唤醒它只读 plan、qa/index.md、qa/final.md 做最终范围检查；否则调度员据这三份精简 artifact 收口。
3. 人工上线模式由调度员直接输出变更摘要、提交建议和人工待办，不创建 releaser/release-notes。自动提交模式才创建 releaser，并严格按 PIPELINE.md ④ 节执行。
4. 把既有数据写入简短 retro，state 置 done，追加 metrics.tsv，展示 qa/final.md 中的人工确认项并清理 `.active`。
5. 终验失败、预算预警、CONCERNS 或异常时读 `operations.md`；正常 PASS 不加载。
