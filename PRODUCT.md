# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

主要使用者只有一个：仓库作者本人（GitHub `Mournerliao`，Cursor 账号为企业邮箱）。他每周会看一眼自己上周烧了多少算力、钱花在哪个模型上。

真正的观众是第二拨人：路过 GitHub 个人主页或读到博客文章的开发者。他们的处境是——在别人的页面上停留三到五秒，滑动中扫一眼，不会点开、不会交互、大概率不会回来。他们想知道的只有一句话：这个人用 LLM 的强度到什么量级。

〔推断，待核〕这张卡片会挂在作者的 GitHub 个人主页 README 上，而不只是这个仓库的 README。

## Product Purpose

把作者自己真实的 LLM 消耗量公开出来，并且自动保持更新：每次请求的 token 明细与折算成本，按模型、按天、按周归集，渲染成一张 GitHub 上能直接看的静态卡片，以及一个博客里可切换周次的组件。

成功的样子是两件事同时成立：数字是真的（不是估的、不是猜的），且不需要人工维护就能一直更新下去。

## Positioning

大多数同类展示只能给出请求数、会话数这类间接指标，因为工具本地不吐 token。这个项目走的是账号级官方用量接口，拿到的是逐次请求的 input / output / cache 读写四类 token 明细和该次调用的成本，因此能给出「5.76B tokens、$4,047」这种量级的真实数字，而不是折算或估算。

诚实是这里的核心资产，不是修饰：任何拿不到真实口径的指标，宁可不展示。项目历史上已经因此砍掉过一个采集器（WorkBuddy，其 `used` 字段被证明是上下文占用而非消耗量）。

## Operating Context

- 用量按 ADE 归集：Cursor 是 `cursor`；Codex 走 ChatGPT 订阅的是 `codex`，走中转站的调用不采集（ADR 0003）。
- 作者有两台机器：家里 Windows，公司 Mac。两边都在产生用量。
- Cursor 官方用量接口是**账号级**的，两台机器的用量都在同一份返回里，因此 Cursor 这一路不需要按机器分片采集。代价是丢掉「这次请求发生在哪台机器」这条维度，这是已确认接受的取舍。
- 采集在本机跑（需要本机 Cursor 登录态），产物由 GitHub Actions 单点生成并提交回 `main`。本机只推原始数据，不推产物。
- 日期切分统一按 `Asia/Shanghai`。
- 零运维预算：不维护服务器、不维护数据库、不依赖 Vercel 之类的动态端点。GitHub 上那张卡片必须是仓库里的静态文件。

## Capabilities and Constraints

**已确认可拿到的数据**（Cursor `/api/dashboard/get-filtered-usage-events`，本机登录态，实测通）：

每条事件含 `timestamp`、`model`、`kind`、`tokenUsage{inputTokens, outputTokens, cacheWriteTokens, cacheReadTokens, totalCents}`、`chargedCents`、`cursorTokenFee`、`conversationId`。可按任意时间区间查询，因此采集天然幂等。

**术语，以下三个数不是一回事，不可混用**：

| 口径 | 含义 | 本项目的用法 |
| --- | --- | --- |
| `tokenUsage.totalCents` | token 按模型单价折算的成本（Cursor 接口） | **对外展示用这个**，称 model cost |
| 公开 API 牌价 × token | Codex 没有 `totalCents` 时的替代折算 | 同样称 model cost，在 fold 时补上 |
| `chargedCents` | 实际计费额，含 Cursor 抽成 | 不对外展示 |
| `cursorTokenFee` | Cursor 在按量计费上的加价 | 不对外展示 |

**硬约束**：

- 仓库 `Mournerliao/llm-usage` 是 public。账号是 enterprise、账单挂在公司 team 下，所以实际计费额与套餐信息不进入任何提交物；只公开「模型成本」折算值。原始事件落盘时同样要剥掉 `conversationId`、`owningTeam`、`owningUser`。
- 事件的 `kind` 有五种。`ERRORED_NOT_CHARGED` 与 `ABORTED_NOT_CHARGED` 没有 token 也没有成本，`FREE_CREDIT` 有 token 无计费。展示时不能把它们和正常请求混为一谈。
- token 总量里 cache read 占约 92%（5.32B / 5.76B）。只报一个总数会误导，必须能看到四类拆分。
- GitHub README 里的图是静态 SVG，无 JS、无 hover、无外链字体，且需要亮暗两套。排版按 760 个用户单位，用 `<img width="100%">` 拉满栏宽。
- 博客组件的宽度由正文栏决定，不是视口，所以断点必须是容器查询。常见正文栏在 560–760 之间，窄栏（380）要能退化得体。
- 博客组件读同一份 `data/stats.json`，与 SVG 共用已经物化在 `weeks[].view` 里的展示口径。

**展示范围**（本次确认的需求）：

- 数据按年采集并全量留存，能取到的最早是 2026-04-09（账号开通日），比原计划的 7 月 1 日更早，全都收。
- 明细只展示近一个月，按周聚合。
- 博客组件可切换本周与过去三周，共四周。
- GitHub 卡片只展示本周。
- GitHub 卡片与博客组件页眉印产物刷新时刻（如 `Updated Aug 18, 17:20`），不印最近一次用量的钟点。
- 年度数据先存不展示，后续再用。

## Brand Commitments

项目名与仓库名 `llm-usage`。文案语言为英文。

作者提供了一张参考图（`.cursor/.../image-28c9408f-*.png`，Claude Code 用量卡片）作为**能力参照**——证明「展示具体 token 量与花费」可行——并明确把视觉设计交给执行方。因此该图约束的是内容项（token 量、花费、模型明细、周节奏），不约束视觉语言。

## Evidence on Hand

- 真实用量数据已全量采集并落在 `data/raw/cursor/`：计入 6,232 条事件，2026-04-09 至 2026-08-17，89 个有用量的日子，5.77B tokens，模型成本 $4,055.19。（接口返回 6,339 条，其中 107 条为出错或中止、无 token，不计入。）
- 按周已核算，例如 2026-W33 为 409 次请求 / 479.0M tokens / $364.56；W15 起每周数据连续。
- 单周内模型集中度高：`gpt-5.5-medium`、`claude-opus-4-8-thinking-xhigh`、`claude-opus-5-thinking-high` 三者合计占总成本约 45%。
- 历史设计取舍与已排除方案记录在 `docs/DESIGN.md`（含 WorkBuddy 采集器被摘除的实测依据）。
- 已证明**不存在**的数据：Cursor 本机库（`ai-code-tracking.db`、`state.vscdb`）在当前时段的 token 覆盖率为 0%，`composerData.usageData.costInCents` 是停写于 2025-10 的遗留字段（覆盖率 0.9%）。未来不要再基于本机库编造 token 或成本口径。
- 没有的东西：没有 Claude Code 的用量数据；没有 WorkBuddy 的可靠消耗口径。

## Product Principles

1. **拿不到真实口径就不展示。** 宁可少一个指标，不要一个看起来精确的假数。
2. **不同单位不相加。** token、请求数、成本各有各的标尺，混算出的「总量」没有含义。
3. **产物可从原始数据完整重建。** 周次、日明细与 WeekView 都是纯函数，同一份 raw 重跑任意次结果一致。页眉的 `generated_at` 是唯一例外：它回答「这张卡片还新不新」，必须用写出这一刻的时钟。
4. **公开面只放折算成本。** 企业账单、套餐、抽成、会话 id 不进入任何提交物。
5. **三秒钟要读懂量级。** 观众不会交互，第一眼就得拿到最重要的那个数。

## Accessibility & Inclusion

GitHub 卡片必须在亮暗两种主题下都满足正文对比度 4.5:1，且要带 `alt` 文本——它是一张图，读屏用户只能拿到 alt。博客组件的周次切换必须可键盘操作。
