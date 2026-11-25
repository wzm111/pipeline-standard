# 按任务选择模型

用户只描述任务。调度员利用已有范围和验收判定任务类型、风险和复杂度，不为分类创建agent，不让用户填写参数。

## 优先级与保护

model/effort仍为角色基础方案，可选task_routes表示用户允许的分流。先按“本次 > 项目 > 本机 > 模板”选中整个角色，再在该角色内选择路线。项目固定角色没有task_routes时，不继承本机路由；明确inherit同样屏蔽低层方案。

常规路线仅在 risk=low、complexity=simple 时应用。developer 的 complex_implementation 路线用于明确 complexity=complex 且 risk=low 或 medium 的实现与诊断。风险为 high 时优先选 high_risk_implementation，独立验证选 tester 的 high_risk_verification，两条高风险路线均须明确 risk=high 且有对应配置才应用。风险或复杂度尚未判明时继续核对已有范围，未匹配的分类沿用基础模型。类型不认识、跨角色或配置非法时报错；未传任务上下文的旧调用保持原结果。

| 角色 | 内部任务类型 | 明确低风险简单任务 | 基础方案 |
| --- | --- | --- | --- |
| planner | routine_planning | Sol中：范围已定的简单拆解 | Astra中 |
| scope_guardian | routine_review | Sol中：常规范围核对 | Astra高 |
| developer | scoped_fix | Sol中：目标明确的小修复 | Sol中 |
| tester | ui_validation | Terra中：截图及界面操作验收 | Sol中 |
| releaser | release_summary | Terra低：已验证事实的摘要整理 | 保留原Sol方案 |

本仓库复杂实现路线使用 GPT-6.2 Astra 中强度，高风险实现与方案专家使用 GPT-6.2 Astra 高强度，独立高风险验证使用 GPT-6.2 Sol 高强度。普通 developer、tester 使用 GPT-6.2 Sol 中强度。项目和本机可各自配置，通用安装模板保留 inherit。Claude可独立配置宿主支持的型号，不套用Codex型号；未配置路线保持旧行为。这是选择策略，不是模型质量或提速已验证的结论。

保存 complex_implementation 偏好前，确认本机各宿主使用的 Pipeline 入口已支持该类型。旧版本解析器会拒绝含新类型的配置；回退入口版本时应同时恢复对应的模型配置。

## 分类边界

- 架构不明、跨系统、支付/权限/安全、数据一致性、迁移、公开接口和生产部署等不能因描述短、文件少就判为low/simple。缺信息使用unknown。
- scoped_fix必须有明确范围和回归；ui_validation只针对视觉/操作，代码回归与故障分析用基础tester。
- 跨模块约束、根因不明且需要多假设验证的复杂实现或诊断，选 complex_implementation；先核对风险，高风险任务选 high_risk_implementation。反复失败后的专家指导继续使用 expert-advice.md 的证据与次数预算，不通过改分类绕过 QA 上限。
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

遇到明确阻塞时才按[专家短诊断](expert-advice.md)选择现有planner/scope_guardian的有效配置；先核触发与次数，再解析实际需要的单一角色，不探测或并发试用所有高阶型号。此入口不改永久模型偏好，也不为使用高阶模型给所有任务新增审查阶段。

仅实际委派时应用路由；fast/standard直接执行仍用主会话模型，不为路由强制增加角色。已启动角色与follow-up保留原模型，不因文件变更自动切换；重新创建时才解析。宿主不返回实际模型时记录unknown。

保留请求/实际值、分类与routing原因，比较同类任务的通过率、漏检、返工和总完成时间。本地测试只证明选择与参数转换正确，不证明Terra更快或质量等价；不会隐式执行付费实验。

## 例行发布选择执行者

Codex 对已获授权且验收证据仍有效的例行提交、推送、GitHub Release 或既定部署，先确认候选、目标、入口固定，剩余工作只是执行已有步骤。涉及首次上线、环境变化、守卫修改、并行切流冲突或未知恢复分支时，按原流程完成风险判断，必要时使用现有专家。

使用观察到的主会话型号和强度，以及同一宿主的模型能力证据：

```text
resolve-models.py --runtime codex --role releaser --project <项目根>
  --routine-release --current-model <主会话型号> --current-effort <实际强度>
  --host-evidence <能力证据JSON> --compact
```

- 主会话已是 Sol 低或中时直接承接。主会话为 Astra 或 Sol 高及以上，且当前项目或本机配置有效的 Sol 低或中，并通过宿主预检时，返回 `delegate_releaser` 及原生派发参数。模型或权限证据不足时返回 `direct` 及原因，沿用原交付入口。
- 只有契约、用户授权和宿主允许接手这批具体动作时，才派发一个发布角色。已有发布角色的实际型号和强度与本次配置一致时可续用。交接只给授权边界、固定候选、目标、有效证据和必要验证，按角色派发边界记录宿主实际开始与返回。受管发布由持有运行归属的既有调度员调用，私有回执不传给子会话。无法在原权限内接手时由调度员执行。双方不得同时执行相同发布命令。
- 此入口使用发布角色的基础配置，`release_summary` 继续只用于整理文字。用户本轮指定模型时应用原临时覆盖入口。工具只给执行建议和预检参数；实际模型取宿主反馈，模型不可见就记未知。
