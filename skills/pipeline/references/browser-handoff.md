# 浏览器脚本与样本交接

只在本需求实际使用浏览器验收时读取，不为纯代码任务新建浏览器、样本或角色。专用浏览器工具仍用原宿主执行；此模块提供本地交接和前置条件判断，不模拟点击或宣称已通过UI测试。

首次编写验收也先做前置探测，不要求先有成功recipe。等待页面正常加载、hydration及业务入口就绪后，用宿主当前状态短查目标控件，调用`probe(preconditions, snapshot)`判断页面/marker；missing或unknown就定位入口、选择器或准备状态，不直接进入30秒click盲等。1500ms只是目标短探测预算，不能替代真实页面加载等待；测试框架也可用等价的有界可见性断言。probe不写报告、不提供脚本、不生成验收PASS，recipe仍必须在真实验收后认证。保留失败证据，不能自动将FAIL改称夹具故障；布局、业务断言失败仍须修复复验。

1. 完成一次真实验收后，用run_evidence记录带browser覆盖标签的成功检查。scope须包含实际脚本、合成样本及相关依赖；保存真实日志和运行环境，不把客户数据、登录凭据放进样本。
2. 调用`browser_handoff.record_recipe(root, run_id, owner_token, name, script=..., fixtures=[...], preconditions=..., validated_check=...)`。它核验成功证据和脚本/样本归属，保存不可变recipe并自动加入handoff_files。preconditions为origin（不含路径、凭据、query）、精确path、至少一个实际观察到的marker短标识；必要时带version。
3. 下一批或发布承接原recipe引用，不重写相同脚本或重新猜入口。先通过浏览器当前页面状态做一次短探测（缺控件探测预算1500ms；若有可靠的现成状态直接使用），向`preflight(root, current_run_id, reference, snapshot)`传入url、markers字典及可选version。只传真实新观察，不能拿上次snapshot冒充当前页面。query仅用于当次解析，不落盘。
4. ready=true后沿用脚本与样本执行本次需要的验收；ready=false只返回原因，不提供执行脚本入口。页面错位、marker缺失或unknown时先修正入口/前置条件，不启动默认30秒的盲等。正常页面加载或确实耗时的业务动作仍遵守项目所需等待，不能为了快而跳过。

preflight只读、无重试循环、不启动浏览器，acceptance始终not_run。脚本、样本、原报告或日志变化会拒绝沿用；无关新run不能读取旧recipe当作自动承接。通过delivery_from继承的recipe及原始指纹可持续经过提交、发布等后续阶段。Claude和Codex都在原生浏览器工具边界执行上述步骤；它不是宿主的硬拦截，调用方必须遵守ready判断。
