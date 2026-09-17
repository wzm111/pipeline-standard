# QA 批次循环

standard/thorough 共用；单批打回最多 3 轮。

## Artifact

- `tmp/pipeline/qa/index.md`：每批一行 `batch | gate | round | report | final-items`。
- `tmp/pipeline/qa/<batch>.md`：只保留当前轮完整结果；末尾历史表只保留 `round | gate | 失败编号 | 处理结论`，不得保留旧轮全文。
- `tmp/pipeline/qa/final.md`：全部批次后的一次终验层结果和去重人工确认项。

## 循环

1. developer 完成一批后一次性勾选该批 checkbox，只跑 context 标注的快速/定向检查并简报。
2. 调度员同时让 qa 测本批、developer 开下一批；qa 测试期间 developer 不改已交付文件，收到打回后再暂停当前批修复。
3. qa 只核本批核心验收和快速层。后续批次半成品导致的范围外报错只记录观察，不判 FAIL；跨批回归、重型命令和探索项写入 final-items。
4. `FAIL`：qa 直接把失败编号、复现、实际/预期发给 developer；修复后只复测失败项、影响面和受影响快速命令。
5. `CONCERNS`：暂停并读取 `operations.md` 的裁决规则；用户决定修复或豁免后再继续。
6. `PASS`：更新 index 并封版。每批只向用户发一行；表格快照仅用于等待、预算预警和异常。
7. 全部批次结束后，qa 只读 index 与 final-items，运行终验层一次写 final.md；不重读全部 PASS 批次正文。
