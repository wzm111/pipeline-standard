# thorough 流程

1. 初始化 state.md，创建 planner。计划必须给任务数、批次数、历史校准 ETA、预算上限和拆分建议；默认一次只执行用户确认的一组批次。
2. 闸口 1 必须等待明确确认。随后创建 guardian 评审 plan，最多 2 轮；同一 guardian 留待终验。
3. 创建持久 developer 与 qa；进入测试前读 `qa-loop.md`。按批准批次执行，角色后续只接收 context 差量。
4. 全部批次封版后，qa 只读 `qa/index.md`，运行终验层一次并写 `qa/final.md`。
5. 唤醒同一 guardian，只读 plan、qa/index.md、qa/final.md 与必要源码切片，写 acceptance.md；终验不过由用户决定回开发还是调整计划。
6. 创建 releaser。人工模式产出 release-notes.md 和建议命令；自动提交模式按 PIPELINE.md ④ 节执行并记录结果。默认禁止任何 git 写操作。
7. 闸口 2 展示 acceptance、release notes、retro 摘要和人工确认项；state 置 done，追加 metrics.tsv，清理 `.active`。
8. 续跑、预算、CONCERNS、通知和异常处理按需读 `operations.md`。
