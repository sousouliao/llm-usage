"""核心纯函数单测：事件折叠、周窗口、视图构建、格式化、原始数据读写、契约、SVG。

运行：python tests/test_core.py   （只需 pyyaml，无其他第三方依赖）
"""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from llm_usage import fold, pricing, render  # noqa: E402
from llm_usage import view as weekview  # noqa: E402
from llm_usage.cli import run_collect  # noqa: E402
from llm_usage.collect import (  # noqa: E402
    COLLECTORS,
    CollectContext,
    CollectResult,
    Event,
    persist,
    read_all_events,
    write_events,
)
from llm_usage.collect import chatgpt as chatgpt_collector  # noqa: E402
from llm_usage.collect import cursor as cursor_collector  # noqa: E402
from llm_usage.collect import deepseek as deepseek_collector  # noqa: E402
from llm_usage.contract import TOKEN_KINDS, validate_stats  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")

# 一个完整的 ISO 周（周一到周日），后面多处复用。
WEEK = {"week": "2026-08-10", "start": "2026-08-10", "end": "2026-08-16"}


def row(date, model, source="cursor", requests=1, **kwargs):
    """构造一条日记录（fold_events 的输出形态）。"""
    return {"date": date, "source": source, "model": model,
            "requests": requests, **kwargs}


def tokens(**kwargs):
    """token 四分类的简写，未给出的按 0 填，避免每次写四个字段。"""
    return {"tokens_in": kwargs.get("i", 0), "tokens_out": kwargs.get("o", 0),
            "cache_write": kwargs.get("cw", 0), "cache_read": kwargs.get("cr", 0)}


class TestEventContract(unittest.TestCase):
    def test_to_dict_drops_missing_optionals(self):
        """不报 token 的源不该留下 tokens_in: null 这种半真半假的字段。"""
        d = Event(date="2026-01-01", source="s", model="x", requests=3).to_dict()
        self.assertNotIn("tokens_in", d)
        self.assertNotIn("cost_cents", d)
        self.assertEqual(d["requests"], 3)

    def test_tokens_total_is_none_when_source_reports_nothing(self):
        """「不报 token」必须区别于「报了但是零」。"""
        self.assertIsNone(
            Event(date="d", source="s", model="x", requests=1).tokens_total)

    def test_tokens_total_is_zero_when_source_reports_zeros(self):
        e = Event(date="d", source="s", model="x", requests=1, **tokens())
        self.assertEqual(e.tokens_total, 0)

    def test_tokens_total_sums_all_four_kinds(self):
        e = Event(date="d", source="s", model="x", requests=1,
                  **tokens(i=1, o=2, cw=4, cr=8))
        self.assertEqual(e.tokens_total, 15)

    def test_partial_report_counts_missing_as_zero(self):
        """只报输入输出的源（如 OpenAI 兼容接口）总量就是这两项之和。"""
        e = Event(date="d", source="s", model="x", requests=1,
                  tokens_in=10, tokens_out=5)
        self.assertEqual(e.tokens_total, 15)


class TestFoldEvents(unittest.TestCase):
    def test_sums_same_key(self):
        daily = fold.fold_events([
            Event(date="d", source="cursor", model="m", requests=2,
                  **tokens(i=10)).to_dict(),
            Event(date="d", source="cursor", model="m", requests=3,
                  **tokens(i=5)).to_dict(),
        ])
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["requests"], 5)
        self.assertEqual(daily[0]["tokens_in"], 15)

    def test_keeps_sources_separate(self):
        daily = fold.fold_events([
            row("d", "m", source="cursor"),
            row("d", "m", source="deepseek"),
        ])
        self.assertEqual(len(daily), 2)

    def test_applies_model_aliases(self):
        daily = fold.fold_events(
            [row("d", "hy3", requests=1), row("d", "hy3-ioa", requests=2)],
            aliases={"hy3-ioa": "hy3"},
        )
        self.assertEqual(len(daily), 1)
        self.assertEqual((daily[0]["model"], daily[0]["requests"]), ("hy3", 3))

    def test_missing_field_stays_missing(self):
        """所有参与事件都不报 cost 时，结果里不该凭空出现一个 0。"""
        daily = fold.fold_events([row("d", "m"), row("d", "m")])
        self.assertNotIn("cost_cents", daily[0])
        self.assertNotIn("cache_read", daily[0])

    def test_partial_report_is_summed_not_dropped(self):
        """一半的事件报了 cost，结果就是那一半之和——报了的数据不该因为别人没报而丢。"""
        daily = fold.fold_events([
            row("d", "m", cost_cents=12.5),
            row("d", "m"),
        ])
        self.assertAlmostEqual(daily[0]["cost_cents"], 12.5)

    def test_cost_keeps_sub_cent_precision(self):
        """三次各 0.4 分的调用应得 1.2 分。若中途取整成 0，求和会系统性偏小。"""
        daily = fold.fold_events([row("d", "m", cost_cents=0.4)] * 3)
        self.assertAlmostEqual(daily[0]["cost_cents"], 1.2, places=6)

    def test_empty(self):
        self.assertEqual(fold.fold_events([]), [])


class TestWeekWindow(unittest.TestCase):
    def test_iso_week_start_is_monday(self):
        # 2026-08-17 是周一，2026-08-23 是周日，同属一周。
        self.assertEqual(fold.iso_week_start("2026-08-17").isoformat(),
                         "2026-08-17")
        self.assertEqual(fold.iso_week_start("2026-08-23").isoformat(),
                         "2026-08-17")

    def test_sunday_belongs_to_the_week_that_started_monday(self):
        """周日归上一个周一，而不是开启新的一周——这是 ISO 周与「自然周」的分歧点。"""
        self.assertEqual(fold.iso_week_start("2026-08-16").isoformat(),
                         "2026-08-10")

    def test_recent_weeks_is_newest_first_and_contiguous(self):
        weeks = fold.recent_weeks("2026-08-19", count=4)
        self.assertEqual([w["start"] for w in weeks],
                         ["2026-08-17", "2026-08-10", "2026-08-03", "2026-07-27"])
        for week in weeks:
            self.assertEqual(fold.iso_week_start(week["end"]).isoformat(),
                             week["start"])

    def test_weeks_are_derived_from_data_not_from_today(self):
        """周次由最新数据日推出，所以同一份 raw 在任何时刻重跑都得到同一批周。"""
        daily = fold.fold_events([row("2026-05-06", "m")])
        stats = fold.build_stats(daily)
        self.assertEqual(stats["weeks"][0]["start"], "2026-05-04")

    def test_daily_window_is_trimmed_to_the_four_weeks(self):
        daily = fold.fold_events([
            row("2026-08-17", "m"),      # 本周
            row("2026-07-27", "m"),      # 窗口内最早一天
            row("2026-07-26", "m"),      # 窗口外，应被裁掉
        ])
        stats = fold.build_stats(daily)
        self.assertEqual(sorted(r["date"] for r in stats["daily"]),
                         ["2026-07-27", "2026-08-17"])

    def test_year_summary_keeps_data_outside_the_display_window(self):
        """裁掉的历史仍要进年度汇总，否则「先收集，后展示」就落空了。"""
        daily = fold.fold_events([
            row("2026-04-09", "m", requests=7, cost_cents=100.0),
            row("2026-08-17", "m", requests=3, cost_cents=50.0),
        ])
        stats = fold.build_stats(daily)
        year = stats["year"]
        self.assertEqual(year["year"], "2026")
        self.assertEqual(year["start"], "2026-04-09")
        self.assertEqual(year["requests"], 10)
        self.assertAlmostEqual(year["cost_cents"], 150.0)
        self.assertEqual(year["days_active"], 2)
        self.assertEqual([m["month"] for m in year["months"]],
                         ["2026-04", "2026-08"])

    def test_empty_stats_has_no_weeks(self):
        stats = fold.build_stats([])
        self.assertIsNone(stats["latest_date"])
        self.assertEqual(stats["weeks"], [])
        self.assertTrue(stats["generated_at"])
        self.assertTrue(stats["updated_display"].startswith("Updated "))

    def test_generated_at_is_the_only_clock_field(self):
        """同一份 raw 换个时刻重跑，周次与数字不变，只有页眉时间戳变。"""
        daily = fold.fold_events([row("2026-05-06", "m")])
        t1 = datetime(2026, 8, 18, 17, 20, tzinfo=TZ)
        t2 = datetime(2026, 8, 18, 18, 5, tzinfo=TZ)
        first = fold.build_stats(daily, now=t1)
        second = fold.build_stats(daily, now=t2)
        self.assertEqual(first["weeks"], second["weeks"])
        self.assertEqual(first["daily"], second["daily"])
        self.assertEqual(first["generated_at"], "2026-08-18T17:20:00+08:00")
        self.assertEqual(first["updated_display"], "Updated Aug 18, 17:20")
        self.assertEqual(second["updated_display"], "Updated Aug 18, 18:05")


# OpenAI 公开 API 牌价（美元 / 百万 token，短上下文）。测试里用字面量当独立预期值，
# 不从实现倒推。来源：https://developers.openai.com/api/docs/pricing
# gpt-5.6-sol 为促销价，至少到 2026-11-21。
SOL = {"input": 4.00, "cache_read": 0.40, "cache_write": 5.00, "output": 20.00}


class TestListPrices(unittest.TestCase):
    def test_one_million_input_tokens_is_four_dollars(self):
        cents = pricing.cost_cents_from_tokens(
            tokens_in=1_000_000, tokens_out=0, cache_write=0, cache_read=0,
            rates=SOL)
        self.assertAlmostEqual(cents, 400.0)

    def test_cache_write_replaces_overlapping_input_not_adds(self):
        """写入缓存的那部分按 1.25x 计价，不再按 1x 加一遍。"""
        cents = pricing.cost_cents_from_tokens(
            tokens_in=800_000, tokens_out=0, cache_write=800_000, cache_read=0,
            rates=SOL)
        self.assertAlmostEqual(cents, 400.0)  # 0.8M × $5.00

    def test_four_kinds_use_their_own_rates(self):
        # 1M 未写入的输入 $4 + 1M 写入 $5 + 10M 缓存读 $4 + 0.1M 输出 $2
        cents = pricing.cost_cents_from_tokens(
            tokens_in=2_000_000, tokens_out=100_000,
            cache_write=1_000_000, cache_read=10_000_000, rates=SOL)
        self.assertAlmostEqual(cents, 1500.0)

    def test_does_not_overwrite_vendor_cost(self):
        daily = [row("2026-08-10", "gpt-5.6-sol", source="cursor",
                     cost_cents=12.0, **tokens(i=1_000_000))]
        filled = pricing.fill_list_prices(
            daily, {"gpt-5.6-sol": SOL}, {})
        self.assertAlmostEqual(filled[0]["cost_cents"], 12.0)

    def test_fills_missing_cost_from_the_price_table(self):
        daily = [row("2026-08-10", "gpt-5.6-sol", source="codex",
                     **tokens(i=1_000_000))]
        filled = pricing.fill_list_prices(
            daily, {"gpt-5.6-sol": SOL}, {})
        self.assertAlmostEqual(filled[0]["cost_cents"], 400.0)

    def test_price_alias_does_not_rename_the_model(self):
        daily = [row("2026-08-10", "codex-auto-review", source="codex",
                     **tokens(i=1_000_000))]
        filled = pricing.fill_list_prices(
            daily, {"gpt-5.6-sol": SOL},
            {"codex-auto-review": "gpt-5.6-sol"})
        self.assertEqual(filled[0]["model"], "codex-auto-review")
        self.assertAlmostEqual(filled[0]["cost_cents"], 400.0)

    def test_unknown_model_stays_unpriced(self):
        daily = [row("2026-08-10", "unknown", source="codex", **tokens(i=10))]
        filled = pricing.fill_list_prices(daily, {"gpt-5.6-sol": SOL}, {})
        self.assertNotIn("cost_cents", filled[0])

    def test_mixed_week_hero_is_a_single_dollar_amount(self):
        """有官方折算的和按牌价补上的加在同一个数字里，不再拼 Subscription。"""
        daily = pricing.fill_list_prices([
            row("2026-08-10", "opus", source="cursor", requests=1,
                cost_cents=100.0, **tokens(i=10)),
            row("2026-08-10", "gpt-5.6-sol", source="codex", requests=1,
                **tokens(i=1_000_000)),
        ], {"gpt-5.6-sol": SOL}, {})
        view = weekview.build_week_view(daily, WEEK, subscription_sources=["codex"])
        self.assertEqual(view["cost_display"], "$5.00")
        by_label = {m["label"]: m["cost_display"] for m in view["models"]}
        self.assertEqual(by_label["opus"], "$1.00")
        self.assertEqual(by_label["gpt-5.6-sol"], "$4.00")

    def test_build_stats_uses_committed_sol_list_price(self):
        daily = fold.fold_events([
            row("2026-08-10", "gpt-5.6-sol", source="codex",
                **tokens(i=1_000_000)),
        ])
        stats = fold.build_stats(daily)
        self.assertEqual(stats["weeks"][0]["view"]["cost_display"], "$4.00")
        self.assertAlmostEqual(stats["daily"][0]["cost_cents"], 400.0)


class TestFormatters(unittest.TestCase):
    def test_token_magnitudes(self):
        self.assertEqual(weekview.format_tokens(0), "0")
        self.assertEqual(weekview.format_tokens(999), "999")
        self.assertEqual(weekview.format_tokens(1_000), "1.0K")
        self.assertEqual(weekview.format_tokens(1_500_000), "1.5M")
        self.assertEqual(weekview.format_tokens(479_000_000), "479.0M")
        self.assertEqual(weekview.format_tokens(5_770_000_000), "5.77B")

    def test_missing_tokens_render_as_dash_not_zero(self):
        """不报 token 的源必须显示横线：显示 0 会被读成「一个 token 都没用」。"""
        self.assertEqual(weekview.format_tokens(None), "—")
        self.assertEqual(weekview.format_cost(None), "—")

    def test_cost_is_cents_to_dollars_with_grouping(self):
        self.assertEqual(weekview.format_cost(0), "$0.00")
        self.assertEqual(weekview.format_cost(4056), "$40.56")
        self.assertEqual(weekview.format_cost(405_519), "$4,055.19")

    def test_rounds_half_away_from_zero(self):
        """定点格式化在半分位上远离零取整，与 TS 侧逐位一致。

        0.125 和 0.25 在二进制里是精确值，所以是真正的平局。Python 内建的
        ``f"{0.125:.2f}"`` 走银行家舍入给出 0.12，这里必须是 0.13——JS 没有等价的
        银行家舍入内建，两边若各用内建，边界值就会显示成不同的数。
        """
        self.assertEqual(f"{0.125:.2f}", "0.12")        # 内建的行为，作为对照
        self.assertEqual(weekview._fixed(0.125, 2), "0.13")
        self.assertEqual(weekview._fixed(0.25, 1), "0.3")
        self.assertEqual(weekview._fixed(-0.25, 1), "-0.3")

    def test_day_and_range(self):
        self.assertEqual(weekview.format_day("2026-08-03"), "Aug 3")
        self.assertEqual(weekview.format_range("2026-08-10", "2026-08-16"),
                         "Aug 10 – Aug 16")

    def test_format_updated(self):
        self.assertEqual(
            weekview.format_updated("2026-08-18T17:20:00+08:00"),
            "Updated Aug 18, 17:20")
        self.assertEqual(weekview.format_updated(""), "")
        self.assertEqual(weekview.format_updated(None), "")


class TestBuildWeekView(unittest.TestCase):
    def _daily(self):
        return fold.fold_events([
            # 周内
            row("2026-08-10", "big", requests=10, cost_cents=1000.0,
                **tokens(i=100, o=50, cw=200, cr=9000)),
            row("2026-08-12", "big", requests=5, cost_cents=500.0,
                **tokens(i=50, o=25, cw=100, cr=4000)),
            row("2026-08-12", "small", requests=2, cost_cents=100.0,
                **tokens(i=10, o=5, cw=20, cr=900)),
            # 周外，不该出现
            row("2026-08-17", "next-week", requests=99, cost_cents=9999.0,
                **tokens(i=1)),
        ])

    def test_filters_to_the_week(self):
        view = weekview.build_week_view(self._daily(), WEEK)
        self.assertEqual({m["label"] for m in view["models"]}, {"big", "small"})
        self.assertEqual(view["basis"], "tokens")
        self.assertEqual([m["label"] for m in view["models"]], ["big", "small"])
        self.assertAlmostEqual(view["models"][0]["pct"],
                               (100 + 50 + 200 + 9000 + 50 + 25 + 100 + 4000)
                               / view["tokens_total"] * 100)

    def test_totals_are_summed_over_the_week(self):
        view = weekview.build_week_view(self._daily(), WEEK)
        self.assertEqual(view["requests"], 17)
        self.assertAlmostEqual(view["cost_cents"], 1600.0)
        self.assertEqual(view["tokens_total"], 100 + 50 + 200 + 9000
                         + 50 + 25 + 100 + 4000 + 10 + 5 + 20 + 900)

    def test_ranks_by_tokens_even_when_cost_is_available(self):
        """订阅源没有成本，排行必须按 token，不能再被 Cursor 的美元金额带走。"""
        daily = fold.fold_events([
            row("2026-08-10", "cheap-heavy", requests=1, cost_cents=1.0,
                **tokens(i=10_000)),
            row("2026-08-10", "pricey-light", requests=1, cost_cents=9_999.0,
                **tokens(i=10)),
        ])
        view = weekview.build_week_view(daily, WEEK)
        self.assertEqual(view["basis"], "tokens")
        self.assertEqual([m["label"] for m in view["models"]],
                         ["cheap-heavy", "pricey-light"])

    def test_unpriced_subscription_source_renders_as_label_not_dash(self):
        """牌价表里没有的模型才退回 Subscription，不能写成横线或 $0。"""
        daily = fold.fold_events([
            row("2026-08-10", "unknown", source="codex", requests=3,
                **tokens(i=100, o=20, cr=800)),
        ])
        view = weekview.build_week_view(daily, WEEK,
                                       subscription_sources=["codex"])
        self.assertEqual(view["cost_display"], "Subscription")
        self.assertEqual(view["models"][0]["cost_display"], "Subscription")

    def test_priced_subscription_does_not_join_the_hero_cell(self):
        """有金额时主数字只留美元。未入表的订阅模型仍在行里标 Subscription。"""
        daily = fold.fold_events([
            row("2026-08-10", "opus", source="cursor", requests=1,
                cost_cents=100.0, **tokens(i=10)),
            row("2026-08-10", "mimo-v2.5", source="codex", requests=1,
                **tokens(i=20)),
        ])
        view = weekview.build_week_view(daily, WEEK,
                                       subscription_sources=["codex"])
        self.assertEqual(view["cost_display"], "$1.00")
        by_label = {m["label"]: m["cost_display"] for m in view["models"]}
        self.assertEqual(by_label["opus"], "$1.00")
        self.assertEqual(by_label["mimo-v2.5"], "Subscription")

    def test_same_model_from_two_sources_is_aggregated(self):
        """展示层按模型聚合；两个 ADE 的同名模型合成一行。"""
        daily = fold.fold_events([
            row("2026-08-10", "gpt-5.5", source="codex", requests=2,
                **tokens(i=100)),
            row("2026-08-10", "gpt-5.5", source="cursor", requests=3,
                **tokens(i=50)),
        ])
        view = weekview.build_week_view(daily, WEEK,
                                       subscription_sources=["codex"])
        self.assertEqual(len(view["models"]), 1)
        self.assertEqual(view["models"][0]["requests"], 5)
        self.assertEqual(view["models"][0]["tokens_total"], 150)

    def test_falls_back_to_requests_when_no_tokens_or_cost(self):
        daily = fold.fold_events([
            row("2026-08-10", "a", requests=3),
            row("2026-08-10", "b", requests=9),
        ])
        view = weekview.build_week_view(daily, WEEK)
        self.assertEqual(view["basis"], "requests")
        self.assertEqual([m["label"] for m in view["models"]], ["b", "a"])
        self.assertEqual(view["tokens_display"], "—")

    def test_breakdown_is_in_fixed_order_not_by_size(self):
        """配色按分类固定，不能因为某周缓存占比变了就换颜色。"""
        view = weekview.build_week_view(self._daily(), WEEK)
        self.assertEqual([s["kind"] for s in view["breakdown"]],
                         list(TOKEN_KINDS))
        self.assertEqual([s["label"] for s in view["breakdown"]],
                         ["Input", "Output", "Cache write", "Cache read"])
        self.assertAlmostEqual(sum(s["pct"] for s in view["breakdown"]), 100.0)

    def test_days_always_has_seven_entries(self):
        """没有用量的那天也要在，否则日条形图的横轴会随数据伸缩。"""
        view = weekview.build_week_view(self._daily(), WEEK)
        self.assertEqual(len(view["days"]), 7)
        self.assertEqual([d["date"] for d in view["days"]][0], "2026-08-10")
        self.assertEqual([d["date"] for d in view["days"]][-1], "2026-08-16")
        self.assertEqual(view["days"][1]["requests"], 0)      # 周二无用量
        self.assertEqual(view["days"][2]["requests"], 7)      # 周三 5 + 2
        self.assertEqual(view["days"][2]["tokens_display"],
                         weekview.format_tokens(view["days"][2]["tokens_total"]))
        self.assertEqual(view["days"][1]["tokens_display"], "—")
        self.assertEqual([d["weekday"] for d in view["days"]],
                         ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

    def test_limit_truncates_models(self):
        view = weekview.build_week_view(self._daily(), WEEK, limit=1)
        self.assertEqual([m["label"] for m in view["models"]], ["big"])

    def test_ties_break_on_label_so_both_languages_agree(self):
        daily = fold.fold_events([
            row("2026-08-10", "tie-b", requests=7),
            row("2026-08-10", "tie-a", requests=7),
        ])
        view = weekview.build_week_view(daily, WEEK)
        self.assertEqual([m["label"] for m in view["models"]], ["tie-a", "tie-b"])

    def test_empty_week(self):
        view = weekview.build_week_view(self._daily(), None)
        self.assertIsNone(view["week"])
        self.assertEqual(view["models"], [])
        self.assertEqual(view["tokens_display"], "—")

    def test_week_with_no_usage(self):
        view = weekview.build_week_view(
            [], {"week": "x", "start": "2026-08-10", "end": "2026-08-16"})
        self.assertEqual(view["requests"], 0)
        self.assertEqual(len(view["days"]), 7)
        self.assertEqual(view["breakdown"], [])


class TestCursorCollector(unittest.TestCase):
    def _raw(self):
        ms = int(datetime(2026, 8, 17, 10, 0, tzinfo=TZ).timestamp() * 1000)
        return [
            {"timestamp": ms, "model": "opus", "kind": "USAGE_EVENT_KIND_USAGE_BASED",
             "tokenUsage": {"inputTokens": 10, "outputTokens": 20,
                            "cacheWriteTokens": 30, "cacheReadTokens": 40,
                            "totalCents": 1.5}},
            {"timestamp": ms, "model": "opus",
             "kind": "USAGE_EVENT_KIND_INCLUDED_IN_BUSINESS",
             "tokenUsage": {"inputTokens": 1, "outputTokens": 2,
                            "cacheWriteTokens": 3, "cacheReadTokens": 4,
                            "totalCents": 0.25}},
            {"timestamp": ms, "model": "opus",
             "kind": "USAGE_EVENT_KIND_ERRORED_NOT_CHARGED", "tokenUsage": {}},
            {"timestamp": ms, "model": "opus",
             "kind": "USAGE_EVENT_KIND_ABORTED_NOT_CHARGED", "tokenUsage": {}},
        ]

    def _day_of(self, ms):
        return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d")

    def test_aggregates_by_day_and_model(self):
        events = cursor_collector.to_events(self._raw(), self._day_of)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.date, "2026-08-17")
        self.assertEqual(e.tokens_in, 11)
        self.assertEqual(e.cache_read, 44)
        self.assertAlmostEqual(e.cost_cents, 1.75)

    def test_skips_events_that_produced_no_tokens(self):
        """出错与中止的调用没有产生 token，计入会虚增请求数。"""
        events = cursor_collector.to_events(self._raw(), self._day_of)
        self.assertEqual(events[0].requests, 2)

    def test_included_in_business_still_counts(self):
        """套餐内的调用同样消耗了算力，本项目量的是消耗而不是账单。"""
        only = [r for r in self._raw()
                if r["kind"] == "USAGE_EVENT_KIND_INCLUDED_IN_BUSINESS"]
        events = cursor_collector.to_events(only, self._day_of)
        self.assertEqual(events[0].requests, 1)
        self.assertAlmostEqual(events[0].cost_cents, 0.25)

    def test_empty_input(self):
        self.assertEqual(cursor_collector.to_events([], self._day_of), [])


class TestChatgptCollector(unittest.TestCase):
    def _day_of(self, ts):
        return chatgpt_collector._day_of_timestamp(ts, TZ)

    def _records(self, *, provider="openai", model="gpt-5.6-sol", usage=None):
        usage = usage or {
            "input_tokens": 80453,
            "cached_input_tokens": 79616,
            "cache_write_input_tokens": 0,
            "output_tokens": 179,
            "reasoning_output_tokens": 41,
            "total_tokens": 80632,
        }
        return [
            {"type": "session_meta",
             "payload": {"model_provider": provider}},
            {"type": "turn_context", "payload": {"model": model}},
            {"timestamp": "2026-08-17T08:39:50.890Z", "type": "event_msg",
             "payload": {"type": "token_count",
                         "info": {"last_token_usage": usage,
                                  "total_token_usage": {
                                      "input_tokens": 999999,
                                      "output_tokens": 999999}}}},
        ]

    def test_any_provider_belongs_to_codex_ade(self):
        raw = chatgpt_collector.parse_rollout(self._records())
        events = chatgpt_collector.to_events(raw, self._day_of)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "codex")
        self.assertEqual(events[0].model, "gpt-5.6-sol")
        self.assertEqual(events[0].date, "2026-08-17")

    def test_input_does_not_double_count_cache(self):
        raw = chatgpt_collector.parse_rollout(self._records())
        e = chatgpt_collector.to_events(raw, self._day_of)[0]
        self.assertEqual(e.tokens_in, 80453 - 79616)
        self.assertEqual(e.cache_read, 79616)
        self.assertEqual(e.tokens_out, 179)
        self.assertEqual(e.cache_write, 0)
        self.assertIsNone(e.cost_cents)
        # reasoning 是 output 的子集，总量不应再加一次
        self.assertEqual(e.tokens_total, (80453 - 79616) + 179 + 0 + 79616)

    def test_relay_provider_still_belongs_to_codex(self):
        raw = chatgpt_collector.parse_rollout(
            self._records(provider="krill", model="gpt-5.5"))
        events = chatgpt_collector.to_events(raw, self._day_of)
        self.assertEqual(events[0].source, "codex")
        self.assertEqual(events[0].model, "gpt-5.5")

    def test_skips_zero_token_counts(self):
        raw = chatgpt_collector.parse_rollout(self._records(usage={
            "input_tokens": 0, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 0,
        }))
        self.assertEqual(chatgpt_collector.to_events(raw, self._day_of), [])

    def test_does_not_sum_cumulative_total(self):
        """同一会话两条 last，总量应是两次 last 之和，不是 total_token_usage。"""
        records = self._records(usage={
            "input_tokens": 10, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 2,
        })
        records.append({
            "timestamp": "2026-08-17T09:00:00.000Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": 30, "cached_input_tokens": 20,
                    "cache_write_input_tokens": 0, "output_tokens": 4,
                },
                "total_token_usage": {
                    "input_tokens": 40, "output_tokens": 6,
                },
            }},
        })
        events = chatgpt_collector.to_events(
            chatgpt_collector.parse_rollout(records), self._day_of)
        self.assertEqual(events[0].requests, 2)
        self.assertEqual(events[0].tokens_in, 10 + (30 - 20))
        self.assertEqual(events[0].tokens_out, 6)
        self.assertEqual(events[0].cache_read, 20)

    def test_source_is_always_codex_ade(self):
        self.assertEqual(chatgpt_collector.source_for_provider("openai"),
                         "codex")
        self.assertEqual(chatgpt_collector.source_for_provider("xiaomi-mimo"),
                         "codex")
        self.assertEqual(chatgpt_collector.source_for_provider(None), "codex")

    def test_default_codex_home_is_cross_platform(self):
        self.assertEqual(chatgpt_collector._codex_home({}),
                         Path.home() / ".codex")

    def test_codex_home_expands_windows_environment_syntax(self):
        with mock.patch.dict(
                os.environ, {"CODEX_TEST_HOME": str(Path("test-home"))}):
            self.assertEqual(
                chatgpt_collector._codex_home({
                    "codex_home": "%CODEX_TEST_HOME%/.codex",
                }),
                Path("test-home") / ".codex",
            )


class TestDeepseekCollector(unittest.TestCase):
    USD_CNY = 7.2

    def _zip(self, amount: str, cost: str, *,
             amount_name="amount-2026-9.csv",
             cost_name="cost-2026-9.csv") -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"usage_data_2026_9/{amount_name}", amount)
            zf.writestr(f"usage_data_2026_9/{cost_name}", cost)
        return buf.getvalue()

    def _legacy_amount(self) -> str:
        return (
            "user_id,utc_date,model,api_key_name,api_key,type,price,amount\n"
            "uuid-1,2026-09-11,deepseek-flash,work,sk-SECRETKEY,request_count,,100\n"
            "uuid-1,2026-09-11,deepseek-flash,work,sk-SECRETKEY,output_tokens,4,10\n"
            "uuid-1,2026-09-11,deepseek-flash,work,sk-SECRETKEY,"
            "input_cache_miss_tokens,1,20\n"
            "uuid-1,2026-09-11,deepseek-flash,work,sk-SECRETKEY,"
            "input_cache_hit_tokens,0.02,400\n"
            "uuid-1,2026-09-11,deepseek-flash,home,sk-OTHERKEY,request_count,,50\n"
            "uuid-1,2026-09-11,deepseek-flash,home,sk-OTHERKEY,output_tokens,4,5\n"
        )

    def _legacy_cost(self) -> str:
        return (
            "user_id,utc_date,model,wallet_type,cost,currency\n"
            "uuid-1,2026-09-11,deepseek-flash,Paid,-1.44,CNY\n"
            "uuid-1,2026-09-11,deepseek-flash,Paid,-0.72,CNY\n"
        )

    def test_legacy_headers_map_tokens_and_abs_cost(self):
        amount = deepseek_collector.parse_csv(self._legacy_amount())
        cost = deepseek_collector.parse_csv(self._legacy_cost())
        events = deepseek_collector.to_events(
            amount, cost, usd_cny=self.USD_CNY)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.source, "deepseek")
        self.assertEqual(e.model, "deepseek-flash")
        self.assertEqual(e.date, "2026-09-11")
        self.assertEqual(e.requests, 150)
        self.assertEqual(e.tokens_in, 20)
        self.assertEqual(e.tokens_out, 15)
        self.assertEqual(e.cache_read, 400)
        self.assertIsNone(e.cache_write)
        self.assertAlmostEqual(e.cost_cents, 30.0)

    def test_iso_headers_use_beijing_date_part(self):
        amount = deepseek_collector.parse_csv(
            "start_time_iso,end_time_iso,model,api_key_name,api_key,type,"
            "price,amount\n"
            "2026-09-10T00:00:00+08:00,2026-09-11T00:00:00+08:00,"
            "deepseek-flash,work,sk-SECRET,request_count,,130\n"
            "2026-09-10T00:00:00+08:00,2026-09-11T00:00:00+08:00,"
            "deepseek-flash,work,sk-SECRET,output_tokens,4,8\n"
            "2026-09-10T00:00:00+08:00,2026-09-11T00:00:00+08:00,"
            "deepseek-flash,work,sk-SECRET,input_cache_miss_tokens,1,3\n"
            "2026-09-10T00:00:00+08:00,2026-09-11T00:00:00+08:00,"
            "deepseek-flash,work,sk-SECRET,input_cache_hit_tokens,0.02,90\n"
        )
        cost = deepseek_collector.parse_csv(
            "start_time_iso,end_time_iso,model,wallet_type,cost,currency\n"
            "2026-09-10T00:00:00+08:00,2026-09-11T00:00:00+08:00,"
            "deepseek-flash,Paid,-7.2,CNY\n"
        )
        e = deepseek_collector.to_events(amount, cost, usd_cny=self.USD_CNY)[0]
        self.assertEqual(e.date, "2026-09-10")
        self.assertEqual(e.requests, 130)
        self.assertEqual(e.tokens_in, 3)
        self.assertEqual(e.tokens_out, 8)
        self.assertEqual(e.cache_read, 90)
        self.assertAlmostEqual(e.cost_cents, 100.0)

    def test_compact_utc_date(self):
        amount = deepseek_collector.parse_csv(
            "utc_date,model,type,amount\n"
            "20260909,deepseek-v4-flash,request_count,1\n"
            "20260909,deepseek-v4-flash,output_tokens,970\n"
            "20260909,deepseek-v4-flash,input_cache_miss_tokens,390\n"
        )
        e = deepseek_collector.to_events(amount, [], usd_cny=self.USD_CNY)[0]
        self.assertEqual(e.date, "2026-09-09")
        self.assertEqual(e.tokens_out, 970)
        self.assertEqual(e.tokens_in, 390)
        self.assertIsNone(e.cost_cents)

    def test_empty_input(self):
        self.assertEqual(
            deepseek_collector.to_events([], [], usd_cny=self.USD_CNY), [])

    def test_extract_csvs_and_events_drop_secrets(self):
        blob = self._zip(self._legacy_amount(), self._legacy_cost())
        amount, cost = deepseek_collector.extract_csvs(blob)
        dumped = json.dumps(amount) + json.dumps(cost)
        self.assertNotIn("sk-", dumped)
        self.assertNotIn("SECRET", dumped)
        self.assertNotIn("user_id", dumped)
        events = deepseek_collector.to_events(
            amount, cost, usd_cny=self.USD_CNY)
        self.assertNotIn("sk-", json.dumps(events[0].to_dict()))

    def test_month_windows_cover_partial_months(self):
        windows = deepseek_collector._month_windows(
            "2026-08-15", "2026-09-14", TZ)
        self.assertEqual(len(windows), 2)
        start0, end0 = windows[0]
        start1, end1 = windows[1]
        self.assertEqual(
            datetime.fromtimestamp(start0, TZ).strftime("%Y-%m-%d"),
            "2026-08-15")
        self.assertEqual(
            datetime.fromtimestamp(end0, TZ).strftime("%Y-%m-%d"),
            "2026-09-01")
        self.assertEqual(
            datetime.fromtimestamp(start1, TZ).strftime("%Y-%m-%d"),
            "2026-09-01")
        self.assertEqual(
            datetime.fromtimestamp(end1, TZ).strftime("%Y-%m-%d"),
            "2026-09-15")


class TestCollectSeam(unittest.TestCase):
    def test_cursor_collect_uses_injected_fetch(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01")
        raw = TestCursorCollector()._raw()
        result = cursor_collector.collect(
            ctx, {"name": "cursor"}, fetch=lambda start, end: raw)
        self.assertFalse(result.machine_shard)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].requests, 2)
        self.assertIn("2026-08-17", result.days)

    def test_deepseek_collect_uses_injected_fetch(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-09-01",
                             as_of="2026-09-14")
        ds = TestDeepseekCollector()
        blob = ds._zip(ds._legacy_amount(), ds._legacy_cost())
        result = deepseek_collector.collect(
            ctx, {"name": "deepseek"},
            fetch=lambda start, end: blob, usd_cny=ds.USD_CNY)
        self.assertFalse(result.machine_shard)
        self.assertEqual(result.events[0].source, "deepseek")
        self.assertEqual(result.events[0].requests, 150)
        self.assertIn("2026-09-11", result.days)

    def test_persist_deepseek_account_level_writes_under_source(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-09-01",
                             as_of="2026-09-14")
        ds = TestDeepseekCollector()
        blob = ds._zip(ds._legacy_amount(), ds._legacy_cost())
        with tempfile.TemporaryDirectory() as tmp:
            ctx.root = Path(tmp)
            result = deepseek_collector.collect(
                ctx, {"name": "deepseek"},
                fetch=lambda start, end: blob, usd_cny=ds.USD_CNY)
            persist(ctx, result)
            paths = sorted(p.relative_to(ctx.root).as_posix()
                           for p in ctx.root.rglob("*.json"))
            self.assertEqual(paths, ["data/raw/deepseek/2026-09.json"])
            text = (ctx.root / "data" / "raw" / "deepseek" / "2026-09.json"
                    ).read_text(encoding="utf-8")
            self.assertNotIn("sk-", text)
            self.assertNotIn("user_id", text)
            self.assertNotIn("SECRET", text)

    def test_chatgpt_collect_uses_injected_rollouts(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01")
        records = TestChatgptCollector()._records()
        result = chatgpt_collector.collect(ctx, {}, rollouts=[records])
        self.assertTrue(result.machine_shard)
        self.assertEqual(result.events[0].source, "codex")

    def test_persist_account_level_writes_under_source(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01",
                             as_of="2026-08-31")
        with tempfile.TemporaryDirectory() as tmp:
            ctx.root = Path(tmp)
            result = cursor_collector.collect(
                ctx, {"name": "cursor"},
                fetch=lambda start, end: TestCursorCollector()._raw())
            persist(ctx, result)
            paths = sorted(p.relative_to(ctx.root).as_posix()
                           for p in ctx.root.rglob("*.json"))
            self.assertEqual(paths, ["data/raw/cursor/2026-08.json"])

    def test_persist_writes_empty_month_when_as_of_crosses_boundary(self):
        """负责区间跨月时，没有用量的那个月也要落盘，避免旧记录留成幽灵。"""
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01",
                             as_of="2026-09-01")
        with tempfile.TemporaryDirectory() as tmp:
            ctx.root = Path(tmp)
            result = cursor_collector.collect(
                ctx, {"name": "cursor"},
                fetch=lambda start, end: TestCursorCollector()._raw())
            persist(ctx, result)
            paths = sorted(p.relative_to(ctx.root).as_posix()
                           for p in ctx.root.rglob("*.json"))
            self.assertEqual(paths, [
                "data/raw/cursor/2026-08.json",
                "data/raw/cursor/2026-09.json",
            ])

    def test_persist_machine_shard_groups_by_event_source(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01",
                             machine="work-mac", as_of="2026-08-31")
        chat = TestChatgptCollector()
        with tempfile.TemporaryDirectory() as tmp:
            ctx.root = Path(tmp)
            result = chatgpt_collector.collect(
                ctx, {},
                rollouts=[chat._records(),
                          chat._records(provider="krill", model="gpt-5.5")])
            persist(ctx, result)
            paths = sorted(p.relative_to(ctx.root).as_posix()
                           for p in ctx.root.rglob("*.json"))
            self.assertEqual(paths, [
                "data/raw/codex/work-mac/2026-08.json",
            ])

    def test_persist_machine_shard_requires_machine(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01")
        result = chatgpt_collector.collect(
            ctx, {}, rollouts=[TestChatgptCollector()._records()])
        with self.assertRaises(SystemExit):
            persist(ctx, result)


class TestRunCollectIsolation(unittest.TestCase):
    def test_cursor_failure_does_not_block_codex(self):
        """账号级 Cursor 失败时，本机 Codex 仍要落盘。这是 Windows 定时任务的真实形状。"""
        class Boom:
            @staticmethod
            def collect(ctx, cfg):
                raise SystemExit("拿不到 Cursor 登录态")

        class Ok:
            @staticmethod
            def collect(ctx, cfg):
                return CollectResult(
                    events=[Event(date="2026-08-17", source="codex",
                                  model="x", requests=1, tokens_in=1,
                                  tokens_out=0, cache_write=0, cache_read=0)],
                    days=["2026-08-17"],
                    machine_shard=True,
                )

        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01",
                             machine="home-win")
        sources = [
            {"name": "cursor", "type": "cursor"},
            {"name": "chatgpt", "type": "chatgpt"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx.root = Path(tmp)
            with mock.patch.dict(COLLECTORS, {"cursor": Boom, "chatgpt": Ok}):
                count = run_collect(ctx, sources, only=None)
            self.assertEqual(count, 1)
            paths = sorted(p.relative_to(ctx.root).as_posix()
                           for p in ctx.root.rglob("*.json"))
            self.assertEqual(paths, ["data/raw/codex/home-win/2026-08.json"])

    def test_all_sources_failing_still_exits(self):
        class Boom:
            @staticmethod
            def collect(ctx, cfg):
                raise RuntimeError("network down")

        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01")
        with mock.patch.dict(COLLECTORS, {"cursor": Boom}):
            with self.assertRaises(SystemExit):
                run_collect(ctx, [{"name": "cursor", "type": "cursor"}],
                            only=None)

    def test_persist_config_error_is_not_swallowed(self):
        """缺 machine 是配置错误，不能被当成「这个源失败、别的继续」。"""
        class CursorOk:
            @staticmethod
            def collect(ctx, cfg):
                return CollectResult(
                    events=[Event(date="2026-08-17", source="cursor",
                                  model="x", requests=1)],
                    days=["2026-08-17"],
                    machine_shard=False,
                )

        class CodexOk:
            @staticmethod
            def collect(ctx, cfg):
                return CollectResult(
                    events=[Event(date="2026-08-17", source="codex",
                                  model="x", requests=1, tokens_in=1,
                                  tokens_out=0, cache_write=0, cache_read=0)],
                    days=["2026-08-17"],
                    machine_shard=True,
                )

        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-08-01")
        sources = [
            {"name": "cursor", "type": "cursor"},
            {"name": "chatgpt", "type": "chatgpt"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx.root = Path(tmp)
            with mock.patch.dict(COLLECTORS, {
                    "cursor": CursorOk, "chatgpt": CodexOk}):
                with self.assertRaises(SystemExit) as raised:
                    run_collect(ctx, sources, only=None)
            self.assertIn("machine", str(raised.exception))


class TestRawLayer(unittest.TestCase):
    def _event(self, date="2026-08-17", requests=3):
        return Event(date=date, source="cursor", model="x", requests=requests)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_events(root, "cursor", [self._event()], ["2026-08-17"])
            back = read_all_events(root)
            self.assertEqual(len(back), 1)
            self.assertEqual(back[0]["requests"], 3)
            self.assertEqual(back[0]["source"], "cursor")

    def test_rerun_is_idempotent(self):
        """同一天重复采集不应累积出重复记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for _ in range(3):
                write_events(root, "cursor", [self._event()], ["2026-08-17"])
            self.assertEqual(len(read_all_events(root)), 1)

    def test_untouched_days_survive(self):
        """只负责今天的一次采集，不能抹掉昨天已经记下的数据。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_events(root, "cursor", [self._event("2026-08-16")],
                         ["2026-08-16"])
            write_events(root, "cursor", [self._event("2026-08-17")],
                         ["2026-08-17"])
            self.assertEqual(sorted(r["date"] for r in read_all_events(root)),
                             ["2026-08-16", "2026-08-17"])

    def test_day_in_window_with_no_usage_is_cleared(self):
        """负责的那天若这次没有用量，旧记录要被清掉，不能留成幽灵。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_events(root, "cursor", [self._event()], ["2026-08-17"])
            write_events(root, "cursor", [], ["2026-08-17"])
            self.assertEqual(read_all_events(root), [])

    def test_sources_do_not_share_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for source in ("cursor", "deepseek"):
                write_events(root, source, [
                    Event(date="2026-08-17", source=source, model="x",
                          requests=1)], ["2026-08-17"])
            self.assertEqual(
                sorted(p.relative_to(root).as_posix()
                       for p in root.glob("data/raw/*/*.json")),
                ["data/raw/cursor/2026-08.json", "data/raw/deepseek/2026-08.json"])
            self.assertEqual(len(read_all_events(root)), 2)

    def test_machine_shard_does_not_collide_with_account_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_events(root, "codex", [Event(
                date="2026-08-17", source="codex", model="x", requests=1)],
                ["2026-08-17"], shard="work-mac")
            write_events(root, "codex", [Event(
                date="2026-08-17", source="codex", model="x", requests=2)],
                ["2026-08-17"], shard="home-win")
            paths = sorted(p.relative_to(root).as_posix()
                           for p in root.rglob("*.json"))
            self.assertEqual(paths, [
                "data/raw/codex/home-win/2026-08.json",
                "data/raw/codex/work-mac/2026-08.json",
            ])
            self.assertEqual(sum(r["requests"] for r in read_all_events(root)), 3)

    def test_legacy_machine_first_layout_is_ignored(self):
        """旧的 data/raw/<machine>/<source>/ 不能再折进总量。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "data" / "raw" / "work-mac" / "cursor" / "2026-08.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "source": "cursor", "month": "2026-08",
                "days": {"2026-08-17": [{
                    "date": "2026-08-17", "source": "cursor",
                    "model": "x", "requests": 9,
                }]},
            }), encoding="utf-8")
            write_events(root, "cursor", [self._event(requests=3)], ["2026-08-17"])
            back = read_all_events(root)
            self.assertEqual(len(back), 1)
            self.assertEqual(back[0]["requests"], 3)

    def test_spans_month_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_events(root, "cursor",
                         [self._event("2026-07-31"), self._event("2026-08-01")],
                         ["2026-07-31", "2026-08-01"])
            self.assertEqual(len(read_all_events(root)), 2)
            self.assertEqual(sorted(p.name for p in root.glob("data/raw/*/*.json")),
                             ["2026-07.json", "2026-08.json"])

    def test_corrupt_file_is_rebuilt_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "data" / "raw" / "cursor" / "2026-08.json"
            path.parent.mkdir(parents=True)
            path.write_text("{ 这不是 json", encoding="utf-8")
            write_events(root, "cursor", [self._event()], ["2026-08-17"])
            self.assertEqual(len(read_all_events(root)), 1)


class TestCollectContext(unittest.TestCase):
    def test_today_uses_as_of_when_set(self):
        ctx = CollectContext(tz=TZ, root=Path("."), as_of="2026-08-31")
        self.assertEqual(ctx.today(), "2026-08-31")

    def test_days_since_is_ascending_and_includes_today(self):
        ctx = CollectContext(tz=TZ, root=Path("."),
                             since="2026-08-10", as_of="2026-08-12")
        self.assertEqual(ctx.days_since(),
                         ["2026-08-10", "2026-08-11", "2026-08-12"])

    def test_days_between_is_empty_when_reversed(self):
        ctx = CollectContext(tz=TZ, root=Path("."))
        self.assertEqual(ctx.days_between("2026-08-12", "2026-08-10"), [])

    def test_day_of_uses_configured_tz_not_utc(self):
        """UTC+8 的凌晨零点半必须归到当天，而不是被 UTC 拉回前一天。"""
        ctx = CollectContext(tz=TZ, root=Path("."))
        ms = int(datetime(2026, 8, 17, 0, 30, tzinfo=TZ).timestamp() * 1000)
        self.assertEqual(ctx.day_of(ms), "2026-08-17")

    def test_since_ms_covers_the_whole_first_day(self):
        ctx = CollectContext(tz=TZ, root=Path("."), since="2026-07-01")
        self.assertEqual(ctx.day_of(ctx.since_ms()), "2026-07-01")


class TestContract(unittest.TestCase):
    def _stats(self):
        return fold.build_stats(fold.fold_events([
            row("2026-08-17", "m", requests=2, cost_cents=5.5, **tokens(i=1))]))

    def test_valid_stats_pass(self):
        self.assertEqual(validate_stats(self._stats()), [])

    def test_build_stats_embeds_week_view(self):
        stats = self._stats()
        self.assertIn("view", stats["weeks"][0])
        card = stats["weeks"][0]["view"]
        self.assertEqual(card["week"], stats["weeks"][0]["week"])
        self.assertIn("tokens_display", card)
        self.assertIn("tokens_display", card["days"][0])

    def test_missing_fields_are_reported(self):
        self.assertTrue(validate_stats({"daily": []}))

    def test_wrong_schema_version_is_reported(self):
        stats = self._stats()
        stats["schema_version"] = 2
        self.assertTrue(any("schema_version" in e for e in validate_stats(stats)))

    def test_null_token_field_is_reported(self):
        """显式的 null 意味着上游把「没有」和「零」搞混了，必须报出来。"""
        stats = self._stats()
        stats["daily"][0]["tokens_in"] = None
        self.assertTrue(any("tokens_in" in e for e in validate_stats(stats)))

    def test_negative_amount_is_reported(self):
        stats = self._stats()
        stats["daily"][0]["requests"] = -1
        self.assertTrue(any("requests" in e for e in validate_stats(stats)))

    def test_malformed_week_is_reported(self):
        stats = self._stats()
        stats["weeks"][0]["week"] = "2026-34"
        self.assertTrue(any("week" in e for e in validate_stats(stats)))

    def test_generated_stats_conforms(self):
        path = os.path.join(ROOT, "data", "stats.json")
        if not os.path.exists(path):
            self.skipTest("data/stats.json 尚未生成，先跑 python -m llm_usage --skip-collect")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(validate_stats(json.load(f)), [])


class TestSvgRenderer(unittest.TestCase):
    def _view(self, **over):
        daily = fold.fold_events([
            row("2026-08-10", "opus", requests=10, cost_cents=2650.0,
                **tokens(i=100, o=50, cw=200, cr=9000)),
            row("2026-08-11", "grok <x>", requests=4, cost_cents=430.0,
                **tokens(i=40, o=20, cw=80, cr=3000)),
        ])
        return weekview.build_week_view(daily, WEEK, **over)

    def test_escapes_labels(self):
        svg = render.render_svg(self._view())
        self.assertIn("grok &lt;x&gt;", svg)
        self.assertNotIn("grok <x>", svg)

    def test_shows_tokens_and_cost(self):
        svg = render.render_svg(self._view())
        self.assertIn("12.5K", svg)          # token 总量 12,490
        self.assertIn("$30.80", svg)         # 折算成本
        self.assertIn("14 requests", svg)
        self.assertIn("This week", svg)
        self.assertIn("Model cost", svg)

    def test_omits_cost_disclaimer(self):
        """口径说明放在 README，不印在卡片上。"""
        svg = render.render_svg(self._view())
        self.assertNotIn("非账单金额", svg)
        self.assertNotIn("缓存读取占", svg)
        self.assertNotIn("not a bill", svg.lower())
        import re
        self.assertIsNone(re.search(r"[\u4e00-\u9fff]", svg),
                          "card copy should be English")

    def test_height_grows_with_model_count(self):
        few = render.render_svg(weekview.build_week_view(
            fold.fold_events([row("2026-08-10", "only", cost_cents=1.0,
                                       **tokens(i=1))]), WEEK))
        many = render.render_svg(self._view())
        self.assertGreater(_svg_height(many), _svg_height(few))

    def test_theme_changes_colors(self):
        light = render.render_svg(self._view(), "light")
        dark = render.render_svg(self._view(), "dark")
        self.assertIn(render.THEMES["light"]["bg"], light)
        self.assertIn(render.THEMES["dark"]["bg"], dark)
        self.assertNotIn(render.THEMES["dark"]["bg"], light)

    def test_breakdown_bar_right_edge_is_flush(self):
        """末段吃掉舍入误差，否则四段之和会差出一两个像素的白缝。"""
        svg = render.render_svg(self._view())
        import re
        rects = [(float(x), float(w)) for x, w in
                 re.findall(r'<rect x="([\d.]+)" y="164" width="([\d.]+)"', svg)]
        self.assertTrue(rects)
        right = max(x + w for x, w in rects)
        self.assertAlmostEqual(right, render.CARD_W - render.PAD, places=3)

    def test_empty_week_renders_placeholder(self):
        svg = render.render_svg(weekview.build_week_view([], None))
        self.assertIn("No data", svg)

    def test_header_prints_updated_display(self):
        """刷新时刻进页眉右端，不另起一节，也不撑高卡片。"""
        stamp = "Updated Aug 18, 17:20"
        plain = render.render_svg(self._view())
        stamped = render.render_svg(self._view(), updated_display=stamp)
        self.assertIn(stamp, stamped)
        self.assertIn("This week", stamped)
        self.assertIn("Aug 10 – Aug 16", stamped)
        self.assertIn(f". {stamp}", stamped)
        self.assertEqual(_svg_height(stamped), _svg_height(plain))

    def test_has_accessible_label(self):
        svg = render.render_svg(self._view())
        self.assertIn("<title>", svg)
        self.assertIn('role="img"', svg)

    def test_render_files_writes_both_themes(self):
        stats = fold.build_stats(fold.fold_events([
            row("2026-08-10", "m", cost_cents=1.0, **tokens(i=1))]))
        original = render.ASSETS
        with tempfile.TemporaryDirectory() as tmp:
            render.ASSETS = Path(tmp)
            try:
                written = render.render_files(stats)
            finally:
                render.ASSETS = original
            self.assertEqual(sorted(p.name for p in written),
                             ["widget-dark.svg", "widget-light.svg"])


def _svg_height(svg: str) -> float:
    import re
    return float(re.search(r'height="([\d.]+)"', svg).group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
