# LLM 用量看板 · 重构设计

> 目标读者：本人
> 关联：日常使用说明见仓库根 `README.md`，产品前提见 `PRODUCT.md`

本文是按时间累积的设计日志，不是当前架构的说明书。**当前架构以第 10、11、12 节为准。**

- 第 2 节：第一轮重构前的问题，含实测证据。仍然有效，是很多决定的依据。
- 第 3 节：第一轮的目标架构。**其中 3.1、3.3、3.5、3.6 已被第 8 节推翻**，见下表。
- 第 4、5、6 节：第一轮的展示口径、迁移路径与待确认项，多数已由第 8 节结案。
- 第 7 节：第一轮实现后与计划的偏差。**7.1、7.3 已被第 8 节推翻。**
- 第 8 节：第二轮（v3）的架构与推翻的理由。
- 第 9 节：接入 ChatGPT / Codex。
- 第 10 节：第三轮（v4）：WeekView 物化、采集 seam、目录收成 `llm_usage/`。
- 第 11 节：Codex 按公开 API 牌价补 model cost。**金额列的现状以这一节为准。**
- 第 12 节：页眉刷新时刻。**`generated_at` 是对「产物不读时钟」的定点例外。**
- 第 13 节：接入 DeepSeek，控制台导出，人民币账单折美元。
- 第 14 节：接入 Antigravity，本机会话库的 protobuf 元数据，按 Gemini 牌价补成本。

被推翻的部分不删，因为「为什么放弃它」本身是有用的记录。但别照着它们实现：

| 已作废 | 现在的做法 | 理由见 |
| --- | --- | --- |
| 3.1 raw 按 `<machine>` 分片 | 只按 `<source>/<月>` 分片 | 8.2 |
| 3.3 快照差分 + `data/state/` | 幂等的区间重采 | 7.2、8.2 |
| 3.5 `unit` + `amount` 分桶 | 具名 token 字段 + `cost_cents` | 8.3 |
| 3.6 `machine` 展示维度 | 无此维度（接口不提供） | 8.2 |
| 7.1 Cursor 走本机 `ai-code-tracking.db` | 走官方 dashboard 接口 | 8.1 |
| 9.2 `$X · 订阅` | 有牌价就写美元；对不上才写 Subscription | 11 |

---

## 1. 约束与目标

### 硬约束

- **两台机器**：Windows 在家，Mac 在公司。两边都会产生用量，都需要采集，都需要推回同一个仓库。
- **多种源，口径不同**：WorkBuddy 给的是会话累计积分，Cursor 给的是请求数，OpenAI 兼容接口给的是真 token。
- **公开仓库**：`Mournerliao/llm-usage` 是 public，README 的图片依赖它可公开访问。
- **零运维预算**：不希望为这个玩具项目长期维护服务器或数据库。

### 目标

1. 任何一台机器的采集都不会覆盖或丢失另一台的历史数据。
2. 历史是累积的，且可从原始数据完整重建。
3. 跨天的长会话，用量归到实际发生的那一天。
4. 不同单位的用量不被混在一起相加。
5. 减少组件数量和外部依赖，故障面越小越好。

---

## 2. 现方案的问题

以下都在当前代码和实际数据库里核实过，不是推测。其中 2.8 最严重：它说明现在展示的那个数字本身就不是用量。

### 2.1 原始数据按源名整文件覆盖，第二台机器必然丢数据

`collectors.save_records` 的落盘路径是 `data/local/<source>.json`，文件名只由源名决定，写入方式是整文件覆盖。`aggregate()` 又是从 `data/cloud` + `data/local` 全量重读、重算、重写 `stats.json`。

后果：在 Mac 上跑一次 local 采集，`data/local/workbuddy.json` 被今天的记录整个盖掉，昨天在 Windows 上采的 `2026-08-16` 两条记录永久消失。`stats.json` 目前只有一天数据，是这个结构的必然结果，不是数据量不够。

### 2.2 `data/cloud/` 被 gitignore，云端数据会被本地聚合抹掉

`.gitignore` 第 5 行忽略了 `data/cloud/`，所以 API 类源采到的数据从未进入仓库。一旦在某台机器上跑过 `--mode cloud` 并推送，另一台机器跑 local 时 `load_side("cloud")` 读到空目录，重算出的 `stats.json` 会把云端数据清空。

### 2.3 WorkBuddy 采集口径错误，跨天会话的增量永远采不到

`session_usage.used` 是**会话累计值**。而 `collectors/workbuddy.py` 按 `sessions.created_at` 归日，并用 `if day != date: continue` 只保留当天创建的会话。

两条组合起来：昨天创建的会话今天继续用，`used` 从 12 万涨到 30 万，这 18 万增量不会被任何一天采集到（归日看创建时间，过滤只留今天创建的）。反过来，跨三天的长会话，全部用量记在第一天。

### 2.4 时区口径在两处不一致

`update-local.sh` 用 `date -u` 取日期，`collectors/workbuddy.py` 用 `datetime.fromtimestamp()`（运行机器的本地时区）。在 UTC+8 的早上八点之前运行，两者相差一天。

### 2.5 推送没有 rebase，第二台机器直接失败

`update-local.sh` 结尾是 `git push origin main`，前面没有 `git pull`。

### 2.6 不同单位的数字被相加

`Record` 只有 `input_tokens` / `output_tokens` 两个数值槽，WorkBuddy 的积分被整体塞进 `input_tokens`（代码注释里已承认）。当前只有一个源，所以 SVG 顶部的 `187,604 tokens` 还看得过去；接入第二个源后，这个总量就是把积分、请求数和 token 加在一起，排行的百分比会被单位量级最大的源主导。

### 2.7 排行与 SVG 模板存在两份

`ranking.py` + `render.py` 和 `api/widget.py` 各有一份，原因是 Vercel 只打包 `api/` 目录。`tests/test_core.py::test_renderers_stay_in_sync` 用字符串全等把两份锁在一起。这个测试能防漏改，但它保护的是重复本身——改一次配色要动两处。

### 2.8 `session_usage.used` 不是用量，是上下文占用

在本机 `~/.workbuddy/workbuddy.db`（10 个会话，2026-06-17 至 2026-08-05）实测：

| model | used | size | used/size |
| --- | --- | --- | --- |
| hy3-ioa | 155380 | 192000 | 0.81 |
| hy3-ioa | 96715 | 192000 | 0.50 |
| auto | 48070 | **168000** | 0.29 |
| auto | 35795 | **168000** | 0.21 |

`size` 并非如现有 README 所说「恒为 192000」，它随模型变化：`hy3-ioa` 是 192000，`auto` 是 168000。这两个数字正是模型的上下文窗口大小。而 `used` 在全部 10 条记录中始终小于 `size`，比值分布在 0.18 到 0.81 之间，从未接近或越过 1。

结论：`used` 是**该会话结束时的上下文占用长度**，不是累计消耗。把它跨会话求和没有「用了多少」的含义——一个聊了 50 轮但中途压缩过上下文的会话，`used` 可能很低；一个只读了一个大文件就结束的会话，`used` 反而很高。

因此 `data/stats.json` 里现有那条 `187,604 tokens` 并不是 8 月 16 日的 token 消耗，而是当天两个会话的上下文占用之和，一个没有业务含义的数。这条要在迁移时明确作废，不能当作历史保留。

这也意味着 3.3 的快照差分不能对 `used` 做：`used` 会因上下文压缩而下降，不是单调累加量，差分出来的正增量没有意义。

### 2.9 模型名跨机器不一致，且 `auto` 不是模型

Windows 采到的模型名是 `hy3`，Mac 库里是 `hy3-ioa` 和 `auto`。同一个底层模型在两台机器上会被记成两个不同标签，模型维度的排行会被拆散。

`auto` 更是个路由别名而非模型——实际使用的模型 id 出现在 `session_usage.credit_json` 的 key 里（32 位 hex）。直接把 `auto` 当模型展示是错的。

---

## 3. 目标架构

### 3.1 三层数据

```
data/state/<machine>/<source>.json          # 采集游标：上次快照，用于差分。每台机器独立
data/raw/<machine>/<source>/<YYYY-MM>.jsonl # 增量事件，append-only，永不重写
data/stats.json                             # 产物，由 raw 全量 fold 得到
assets/widget-*.svg                         # 产物，由 stats.json 渲染
```

三条设计原则：

**路径里带 `machine`。** 两台机器写不同文件，git 层面不存在同一文件的并发修改，rebase 永远不需要手工解冲突。

**raw 只追加，不重写。** 每月一个 JSONL 文件，新事件追加在末尾，git diff 天然是纯增量。`aggregate` 退化为对 raw 的纯 fold，重跑任意次结果一致，历史永远可从 raw 完整重建。

**产物与原始数据分离。** `stats.json` 和 SVG 都是可重新生成的，不参与冲突解决（见 3.7）。

按 `<machine>/<source>/<月>` 分片，两台机器三个源一年约 72 个文件，规模可控，按月归档和裁剪也方便。

### 3.2 先确定测量什么

2.8 之后，「WorkBuddy 用了多少 token」这个问题在当前的库里没有可靠答案。三个候选字段的实测情况：

| 字段 | 语义 | 可用性 |
| --- | --- | --- |
| `session_usage.used` | 会话结束时的上下文占用 | 不是消耗量，作废 |
| `session_usage.credit_json` | `{模型id: 浮点数}`，疑似真实计费 | 10 条里只有 3 条有值，覆盖不全且规律不明 |
| `sessions` 行本身 | 会话数、模型、时间 | 完整可靠 |

`credit_json` 的样本形如 `{"26db62...":5.03,"aefa1d...":254.18}`，浮点值、按模型 id 分开，很像真实的积分消耗，而且能顺带解开 `auto` 背后的真实模型。但它只在 10 个会话中的 3 个里有值，且不呈时间规律（最新的 2026-08-05 两条没有，最早的 2026-06-17 反而有），成因未知，暂时不能作为主指标。

所以本次重构对 WorkBuddy **先测能可靠测到的东西**：

- `sessions` 数（按天、按模型、按机器）
- 会话时长（`created_at` → `last_activity_at`）
- 上下文占用峰值 `used / size`，作为一个独立的辅助指标，明确标注它是「上下文占用率」而不是用量

对应到契约，WorkBuddy 的 `unit` 取 `sessions`，`amount` 就是会话数。这个数字诚实、可加、跨机器可比，代价是失去了「token 消耗」这个更想要的口径。等 `credit_json` 的规律搞清楚（见 6.1），再把它作为第二个 unit 加进来。

不必为此惋惜：真正有 token 口径的是 OpenAI 兼容那一路，它们本来就返回准确的 input/output。多单位并存本来就是 3.5 要解决的问题。

### 3.3 快照差分：处理累计型数据源

对于确实单调累加的字段（如 `credit_json` 的积分值、或将来接入的其他累计型源），不再问「今天用了多少」，改成「和上次观测比多了多少」。会话数这类可以直接按事件时间归日的指标不需要差分。

```
采集一次（source, machine, 观测时刻 T）：
    cur  = 读 db 全量 → {session_id: (model, used)}
    prev = load_state(machine, source)

    if prev is None:                      # 冷启动
        save_state(cur, at=T)
        return []                         # 只写基线，不产出事件

    events = []
    for sid, (model, used) in cur.items():
        before = prev.get(sid, 0)
        delta  = used - before
        if delta > 0:
            events.append(Event(
                at=T, machine=machine, source=source, model=model,
                unit="credits", amount=delta,
                requests=1 if sid not in prev else 0,
            ))
        elif delta < 0:
            warn(f"{sid} 的 used 变小了（{before} → {used}），可能是 db 被重置，本次跳过")

    save_state(cur, at=T)
    return events
```

要点：

- **归日按事件的 `at` 在统一时区下计算**，与 `sessions.created_at` 无关。跨天会话被自然切分到各自那天。
- **冷启动只写基线不产出事件**。第一次在 Mac 上跑时，db 里已有的历史累计值不会被一次性算成当天用量。代价是这部分历史永久缺失，但它本来也无法归日，这是正确的取舍。
- **`used` 变小时告警并跳过**，不产生负增量。
- **`requests` 语义收紧**：只在会话首次出现时记 1，后续增量不重复计。这比现行的「每会话恒记 1」准确。真实的请求/轮次数拿不到——库里只有 `sessions` / `session_usage` / `workspaces` / `automations*` 几张表，没有任何 messages 或 turns 表，所以会话级是 WorkBuddy 能给到的最细粒度。

采集频率建议提到每小时一次（只写本地，不推送），每天推送一次。成本几乎不变，但差分粒度细 24 倍，日切不再是需要小心处理的边界。

对 API 类源（OpenAI 兼容接口），接口本身就返回区间用量，不需要差分，直接把返回结果转成事件追加到 raw 即可。但同样要落到 `data/raw/` 而不是被 gitignore 的目录。

### 3.4 事件格式

`data/raw/work-mac/workbuddy/2026-08.jsonl`：

```jsonl
{"at":"2026-08-17T09:00:00+08:00","machine":"work-mac","source":"workbuddy","model":"hy3","unit":"credits","amount":12000,"amount_in":null,"amount_out":null,"requests":1}
{"at":"2026-08-17T10:00:00+08:00","machine":"work-mac","source":"workbuddy","model":"hy3","unit":"credits","amount":3400,"amount_in":null,"amount_out":null,"requests":0}
```

`amount_in` / `amount_out` 在源能拆分输入输出时填写，不能拆分时为 `null`。这比把积分塞进 `input_tokens` 诚实。

### 3.5 单位分层

新增 `unit` 字段，取值 `tokens` / `credits` / `requests`。聚合时**按 `unit` 分桶，不跨单位求和**。

`stats.json` 契约升到 v2：

```json
{
  "schema_version": 2,
  "latest_date": "2026-08-17",
  "total_dates": 2,
  "daily": [
    {
      "date": "2026-08-17",
      "machine": "work-mac",
      "source": "workbuddy",
      "model": "hy3",
      "unit": "credits",
      "amount": 15400,
      "amount_in": null,
      "amount_out": null,
      "requests": 1
    }
  ]
}
```

顶层加 `schema_version`，让 React 组件和渲染器能识别契约漂移，而不是靠字段名沉默地对不上。`stats.schema.json` 同步更新为 v2，仍然是单一事实源。

### 3.6 machine 维度

`machine` 既是分片键，也是一条有价值的展示轴：Windows 在家、Mac 在公司，这条轴大致等于「个人时间 / 工作时间」，比现有的 ADE 维度更能说明问题。数据里始终保留，展示时默认聚合掉，需要时切换。

标识符**在 `sources.yaml` 里显式配置**，不用 `socket.gethostname()` 探测。显式的名字稳定、可读，也避免把公司内网主机名写进公开仓库。

```yaml
machine: work-mac          # 或 home-win
timezone: Asia/Shanghai    # 所有日切统一按它计算

model_aliases:             # 见下：跨机器归一
  hy3-ioa: hy3

sources:
  - name: workbuddy
    type: workbuddy
```

时区用标准库 `zoneinfo`（Python 3.12 自带），不引入新依赖。

**模型名归一。** 2.9 已确认同一模型在两台机器上的标签不同（`hy3` 与 `hy3-ioa`），不归一的话模型排行会把同一个模型拆成两行。归一表放在配置里而不是代码里，新增别名不需要改代码。

原始事件里**保留原名**，归一只在聚合阶段做。这样将来发现映射错了可以重跑修正，而不是把错误固化进 raw。

`auto`（WorkBuddy）和 `auto-smart`（Cursor）不进归一表——它们是路由别名，不是模型。

原计划把它们改标成 `auto (未解析)`，实现时放弃了：SVG 的标签列只有 112px，加后缀会被截断成更难读的样子，而 `auto` 本身对我这个唯一读者已经足够自解释。等 6.2 解出背后的真实模型 id 再处理，届时是把它们**换成真名**，而不是加注释。

### 3.7 两台机器的同步

采用文件分片 + 产物单点生成。

**机器侧**（`update-local.sh`）只负责推送自己分片的原始数据：

```
git pull --rebase --autostash origin main
git add data/state/<machine> data/raw/<machine>
git commit -m "chore(<machine>): usage raw @ <date>"
git push origin main        # 失败则重试一次 pull --rebase && push
```

因为 `data/state/` 和 `data/raw/` 的路径都以 `<machine>` 开头，两台机器改的永远是不同文件，rebase 不会产生冲突。

**产物侧**由 GitHub Actions 生成。监听 `push` 到 `data/raw/**`，跑 `aggregate` + `render`，把 `data/stats.json` 和 `assets/*.svg` 提交回 main。

这样机器永远不提交产物，产物永远由单点串行生成，冲突面归零。聚合和渲染是纯 stdlib、不需要任何 secret，public 仓库的 Actions 免费。

**无 CI 的退路**：如果不想用 Actions，机器侧改成两阶段——先提交并推送 raw，成功后本地重跑聚合渲染，再单独提交产物。冲突只可能出现在产物上，而产物遇到冲突时不需要合并，直接 `git checkout --theirs` 后重新生成覆盖即可。

### 3.8 展示层：移除 Vercel

数据一天更新一次，push 的那一刻内容就已确定；GitHub 的 camo 还会缓存图片 5 到 10 分钟。动态端点没有换来实时性，却引入了一个可能 502 的外部依赖（端点运行时从 raw.githubusercontent 拉数据，那边抽风时 README 就是裂图）。

改为 CI 在产物阶段生成静态 SVG 提交进仓库，README 直接引用：

```markdown
<img src="assets/widget-model-light.svg#gh-light-mode-only">
<img src="assets/widget-model-dark.svg#gh-dark-mode-only">
```

可以删掉：`api/`、`vercel.json`、`pyproject.toml` 里的 Vercel 配置、`api/widget.py` 中复制的排行与模板逻辑、以及锁住这份重复的 `test_renderers_stay_in_sync`。`ranking.py` / `render.py` 成为唯一实现。

损失的能力：`?date=` 和 `?theme=` 查询参数。theme 用上面的 `#gh-*-mode-only` 双图解决；`?date=` 在 README 场景没有使用者。博客的 React 组件本来就直接读 `stats.json`，不受影响，真正的交互留在它那边。

---

## 4. 混合单位的展示口径

这是本次改动里唯一还没定的展示问题。当一天内同时存在 tokens、credits、requests 三种单位时：

- 顶部汇总行按单位并列，例如 `412,003 tokens · 187,604 credits · 34 requests`，不再有单一「总量」。
- 排行的百分比只在同单位内计算。

倾向的做法：SVG 卡片按 unit 分节渲染，每节内部各自算占比；单位超过两种时只渲染前两节，其余并入汇总行。React 组件因为有 Tab，可以做得更完整。

替代方案是引入一个统一折算口径（比如按各源单价折成人民币），把所有源拉到同一标尺上。这样能恢复单一总量和全局排行，但需要维护一张价格表，且 WorkBuddy 的内部积分没有公开单价。倾向暂不做。

---

## 5. 迁移路径

每一阶段独立可用，不做一次性重写。

**阶段 0：作废现有数据，从零开始。** 原计划是迁移 `data/local/workbuddy.json` 里 `2026-08-16` 的两条记录，但 2.8 证明那两个数（128659 / 58945）是上下文占用而非消耗，迁过去只会把一个错误口径带进新库。直接丢弃，`data/stats.json` 从空开始重建。

顺带确认一个边界：Mac 库里有 2026-06-17 至 2026-08-05 共 10 个历史会话。按 3.3 的冷启动规则，首次采集只写基线、不产出事件，所以这批历史不会被一次性算成某一天的用量。它们本来也无法可靠归日，丢掉是正确的。

如果确实想要历史曲线，可以做一次性回填：按 `sessions.created_at` 把 10 个会话摊到 6 至 8 月，`unit` 记为 `sessions`。这个口径（会话数）不受 2.8 影响，是可靠的。事件里加 `backfill: true` 标记，与差分产出的数据区分开。

**阶段 1：数据层。**（已完成）引入 `machine` / `timezone` / `model_aliases` 配置、`unit` 字段、raw 新布局；WorkBuddy 采集器改为按 3.2 的口径产出会话事件；`aggregate` 改为对 raw 的 fold；契约升 v2。

**阶段 2：同步。**（已完成）`update-local.sh` 加 rebase 与重试；加 GitHub Actions 生成产物；`data/cloud/` 目录随新布局废弃。

**阶段 3：移除 Vercel。**（已完成）静态 SVG 进仓库，删 `api/` 与 `vercel.json`。

**阶段 4：把 WorkBuddy 的消耗口径找回来。** 取决于 6.1 能否解出 `credit_json` 的规律。这是目前唯一还缺的东西——Cursor 侧已经有可用口径（见 7.1）。

---

## 6. 待确认

> **已排除：两台机器的会话不重叠。** 原本担心 WorkBuddy 云端同步会话导致双重计数。实测 Mac 库有 10 个会话（2026-06-17 至 2026-08-05），Windows 那批的 `used` 值 128659 / 58945 在 Mac 库里一条都没有，模型标签也不同（Mac 是 `hy3-ioa` / `auto`，Windows 是 `hy3`）。两台机器的库是各自独立的本地文件，按 `machine` 分片安全，3.6 的 machine 维度成立。

### 6.1 WorkBuddy 整体待重做（当前已摘除）

第 2.8 节证明 `used` 不是消耗量，替代方案（只统计会话数）虽然诚实但信息量太低，不值得为它维护一个采集器和一条公开数据。**所以 WorkBuddy 采集器已从代码里摘除**，等口径想清楚再重做。

被摘除的内容：`collectors/workbuddy.py`、`sources.example.yaml` 里的源配置、`data/raw/*/workbuddy/` 下的原始数据、以及 `hy3-ioa → hy3` 的别名。实现代码可在 git 历史里找回，探查结论保留在本文 2.8 与下面的 `credit_json` 分析里。

重做的前提是先解出 `credit_json` 的规律。已知：

- 格式为 `{32位hex模型id: 浮点数}`，例如 `{"26db62...":5.03,"aefa1d...":254.18}`
- 10 个会话里只有 3 个有值
- 不呈时间规律：最新的两个 2026-08-05 会话没有值，最早的 2026-06-17 反而有

需要弄清楚：值的单位是什么（积分？人民币？），为什么大部分会话是空的（只有跨 expert / 跨模型调用才记录？只有计费模型才记录？某个版本之后才开始写？），以及 hex id 到模型名的映射从哪来。

可行的探查方式是在 WorkBuddy 里主动跑几个不同类型的会话，观察哪些会写入 `credit_json`。搞清楚之后，WorkBuddy 就能以 `credits` 为 unit 接入 3.3 的差分逻辑——`credit_json` 是累计值，这会是第一个真正需要差分的源。

顺带能解决另一件事：`sessions.model = 'auto'` 时真实模型未知，而 `credit_json` 的 key 恰好是 32 位 hex 模型 id。解出 id 到名字的映射，`auto` 就能显示成真实模型名。

### 6.2 Cursor 的路由别名 `auto-smart`

同上，`auto-smart` 是 Cursor 的自动选型，不是模型。目前原样显示。要解开它需要另找映射来源，`ai_code_hashes` 表里没有。优先级低——实测它只占 17 / 597 个请求。

### 6.3 公开仓库的信息暴露

`hy3` 是内部模型名。加上 `machine` 维度后，公开数据会包含：在用哪些内部工具、每天什么时段在工作、工作日与周末的差异。这可能是有意为之，但值得先确认公司对这类信息的口径，比事后清理 commit 历史容易。

如果需要收敛，可选的做法是给源配置一个 `public_alias`（`hy3` → `internal-model-a`），raw 保留真名并留在私有仓库，只把脱敏后的产物推到公开仓库。这会引入第二个仓库，复杂度上升，仅在确实需要时再做。

### 6.4 raw 的长期保留策略

按月分片后，一年约 72 个文件。暂不做裁剪。如果 `stats.json` 的体积成为问题（daily 行数随天数线性增长，React 组件每次要全量拉取），可以让产物只保留最近 90 天的明细，更早的数据折叠成月度汇总。等实际数据量到了再说。

---

## 7. 实现后与原计划的偏差

### 7.1 Cursor 有比预期好得多的数据源

原计划（旧 6.3）倾向走 Cursor 官方 usage 接口，因为担心本地库结构不稳。实际探查发现一个更合适的库：`~/.cursor/ai-tracking/ai-code-tracking.db`。它是 Cursor 自己用来统计「AI 写了多少代码」的库，表结构窄、字段语义明确，不需要解析 `state.vscdb`（本机 4.2 GB）里的 composer 结构。

`ai_code_hashes` 表每行是一段 AI 产出的代码，带 `requestId` / `conversationId` / `model` / `source` / `createdAt`。实测本机有 60,350 行、597 个 requestId、130 个会话、9 个模型，覆盖 2026-07-20 至 08-17。

采用的口径是**按模型的 distinct requestId 数**（`unit="requests"`）。没有选 hash 行数，因为那反映的是单次请求改了多少代码，量级受改动大小影响太大（均值约 100 行/请求）。

已核实的边界，都写进了 `collectors/cursor.py` 的模块文档：

- `requestId` 从不跨天（实测 0 例），所以一次请求只归一天，不需要差分。
- `source='human'` 的行 `requestId` 与 `model` 全为 NULL，是人工代码，整段跳过。
- `source='tab'` 的行有 `requestId` 但 `model` 为 NULL，归为 `cursor-tab`。
- 极少数 `requestId` 跨两个模型（2 / 597），在两个模型下各记一次，不做归属判定。

一个重要约束：**该库只保留最近约一个月**。好处是首次采集一次性回填了 21 天历史（比从零开始好得多）；代价是必须定期采集，否则更早的数据滚出窗口后永久丢失。

### 7.2 不需要快照差分，改成幂等的窗口重采

原计划的核心机制是快照差分（3.3），针对「只暴露累计值」的源。但实际接入的两个源都不需要它：

- Cursor 的 `ai_code_hashes` 带时间戳，可以直接按天查询。
- WorkBuddy 的会话数同样按 `created_at` 直接归日。

所以实现改成更简单的方案：每次采集重算回看窗口（默认 30 天）内的每一天，覆盖写回。这让采集天然幂等——跑一次和跑十次结果相同，补跑漏跑都能自愈，也不需要维护 `data/state/` 游标文件。3.3 的差分逻辑保留在文档里，等真的接入累计型源（比如 `credit_json`）时再实现。

对应地，原计划的 `data/state/` 目录没有创建，raw 布局简化为：

```
data/raw/<machine>/<source>/<YYYY-MM>.json
```

文件内容是 `{"days": {"2026-08-17": [事件...]}}`，写入时按天替换。「负责的那天没有用量」和「那天没被采过」因此可以区分：前者留一个空数组，旧记录会被正确清掉，不会留成幽灵。

### 7.3 单位标签需要两套中文写法

小节标题用名词（「请求」），汇总行要带量词（「14 次请求」）。中文量词的位置不能靠拼接推出来，所以 `ranking.py` 与 `types.ts` 各维护 `UNIT_LABELS` 和 `UNIT_COUNTED` 两张表。

另外发现一个冗余：只有一种单位时，小节标题和顶部汇总行显示同一个数。现在单节时省掉标题。

### 7.4 用跨语言 parity 测试替代 SVG 字符串比对

原方案里 `test_renderers_stay_in_sync` 用字符串全等锁住两份 SVG 模板，2.7 批评过它「保护的是重复本身」。移除 Vercel 后 SVG 只剩一份实现，那个测试自然消失。

但 Python 与 TS 两个渲染器的**计算口径**仍然可能漂移，这是真实风险。新增 `tests/test_parity.py`：用 esbuild 把 TS 的 `buildView` 打包成单文件，交给 node 执行，再与 Python 的 `ranking.build_view` 逐字段对比（分节顺序、每节总量、行顺序、占比到小数 9 位）。测试用例刻意覆盖多单位、跨机器、同名模型跨源、以及并列值排序这几个容易漂移的点，并额外用仓库里真实的 `stats.json` 再对一遍。

为此把 `buildView` 从 `UsageWidget.tsx` 拆到 `react/src/view.ts`——纯函数不该住在组件文件里，拆开后 node 可以直接导入，不必拉进 React。

缺 node 或 `react/node_modules` 时该测试自动跳过，不阻塞 Python 侧；CI 里把两者都装上，确保断言真的跑到。

### 7.5 配置拆成两份

原计划只提到「machine 写在 sources.yaml 里」。实现时发现一个矛盾：`model_aliases` 和 `timezone` 是聚合阶段需要的，而聚合跑在 CI 上，`sources.yaml` 又因为含 `base_url` 等本机信息被 gitignore 掉了，CI 永远读不到。

所以拆成两份：`sources.yaml`（不提交，本机采集用）与 `config/aggregate.yaml`（提交，时区与别名表）。CI 拿着仓库就能重新生成产物，不需要任何 secret。

---

## 8. 第二轮（v3）：换数据源，加成本，改周维度

触发点是两件事：一是发现展示得太简单，缺 token 明细和花费；二是查清了本机数据源根本给不出这两样。

### 8.1 Cursor 的数据源换成官方接口

第 7.1 节选了本机的 `ai-code-tracking.db`，口径是「按模型的 distinct requestId 数」。这个选择在「只想知道用了多少次」的前提下是对的，但一旦要 token 和花费就走不通了——那个库里根本没有这两类字段。

于是把本机所有候选逐个实测排除：

| 候选 | 实测结果 | 结论 |
| --- | --- | --- |
| `ai-code-tracking.db` | 有 requestId 与 model，无任何 token / cost 字段 | 只能数请求数 |
| `state.vscdb` 的 `bubbleId*` | 69,433 条带 `tokenCount`，仅 516 条非零（0.7%）；这 516 条里只有 1 条带 `requestId`，与当前窗口的 600 个 requestId 交集为 0 | 旧版本残留，当前时段覆盖率 0% |
| `state.vscdb` 的 `composerData*` | 3,147 个会话中 28 个有 `usageData.costInCents`（0.9%），且全部停在 2025-10 | 遗留字段 |

所以本机拿不到 token 和成本，唯一的真实来源是账号级的 `POST /api/dashboard/get-filtered-usage-events`，用本机 Cursor 登录态的 session cookie 认证。实测它比本机库好三个量级：

- **有完整 token 明细**：输入 / 输出 / 缓存写入 / 缓存读取四类分开。
- **有折算成本**：`tokenUsage.totalCents`。
- **能回溯到账号开通日**（本账号 2026-04-09），不像本机库只留最近一个月。首次采集一次性拿到 6,339 条事件、89 个有用量的日子。
- **可按任意区间查询**，所以每次重采全量覆盖写回，采集天然幂等。

一个必须记下的实现细节：请求必须带 `Origin` 与 `Referer` 头。接口对状态变更类请求做同源校验，缺了会返回 403 `Invalid origin for state-changing request`，即使 cookie 完全正确——这一点排查掉了不少时间。

### 8.2 machine 维度消失了

这是换数据源的直接代价，也是对第一轮核心设计的推翻。

第一轮把 `machine` 当成分片键**和**展示轴（3.6：「这条轴大致等于个人时间 / 工作时间」）。但官方接口是**账号级**的：两台机器的用量都在同一份返回里，接口不告诉你哪条来自哪台机器。

于是按机器分片从「保证互不覆盖的机制」变成了「重复计数的来源」——两台机器各自采集，会把同一份账号数据写进两个文件，聚合时算两遍。所以 raw 布局改成 `data/raw/<source>/<月>.json`，`machine` 字段从契约里整个去掉。

代价是真的失去了「哪台机器用得多」。接口不提供这个信息，硬造一个只会是假的，所以不造。

新的冲突面：两台机器现在写同一个文件，rebase 可能撞上冲突（第一轮那种「天然无冲突」的性质没了）。但两边的内容是同一份账号数据的快照，取任意一边都对，之后任何一次采集都会把它重写成最新的完整状态。这个退让是可接受的，写进了 `update-local.sh` 的注释。

### 8.3 去掉 unit，改用具名字段

第一轮引入 `unit` + `amount`，是为了防止把积分、请求数、token 加到一起（2.6）。那个担忧是对的，但机制选错了。

现在两个源都是 token 口径，而要表达的东西变成了：四类 token、请求数、成本。用 `unit` 分桶来表达这个，得让同一次调用产出好几条 unit 不同的事件，聚合和展示都要一层分桶逻辑去拦。

改成具名字段之后，「不同口径不能相加」变成了**类型层面的事实**：`tokens_in` 和 `requests` 是两个字段，根本没有把它们加起来的路径，不需要运行时逻辑去防。顺带删掉了 `UNIT_ORDER` / `UNIT_LABELS` / `UNIT_COUNTED` 三张表和 7.3 记录的那套中文量词处理。

一条规则值得单独记：拿不到某个口径时字段**缺失**，而不是填 0。「这个源不报 token」和「报了但确实是零」是两件事，混掉的话展示层就没法决定该显示横线还是 0。`schema.py` 会把显式的 `null` 报为错误——那意味着上游把这两件事搞混了。

成本用**浮点的分**而不是整数分。单次调用的成本常在一分以下（实测有 0.4137 分这样的值），过早取整会让求和系统性偏小。

### 8.4 展示的三层收窄

需求是「详细数据只看近一个月、按周展示、GitHub 只放本周」。落成三层：

| 层 | 范围 | 在哪展示 |
| --- | --- | --- |
| 周明细 | 最近 4 个 ISO 周（≤28 天） | README 只放本周；React 组件可切四周 |
| 年度汇总 | 当年，按月与按模型两个口径 | **先存不展示**，等数据攒够一年 |
| 完整历史 | 全部 | 只在 `data/raw` 里，随时可重新切窗口 |

**周次由数据决定，不由时钟决定。** 「本周」取的是 `latest_date` 所在的 ISO 周，不是运行时的当天。这样 stats.json 完全由 raw 决定：同一份 raw 在周一和周三重跑得到同一份产物，幂等性成立。若改读系统时钟，不但幂等性没了，连着几天没用量时卡片还会显示一片空白。

代价是极端情况下「本周」可能指的是上一周（比如周一还没用量时）。可接受：卡片上同时印着确切的日期区间。

顺带结掉了 6.4 的担忧——`stats.json` 不再随天数线性增长，`daily` 恒定在 4 周窗口内。

### 8.5 格式化收进共享视图

第一轮的 parity 测试（7.4）只比对分节、总量、行序、占比。这一轮把 `*_display` 字符串也放进视图并纳入比对。

因为「479.0M 还是 0.48B」涉及量级选择与舍入方向，两个渲染器各写一遍必然漂移，而这种漂移恰好是最难发现的：两边都「看起来对」，只是数字不一样。

舍入不能用各语言的内建格式化：Python 的 `f"{x:.1f}"` 是银行家舍入，JS 的 `toFixed` 是四舍五入。`0.125` 保留两位，前者给 `0.12`，后者给 `0.13`。所以两边都手写 `floor(|x| * 10^n + 0.5)`，在 IEEE754 双精度下逐位相同，测试里专门有一条用例钉住这个差异。

同理，累加**不做中途取整**：两端按同一顺序累加同一批双精度浮点，结果逐位相同；中途取整反而会制造出这个测试要抓的那种漂移。

### 8.6 视觉：纸白墨黑，一条线分层

只有两个硬约束，其余都是取舍：

GitHub 把 README 里的 SVG 当图片渲染（camo 代理），所以**不能加载外部字体、不能有 JS / hover / 动画**。字体只能用系统栈。

因此数字统一走系统等宽栈：等宽字形天生按位对齐，金额与 token 量成列后小数点自然对齐；而且三个平台都有，不会退化成后备字体导致排版错位。语言文字用系统无衬线栈。等宽在这里是给数据做位对齐，不是拿它当技术感的装饰。

SVG 卡片按 760 个用户单位排版，``<img width="100%">`` 拉满 README 栏宽。放大没事；若 viewBox 宽于显示宽度，字号会被等比缩小，10.5px 的注解会变成读不清的 6px——那只会发生在 GitHub 窄屏上。

两个值得记的细节：

- **`tokens` 后缀用同一个 `<text>` 里的第二个 `tspan`**，让它按前一段的实际排版宽度自然流动。最初是算偏移量，那要假设等宽字形的 advance 是 0.6em——macOS 的 SF Mono 是，Windows 的 Consolas 不是，后缀会贴上或飘开。
- **构成条最浅的那一阶仍明显深于卡片底色。** 缓存读取常占九成宽度，若取到接近白色，整条会被读成「只填了 7% 的进度条」，而它其实是填满的四段构成。相邻段之间用 1px 底色缝隙切开，让只有几个像素宽的段也仍然读作独立的段。比例保持线性，不做视觉放大。

React 组件的断点是**容器查询**（`@container` + `@xl:`）而不是视口断点。博客里组件宽度由正文栏决定，视口 1440 而正文只有 600 是常态；用视口断点会在窄栏里错版。这个问题是截图核对时才暴露的——headless Chrome 忽略了小于 500px 的 `--window-size`，一开始看到的「移动端」其实是 500px 视口。

组件比 SVG 多一条七天条形（静态卡片里放不下）。它有一条**贯通的基线**，让没有用量的那天读作「零」而不是「这天缺数据」；基线若放进每一列，列间距会把它切成七段虚线。

### 8.7 公开成本金额的口径

6.3 担心的信息暴露，在加入金额后变得更具体：这是企业账号，账单挂在公司 team 下，而仓库是公开的。

所以只公开 `tokenUsage.totalCents`，即 **token 按各模型单价折算出的成本**，它反映用掉了多少算力。刻意不取另外两个：`chargedCents` 是实际计费额（含 Cursor 抽成），`cursorTokenFee` 是抽成本身。计费额与套餐、折扣信息不进入任何提交物。

同理，事件里的 `conversationId` / `owningTeam` / `owningUser` 不落盘——聚合到 (日期, 模型) 粒度后它们自然消失。

`kind` 的处理也据此决定：`USAGE_BASED`、`INCLUDED_IN_BUSINESS`、`FREE_CREDIT` 三种都计入，因为它们都是真实发生的算力消耗，区别只在谁付钱，而本项目量的是消耗不是账单。`ERRORED_NOT_CHARGED` 与 `ABORTED_NOT_CHARGED` 跳过，它们没有产生任何 token，计入会虚增请求数。

口径说明写在 README 的「关于模型成本」一节，不印在卡片上：卡片上已经有「模型成本」这个标签，四类 token 的占比也能从构成条直接读出来，再加一句脚注是重复。

### 8.8 WorkBuddy 仍未回来

6.1 的状态不变：`credit_json` 的规律还没解出，采集器仍在摘除状态。这一轮没有推进它。

---

## 9. 接入 ChatGPT / Codex

Cursor 之后的第二个真实 token 源。ChatGPT Plus 没有官方逐次用量接口，本机
`state_5.sqlite.tokens_used` 又是会话累计、无拆分，所以走 `~/.codex/sessions`
jsonl 里的 `token_count` / `last_token_usage`。

三条和 Cursor 不同的约束：

1. **本机源，必须按机器分片。** 路径是 `data/raw/<source>/<machine>/<月>.json`。
   `Event` 仍然没有 machine 字段。旧的 `data/raw/<machine>/<source>/` 布局靠
   「文件里的 source 对不上第一段路径」跳过，避免把第一轮残留折进总量。
2. **排行永远按 token。** 订阅没有逐次成本，再按成本排会把 Codex 吃成 0%。金额列：
   Cursor 仍是美元；`codex` 显示「订阅」；两者同时出现时写成 `$X · 订阅`。
3. **Source 是 ADE。** 已由 [ADR 0002](adr/0002-source-is-ade.md) 取代：不按 provider 分源。其中「中转站也归 `codex`」又被 [ADR 0003](adr/0003-codex-official-provider-only.md) 取代：只采 `model_provider=openai`，中转站调用不采集。

---

## 10. 第三轮（v4）：物化 WeekView，收目录

第 8.5 节把格式化收进「共享视图函数」，物理上却是 Python 与 TS 各写一份，靠
parity 测试钉住。ChatGPT 接入时改一次订阅文案就要抄三处。这不是共享模块，是用
测试锁住的双 adapter。

**现在的做法：** fold 对四个 ISO 周各算一次 `WeekView`，写入 `stats.json` 的
`weeks[i].view`。`llm_usage/render.py` 与 `widget` 只读产物。契约升到 v4。
详见 `docs/adr/0001-weekview-in-stats.md`。

顺带三件事：

1. **采集 seam。** `CollectResult.machine_shard` 声明落盘策略，`persist` 按
   `Event.source` 分组写入。`LOCAL_TYPES` 从编排里消失。cursor / chatgpt 的
   I/O 从「内部创建」改成注入（`fetch` / `rollouts`），测试穿过 `collect()`。
2. **闲置 adapter 拿掉。** `openai_compatible` 从未进入热路径，删掉。真有这类
   源再加回来。
3. **目录。** Python 管线收进 `llm_usage/`（`collect/`、`fold`、`view`、
   `render`、`contract`）；React 目录改名 `widget/`。根上留三行 `run.py`。
   契约常量从 `stats.schema.json` 读出，不再手抄 `TOKEN_KINDS`。

---

## 11. Codex 按 API 牌价补 model cost

第 9 节把金额列写成 `$X · Subscription`，是因为当时决定不估 Codex 的单价。
社区里 ccusage / ai-coding-usage-card 的做法是：有官方预计价就用官方的，没有
就按公开 API 牌价乘 token，并标明这是 API-equivalent，不是账单。

本项目的 model cost 本来就是「token × 单价」，不是发票。Cursor 的单价来自
`totalCents`；Codex 的 raw 没有这个字段，所以在 fold 时用
`config/aggregate.yaml` 里的 `list_prices` 补上。raw 不改，牌价变了重跑
`--skip-collect` 即可。

对不上牌价的模型（如额度耗尽后顶上的 `gpt-reserve`）仍显示 Subscription。主数字有金额
时只写美元，不再和 Subscription 拼在同一个格子里。

---

## 12. 页眉的刷新时刻

卡片要回答「这张图还新不新」。周区间已经告诉观众数据覆盖哪几天，不能再拿
`latest_date` 充数——那是最近一次有用量的日期，连着几天没请求时会停在那里。

所以 fold 写出 `generated_at`（配置时区的 ISO 时间戳）和已经格式化好的
`updated_display`（`Updated Aug 18, 17:20`）。两端渲染器只读文案，不自己算。

这是对第 8.4 节「周次由数据决定、不由时钟决定」的定点例外：周次、日明细、
WeekView 仍然完全由 raw 决定；同一份 raw 换个时刻重跑，只有这两项会变。不把
时钟写进周窗口，也不把相对时间（2 hours ago）写进 SVG——GitHub 当图片渲染，
没有 JS 去刷新它。

放在页眉这一行：左边是窗口（This week · 区间），右边是新鲜度。刷新时刻跟区间
同属快照元数据，不另起一节——单独占底下一行会把一句注脚抬成第五块内容。
博客组件切周时右端不动，因为它属于这份产物，不属于某一周。

---

## 13. DeepSeek：控制台导出，账单折美元

第三个 ADE。官方 `GET /user/balance` 只有余额，没有日期、模型、token；
`/v1/usage` 一类路径 404。用量页背后的 `platform.deepseek.com/api/v0/usage/*`
只认控制台 `userToken`：API Key 打是 `40003`，只带 cookie 是 `40002 Missing
Token`。所以认证和 Cursor 同类——账号会话，不是 `sk-`。

采集走月度导出 ZIP（`/api/v0/usage/export?tz=28800`），一份里同时有 amount
（日 × 模型的请求与三类 token）和 cost（人民币账单）。`input_cache_miss` /
`hit` / `output` 分别进 `tokens_in` / `cache_read` / `tokens_out`。DeepSeek
磁盘 KV 缓存不收写入费，没有 write 口径，字段省略。负号是扣费，取绝对值。

金额用账单，不用 `list_prices`：2026-09 起分峰谷，日合计 token 恢复不出高峰
占比，CSV 已经含峰谷。人民币按 `config/aggregate.yaml` 的 `usd_cny` 在采集时
折成美元分，fold / 渲染零改动。改汇率要重采，不能只靠 `--skip-collect`。
DeepSeek 不进 `subscription_sources`。

账号级，路径 `data/raw/deepseek/<月>.json`，不按机器分片。CSV 里的 `api_key`
/ `user_id` / `wallet_type` / `api_key_name` 落盘前丢掉。不升 schema，不动
WeekView。不复活已删的 `openai_compatible`。

---

## 14. Antigravity：本机会话库

第四个 ADE。没有能按天查 token 的接口：客户端调的 Cloud Code 内部接口
`v1internal:retrieveUserQuotaSummary` / `fetchAvailableModels` 只给 5 小时 /
每周窗口的 `remainingFraction`，和 ChatGPT 的 `wham/usage` 同类；本地语言服务的
`GetUserAnalyticsSummary` 能按区间查，但响应里只有 `ChatStats`
（`chats_sent`、`chats_accepted`…）与补全接受数，没有 token。二进制里的
`GetCascadeAnalytics` / `GetTeamCreditEntries` 是 Windsurf 团队后台遗留，个人
Google 账号没有对应服务端。这些接口还都要伪装客户端 UA，比读本地库更脆。

所以和 Codex 一样读本机：`~/.gemini/antigravity/conversations/<id>.db`，一会话
一个 SQLite（CLI 在同级 `antigravity-cli`，同名只读一次）。库用 WAL，只读打开也
要写 `-shm`，采集时连 `-wal` 一起复制到临时目录再读。

用量是没有公开 schema 的 protobuf，字段编号按 2026-09 的 15 个会话、1925 次调用
实测：

| 位置 | 字段 | 含义 | 依据 |
| --- | --- | --- | --- |
| `steps.metadata` | 1.1 / 9 / 12 | 秒级时间戳 / 用量 / 消息 ID | 时间落在会话起止之间 |
| 用量 | 2 | 未缓存输入，**不含**缓存读 | 恒远小于 5，且随对话变长稳定在数千 |
| 用量 | 5 | 缓存读 | 随对话推进单调增长，符合前缀缓存 |
| 用量 | 3 = 9 + 10 | 输出 = 思考 + 正文 | 1925 条无一例外 |
| 用量 | 11 | 响应 ID | 去重键 |
| `gen_metadata.data` | 4 / 1.19 | 消息 ID / 模型名 | 与 steps 的 12 一一对应 |

几条约束：

1. **只算一份。** `gen_metadata` 里有同一份用量的副本，只取模型名；同一响应 ID
   出现多次也只算一次。
2. **缓存写缺省。** 字段 4 从未出现，`cache_write` 省略而不是写 0。金额按
   `gemini-3.8-flash` 的公开牌价在 fold 时补，`antigravity` 进
   `subscription_sources`。牌价是促销价，2027-01-01 起翻倍，届时改表重跑
   `--skip-collect`。
3. **格式漂移时拒写。** 编号是反推的，客户端升级可能挪位。`3 = 9 + 10` 当自检；
   解码失败、自检不过或读库出错时整源放弃，本次不覆盖 raw——少读一个会话会把
   那几天覆盖成偏小的值，宁可不更新。
4. **只读元数据。** 只查 `steps.metadata` 与 `gen_metadata.data`，不碰
   `step_payload` 里的对话正文。
5. **本机源的老问题。** 按机器分片，两台都要采。会话在客户端里删掉，下次重采时
   那几天的历史也跟着变少，这一点和 Codex 相同，账号级源没有。

