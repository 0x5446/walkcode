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
