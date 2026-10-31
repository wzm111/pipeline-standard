# 浏览器脚本与样本交接

只在本需求实际使用浏览器验收时读取，不为纯代码任务新建浏览器、样本或角色。专用浏览器工具仍用原宿主执行；此模块提供本地交接和前置条件判断，不模拟点击或宣称已通过UI测试。

首次编写验收也先做前置探测，不要求先有成功recipe。等待页面正常加载、hydration及业务入口就绪后，用宿主当前状态短查目标控件，调用`probe(preconditions, snapshot)`判断页面/marker；missing或unknown就定位入口、选择器或准备状态，不直接进入30秒click盲等。1500ms只是目标短探测预算，不能替代真实页面加载等待；测试框架也可用等价的有界可见性断言。probe不写报告、不提供脚本、不生成验收PASS，recipe仍必须在真实验收后认证。保留失败证据，不能自动将FAIL改称夹具故障；布局、业务断言失败仍须修复复验。

当前会话直做、developer自测和standard也在实际操作点采用此规则，不等创建tester才执行。Playwright复用项目现有的页面就绪步骤后，可直接短查真实定位器：

```js
// 页面加载、hydration和打开目标入口的既有步骤已完成。
const target = page.getByTestId('实际观察到的目标标识');
await target.waitFor({ state: 'visible', timeout: 1500 });
if (!await target.isEnabled() || !await target.isEditable()) {
  throw new Error('目标尚未可填写，先核对页面状态');
}
await target.fill('合成验收数据');
// 继续原有业务断言及其合理等待；短查不代表业务已通过。
```

短查失败保留原异常，停止这条依赖目标的操作链，核对入口、选择器、状态和夹具后有界复验；不catch后继续fill/click，不把真实界面缺陷一概归为夹具问题。已有可靠宿主snapshot时直接判断，无需另开浏览器或重复截图；专用工具按其原生状态接口处理。

可见不等于可操作：点击目标还须enabled，填写目标另须editable；禁用的保存按钮先核对工作区是否处于编辑/有变更状态，不强制点击或靠增加超时放行。仅本次需要操作的控件声明`targets={"save":"click","name":"fill"}`，宿主新观察传`targets={"save":{"visible":true,"enabled":true},"name":{"visible":true,"enabled":true,"editable":true}}`；probe/preflight对false或unknown返回ready=false和缺少的状态。旧recipe未声明targets仍兼容，但实际调用方仍须核对当前目标可交互性；不为纯阅读目标机械增加这些字段。

改动涉及SSR、共享状态或缓存时，在耗时全量终验/构建前，先用一条代表性链路覆盖实际适用的生命周期：SSR首屏→hydration→真实客户端刷新/重取→缓存后重载，并轻量切换受影响的另一入口检查状态一致性。涉及第三方SDK时，也尽早核对实际初始化、回调时序和失败恢复；VM/mock通过与真实SDK兼容分开记录，使用已有可靠入口，不为快检额外发布或调用付费服务。缺陷尽早修复，再执行必要终验。纯静态/非浏览器改动不机械增加这些步骤，已有有效链路直接复用。

测试helper操作应用内单例、缓存或诊断状态时，先沿用页面实际使用的测试桥接/模块实例。Vite等开发环境可能把带`?t`的实际模块与bare import分成不同ESM实例；不要通过删query或猜路径另导一份状态。确需导入时按本次页面已观察的模块图使用相同URL，并用合成状态核对helper写入与页面读取一致、随后清理。该检查并入原夹具快检，不新增一轮浏览器验收。已修正的helper和样本随recipe交接，后续仅刷新失效部分；不为测试修改生产业务逻辑或增加全局暴露。运行时URL/query只用于当次定位，不作为凭据或业务数据落盘；recipe仍遵守下方origin/path边界。怀疑夹具问题时保留原失败，依据真实实例差异和原业务断言的复测结果再分类，不能仅凭猜测豁免FAIL。

1. 在原生浏览器调用前后按[evidence.md](evidence.md)使用observed_checks登记一次真实观察，保存带browser覆盖标签的检查结果；成功仍须有实际业务断言。scope须包含实际脚本、合成样本及相关依赖；保存真实日志和运行环境，不把客户数据、登录凭据放进样本。同一次返回的补充说明只引用原check，不再新建一条同源check；同内容的新执行使用新observation_id。
2. 调用`browser_handoff.record_recipe(root, run_id, owner_token, name, script=..., fixtures=[...], preconditions=..., validated_check=...)`。它核验成功证据和脚本/样本归属，保存不可变recipe并自动加入handoff_files。preconditions为origin（不含路径、凭据、query）、精确path、至少一个实际观察到的marker短标识；必要时带version。
3. 下一批或发布承接原recipe引用，不重写相同脚本或重新猜入口。先通过浏览器当前页面状态做一次短探测（缺控件探测预算1500ms；若有可靠的现成状态直接使用），向`preflight(root, current_run_id, reference, snapshot)`传入url、markers字典及可选version。只传真实新观察，不能拿上次snapshot冒充当前页面。query仅用于当次解析，不落盘。
4. ready=true后沿用脚本与样本执行本次需要的验收；ready=false只返回原因，不提供执行脚本入口。页面错位、marker缺失或unknown时先修正入口/前置条件，不启动默认30秒的盲等。正常页面加载或确实耗时的业务动作仍遵守项目所需等待，不能为了快而跳过。

preflight只读、无重试循环、不启动浏览器，acceptance始终not_run。脚本、样本、原报告或日志变化会拒绝沿用；无关新run不能读取旧recipe当作自动承接。通过delivery_from继承的recipe及原始指纹可持续经过提交、发布等后续阶段。Claude和Codex都在原生浏览器工具边界执行上述步骤；它不是宿主的硬拦截，调用方必须遵守ready判断。
