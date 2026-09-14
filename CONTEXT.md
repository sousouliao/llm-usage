# LLM 用量

把作者自己真实的 LLM 消耗量公开出来：按天、按源、按模型归集 token 与模型成本，渲染成 GitHub 上的静态卡片和博客里可切换周次的组件。

## Language

**Event**:
某一天、某个源、某个模型上的用量。`requests` 必填；四类 token 与 `cost_cents` 可缺席（缺席 ≠ 0）。
_Avoid_: Record, unit/amount

**ADE**:
产生用量的编码工具。当前是 Cursor、Codex、DeepSeek。接自己的订阅还是接中转站，都不改变它是哪一个 ADE。
_Avoid_: provider, 中转站, ChatGPT（那是 Codex 可能使用的订阅，不是 ADE）

**Source**:
用量归属的 ADE，落在 `Event.source` 与 raw 路径第一段。当前是 `cursor`、`codex`、`deepseek`。
_Avoid_: provider, chatgpt, krill, custom, headroom（Codex `model_provider` 不是 Source）

**Collector**:
把某源的外部形态翻译成 Event 的 adapter。声明是否按机器分片，不自己碰文件系统。

**Raw**:
`data/raw/` 下按月分片的原始事件。账号级源 `<source>/<月>.json`；本机源 `<source>/<machine>/<月>.json`。

**Fold**:
读全部 Event，归一模型名，按 (date, source, model) 累加，切出展示窗口与年度汇总。

**WeekView**:
某一周已经算好的展示视图：总量、显示字符串、构成条、模型排行、七天格子。写在 `stats.json` 的 `weeks[].view` 里。
_Avoid_: 视图函数（渲染器不再计算）

**模型成本**:
写入 `cost_cents` 的美元分。Cursor 用接口返回的 `totalCents`；Codex 用公开 API 牌价在 fold 时补上（不是账单）。DeepSeek 用平台人民币账单，采集时按 `usd_cny` 折成美元分。

**Subscription source**:
raw 里没有官方成本的源。fold 时按 API 牌价补金额；对不上牌价的模型，金额列仍显示 Subscription。

**Updated display**:
产物刷新时刻的展示文案，如 `Updated Aug 18, 17:20`。由 `generated_at` 在 fold 时格式化，两端渲染器只读这一项。不参与周次窗口。

**Machine**:
本机标识。只当 raw 分片键，不进 Event，也不进公开展示。
