# ADR 0055: 回合中段叙述上进度卡——💬 行随工具时间线就地更新

Date: 2026-07-19

Status: Accepted; implemented

## Context

Agent 在工具调用之间输出的叙述文本（"我现在要干嘛"）是长回合里最能回答
"它还活着吗、在干什么"的信号。用户当天两次以为会话死了，都是长回合频道
静默造成的。此前两条镜像路径对这类文本的处理都不对：

- **TUI 镜像**：hook 载荷根本不含中段叙述（Stop 只带 last_assistant_message，
  Pre/PostToolUse 只带工具信息）——彻底丢失。
- **headless**：并没有如直觉认为的"被丢弃"——`_convert_sdk_message` 把
  同消息的文本提成 TURN_DELTA **追加在工具事件之后**：乱序气泡刷屏，还把
  工具 burst 卡切碎（每条叙述 seal 一次）。

用户拍板的形态："做成卡片，然后不断更新"。

## Decision

中段叙述统一汇入**现有的工具进度 burst 卡**，渲染为 💬 行，与工具行按
时间线交错；永不发气泡、永不 seal burst。回合最终文本照旧走正式消息。

**headless 路径**：新事件 `TURN_NARRATION`。转换器规则改为——文本与工具
块同消息时，插到事件列表**最前**（内容顺序即叙述先于工具）作 narration；
纯文本消息维持 TURN_DELTA 气泡（通常是回合末文本）。事件泵把 narration
视图直接 upsert 进 burst 卡后 continue。

**TUI 路径**：hook 载荷带 `transcript_path`。runtime 维护每会话
`(path, offset)` 游标：

- 首次见到（或文件更换/缩短）**快进到 EOF 不外发**——严禁把历史回放进频道；
- 每个 tool hook 先排水增量：只消费完整 JSONL 行（残尾留给下次）、过滤
  `isSidechain`、非 assistant、非 text 块，提取出的叙述在工具行之前 upsert；
- `stop` / `user-prompt-submit` 只推进游标不外发——回合末文本已经以气泡
  发出，不能在卡上重复一份。

**渲染**：lark `_tool_progress_line` 对 `{"kind":"narration"}` 条目渲染
`💬 文本`（单行 300 字截断；入卡状态 600 字截断）；卡片颜色只看工具行
（叙述行无状态，不得把全绿 burst 压成灰）；超过 30 行折叠头部（lark 卡有
体积上限，marathon burst 不能把 patch 调用直接打挂）。telegram 文本渲染
用 `> 引用` 行。

## Consequences

- 长回合里频道不再静默：burst 卡随"叙述→工具→叙述→工具"实时就地更新，
  零新增气泡。
- headless 频道会话的乱序叙述气泡消失（行为变化：这些文本从气泡改进卡）。
- 已知残留：TUI 路径下，最后一个工具之后、回合末文本之前如果还有独立的
  叙述消息，会被 stop 的游标快进吞掉（罕见：模型极少连续输出两条相邻
  文本消息）；codex 的 reasoning/中段消息本版未处理。
- 叙述行随 burst seal 一起丢弃（transient，不持久化）。

## Revision（发版前 deep-review 采纳，同版修复）

两维审查报 1 High + 5 Medium + 2 Low，全部采纳：

1. **High——游标必须绑定捕获时刻**：排水延迟（defer 队列 1s 节拍或积压）
   下，"处理时读到 EOF"会把 hook 之后写入的回合末文本提成叙述行（随后
   Stop 又发同一文本气泡=重复），或让 stop 快进吞掉下一回合开头的叙述。
   修复：hook 捕获点（CLI 入口/进程内 ingress）盖 `_walkcode_transcript_size`
   边界戳，排水与快进都以各自 hook 的边界为上限（无戳的旧队列条目退化为
   处理时边界）。
2. 文件身份：游标带 `(st_dev, st_ino)`，同路径原子替换成更大文件不再从旧
   offset 续读（fstat 一致快照，消除 stat/open 窗口）。
3. 读取上限 2 MiB/批；超上限的单行跳过（碎片 JSON 解析失败无害），游标
   不会卡死。
4. 首见即 stat 失败返回 None 游标、调用方不落盘——否则 (path,0) 会在文件
   出现后整本回放。
5. 游标表统一 LRU 写入口（每写重插+收缩到 512），advance 单调不回退
   （乱序旧 hook 不会重发已镜像叙述）。
6. `TURN_NARRATION` 纳入 ACTIVE 生命周期集合（与 open_turn 一致）。
7. 文本渲染多行叙述逐行加 `> `。
8. 残留（记录不修）：defer 队列 recent-first 调度在积压 >5 分钟时可能让同
   会话的 Stop 先于旧工具 hook 处理——有捕获边界+单调 advance 后，后果从
   "重复/乱序"降级为"漏一段叙述"；排队序保序是后续 defer 调度器的活。

R2 确认轮对修复本身构造出两个反例，再修：

9. **边界戳绑定文件身份**：只盖 size 时，"hook 捕获于文件 A → 排水前被
   替换成更大的文件 B"会让首见游标落在 B 的 boundary 处、下一读把 B 的
   剩余历史当实时叙述发出。改为一次 open+fstat 原子盖
   `_walkcode_transcript_size` + `_walkcode_transcript_file_key`（dev,ino）；
   身份不匹配的边界一律不适用——首见跳 EOF、既有游标原地不动不外发。
10. **丢弃态跨批次**：超限单行跳过后若行尾碎片恰好是合法 assistant JSON，
    会被当成一行解析（构造反例：2 MiB 空白 + 合法条目同一物理行）。游标
    增加第 4 元 `discarding`：置位后逐批丢弃到该行真正的换行符，任何
    中线碎片都进不了解析器。
11. defer_tui_hook / gate_tui_hook 直接调用路径在入队时补盖捕获戳（时间 +
    transcript 边界），不再等到排水时。
12. 残留（记录不修）：同 inode 原地截断后回长（Claude transcript 是
    append-only，此形态不真实发生）可能漏掉低于旧游标的新字节；网络盘
    st_ino 不稳定时退化为"漏叙述"或"首见跳过"，不会回放（本机场景为
    APFS，声明范围内可靠）。

R3 再收口三处：

13. 无身份（size-only，legacy payload）边界禁止定位新游标——首见一律跳
    EOF；只有既有游标（同文件身份）才拿它当读取上限。
14. advance 前跳保守保留 discarding（无法证明跨过了超限行的真实换行；
    误清会把行尾碎片喂给解析器，最坏保留只是多丢一行）。
15. 丢弃完成后同一批次的剩余字节当场解析（早退会让合法叙述迟一拍、甚至
    被随后的 Stop advance 吞掉）；裁剪后的不满窗不再据以判定超限行。

R4 终轮：advance 路径补上与 reader 同款的 keyless 边界封堵（无同文件旧
游标时一律跳 EOF），并给"裁剪不满窗不判超限"补反例测试（近 cap 合法行
不得被误丢）。四轮共报 1 High + 2 High(反例) + 若干 Medium，全部闭合或
显式记录为残留。

## Revision 2（v0.14.7）：headless 判定假设错误，线上无效

v0.14.6 上线后现场验收失败：headless 会话完全没有 💬 行。实测 bundled
CLI 的 stream-json——**每个 content block 是独立的 assistant 事件**
（[thinking]、[text]、[tool_use] 各一条），text 与 tool_use 永远不同消息。
"同消息含工具块"的叙述判定在真实流上一次都没触发（只在合成 fixture 上
成立），中段文本仍走 TURN_DELTA 气泡。四轮 deep-review 均未发现——
reviewer 审的是给定假设。教训与 ps locale 事故同构：**mock 形状 ≠ 生产
形状，接外部数据流必须先实测一份真样本**。

修复：叙述判定改为**顺序判定**，在事件泵里缓一拍——每段 turn_delta 悬置，
由下一个事件决定归宿：
- 后继是工具事件（或再来一段文本）→ 它是叙述 → 💬 上卡、不 seal；
- 后继是 turn_completed / permission / ask_user / 错误 / 流结束 / 所有权
  围栏退出 → 它是最终文本 → 原样发气泡（并沿用 turn_completed 去重与
  seal 语义，位置与旧行为一致）；
- background_tasks 账本节拍不决定归宿（它与真后继事件交错）。

转换器里"同消息合装 → TURN_NARRATION"的路径保留为兼容兜底。codex 的
agent_message 走同一个泵，顺带获得同样的叙述判定。TUI transcript 游标
路径提取 text 条目不要求同条含工具块，天然兼容拆分形状，无需改动。
