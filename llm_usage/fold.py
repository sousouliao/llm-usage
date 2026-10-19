"""汇总：把 data/raw 下所有源的事件 fold 成 data/stats.json。

这一层是纯粹的归约：读原始事件 → 归一模型名 → 按 (date, source, model) 累加 →
切出展示窗口与年度汇总 → 为每个展示周写出 WeekView → 写产物。不做采集、不访问
网络、不读任何本机专属路径，所以 CI 拿着仓库就能重新生成产物，不需要配置任何密钥。

== 为什么周次由数据决定，而不是由「今天」决定 ==

「本周」取的是**最近有用量的那一天所在的 ISO 周**，不是运行时的当天。这样
周次、日明细和 WeekView 完全由 raw 决定：同一份 raw 重跑任意次、在任何时刻跑，
这三块都一样。若改成读系统时钟，同一份原始数据在周一和周三会产出不同产物，
幂等性就没了，而且连着几天没用量时卡片会显示一片空白。

**没有任何一项读运行时钟。** 页眉右端要回答「这份产物还新不新」，这里用「数据覆盖
到哪天」来回答（``generated_at`` / ``updated_display``）：它只会随新数据前进，不会
因为重建而漂移。早先这两项写的是「产物写出时刻」，那让同一份 raw 每次重建都产出
一个只有时间戳不同的提交，CI 因此永远有 diff、也更容易撞上并发推送。

所以：同一份 raw 重建，产物逐字节相同，CI 也就没有东西可提交。

== 展示窗口与年度数据 ==

``daily`` 只保留最近四个 ISO 周的明细（≤28 天），够博客组件做四周切换，也让
stats.json 不随天数无限增长。``year`` 是当年的汇总（月度与模型两个口径），现在
不展示，先存着。完整历史永远在 raw 里，随时可以重新切窗口。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from llm_usage import REPO_ROOT, config, pricing, view
from llm_usage.collect import read_all_events
from llm_usage.contract import SCHEMA_VERSION, TOKEN_KINDS

# 展示窗口的周数：本周加过去三周。博客组件的切换范围与这里一致。
DISPLAY_WEEKS = 4


def fold_events(events: list[dict], aliases: dict[str, str] | None = None) -> list[dict]:
    """纯函数：把原始事件归约成日记录，按 (date, source, 归一后的 model) 累加。

    token 的四个字段与 ``cost_cents`` 遵守同一条规则：只要有任何一条参与事件报了这
    个字段，结果就是所有报了的值之和；全都没报则保持 ``None``。这样「这个源不报
    token」和「报了但确实是零」在产物里仍然可区分。

    deletion test：删掉本函数，aggregate() 就必须把归一与累加逻辑搬回编排流程，
    复杂度被集中而非平移。
    """
    aliases = {} if aliases is None else aliases
    fields = TOKEN_KINDS + ("cost_cents",)
    buckets: dict[tuple, dict] = {}

    for e in events:
        model = config.normalize_model(e.get("model") or "unknown", aliases)
        key = (e.get("date"), e.get("source") or "unknown", model)
        row = buckets.setdefault(key, {"requests": 0,
                                       **{f: None for f in fields}})
        row["requests"] += int(e.get("requests") or 0)
        for field in fields:
            value = e.get(field)
            if value is None:
                continue
            row[field] = (row[field] or 0) + value

    daily = []
    for (date, source, model), row in buckets.items():
        entry = {"date": date, "source": source, "model": model,
                 "requests": row["requests"]}
        for field in TOKEN_KINDS:
            if row[field] is not None:
                entry[field] = int(row[field])
        if row["cost_cents"] is not None:
            entry["cost_cents"] = round(row["cost_cents"], 4)
        daily.append(entry)

    daily.sort(key=lambda r: (r["date"], r["source"], r["model"]))
    return daily


# ------------------------------------------------------------------ 周与年窗口

def iso_week_start(day: str) -> Date:
    """某日期所在 ISO 周的周一。"""
    d = Date.fromisoformat(day)
    return d - timedelta(days=d.weekday())


def week_key(day: str | Date) -> str:
    """ISO 周编号，如 ``2026-W34``。"""
    d = Date.fromisoformat(day) if isinstance(day, str) else day
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def recent_weeks(latest: str, count: int = DISPLAY_WEEKS) -> list[dict]:
    """以 ``latest`` 所在周为最新，向前取 ``count`` 个 ISO 周，新的在前。"""
    newest = iso_week_start(latest)
    weeks = []
    for offset in range(count):
        start = newest - timedelta(weeks=offset)
        end = start + timedelta(days=6)
        weeks.append({
            "week": week_key(start),
            "start": start.isoformat(),
            "end": end.isoformat(),
        })
    return weeks


def build_year(daily: list[dict], latest: str) -> dict:
    """当年汇总：月度与模型两个口径。现在不展示，先存着。"""
    year = latest[:4]
    rows = [r for r in daily if r["date"][:4] == year]
    if not rows:
        return {"year": year, "start": None, "end": None, "days_active": 0,
                "months": [], "models": []}

    by_month: dict[str, list[dict]] = defaultdict(list)
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_month[r["date"][:7]].append(r)
        by_model[r["model"]].append(r)

    totals = view.fold_rows(rows, round_cost=True)
    models = [{"model": m, **view.fold_rows(rs, round_cost=True)}
              for m, rs in by_model.items()]
    models.sort(key=lambda r: (-(r["tokens_total"] or 0), -(r["cost_cents"] or 0),
                               r["model"]))
    return {
        "year": year,
        "start": min(r["date"] for r in rows),
        "end": max(r["date"] for r in rows),
        "days_active": len({r["date"] for r in rows}),
        **totals,
        "months": [{"month": m, **view.fold_rows(rs, round_cost=True)}
                   for m, rs in sorted(by_month.items())],
        "models": models,
    }


def _data_stamp(latest: str | None) -> tuple[str, str]:
    """页眉右端自报「数据覆盖到哪」：数据末日零点（配置时区）与它的展示文案。

    这里刻意**不读运行时钟**。产物由 raw 完全决定，才谈得上幂等：同一份 raw 无论在
    本机、在 CI 还是补跑三天后重建，写出的字节都一样，CI 也就不会每次都产出一个
    只有时间戳不同的提交。新鲜度由「覆盖到哪天」表达，它只会随新数据前进，不会
    因为重建而漂移。
    """
    if latest is None:
        return "", ""
    tz = ZoneInfo(config.load_aggregate_config()["timezone"])
    stamp = datetime.fromisoformat(latest).replace(tzinfo=tz).isoformat()
    return stamp, view.format_updated(stamp)


def build_stats(daily_all: list[dict]) -> dict:
    """把全量日记录切成 stats.json：展示窗口的明细 + 带视图的周次 + 年度汇总。

    纯函数：同一份 ``daily_all`` 在任意时刻、任意机器上跑，结果逐字节相同。
    """
    subs = config.subscription_sources()
    dates = sorted({r["date"] for r in daily_all})
    latest = dates[-1] if dates else None
    generated_at, updated_display = _data_stamp(latest)
    if latest is None:
        return {"schema_version": SCHEMA_VERSION,
                "timezone": config.load_aggregate_config()["timezone"],
                "latest_date": None,
                "generated_at": generated_at,
                "updated_display": updated_display,
                "weeks": [], "sources": [],
                "subscription_sources": subs,
                "daily": [], "year": None}

    priced = pricing.fill_list_prices(
        daily_all, config.list_prices(), config.price_aliases())
    weeks = recent_weeks(latest)
    window_start = weeks[-1]["start"]
    daily = [r for r in priced if r["date"] >= window_start]
    for week in weeks:
        week["view"] = view.build_week_view(
            daily, week, subscription_sources=subs)

    return {
        "schema_version": SCHEMA_VERSION,
        "timezone": config.load_aggregate_config()["timezone"],
        "latest_date": latest,
        "generated_at": generated_at,
        "updated_display": updated_display,
        "weeks": weeks,
        "sources": sorted({r["source"] for r in daily_all}),
        "subscription_sources": subs,
        "daily": daily,
        "year": build_year(priced, latest),
    }


def aggregate(root: Path = REPO_ROOT) -> dict:
    events = read_all_events(root)
    daily_all = fold_events(events, config.model_aliases())
    stats = build_stats(daily_all)

    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    year = stats.get("year") or {}
    print(f"[ok] 汇总完成：{len(events)} 条事件 → {len(daily_all)} 条日记录，"
          f"展示窗口保留 {len(stats['daily'])} 条")
    if year.get("days_active"):
        cost = (year.get("cost_cents") or 0) / 100
        print(f"[ok] {year['year']} 年累计：{year['days_active']} 天，"
              f"{year.get('requests', 0)} 次请求，"
              f"{(year.get('tokens_total') or 0) / 1e9:.2f}B tokens，${cost:,.2f}")
    return stats


if __name__ == "__main__":
    aggregate()
