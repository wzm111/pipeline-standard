# 上下文差量复用

本接口由智能体维护，不要求用户配置。仅在首次实际委派、交接或原上下文确已丢失时使用；同一会话已读的规则直接沿用。

`context_cache.lookup(root, run_id, owner_token, "base", sources)`核验本项目的来源指纹及原run已注册的不可变摘要。base自动加入PIPELINE.md、AGENTS.md、CLAUDE.md（含不存在的文件以便新增时失效）；sources补充本批必需规范来源，调用方负责声明其它语义依赖。不要把整个仓库、日志或无关业务资料塞入基线。

- unchanged：仅返回reference和content_bytes，不输出摘要正文。原角色已读且上下文仍在时只发本批差量，不重新总结或读原文。
- refresh_required：返回sources和读取前fingerprint；读取所需原文、生成一次摘要后调用`save(root, run_id, owner_token, ticket, content)`。输入期间变化则拒绝保存，重新定位变更。不能在总结结束后补造“读取前”指纹。
- 新角色、会话压缩导致内容丢失或需要核对摘要时，显式`load(root, reference)`读取一次正文；该只读接口同时校验来源及报告。命中只减少重复输出和整理，仍会在本机读取文件计算指纹，不能把字节数宣称为节省token。

返回reference包括原run_id、项目内path及报告sha256，可跨同项目run引用。路径内容不可改写；新摘要生成新引用，来源或摘要被改均使旧缓存失效。摘要不替代项目规则：缺字段、发现冲突或原文要求复核时读对应来源，以最新契约为准。无Python继续使用base.md及来源指纹规则。

角色交接包仍是base/run/角色差量三部分：有缓存时以reference指向的content作为base，首次接收者用load；无缓存时兼容原base.md。不要同时复制一份等价base.md和完整摘要到prompt。每个角色是否已经加载由当前调度上下文判断，脚本不猜测宿主记忆状态。
