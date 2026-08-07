# ADR 0062 — 接住 codex 的 `error` 通知，并给未知事件留痕

- 状态：Accepted
- 日期：2026-08-07
- 版本：v0.14.21
- 相关：[ADR 0061](0061-never-close-a-turn-in-silence.md)（回合永不静默收尾）、
  [ADR 0060](0060-codex-resident-event-listening.md)（codex 常驻跨回合监听）

## 背景：错误信息一直在管子里，我们没接

ADR 0061 让"整轮零输出"能在频道里发声，但那是**事后**的：用户等 17 秒，
收到一句"本轮没有返回任何内容"，仍然不知道为什么。

复盘 8/7 事故时挖到：codex app-server v2 协议**有** `error` 通知
（`codex app-server generate-json-schema` 生成的 `ServerNotification.json`，
`method: "error"`）：

```
ErrorNotification {
  error: TurnError { message, additionalDetails?, codexErrorInfo? },
  threadId, turnId,
  willRetry: bool        ← 关键：codex 是否还在退避重试
}
```

`codexErrorInfo` 覆盖了我们关心的全部失败形态：`badRequest`、`unauthorized`、
`serverOverloaded`、`usageLimitExceeded`、`contextWindowExceeded`、
`internalServerError`、`sandboxError`、`cyberPolicy`，以及四个带
`httpStatusCode` 的对象变体：`httpConnectionFailed`、
`responseStreamConnectionFailed`、`responseStreamDisconnected`、
`responseTooManyFailedAttempts`（重试耗尽）。

`CodexAppServerTransport._convert_event` 里**一个字符都没接**——未知类型
一律 `return None`，连日志都不记。所以 8/7 那六次上游 400，codex 每次都
如实通知了我们，六次全被丢进黑洞。

> 澄清一个容易走偏的判断：codex 的 rollout 文件里搜不到任何 error 事件
> （6.7M 条落盘事件，0 条）。那只能说明**没持久化**——错误通知是瞬时的，
> 不属于会话历史。不能据此推断"codex 没抛"。

## 决定

### 1. `error` 通知按 `willRetry` 分流

- `willRetry: true` → `TURN_NARRATION`，带 `diagnostic: True`。
  走工具进度卡的 💬 行（ADR 0055），不刷屏——重试可能连发六次。
  文案形如 `⚠️ 上游报错，正在重试：响应流连接失败 · user message must have content（HTTP 400）`。
- `willRetry: false` → `SESSION_ERROR`，是真气泡。
  文案形如 `⚠️ 本轮失败（代理已放弃重试）：重试次数耗尽`。
  同时把 lifecycle 置为 `ERROR_RECOVERABLE`（既有语义，用户重发即可）。

`diagnostic: True` 的 narration **不计入** ADR 0061 的 `turn_produced_output`：
一个只留下重试记录的回合，结尾仍然要说"本轮没有返回任何内容"。重试过程不是回答。

### 2. 未知事件类型按类型记一次日志

`_convert_event` 丢弃任何未识别类型前，`_log_degrade("codex_event_type_unhandled",
event_type=…)`，每种类型每进程一次。

一行日志换掉一整类盲区。目前已知未接的还有 `turn_aborted`（历史数据里 6402 次）、
`model_reroute`、`context_compacted`、`thread_rolled_back`——它们是否需要面向用户
另说，但"codex 说了我们不知道"这件事本身必须可见。

## 影响

- 上游异常时用户在第一次失败（约 4 秒）就看到原因，而不是等重试耗尽。
- 频道多出 💬 诊断行；`willRetry` 为真时不产生独立气泡。
- 未知事件日志一次性，不影响热路径（一个 set 查询）。

## 验证

- `tests/test_channel_native_codex.py`：`willRetry` 真/假两条分支、
  无 `codexErrorInfo` 的降级、未知类型按类型只记一次。
- `tests/test_channel_native_silent_turn.py`：两次重试 + 空完成的完整序列
  （既看到 `正在重试` 与 `HTTP 400`，也仍然收到收尾的零输出告警）；
  放弃重试后 `重试次数耗尽` 进频道。
- 协议来源：`codex app-server generate-json-schema --out <dir>` 的
  `ServerNotification.json` / `v2/ErrorNotification.json`（codex 0.144.5）。
