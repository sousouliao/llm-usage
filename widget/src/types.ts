// TS 侧对数据契约的镜像。单一事实源是仓库根的 stats.schema.json。
// 展示口径已经物化在 weeks[].view 里，本文件只描述形状，不再重新计算。

export const SCHEMA_VERSION = 4;

/** token 的四个分类。只用于构成条配色；数字本身来自 view.breakdown。 */
export const TOKEN_KINDS = [
  "tokens_in",
  "tokens_out",
  "cache_write",
  "cache_read",
] as const;

export type TokenKind = (typeof TOKEN_KINDS)[number];

export interface UsageRow {
  date: string;
  source: string;
  model: string;
  requests: number;
  /** 以下字段缺失表示「这个源不报该口径」，与「报了但是零」不是一回事。 */
  tokens_in?: number;
  tokens_out?: number;
  cache_write?: number;
  cache_read?: number;
  /** token 按各模型单价折算出的成本，单位为分。不是账单金额。 */
  cost_cents?: number;
}

/** 排序与占比的口径。永远按 token，没有 token 时退到请求数。 */
export type Basis = "cost" | "tokens" | "requests";

export interface ModelRow {
  label: string;
  requests: number;
  tokens_total: number | null;
  cost_cents: number | null;
  tokens_display: string;
  cost_display: string;
  requests_display: string;
  /** 在本周内的占比（0~100）。 */
  pct: number;
}

export interface BreakdownSegment {
  kind: TokenKind;
  label: string;
  amount: number;
  display: string;
  pct: number;
}

export interface DayCell {
  date: string;
  weekday: string;
  requests: number;
  tokens_total: number | null;
  cost_cents: number | null;
  tokens_display: string;
}

export interface WeekView {
  week: string | null;
  start: string | null;
  end: string | null;
  range_display: string;
  basis: Basis;
  requests: number;
  tokens_total: number | null;
  cost_cents: number | null;
  tokens_display: string;
  cost_display: string;
  requests_display: string;
  breakdown: BreakdownSegment[];
  models: ModelRow[];
  days: DayCell[];
}

/** 可切换的一周。取自 stats.json 的 weeks，新的在前。view 已在 fold 时算好。 */
export interface WeekRef {
  week: string;
  start: string;
  end: string;
  view: WeekView;
}

/** 年度汇总。现在不展示，先存着。 */
export interface YearSummary {
  year: string;
  start: string | null;
  end: string | null;
  days_active: number;
  requests?: number;
  tokens_total?: number | null;
  cost_cents?: number | null;
  months: { month: string; requests: number; tokens_total: number | null }[];
  models: { model: string; requests: number; tokens_total: number | null }[];
}

export interface UsageStats {
  schema_version: number;
  timezone: string;
  latest_date: string | null;
  weeks: WeekRef[];
  sources: string[];
  /** 订阅制源。raw 没有官方成本；对不上牌价的模型金额列显示 Subscription。 */
  subscription_sources?: string[];
  daily: UsageRow[];
  year: YearSummary | null;
  /** 数据覆盖到哪一天：数据末日在配置时区的零点（ISO 8601），无数据时为空串。 */
  generated_at?: string;
  /** generated_at 的展示文案，如 Updated Oct 19。渲染器不自己格式化。 */
  updated_display?: string;
}

/** 主题模式：跟随系统，或强制浅色 / 深色。 */
export type WidgetTheme = "auto" | "light" | "dark";

export interface UsageWidgetProps {
  /** 运行时拉取的数据地址（如 jsDelivr / GitHub raw）。与 data 二选一。 */
  dataUrl?: string;
  /** 构建时直接注入的数据（无需网络请求）。 */
  data?: UsageStats;
  /** 指定初始展示哪一周（ISO 周编号，如 2026-W34），默认最新一周。 */
  week?: string;
  /** 卡片标题，默认 “LLM usage”。 */
  title?: string;
  /** 卡片最大宽度（px），默认 760；窄容器中自动收缩。 */
  width?: number;
  /** 主题模式，默认跟随系统。 */
  theme?: WidgetTheme;
}

const EMPTY_VIEW: WeekView = {
  week: null,
  start: null,
  end: null,
  range_display: "",
  basis: "requests",
  requests: 0,
  tokens_total: null,
  cost_cents: null,
  tokens_display: "—",
  cost_display: "—",
  requests_display: "0",
  breakdown: [],
  models: [],
  days: [],
};

function isWeekView(v: unknown): v is WeekView {
  if (!v || typeof v !== "object") return false;
  const view = v as Record<string, unknown>;
  return (
    typeof view.tokens_display === "string" &&
    typeof view.cost_display === "string" &&
    Array.isArray(view.models) &&
    Array.isArray(view.days) &&
    Array.isArray(view.breakdown)
  );
}

/** 运行时契约校验：stats.json 一旦不匹配契约，组件不再静默渲染错乱数据。 */
export function isValidStats(v: unknown): v is UsageStats {
  if (!v || typeof v !== "object") return false;
  const s = v as Record<string, unknown>;
  if (s.schema_version !== SCHEMA_VERSION) return false;
  if (!Array.isArray(s.daily) || !Array.isArray(s.weeks)) return false;
  const weeksOk = s.weeks.every((w) => {
    const week = w as Record<string, unknown>;
    return (
      typeof week?.week === "string" &&
      typeof week?.start === "string" &&
      typeof week?.end === "string" &&
      isWeekView(week.view)
    );
  });
  if (!weeksOk) return false;
  return s.daily.every((r) => {
    if (!r || typeof r !== "object") return false;
    const row = r as Record<string, unknown>;
    return (
      typeof row.date === "string" &&
      typeof row.source === "string" &&
      typeof row.model === "string" &&
      typeof row.requests === "number"
    );
  });
}

export { EMPTY_VIEW };
