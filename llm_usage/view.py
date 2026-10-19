"""纯函数：把 stats.daily 归约成某一周的展示视图。

fold 在写出 stats.json 时对每个展示周调用一次，把结果写进 ``weeks[i].view``。
两个渲染器（SVG 与 widget）只消费这份产物，不再各自 fold。

== 为什么格式化也在这里 ==

视图里带 ``*_display`` 字符串，而不是让两个渲染器各自把数字转成文本。因为
「479.0M」这种写法涉及量级选择与舍入方向，渲染器各写一遍必然漂移。

deletion test：删掉本模块，fold 就必须把取周、排序、占比、格式化搬回去，
两个渲染器会立刻开始各写一份。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any

from llm_usage.contract import TOKEN_KINDS

TOKEN_LABELS = {
    "tokens_in": "Input",
    "tokens_out": "Output",
    "cache_write": "Cache write",
    "cache_read": "Cache read",
}

# 排序与占比永远按 token（有 token 的源都在同一标尺上）。订阅制源若已按 API 牌价
# 补上成本，金额列写美元；对不上牌价的才写 Subscription。
BASIS_LABELS = {
    "cost": "Cost",
    "tokens": "Tokens",
    "requests": "Requests",
}

SUBSCRIPTION_LABEL = "Subscription"

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# 模型行展示前 N 个。只活在这一层，渲染器不再各自截断。
MODEL_LIMIT = 6


# ------------------------------------------------------------------ 数字格式化

def _fixed(value: float, digits: int) -> str:
    """定点格式化，四舍五入远离零。"""
    factor = 10 ** digits
    scaled = math.floor(abs(value) * factor + 0.5)
    sign = "-" if value < 0 and scaled != 0 else ""
    text = str(int(scaled))
    if digits == 0:
        return sign + text
    text = text.rjust(digits + 1, "0")
    return f"{sign}{text[:-digits]}.{text[-digits:]}"


def _group(intpart: str) -> str:
    """千分位分隔。"""
    out = []
    for offset, ch in enumerate(reversed(intpart)):
        if offset and offset % 3 == 0:
            out.append(",")
        out.append(ch)
    return "".join(reversed(out))


def format_tokens(value: int | None) -> str:
    """token 量的紧凑写法：4,047 → 4.0K，479,000,000 → 479.0M。"""
    if value is None:
        return "—"
    n = abs(value)
    if n >= 1_000_000_000:
        return _fixed(value / 1_000_000_000, 2) + "B"
    if n >= 1_000_000:
        return _fixed(value / 1_000_000, 1) + "M"
    if n >= 1_000:
        return _fixed(value / 1_000, 1) + "K"
    return str(int(value))


def format_cost(cents: float | None) -> str:
    """分 → 美元，带千分位与两位小数。"""
    if cents is None:
        return "—"
    text = _fixed(cents / 100, 2)
    neg = text.startswith("-")
    if neg:
        text = text[1:]
    intpart, _, frac = text.partition(".")
    return f"{'-' if neg else ''}${_group(intpart)}.{frac}"


def format_billing(cost_cents: float | None, sources: set[str] | list[str],
                   subscription_sources: set[str] | list[str]) -> str:
    """金额列：有折算成本就只写美元，不和 Subscription 拼在同一个格子里。"""
    if cost_cents is not None:
        return format_cost(cost_cents)
    if set(sources) & set(subscription_sources):
        return SUBSCRIPTION_LABEL
    return "—"


def format_count(value: int | None) -> str:
    if value is None:
        return "—"
    return _group(str(int(abs(value)))) if value >= 0 else "-" + _group(str(-int(value)))


def format_day(day: str) -> str:
    """2026-08-17 → Aug 17."""
    _, month, dom = day.split("-")
    return f"{MONTH_ABBR[int(month) - 1]} {int(dom)}"


def format_range(start: str, end: str) -> str:
    return f"{format_day(start)} – {format_day(end)}"


def format_updated(generated_at: str | None) -> str:
    """数据覆盖末日 → ``Updated Oct 19``。空值给空串，渲染器直接省略。

    入参是产物里那份快照的时刻，它恒为数据末日的配置时区零点（见
    ``fold._data_stamp``），所以只取日期部分。产物不读时钟，这里也就不需要时针。
    """
    if not generated_at:
        return ""
    dt = datetime.fromisoformat(generated_at)
    return f"Updated {MONTH_ABBR[dt.month - 1]} {dt.day}"


# ---------------------------------------------------------------------- 归约

def fold_rows(rows: list[dict[str, Any]], *, round_cost: bool = False) -> dict[str, Any]:
    """按字段求和。全都没报的字段保持 ``None``，不塞 0。

    ``round_cost``：year 汇总中途取整到四位；WeekView 故意不取整，显示位数由
    ``format_cost`` 统一决定。
    """
    out: dict[str, Any] = {
        "requests": sum(int(r.get("requests") or 0) for r in rows),
    }
    for field in TOKEN_KINDS:
        values = [r[field] for r in rows if r.get(field) is not None]
        out[field] = sum(values) if values else None
    costs = [r["cost_cents"] for r in rows if r.get("cost_cents") is not None]
    if costs:
        total = sum(costs)
        out["cost_cents"] = round(total, 4) if round_cost else total
    else:
        out["cost_cents"] = None
    present = [out[f] for f in TOKEN_KINDS if out[f] is not None]
    out["tokens_total"] = sum(present) if present else None
    return out


def _basis_of(totals: dict[str, Any]) -> str:
    if totals.get("tokens_total"):
        return "tokens"
    return "requests"


def _basis_value(row: dict[str, Any], basis: str) -> float:
    if basis == "cost":
        return float(row.get("cost_cents") or 0)
    if basis == "tokens":
        return float(row.get("tokens_total") or 0)
    return float(row.get("requests") or 0)


def build_week_view(
    daily: list[dict[str, Any]],
    week: dict[str, str] | None,
    limit: int = MODEL_LIMIT,
    subscription_sources: list[str] | None = None,
) -> dict[str, Any]:
    """构建某一周的展示视图。

    ``week`` 形如 ``{"week": "2026-W34", "start": "2026-08-17", "end": "2026-08-23"}``。

    ``models`` 按 token 降序，``pct`` 是该行在本周内的 token 占比（0~100）。
    ``days`` 恒为 7 项（周一到周日），没有用量的那天补零，让日条形图的横轴稳定。
    ``subscription_sources`` 里对不上牌价的模型，金额列显示 Subscription。
    """
    if not week:
        return {"week": None, "start": None, "end": None, "range_display": "",
                "basis": "requests", "requests": 0, "tokens_total": None,
                "cost_cents": None, "tokens_display": "—", "cost_display": "—",
                "requests_display": "0", "breakdown": [], "models": [], "days": []}

    start, end = week["start"], week["end"]
    rows = [r for r in daily if start <= r.get("date", "") <= end]
    sub_set = set(subscription_sources or ())

    totals = fold_rows(rows)
    basis = _basis_of(totals)

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r.get("model") or "unknown"].append(r)

    model_rows = []
    for name, group in by_model.items():
        agg = fold_rows(group)
        model_rows.append({
            "label": name,
            "requests": agg["requests"],
            "tokens_total": agg["tokens_total"],
            "cost_cents": agg["cost_cents"],
            "tokens_display": format_tokens(agg["tokens_total"]),
            "cost_display": format_billing(
                agg["cost_cents"], {r.get("source") or "" for r in group},
                sub_set),
            "requests_display": format_count(agg["requests"]),
        })

    basis_total = sum(_basis_value(r, basis) for r in model_rows)
    for row in model_rows:
        row["pct"] = (_basis_value(row, basis) / basis_total * 100) if basis_total else 0.0
    model_rows.sort(key=lambda r: (-_basis_value(r, basis), r["label"]))

    # token 四分类的构成，按固定顺序而非大小排，让配色在各周之间稳定。
    breakdown = []
    if totals["tokens_total"]:
        for field in TOKEN_KINDS:
            value = totals[field]
            if value is None:
                continue
            breakdown.append({
                "kind": field,
                "label": TOKEN_LABELS[field],
                "amount": value,
                "display": format_tokens(value),
                "pct": value / totals["tokens_total"] * 100,
            })

    day_index: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        day_index[r["date"]].append(r)
    first = Date.fromisoformat(start)
    days = []
    for offset in range(7):
        day = (first + timedelta(days=offset)).isoformat()
        agg = fold_rows(day_index.get(day, []))
        days.append({
            "date": day,
            "weekday": WEEKDAY_LABELS[offset],
            "requests": agg["requests"],
            "tokens_total": agg["tokens_total"],
            "cost_cents": agg["cost_cents"],
            "tokens_display": format_tokens(agg["tokens_total"]),
        })

    return {
        "week": week["week"],
        "start": start,
        "end": end,
        "range_display": format_range(start, end),
        "basis": basis,
        "requests": totals["requests"],
        "tokens_total": totals["tokens_total"],
        "cost_cents": totals["cost_cents"],
        "tokens_display": format_tokens(totals["tokens_total"]),
        "cost_display": format_billing(
            totals["cost_cents"], {r.get("source") or "" for r in rows}, sub_set),
        "requests_display": format_count(totals["requests"]),
        "breakdown": breakdown,
        "models": model_rows[:limit],
        "days": days,
    }
