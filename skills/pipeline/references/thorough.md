# thorough 流程

使用run_state.py时下文state.md对应runs/<run_id>.json，不同时维护两份。收口前确认本run执行者停止，按归属清理标记；不删除未知活动锁。

1. 初始化 state.md，创建 planner。计划必须给任务数、批次数、历史校准 ETA、预算上限和拆分建议；默认一次只执行用户确认的一组批次。
2. 闸口 1 必须等待明确确认。随后创建 guardian 评审 plan，最多 2 轮；同一 guardian 留待终验。
3. 创建持久developer；首批冻结后创建持久qa，确有可独立完成的验收准备工作时才提前创建。进入测试前读 `qa-loop.md`，按批准批次执行；角色后续只接收context差量，并行须确认生成目录等资源无冲突。
4. 全部批次封版后，qa 只读 `qa/index.md`，运行终验层一次并写 `qa/final.md`。
5. 唤醒同一 guardian，只读 plan、qa/index.md、qa/final.md 与必要源码切片，写 acceptance.md；终验不过由用户决定回开发还是调整计划。
6. 使用同一releaser；完整链中尚未创建时才创建一次。按delivery.md读取精简验收与交付凭证，直接承接有效结果，不因提交/发布重新规划、重读所有PASS报告或重跑全量。人工模式给摘要和建议命令；自动模式按契约④与当前授权执行。
7. 闸口 2 展示 acceptance、release notes、retro 摘要和人工确认项；state 置 done，追加 metrics.tsv，清理 `.active`。
8. 续跑、预算、CONCERNS、通知和异常处理按需读 `operations.md`。
