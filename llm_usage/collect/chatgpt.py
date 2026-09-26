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

== 只采官方 provider ==

Source 是 ADE，不是计费后端，用量都归 ``codex``。但 Codex 日志里的
``model_provider`` 只有 ``openai``（ChatGPT 订阅）会被采集；``krill`` / ``custom`` /
``tencent_codebuddy`` 这类中转站的调用直接丢弃，见 ADR 0003。

负责范围按**全部** provider 的最早一天算，而不只看保留下来的事件：这样只有中转站
用量的日子也会被覆盖成空，旧 raw 里残留的中转站数据在重采时自然清掉。

== 模型名从哪来 ==

``turn_context.model`` 是主来源。Codex Desktop 的子代理会话会先写若干条
``token_count``，``turn_context`` 到中途才出现；这段时间的模型在
``thread_settings_applied.thread_settings.model`` 里。两者都没有时，用本会话里
第一个出现的模型回填，而不是记成 ``unknown``。

这是本机源：会话只存在于产生它的那台机器，两台机器都要采，文件按 machine 分片。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import CollectResult, Event, expand_user_path

OPENAI_PROVIDER = "openai"
ADE_SOURCE = "codex"


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
    model: str | None = None
    first_model: str | None = None
    provider = OPENAI_PROVIDER
    out: list[dict] = []
    for obj in records:
        typ = obj.get("type")
        payload = obj.get("payload") or {}
        if typ == "session_meta":
            provider = payload.get("model_provider") or provider
            continue
        if typ == "turn_context":
            model = payload.get("model") or model
            provider = payload.get("model_provider") or provider
            first_model = first_model or model
            continue
        if typ != "event_msg":
            continue
        if payload.get("type") == "thread_settings_applied":
            settings = payload.get("thread_settings") or {}
            model = settings.get("model") or model
            provider = settings.get("model_provider_id") or provider
            first_model = first_model or model
            continue
        if payload.get("type") != "token_count":
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
    for row in out:
        row["model"] = row["model"] or first_model or "unknown"
    return out


def _uncached_input(usage: dict) -> int:
    """``input_tokens`` 已含 cache read，减掉才不会和 ``cache_read`` 重复计入总量。"""
    inp = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    return max(inp - cached, 0)


def to_events(raw: list[dict], day_of) -> list[Event]:
    """把 ``parse_rollout`` 的结果按 (日期, 模型) 聚合成 ``Event``，只保留官方 provider。

    不填 ``cost_cents``：Plus / Pro 是订阅配额，接口不给官方单价。fold 阶段按
    公开 API 牌价补 API-equivalent 的 model cost，raw 保持原样。
    """
    buckets: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0,
                 "cache_write": 0, "cache_read": 0})

    for row in raw:
        if row.get("provider") != OPENAI_PROVIDER:
            continue
        usage = row.get("last_token_usage") or {}
        tokens_in = _uncached_input(usage)
        tokens_out = int(usage.get("output_tokens") or 0)
        cache_write = int(usage.get("cache_write_input_tokens") or 0)
        cache_read = int(usage.get("cached_input_tokens") or 0)
        if not (tokens_in or tokens_out or cache_write or cache_read):
            continue
        bucket = buckets[(day_of(row["timestamp"]), row.get("model") or "unknown")]
        bucket["requests"] += 1
        bucket["tokens_in"] += tokens_in
        bucket["tokens_out"] += tokens_out
        bucket["cache_write"] += cache_write
        bucket["cache_read"] += cache_read

    events = [
        Event(date=date, source=ADE_SOURCE, model=model, **bucket)
        for (date, model), bucket in buckets.items()
    ]
    events.sort(key=lambda e: (e.date, e.model))
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
    """扫描本机 Codex 会话日志并翻译。负责范围从日志里最早一天（含中转站）到今天。

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

    def day_of(ts):
        return _day_of_timestamp(ts, ctx.tz)

    events = [e for e in to_events(raw, day_of) if e.date >= ctx.since]
    relay = sum(1 for r in raw if r.get("provider") != OPENAI_PROVIDER)
    requests = sum(e.requests for e in events)
    print(f"[chatgpt] {n_files or 'fixture'} 个会话 → {len(events)} 条日模型记录"
          f"（codex {requests} 次" + (f"，丢弃中转站 {relay} 条" if relay else "") + "）")

    if not events:
        return CollectResult(events=[], days=[], machine_shard=True)
    first = min(d for d in map(day_of, (r["timestamp"] for r in raw)) if d >= ctx.since)
    return CollectResult(
        events=events,
        days=ctx.days_between(first, ctx.today()),
        machine_shard=True,
    )
