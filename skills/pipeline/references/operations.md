# 运行、裁决与恢复

只在续跑、异常、CONCERNS、预算预警或契约启用通知时读取。

## 续跑

- state.md 非 done 时向用户确认续跑或新开；新开把旧 state 改名留档。state 只保留 status/stage/batches/pending/next。
- 角色失联或超时只重试一次；仍失败时记录现有 artifact 和下一步，state 置 aborted，报告用户并删除 `.active`。

## 裁决分片

- `rulings/index.md` 每条只写：`signature | requirement-or-files | decision | run_id | detail-path`。
- 详细理由写 `rulings/<run-id>.md`。signature 由稳定的问题类型 + 需求编号/文件范围组成，不含日期或轮次。
- 启动/续跑只搜索 index；仅命中当前范围时读取对应 detail。CONCERNS 裁决后同时更新本 run detail 和 index，不读其他历史。

## 通知和预算

- 契约声明 webhook 且环境变量存在时，只在等待闸口、CONCERNS、异常/完成时发送项目名、阶段和待办；未设置静默跳过。
- 预算约 80% 时更新 state，报告已完成、剩余和收口方案，等待继续/收口裁决。

任何完成、失败或中止路径都必须清理 `.active`；已有 state 时同步最终状态。
