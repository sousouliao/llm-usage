"""DeepSeek 采集器：走控制台月度导出，拿日 × 模型的 token 与人民币账单。

== 为什么不用官方 API Key 接口 ==

``GET https://api.deepseek.com/user/balance`` 只返回当前余额，没有日期、模型、
token。``/v1/usage`` 一类路径返回 404。用量页背后的
``platform.deepseek.com/api/v0/usage/*`` 只认控制台会话：用 API Key 打是
HTTP 200 + ``code 40003``；只用 cookie、不带 Bearer 是 ``code 40002
Missing Token``。

所以认证必须是控制台 ``userToken``（``localStorage.userToken``），不是 ``sk-``
API Key。这和 Cursor 读 dashboard cookie 同一类：账号级、可回溯、漏跑可补，
会话会过期。

== 为什么走导出 ZIP，不走 JSON、不拦 chat/completions ==

用量页「导出」对应 ``GET /api/v0/usage/export?start=&end=&tz=28800``，一份 ZIP
里同时有 amount（token / 请求）和 cost（人民币账单）。CSV 的 ``type`` 列把
cache hit / miss / output 钉死，不必从图表堆叠顺序推断。``tz=28800`` 按北京
日历日分桶，和本仓库的 ``Asia/Shanghai`` 一致。

按请求拦截 ``chat/completions`` 的 ``usage`` 字段拿得到四类 token，但没有历史，
漏跑不可补。``openai_compatible`` 从未进过热路径，不复活。

== 字段 ==

    input_cache_miss_tokens  → tokens_in
    input_cache_hit_tokens   → cache_read
    output_tokens            → tokens_out
    request_count            → requests

DeepSeek 的磁盘 KV 缓存不收写入费，没有 cache write 口径，字段省略（不是 0）。

金额用 cost CSV 的 ``cost``（负号是扣费，取绝对值），人民币元按
``config/aggregate.yaml`` 的 ``usd_cny`` 折成美元分写入 ``cost_cents``。不用
公开牌价重算：2026-09 起分峰谷，日合计 token 恢复不出高峰占比，账单已经含峰谷。

CSV 里的 ``api_key`` / ``user_id`` / ``wallet_type`` / ``api_key_name`` 不进
Event。跨 API Key 的同一天同一模型合并。账号级，不按机器分片。
"""
from __future__ import annotations

import csv
import io
import json
import os
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from . import CollectResult, Event

API_HOST = "https://platform.deepseek.com"
EXPORT_PATH = "/api/v0/usage/export"
TZ_OFFSET = 28800

TYPE_REQUEST = "request_count"
TYPE_OUT = "output_tokens"
TYPE_HIT = "input_cache_hit_tokens"
TYPE_MISS = "input_cache_miss_tokens"

_AMOUNT_KEEP = (
    "utc_date", "start_time_iso", "end_time_iso", "model", "type", "amount",
)
_COST_KEEP = (
    "utc_date", "start_time_iso", "end_time_iso", "model", "cost", "currency",
)


# ------------------------------------------------------------------------ 认证

def _platform_token(cfg: dict) -> str:
    """读控制台 userToken。接受裸字符串，也接受 localStorage 里那份 JSON。"""
    env_name = cfg.get("token_env", "DEEPSEEK_PLATFORM_TOKEN")
    raw = os.environ.get(env_name)
    if not raw or not raw.strip():
        raise SystemExit(
            "拿不到 DeepSeek 控制台登录态。打开 https://platform.deepseek.com/usage "
            "登录后，在开发者工具 Console 执行 "
            "JSON.parse(localStorage.getItem('userToken')).value ，把输出设为环境变量 "
            f"{env_name}。不能用 sk- 开头的 API Key。")
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            token = parsed.get("value") or parsed.get("token")
            if token:
                return str(token).strip()
    return raw


# ------------------------------------------------------------------------ 拉取

def fetch_export(token: str, start: int, end: int) -> bytes:
    """下载一个时间窗口的用量 ZIP。``start`` / ``end`` 是北京午夜的 unix 秒。"""
    url = (f"{API_HOST}{EXPORT_PATH}"
           f"?start={int(start)}&end={int(end)}&tz={TZ_OFFSET}")
    req = urllib.request.Request(url, method="GET")
    for key, value in (
        ("Authorization", f"Bearer {token}"),
        ("Accept", "application/octet-stream"),
        ("Origin", API_HOST),
        ("Referer", f"{API_HOST}/usage"),
        # urllib 默认 UA 会被平台当成爬虫拦成 HTML 429。
        ("User-Agent",
         "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
         "AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/131.0.0.0 Safari/537.36"),
    ):
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            raise SystemExit(
                f"DeepSeek 导出拒绝了请求（HTTP {exc.code}）：{detail}\n"
                "userToken 可能已过期，重新登录 platform.deepseek.com 后再复制。") from exc
        raise SystemExit(f"DeepSeek 导出返回 HTTP {exc.code}：{detail}") from exc


def _month_windows(since: str, today: str, tz) -> list[tuple[int, int]]:
    """[since, today] 覆盖到的每个北京自然月 → (start_unix, end_unix)。

    当月的 end 是明天零点，好把今天整天含进去。
    """
    first = datetime.strptime(since, "%Y-%m-%d").date()
    last = datetime.strptime(today, "%Y-%m-%d").date()
    if last < first:
        return []
    windows: list[tuple[int, int]] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        month_start = Date(year, month, 1)
        month_end = (Date(year + 1, 1, 1) if month == 12
                     else Date(year, month + 1, 1))
        lo = max(month_start, first)
        hi = min(month_end, last + timedelta(days=1))
        if lo < hi:
            start_ts = int(datetime(lo.year, lo.month, lo.day, tzinfo=tz).timestamp())
            end_ts = int(datetime(hi.year, hi.month, hi.day, tzinfo=tz).timestamp())
            windows.append((start_ts, end_ts))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return windows


# ------------------------------------------------------------------------ CSV

def _project(row: dict, keep: tuple[str, ...]) -> dict:
    return {key: row[key] for key in keep if key in row}


def parse_csv(text: str) -> list[dict]:
    """把一份 CSV 读成行字典。表头去 BOM，值去空白。"""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v if v is not None else "").strip()
                     for k, v in raw.items()})
    return rows


def extract_csvs(blob: bytes) -> tuple[list[dict], list[dict]]:
    """从导出 ZIP 里抽出 amount / cost 行。只保留非密钥列。"""
    if not blob:
        return [], []
    if blob[:2] != b"PK":
        snippet = blob[:200].decode("utf-8", "replace")
        raise SystemExit(f"DeepSeek 导出不是 zip：{snippet}")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"DeepSeek 导出 zip 损坏：{exc}") from exc
    with zf:
        amount_text = _read_csv_member(zf, "amount")
        cost_text = _read_csv_member(zf, "cost")
    amount = [_project(row, _AMOUNT_KEEP) for row in parse_csv(amount_text)] if amount_text else []
    cost = [_project(row, _COST_KEEP) for row in parse_csv(cost_text)] if cost_text else []
    return amount, cost


def _read_csv_member(zf: zipfile.ZipFile, prefix: str) -> str | None:
    names = [
        name for name in zf.namelist()
        if not name.endswith("/")
        and PurePosixPath(name).name.lower().startswith(prefix)
        and name.lower().endswith(".csv")
    ]
    if not names:
        return None
    return zf.read(names[0]).decode("utf-8", "replace")


def row_date(row: dict) -> str | None:
    """认 utc_date 与 start_time_iso。日期部分按平台 GMT+8 口径原样取。"""
    raw = (row.get("utc_date") or "").strip()
    if raw:
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        return raw[:10]
    iso = (row.get("start_time_iso") or "").strip()
    if iso:
        return iso[:10]
    return None


def _to_float(value: str | None) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def cny_to_cents(cny: float, usd_cny: float) -> float:
    """人民币元 → 美元分。``usd_cny`` 是 1 美元兑多少人民币。"""
    if usd_cny <= 0:
        raise ValueError("usd_cny 必须为正数")
    return round(abs(cny) / usd_cny * 100, 4)


# ------------------------------------------------------------------------ 翻译

def to_events(amount_rows: list[dict], cost_rows: list[dict], *,
              usd_cny: float = 7.2, source: str = "deepseek") -> list[Event]:
    """把 amount / cost 两份导出行聚合成 ``Event``。纯函数，方便单测。"""
    tokens: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cache_read": 0})
    for row in amount_rows:
        day = row_date(row)
        model = (row.get("model") or "").strip() or "unknown"
        if not day:
            continue
        bucket = tokens[(day, model)]
        kind = row.get("type") or ""
        if kind == TYPE_REQUEST:
            bucket["requests"] += _to_int(row.get("amount"))
        elif kind == TYPE_MISS:
            bucket["tokens_in"] += _to_int(row.get("amount"))
        elif kind == TYPE_OUT:
            bucket["tokens_out"] += _to_int(row.get("amount"))
        elif kind == TYPE_HIT:
            bucket["cache_read"] += _to_int(row.get("amount"))

    costs: dict[tuple[str, str], float] = defaultdict(float)
    billed: set[tuple[str, str]] = set()
    for row in cost_rows:
        day = row_date(row)
        model = (row.get("model") or "").strip() or "unknown"
        if not day:
            continue
        key = (day, model)
        costs[key] += abs(_to_float(row.get("cost")))
        billed.add(key)

    keys = set(tokens) | billed
    events = []
    for date, model in sorted(keys):
        bucket = tokens.get((date, model),
                            {"requests": 0, "tokens_in": 0, "tokens_out": 0,
                             "cache_read": 0})
        has_tokens = bucket["tokens_in"] or bucket["tokens_out"] or bucket["cache_read"]
        if not bucket["requests"] and not has_tokens and (date, model) not in billed:
            continue
        kwargs: dict = dict(
            date=date, source=source, model=model,
            requests=bucket["requests"],
            tokens_in=bucket["tokens_in"],
            tokens_out=bucket["tokens_out"],
            cache_read=bucket["cache_read"],
        )
        if (date, model) in billed:
            kwargs["cost_cents"] = cny_to_cents(costs[(date, model)], usd_cny)
        events.append(Event(**kwargs))
    return events


def collect(ctx, cfg: dict, *, fetch=None, usd_cny: float | None = None) -> CollectResult:
    """按月拉取导出 ZIP 并翻译。负责范围从最早一条事件到今天。

    ``fetch(start, end) -> bytes`` 是远端 adapter。默认打官方导出接口；
    测试传入 ZIP 夹具，不必碰网络或 userToken。
    """
    if usd_cny is None:
        from llm_usage.config import usd_cny as load_rate
        usd_cny = load_rate()
    if fetch is None:
        token = _platform_token(cfg)

        def fetch(start, end, _token=token):
            return fetch_export(_token, start, end)

    amount_rows: list[dict] = []
    cost_rows: list[dict] = []
    windows = _month_windows(ctx.since, ctx.today(), ctx.tz)
    for start, end in windows:
        blob = fetch(start, end)
        amount, cost = extract_csvs(blob)
        amount_rows.extend(amount)
        cost_rows.extend(cost)

    source = cfg.get("name", "deepseek")
    events = to_events(amount_rows, cost_rows, usd_cny=usd_cny, source=source)
    events = [e for e in events if ctx.since <= e.date <= ctx.today()]

    print(f"[deepseek] {len(windows)} 个月导出 → {len(events)} 条日模型记录"
          f"，起点 {ctx.since}")

    if not events:
        return CollectResult(events=[], days=[], machine_shard=False)
    return CollectResult(
        events=events,
        days=ctx.days_between(min(e.date for e in events), ctx.today()),
        machine_shard=False,
    )
