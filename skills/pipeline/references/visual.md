# UI 设计与现有视觉基线

实际改动用户可见页面、组件、样式或交互时读取；纯后端、文档或仅提到 UI 的任务不触发。fast、直做与委派沿用同一规则，不因 UI 标签自动升级档位或增加设计/QA 角色。

## 先沿用项目，再补设计判断

实现前查看本次受影响页面/组件、现有 tokens/样式变量、组件库及已批准的设计稿或界面。已有可访问的应用以实际页面作为参照；尚不可运行时先用现有代码/设计资料推进，并保留待看实际界面的缺口。只读相关切片，不扫描全站或要求用户先交截图。

优先延续项目的字体、颜色、间距、密度、圆角与交互习惯。明确本次目标、受影响状态/视口及哪些现有做法继续沿用；用户已要求改版时可在授权范围内调整。没有完整设计稿不等于可以另建风格体系、换全局 tokens 或重做无关页面；复用现有组件能解决时不引入新组件库。

根据当前会话实际可用的 skills 及其适用范围，自动选择并读取合适的设计 skill；如存在且适用，优先使用 `frontend-design-director` 或其带插件前缀的同名 skill。它是设计/实现指导，不等于创建独立角色。项目显式禁用、白名单、依赖及写入边界仍优先；契约未写具体 skill 名本身不应阻止使用本次 UI 任务已授权且可用的设计能力。需要在正常接入中记录时由智能体补充实际选择及范围，不要求用户配置 skill。

只使用已提供的实际入口，不猜安装目录或修改已安装 skill。不可用、调用失败或不适用于当前任务时，简报原因并按下方内置检查继续；不为普通 UI 任务全局安装、外部 clone、调用额外付费服务或更换项目设计系统。

## 最小设计与自查

围绕本次目标核对以下实际适用项，无需另写设计文档或填评分表：

- 信息层级是否清楚，主要操作、提示与当前状态是否容易区分。
- 排版、对齐、间距和密度是否与邻近已批准界面一致，长文本与本次目标视口是否溢出或遮挡。
- 真实涉及的空态、加载、错误、禁用和成功状态是否准确；交互焦点、键盘操作、对比度及触控目标是否可用。
- 修改是否解决原工作流问题，是否给无关页面或全局样式带来变化。

设计 skill 不可用时也执行这些最小检查；不虚构审美分数、固定缺陷数量或强制改动轮数。发现本次范围内的实际问题就修正；无关问题简记，扩大范围仍遵守原授权。

实际视觉判断必须看目标表面：按[浏览器交接](browser-handoff.md)等待加载、hydration 与所需状态就绪，查看真实页面或实际截图，核对视口、状态和版本。已有有效观察可直接沿用，不机械重复开浏览器或截图。仅有构建、单测、DOM/CSS 或 HTTP 成功不能宣称视觉通过；截图存在也不等于已经看过。缺浏览器、截图或所需状态时明确写视觉待验及缺什么，继续可独立完成的工作，不要求用户截图来替代智能体本可执行的验证，也不安静标 PASS。

当前规则提供设计路由和人工观察纪律，不声称已机器证明宿主截图、阅读或审美质量。交付按实际观察说明改了什么、看过什么和剩余缺口；不能把设计 skill 的建议当作真实界面验收结果。

## 本地截图与查看记录

实际 UI 任务进入验收前，由执行者用 visual-declare 明确本任务所需目标、视口、状态、版本和完整 scope，再登记真实截图、查看与实际复核；不要等 QA 收尾才决定是否采用。沿用本 run 私有 receipt；目标用无凭据短标识，完整 scope 包含实际页面/组件、样式、样本和运行器依赖，viewport 为 CSS 宽高整数，state 为实际页面状态短标识，version 为实际候选/版本标识，不保存 URL query 或原始业务输出。截图使用合成数据或已获授权的安全页面，不能含凭据或客户数据；模块不自动识别或脱敏图片。机器只验证显式声明，不宣称自动识别所有未声明或未调用入口的 UI；非 UI 任务不造视觉要求。

1. `visual-declare --receipt <receipt> --name checkout --json '{"scope":["<真实依赖>"],"target":"checkout","viewport":{"width":1280,"height":720},"state":"ready","version":"v1.2.0"}'` 返回本任务 requirement 的实际 `report` 引用。
2. 在真实宿主截图调用前执行 `visual-begin --receipt <receipt> --requirement <返回引用> --name capture-r1`；默认 JPEG，也可 `--format png`。只有 `created:true` 才执行一次宿主调用，把原生截图字节写到返回的 `image_path`（相对项目根，本 run 的 ignored images 目录）；重复 ID 不授权再截图。不另启浏览器引擎、不通过改后缀伪装格式。
3. 宿主实际返回/确认停止后执行 `visual-finish --receipt <receipt> --requirement <同一引用> --name capture-r1 --host-stopped --stop-evidence '<实际返回依据>' --json '{"target":"checkout","viewport":{"width":1280,"height":720},"state":"ready","version":"v1.2.0"}'`。参数必须来自当前实际观察并与声明相符。入口沿用 observed_checks 的真实起止、资源归属及输入稳定性；JPEG/PNG 按字节检查有界文件结构、尺寸和摘要，不是完整像素解码或页面来源认证。失败保留原 ticket/结果；需要关闭失败调用时按原 observe-finish 记录真实非零/异常，不能补写成功或重复调用原 ID。
4. 实际打开/看过返回 capture 里的图片后，才执行 `visual-view --receipt <receipt> --capture <visual-finish返回report> --name view-r1 --viewed`。它只记录调用方声明，不证明宿主读取或审美质量；没有实际查看不可传 viewed。
5. `visual-inspect --run-id <当前run> --requirement <实际引用>` 只读返回 capture_pending/view_pending/view_declared/stale_or_unconfirmed，复核当前任务、源码与图片绑定；不会返回图片内容、凭据或审美 PASS。跨 run/旧 task、缺图、图/源码变化不能作为当前完整证据。

intent=visual 专用于此观察，与普通 test/check 验收独立；不能放入 argv 验收计划或复用为正式测试通过。managed QA 的 single/plan/release 在原门禁后核对本任务已声明要求：缺截图、未查看、未复核、源/图/链失效及 FAIL/CONCERNS 保留 QA 的 CONCERNS，finish 复核后拒绝收口；不会用测试数量替代视觉判断，也不替代真实业务断言或交付授权。

## 发现问题、修改与复核

小范围、低风险 UI 可由调用方自查，声明省略 review 或写 `"review":"self"`。大范围改版、重要页面或实质风险使用 `"review":"independent"`，复用本任务已有 tester 做独立复核，不为简单 UI 自动增加角色。风险由执行者据目标、影响面和项目规则判断，机器不从截图打分或猜业务重要性；项目明确要求独立复核时不能以 self 规避。

实际看过当前图后，用 `visual-review --receipt <receipt> --capture <实际capture引用> --view <实际view引用> --name review-r1 --json '{"gate":"PASS","findings":[]}'` 记录真实判断。无实际问题可一次结束，不凑缺陷数或轮数。真实发现的问题用 `CONCERNS` 或 `FAIL`，findings 仅列本范围内未解决事项的稳定短 id 和非敏感 summary，例如 `{"gate":"CONCERNS","findings":[{"id":"action-overlap","summary":"目标视口主要操作与相邻文本重叠"}]}`。PASS 不可仍有未解决事项；机器只验证记录一致性，审美/可用性判断仍来自实际观察者。

独立复核由已有 accepted 的当前任务 tester 执行：其实际看图后，在 visual-view 与 visual-review 都传同一个 `--reviewer-dispatch <真实tester派发ID>`。入口核验角色派发已被宿主接受、当前任务归属、观察者一致及已知实现者/审查者身份不相同；随意字符串、未接受派发、developer 自称 tester 或沿用实现者的 viewed 声明均不满足。实际模型或宿主身份未知仍保持未知，派发记录与 viewed 声明都不证明任意宿主行为或审美质量。

发现本范围内问题后修正真实源码/状态，再以新 name 执行 begin → 宿主重新截图 → finish → 实际重新看图 → view → review；源码、图片或查看记录变化时，旧判断不能当当前判断。新 review 自动引用上一次不可变 review，未解决问题继续保留相同 id；确已解决的 id 放 `"resolved":["action-overlap"]`。解决旧问题必须有新 capture 与新 view，不能在同一旧截图上把 FAIL/CONCERNS 改成 PASS，也不能静默丢掉原问题。不要以复制旧图片或修改旧报告代替真实重拍重看；原失败和历史引用始终保留。

visual-inspect 增加 `review:"pending"|"PASS"|"CONCERNS"|"FAIL"`：后三者仅为绑定当前 capture/view 的调用方审查结论，status 为 review_declared，并非自动审美认证。新截图但尚未复核时保持 review pending；新 pending/失败捕获、失效源码/图片/查看或被改写的历史引用都不能复活旧 PASS。修复循环沿用原 QA 打回/停止上限，不另增固定轮数；范围外问题只简记，需要扩展时遵守原授权。

## 声明更新、交付承接与可见结果

同一目标确需更新 version、viewport 或 state 时，新的 visual-declare 可带 `"supersedes":"<实际旧requirement引用>"`。必须保留同目标、原 scope 和独立复核要求，旧引用不可环/分叉；旧 finding 随链继承，修正后重新截图、查看并在 review 中明确 resolved。改名另建声明不删除本 task 的旧要求；换 task 也不能静默丢掉旧 task 未解决 UI 交付。已通过的旧任务不为新的无关非 UI 任务自动增加视觉要求。

显式 delivery_from 沿用既有交付收据时，视觉 requirement/capture/view/review 的不可变来源引用一并保留。真正未变的完整链可复用，不机械重拍；当前源码、图片或声明候选不一致时保留待复核，通过上述明确替代和重新拍/看/审完成更新。已知 UI 来源的承接失败不能让视觉缺口消失；无 UI 的历史记录不追溯补要求。验收单独保存图、源码、引用完整性，不能把继承或审美判断说成新一次宿主执行。policy 快照只增加 visual.md 这个固定来源；ack 仍仅为调用方声明，不证明模型读懂/加载了说明。

最终回复必须展示实际看过的目标图片（宿主原生图片或可用的本地绝对路径 Markdown 预览），说明覆盖的视口/状态/版本、真实修改和仍未验证的限制；不能只给构建/测试数量或藏在内部 report 后。finish 成功且有视觉交付时返回 visual 的本地图片路径与覆盖信息，智能体据此展示；不上传图片、不输出凭据/客户原始数据。缺捕获或宿主查看能力时明确待验、所缺条件及可继续的独立工作，不伪称视觉通过。
