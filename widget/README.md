# LLM 用量 · widget

博客里用的用量卡片，读仓库根 `data/stats.json`。界面用 Tailwind CSS v4 与 shadcn
风格的 `Card` / `Tabs`，周次切换基于 Radix UI，支持键盘操作与焦点状态。

widget 与 SVG 不是互相转换的关系：通用 React DOM、shadcn 和 Tailwind 无法无损转成
GitHub 可用的 SVG。两端共用同一份数据契约，各自只做薄渲染——排序、占比、显示字符串
已经写在 `weeks[].view` 里。

**分工**：README 里的 SVG 只有本周一张，因为 GitHub 不执行 JS，静态图里塞多周只会变
拥挤。这个组件负责本周与过去三周的切换，另外多一条七天条形——静态卡片里放不下。

## 本地预览

```bash
npm install
npm run dev
```

Vite 通过 `publicDir` 直接读取仓库根的 `data/stats.json`，预览即所见。预览页默认按 760
/ 560 / 380 三档容器宽度并排渲染亮暗两版；也可以用查询参数只看一档，方便截图核对：

```
http://localhost:5173/?w=380&theme=light&week=2026-W33
```

## 数据来源（两种模式）

- **运行时拉取（推荐用于博客）**：传 `dataUrl`，组件自行 fetch。

```tsx
<UsageWidget dataUrl="https://cdn.jsdelivr.net/gh/sousouliao/llm-usage@main/data/stats.json" />
```

仓库数据一更新，博客组件自动变新，无需重新构建。

- **构建时注入**：在博客构建阶段拿到数据后作为 `data` 传入，零运行时请求。

```tsx
<UsageWidget data={stats} />
```

## 集成到不同框架

- **Next.js（App Router）**：组件已带 `"use client"`，直接 import 使用。
- **Astro**：`<UsageWidget client:load dataUrl="..." />`
- **Vite / CRA / 任意 React**：直接 import。

## Props

| 名称 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `dataUrl` | `string` | — | 运行时数据地址 |
| `data` | `UsageStats` | — | 构建时注入的数据 |
| `week` | `string` | 最新一周 | 初始周次，ISO 周编号如 `2026-W34` |
| `title` | `string` | `LLM usage` | 卡片标题 |
| `width` | `number` | `760` | 最大宽度 (px)，窄容器自动收缩 |
| `theme` | `"auto" \| "light" \| "dark"` | `"auto"` | 主题模式 |

## 断点是容器查询，不是视口

排版跟着**卡片自己的宽度**走（`@container` + `@xl:`），不是视口宽度。博客里这个组件的
宽度由正文栏决定，视口 1440 而正文只有 600 是常态；用视口断点就会在窄栏里错版。

容器窄于 576px 时，模型行的占比条与 Tokens 列先让位——名字和金额是必须读到的，占比条是
锦上添花。

## 数据契约

> **单一事实源**是仓库根的 `stats.schema.json`（当前 v4）。`src/types.ts` 是它的 TS
> 镜像，字段名必须一致。若 `data/stats.json` 漂移，`isValidStats` 会在运行时拦下并
> 提示，而不是静默渲染错乱数据。

```ts
interface UsageStats {
  schema_version: number;      // 必须是 4
  timezone: string;
  latest_date: string | null;
  generated_at: string;        // 数据覆盖到哪天（末日零点，ISO 8601）
  updated_display: string;     // 如 Updated Oct 19
  weeks: { week: string; start: string; end: string; view: WeekView }[];
  sources: string[];
  daily: UsageRow[];           // 仍保留，widget 展示只读 weeks[].view
  year: YearSummary | null;
}
```

## 口径只有一份

取哪一周、按什么排序、占比多少、数字写成什么样，全部在 Python 的 `llm_usage.view`
里算一次，写进 `stats.json` 的 `weeks[].view`。SVG 与这个组件都只读产物。
