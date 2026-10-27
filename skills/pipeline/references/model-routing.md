# 按任务选择模型

用户只描述任务。调度员利用已有范围和验收判定任务类型、风险和复杂度，不为分类创建agent，不让用户填写参数。

## 优先级与保护

model/effort仍为角色基础方案，可选task_routes表示用户允许的分流。先按“本次 > 项目 > 本机 > 模板”选中整个角色，再在该角色内选择路线。项目固定角色没有task_routes时，不继承本机路由；明确inherit同样屏蔽低层方案。

仅risk=low、complexity=simple、类型匹配且配置路线时应用。medium/high/unknown风险或moderate/complex/unknown复杂度均保留基础模型。类型不认识、跨角色或配置非法时报错；未传任务上下文的旧调用保持原结果。

| 角色 | 内部任务类型 | 明确低风险简单任务 | 基础方案 |
| --- | --- | --- | --- |
| planner | routine_planning | Sol中：范围已定的简单拆解 | Astra中 |
| scope_guardian | routine_review | Sol中：常规范围核对 | Astra高 |
| developer | scoped_fix | Terra中：目标明确的小修复 | Sol中 |
| tester | ui_validation | Terra中：截图及界面操作验收 | Sol中 |
| releaser | release_summary | Terra低：已验证事实的摘要整理 | 保留原Sol方案 |

具体型号是本次用户选择，保存在本机/项目配置，不写入通用安装模板。Claude可独立配置宿主支持的型号，不套用Codex型号；未配置路线保持旧行为。这是选择策略，不是模型质量或提速已验证的结论。

## 分类边界

- 架构不明、跨系统、支付/权限/安全、数据一致性、迁移、公开接口和生产部署等不能因描述短、文件少就判为low/simple。缺信息使用unknown。
- scoped_fix必须有明确范围和回归；ui_validation只针对视觉/操作，代码回归与故障分析用基础tester。
- release_summary仅整理已通过验收的事实，不包含部署判断、实际提交、回滚或异常处理；这些使用基础releaser并遵守权限。
- 用户明确指定本次模型时传完整临时角色覆盖，不能再叠加其他路线。

## 智能体接口

```text
resolve-models.py --runtime codex --project <项目根> --role tester
  --task-kind ui_validation --risk low --complexity simple --compact
```

返回有效preference和routing（是否应用、原因、分类、基础模型）；source/path仍指向获胜的角色配置。调度员在当前run记录风险判断依据。可加--host-evidence对最终选中型号做能力预检，模型失效/权限未知时不得静默改用基础型号或永久改偏好。

配置示例：

```json
{
  "model": "gpt-6.1-sol",
  "effort": "medium",
  "task_routes": {
    "ui_validation": {"model": "gpt-6.1-terra", "effort": "medium"}
  }
}
```

路由型号、强度和fallback整体替换，不混入基础强度/回退授权。路线不能嵌套，不能配置其他角色的任务类型。单独修改基础model时清除旧task_routes，除非同时明确提供新路线。

## 实际执行

仅实际委派时应用路由；fast/standard直接执行仍用主会话模型，不为路由强制增加角色。已启动角色与follow-up保留原模型，不因文件变更自动切换；重新创建时才解析。宿主不返回实际模型时记录unknown。

保留请求/实际值、分类与routing原因，比较同类任务的通过率、漏检、返工和总完成时间。本地测试只证明选择与参数转换正确，不证明Terra更快或质量等价；不会隐式执行付费实验。
