"""ChatGPT / Codex 采集器：读本机 ``~/.codex`` 会话日志里的逐次 token。

== 为什么走 jsonl，不走账号接口、也不走 sqlite ==

ChatGPT Plus / Pro 没有官方的逐次 token 用量接口。``/backend-api/wham/usage``
只返回 5 小时 / 7 日窗口的占用百分比，不是历史消耗。OpenAI Platform 的
Admin Usage API 只覆盖 API key，和订阅用量不相交。

``state_5.sqlite`` 的 ``threads.tokens_used`` 是会话累计总数，没有输入 / 输出 /
缓存拆分，跨天会话也无法按天切开——和已经排除的 WorkBuddy ``used`` 同类。

会话 jsonl 里每次模型返回都会写一条 ``token_count``，带 ``last_token_usage``：

    input_tokens, output_tokens, cached_input_tokens, cache_write_input_tokens

``last_token_usage`` 是当次增量；同条里的 ``total_token_usage`` 是会话累计，
不能拿来加。``input_tokens`` 已经包含 cache read，落盘时要先减掉，否则
``tokens_total`` 会把缓存算两遍。``reasoning_output_tokens`` 是 output 的子集，
不再单列。

== 源怎么拆 ==

Source 是 ADE，不是计费后端。Codex 日志里的 ``model_provider``（``openai`` /
``krill`` / ``custom`` / …）只说明当时打向哪个接口；无论走 ChatGPT Plus 还是
中转站，用量都归 ``codex``。Cursor 走官方接口，归 ``cursor``。

这是本机源：会话只存在于产生它的那台机器，两台机器都要采，文件按 machine 分片。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import CollectResult, Event, expand_user_path

# ChatGPT 订阅走的官方 provider，记在日志里但不决定 Source。
OPENAI_PROVIDER = "openai"
ADE_SOURCE = "codex"


def source_for_provider(provider: str | None) -> str:
    """Codex 的任何 provider 都归 ADE ``codex``。保留函数是为了测试与旧调用。"""
    del provider
    return ADE_SOURCE


def _day_of_timestamp(ts: str | int | float, tz) -> str:
    if isinstance(ts, (int, float)):
        seconds = ts / 1000 if ts > 1e12 else ts
        return datetime.fromtimestamp(seconds, tz).strftime("%Y-%m-%d")
    return (datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            .astimezone(tz).strftime("%Y-%m-%d"))


def parse_rollout(records: list[dict]) -> list[dict]:
    """从一份 jsonl 的记录里抽出 ``token_count`` 行。纯函数，方便单测。

    只看用量字段，不读消息正文、路径、标题。
    """
    model = "unknown"
    provider = OPENAI_PROVIDER
    out: list[dict] = []
    for obj in records:
        typ = obj.get("type")
        payload = obj.get("payload") or {}
        if typ == "session_meta":
            if payload.get("model_provider"):
                provider = payload["model_provider"]
            continue
        if typ == "turn_context":
            if payload.get("model"):
                model = payload["model"]
            if payload.get("model_provider"):
                provider = payload["model_provider"]
            continue
        if typ != "event_msg" or payload.get("type") != "token_count":
            continue
        ts = obj.get("timestamp")
        if not ts:
            continue
        usage = (payload.get("info") or {}).get("last_token_usage") or {}
        out.append({
            "timestamp": ts,
            "model": model,
            "provider": provider,
            "last_token_usage": usage,
        })
    return out


def _uncached_input(usage: dict) -> int:
    """``input_tokens`` 已含 cache read，减掉才不会和 ``cache_read`` 重复计入总量。"""
    inp = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    return max(inp - cached, 0)


def to_events(raw: list[dict], day_of) -> list[Event]:
    """把 ``parse_rollout`` 的结果按 (日期, 源, 模型) 聚合成 ``Event``。

    不填 ``cost_cents``：Plus / Pro 是订阅配额，接口不给官方单价。fold 阶段按
    公开 API 牌价补 API-equivalent 的 model cost，raw 保持原样。
    """
    buckets: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0,
                 "cache_write": 0, "cache_read": 0})

    for row in raw:
        usage = row.get("last_token_usage") or {}
        tokens_in = _uncached_input(usage)
        tokens_out = int(usage.get("output_tokens") or 0)
        cache_write = int(usage.get("cache_write_input_tokens") or 0)
        cache_read = int(usage.get("cached_input_tokens") or 0)
        if not (tokens_in or tokens_out or cache_write or cache_read):
            continue
        key = (day_of(row["timestamp"]),
               source_for_provider(row.get("provider")),
               row.get("model") or "unknown")
        bucket = buckets[key]
        bucket["requests"] += 1
        bucket["tokens_in"] += tokens_in
        bucket["tokens_out"] += tokens_out
        bucket["cache_write"] += cache_write
        bucket["cache_read"] += cache_read

    events = [
        Event(date=date, source=source, model=model,
              requests=bucket["requests"],
              tokens_in=bucket["tokens_in"],
              tokens_out=bucket["tokens_out"],
              cache_write=bucket["cache_write"],
              cache_read=bucket["cache_read"])
        for (date, source, model), bucket in buckets.items()
    ]
    events.sort(key=lambda e: (e.date, e.source, e.model))
    return events


def _jsonl_files(home: Path) -> list[Path]:
    files: list[Path] = []
    sessions = home / "sessions"
    if sessions.is_dir():
        files.extend(sessions.rglob("*.jsonl"))
    archived = home / "archived_sessions"
    if archived.is_dir():
        files.extend(archived.glob("*.jsonl"))
    return files


def _load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _codex_home(cfg: dict) -> Path:
    """返回本机 Codex 数据目录，兼容两种平台的环境变量写法。

    默认用 ``Path.home()``，因此 Windows 会自然落到
    ``C:\\Users\\<user>\\.codex``。显式配置同时支持 ``~``、``$HOME`` /
    ``${HOME}`` 和 Windows 常见的 ``%USERPROFILE%``。
    """
    configured = cfg.get("codex_home")
    if not configured:
        return Path.home() / ".codex"
    return expand_user_path(configured)


def collect(ctx, cfg: dict, *, rollouts=None) -> CollectResult:
    """扫描本机 Codex 会话日志并翻译。负责范围从最早一条事件到今天。

    ``rollouts`` 是本机日志 adapter：可迭代的 jsonl 记录列表。默认扫磁盘；
    测试传入内存里的会话，不必碰 ``~/.codex``。
    """
    n_files = 0
    if rollouts is None:
        home = _codex_home(cfg)
        if not home.is_dir():
            print(f"[chatgpt] 找不到 {home}，跳过")
            return CollectResult(events=[], days=[], machine_shard=True)
        files = _jsonl_files(home)
        n_files = len(files)

        def _from_disk():
            for path in files:
                try:
                    yield _load_records(path)
                except OSError as exc:
                    print(f"[warn] 读取 {path.name} 失败: {exc}")

        rollouts = _from_disk()

    raw: list[dict] = []
    for records in rollouts:
        raw.extend(parse_rollout(records))

    events = to_events(raw, lambda ts: _day_of_timestamp(ts, ctx.tz))
    events = [e for e in events if e.date >= ctx.since]

    by_source: dict[str, int] = defaultdict(int)
    for e in events:
        by_source[e.source] += e.requests
    detail = "，".join(f"{name} {n} 次" for name, n in sorted(by_source.items()))
    print(f"[chatgpt] {n_files or 'fixture'} 个会话 → {len(events)} 条日模型记录"
          + (f"（{detail}）" if detail else ""))

    if not events:
        return CollectResult(events=[], days=[], machine_shard=True)
    return CollectResult(
        events=events,
        days=ctx.days_between(min(e.date for e in events), ctx.today()),
        machine_shard=True,
    )
