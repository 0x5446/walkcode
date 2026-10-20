# Deep Review — 飞书大文件下载 + 附件失败留下孤儿话题

- Repo: /Users/alpha/workspace/walkcode
- HeadSHA: 61d8b49（审查对象）；修复提交 2c34fcb
- RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-61d8b49-1792471804.AbG0
- Target: `git diff main...HEAD`（fix/lark-large-file-and-orphan-topic）
- Engine: codex（gpt-6-luna · openai · effort medium，当前配置）；cursor 未登录，跳过
- 维度：correctness / goalfit / maintainability / conventions / concurrency（信号触发）

VERDICT: NEEDS_FIX → 已修复（见下）

## 背景

飞书发 155MB PDF：下载报 234037（单次下载上限 100MB）；且新会话根卡片和会话先于附件下载创建，
下载失败后留下卡在 starting 的卡片和空会话，失败提示又挂在原消息下，成了两个话题。

## 🔴 高置信（已修）

### 分段下载未校验每段的 Content-Range
- 来源：correctness + goalfit（同引擎两维度配对），Phase 2 VERIFIED，Warning
- File: `src/walkcode/channel_native/lark_live.py` `SdkTransport._download_ranged`
- 问题：缺少总大小时用"短分段即结束"推断完整性；服务端忽略 Range 返回 200 整包时会反复累积，
  截断响应会被当作完整文件交给 agent。
- 修复（2c34fcb）：每段必须带 `Content-Range: bytes a-b/total`，且 `a` 等于请求起点、
  `b-a+1` 等于实际长度、分段非空，否则抛 `TransientDeliveryError`。新增 4 种坏分段的测试；
  真实飞书环境复测 162800933 字节，sha256 与首次下载一致。

## ❌ 已驳回

### 大文件下载期间持有入站锁，其他事件排队
- 来源：concurrency，Phase 2 FALSE_POSITIVE
- 理由：main 上附件下载同样在 `_ingress_lock` 内的 `handle_inbound_event` 调用链里执行，
  并非本次引入；SDK 调用经 `asyncio.to_thread` 不阻塞事件循环，卡片回调 2.5 秒后返回中性提示、
  outbox 补发卡片更新，影响限于交互延迟。现在大文件能下载成功，排队时间（约 80 秒）比以前长，
  是已知取舍。

## 维度结论

| 维度 | VERDICT |
|---|---|
| correctness | NEEDS_FIX（上面的分段校验） |
| goalfit | NEEDS_FIX（同一条，R2） |
| maintainability | SAFE |
| conventions | SAFE |
| concurrency | NEEDS_FIX → 回证驳回 |
