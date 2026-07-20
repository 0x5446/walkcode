# Deep Review 综合结论

**VERDICT**: NEEDS_FIX（全部 Warning，无 Critical；门禁通过，Warning 酌情修）
**轮次**：1 / 3
**类型**：code

> 范围：release/v0.14.11 相对 main 的 diff（PR #74）——飞书图片附件 .img 后缀修复（magic bytes 嗅探）+ test_release_scripts 环境隔离
> Review engine：codex codex-cli 0.144.5（host: claude; engine_source: auto）
> Cursor：disabled（composer-2.5 smoke test failed）
> 维度：8 个 codex 并行（correctness errors security concurrency data observability design tests；全部成功）
> Phase 2 验证：7 条已派（另 1 条高共识高自信跳过）；结果 7 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 04dfa1828f55dc584210e1278b3a74c7eaf0433a
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-04dfa18-1784570428.Rb5n
> 规模：161 行 / 5 文件
> plan-only 模式：本 skill 运行不动文件；修复由主流程决定

## 🔴🔴 顶级必修（本次 diff 引入）

### 1. [Warning] src/walkcode/channel_native/__init__.py:5522-5545 (Symbol: _sniff_image_type)
> **一句话**：部分容器格式图片会被误判成另一种格式，后缀和类型都写错。

- **Category**: Bug / DataIntegrity
- **Confidence**: dim-correctness 0.99, dim-data 0.96
- **来源**: correctness + data（≥2 维度命中）
- **问题**: `ftyp` 解析只读 major brand，且把 HEIF 通用品牌 `mif1` 固定映射为 `.heic`/`image/heic`。`mif1` 只说明是 HEIF 容器，不代表 HEIC 编码；主品牌 `mif1` + 兼容品牌 `avif` 的 AVIF 图会被错标 `.heic`。动画 AVIF（`avis`）不识别，退回 `.img`，原故障复现。
- **修复**: 解析 ftyp box 的兼容品牌列表；`avif`/`avis` → `.avif`；`heic`/`heix` → `.heic`；`mif1`/`msf1` 单独出现时扫兼容品牌，找不到明确品牌则返回 None（不猜）。
- **回证**: SKIPPED_HIGH_CONFIDENCE（≥2 维度且 Confidence ≥ 0.9）

## 🔴 高置信必修（单维度 + 回证 VERIFIED，本次 diff 引入）

### 2. [Warning] src/walkcode/channel_native/__init__.py:5272-5280 (Symbol: download_attachment) — design
> **一句话**：后缀和类型分两处判断，文件名与内容不一致时会产生自相矛盾的附件信息。

- **问题**: `_download_suffix` 优先信 `file_name` 后缀，`download_attachment` 又对同一内容二次嗅探只改写 mime。`photo.jpeg` + PNG 字节 → `.jpeg` 路径 + `image/png` 元数据。嗅探被调用两次，事实源分叉。
- **修复**: 只嗅探一次；`file_name` 有后缀时后缀与 mime 都不用嗅探结果；无文件名时后缀与 mime 统一来自同一次嗅探。
- **回证**: VERIFIED @ 5272-5280, 5492-5503

### 3. [Warning] tests/test_channel_native_lark.py:357-421 (Symbol: LarkAdapterTests) — tests
> **一句话**：图片嗅探测试丢掉了类型断言，只对一种格式做了完整校验。

- **问题**: `for content, suffix, _ in self._IMAGE_SAMPLES` 丢弃 mime 列；除 PNG 集成用例外其他 9 种格式 mime 写错测试仍绿。缺截短签名、最短长度、未知 ftyp 品牌负例。
- **修复**: 直接断言 `_sniff_image_type(content) == (suffix, mime)`；补截短/边界/未知品牌负例。
- **回证**: VERIFIED @ 357-421

### 4. [Warning] tests/test_release_scripts.py:118-131 (Symbol: _ScriptGateBase.setUp) — tests
> **一句话**：环境变量清理逻辑没有自己的回归测试，将来被误删不会被发现。

- **问题**: setUp 内联清理 `FEISHU_*`/`WALKCODE_*`，无测试注入宿主变量验证清理生效；常规 CI 环境无这些变量，误删清理逻辑测试仍全绿。
- **修复**: 抽出环境清理纯函数并对其写注入断言测试。
- **回证**: VERIFIED @ 118-131（WALKCODE_ 缺口由本次 diff 引入）

### 5. [Warning] src/walkcode/channel_native/__init__.py:5501-5507 (Symbol: _download_suffix) — observability
> **一句话**：图片识别失败会无声退回旧行为，问题复发时没有日志可查。

- **问题**: 嗅探失败静默回退 `.img`，无 `_log_degrade`，不记录 message_id、内容长度、回退原因。
- **修复**: 嗅探失败且为无文件名图片时打 `_log_degrade("lark_image_sniff_failed", ...)`。
- **回证**: VERIFIED @ 5501-5507（回退分支为 main 已有行为，本次 diff 缩小了其触达面；日志缺口顺手补上）

## 🟡 存量问题（VERIFIED 但 pre-existing，不在本 PR 修，记录残留）

### 6. [Warning] download_attachment 错误处理 — errors
空 content 静默转空字节、下载异常时消息已出队不重试、临时文件写失败不清理。回证：VERIFIED，pre-existing，本次 diff 未加剧。

### 7. [Warning] 附件目录多实例共享 — security
`walkcode-attachments` 目录全实例共用且整体加入 add_dirs；本次改动让图片变得可读，客观上扩大了同用户多实例间的可读面。回证：VERIFIED，目录架构 pre-existing。需要独立 issue 做实例级附件隔离。

### 8. [Warning] 升级前持久化的 .img 附件不迁移 — data (VersionSkew)
main 上已持久化的待接管 AttachmentRef 恢复后不再经过下载嗅探，旧 `.img` 路径继续送达 agent。回证：VERIFIED，pre-existing（旧文件本来就是 .img，本次改动只影响新下载）。

## ❌ 已驳回

无。

## 维度元信息

| 来源 | VERDICT | issues | exit | 备注 |
|---|---|---|---|---|
| dim-correctness | NEEDS_FIX | 1 | 0 | — |
| dim-errors | NEEDS_FIX | 1 | 0 | 回证判 pre-existing |
| dim-security | NEEDS_FIX | 1 | 0 | 回证判 pre-existing（加剧论点成立） |
| dim-concurrency | SAFE | 0 | 0 | — |
| dim-data | NEEDS_FIX | 2 | 0 | #1 与 correctness 配对 |
| dim-observability | NEEDS_FIX | 1 | 0 | — |
| dim-design | NEEDS_FIX | 1 | 0 | — |
| dim-tests | NEEDS_FIX | 2 | 0 | — |
| cursor-holistic | (unavailable) | — | — | smoke test failed |

## 处置决定

- 门禁判定：**无 Critical，门禁通过**（walkcode-release 门禁只阻断 Critical）。
- 本次 diff 引入/可顺手修的 5 条（#1-#5）：**在 PR #74 内修复**。
- 存量 3 条（#6-#8）：记录为残留，不在本 PR 扩 scope；#7 建议开独立 issue。

## 原始报告

- 各维度：`$RUN_DIR/dim-{name}.md`
- 各回证：`$RUN_DIR/verify-{N}.md`
- 元信息：`$RUN_DIR/meta.txt`、`$RUN_DIR/run.json`
