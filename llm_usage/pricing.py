"""按公开 API 牌价给没有官方 cost_cents 的日记录补上折算成本。

Cursor 的金额来自接口返回的 ``tokenUsage.totalCents``，采集时已经写入 raw。
Codex / Plus 与 Antigravity 的 raw 没有这个字段。展示层要的仍是「token × 模型
单价」，所以用 OpenAI / Gemini 公布的短上下文 API 牌价，在 fold 时补上。这是
API-equivalent，不是账单，也不是 ChatGPT credits。

raw 不改：牌价变了可以重跑 ``--skip-collect`` 重建产物。

cache write 按官方口径是 1.25× 未缓存输入价，替代重叠的那部分输入，而不是
再加一遍。日记录已经按天聚合，单次请求是否超过 272K 长上下文阈值无法恢复，
所以只用短上下文牌价。
"""
from __future__ import annotations

from typing import Any

from llm_usage.contract import TOKEN_KINDS

RateTable = dict[str, dict[str, float]]
AliasTable = dict[str, str]


def rates_for(model: str, prices: RateTable, aliases: AliasTable) -> dict[str, float] | None:
    key = aliases.get(model, model)
    rates = prices.get(key)
    if not rates:
        return None
    return rates


def cost_cents_from_tokens(
    *,
    tokens_in: int | float | None = 0,
    tokens_out: int | float | None = 0,
    cache_write: int | float | None = 0,
    cache_read: int | float | None = 0,
    rates: dict[str, float],
) -> float:
    """``rates`` 是美元 / 百万 token：``input`` / ``output`` / ``cache_read`` / ``cache_write``。"""
    inp = float(tokens_in or 0)
    out = float(tokens_out or 0)
    writes = float(cache_write or 0)
    cached = float(cache_read or 0)
    regular = max(inp - writes, 0.0)
    usd = (
        regular / 1_000_000 * rates["input"]
        + writes / 1_000_000 * rates["cache_write"]
        + cached / 1_000_000 * rates["cache_read"]
        + out / 1_000_000 * rates["output"]
    )
    return usd * 100


def fill_list_prices(
    daily: list[dict[str, Any]],
    prices: RateTable,
    aliases: AliasTable,
) -> list[dict[str, Any]]:
    """给缺 ``cost_cents`` 且能对上牌价的行补上折算值。已有官方金额的行不动。"""
    filled = []
    for row in daily:
        entry = dict(row)
        if entry.get("cost_cents") is None:
            rates = rates_for(entry.get("model") or "", prices, aliases)
            has_tokens = any(entry.get(kind) is not None for kind in TOKEN_KINDS)
            if rates is not None and has_tokens:
                entry["cost_cents"] = round(cost_cents_from_tokens(
                    tokens_in=entry.get("tokens_in"),
                    tokens_out=entry.get("tokens_out"),
                    cache_write=entry.get("cache_write"),
                    cache_read=entry.get("cache_read"),
                    rates=rates,
                ), 4)
        filled.append(entry)
    return filled
