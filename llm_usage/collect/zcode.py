"""ZCode 采集器：读本机 ZCode 会话库里的逐次 token。

== 为什么走本地会话库，不走账号接口 ==

GLM 编程套餐没有公开的用量历史接口。官方 ``glm-plan-usage`` 插件与
``open.bigmodel.cn/api/monitor/usage`` 只返回当前配额窗口的余量快照，不是逐次
消耗——和已排除的 Antigravity quota 端点同类。本机 ``model_usage`` 表是唯一的
逐次来源。

== 存储形态 ==

一个 SQLite：``~/.zcode/cli/db/db.sqlite``（两台机器布局相同）。库用 WAL，只读
打开也要写 ``-shm``，所以先把 ``.db`` 与 ``-wal`` 复制到临时目录再读（与
Antigravity 同一招）。

``model_usage`` 每次模型调用一行（实测 2026-10，ZCode 0.16.5）：

    started_at / completed_at（毫秒）   model_id，如 ``GLM-5.3``
    status   input_tokens   output_tokens
    cache_creation_input_tokens   cache_read_input_tokens   raw_usage_json

- ``input_tokens`` **含**缓存读（OpenAI 式）：完成行全部满足
  ``provider_total_tokens = input + output`` 且 ``cache_read ≤ input``。落盘前先
  减掉，否则总量把缓存算两遍——与 Codex 的 ``input_tokens`` 同一口径。
- ``cache_creation_input_tokens`` 恒为 0：GLM 缓存是服务端隐式的，无写入计价。
  这是「报了为零」，落 0 而不是省略。
- 失败 / 取消 / 进行中的行 token 全为零，被「无用量不计请求」自然滤掉；``status``
  仍显式要求 ``completed``，防将来流式更新把进行中的行写成部分用量。

== 为什么不用 rollout 日志 ==

``~/.zcode/cli/rollout/model-io-*.jsonl`` 也带同样的 usage，但它是轮转日志
（``modelIoFullRetentionEnabled``），10 个会话只剩 3 个文件；``model_usage`` 全部
行都在。两者交叉验证过：逐次数字一致。

== 口径与风险 ==

- 删会话会级联删掉它的 ``model_usage`` 行（``on delete cascade``），那部分历史
  下次重采时消失——与 Codex / Antigravity 同类风险，每日采集落进 git 即是缓解。
- 量的是 ZCode 这个工具的消耗。同一编程套餐的 API key 若还挂在 Claude Code、
  Cline 等其他工具上，那些消耗不在这张表里。
- 表结构没有公开契约，靠实测。列缺失或 ``cache_read > input``（input 口径漂移的
  信号）视为格式变更，本次不写 raw，而不是把错位的数字静默落盘。

这是本机源：会话只存在于产生它的那台机器，两台机器都要采，文件按 machine 分片。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path

from . import CollectResult, Event, expand_user_path

ADE_SOURCE = "zcode"

DEFAULT_DB = Path(".zcode") / "cli" / "db" / "db.sqlite"

REQUIRED_COLUMNS = frozenset({
    "model_id", "status", "started_at", "completed_at",
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
})

_SELECT = """
    SELECT model_id, status,
           COALESCE(completed_at, started_at) AS at_ms,
           input_tokens, output_tokens,
           cache_creation_input_tokens, cache_read_input_tokens
    FROM model_usage
"""


class FormatDrift(ValueError):
    """``model_usage`` 表不再符合实测的字段语义。"""


# ---------------------------------------------------------------- 纯函数

def _usage_of(row: dict) -> tuple[int, int, int, int] | None:
    """一行 → ``(净输入, 输出, 缓存写, 缓存读)``；没有用量的行返回 ``None``。"""
    inp = int(row.get("input_tokens") or 0)
    out = int(row.get("output_tokens") or 0)
    writes = int(row.get("cache_creation_input_tokens") or 0)
    cached = int(row.get("cache_read_input_tokens") or 0)
    if cached > inp:
        raise FormatDrift(f"缓存读 {cached} 超过输入 {inp}，input 口径疑似已变")
    if not (inp or out or writes or cached):
        return None
    return max(inp - cached, 0), out, writes, cached


def to_events(rows: list[dict], day_of) -> list[Event]:
    """把 ``model_usage`` 行按 (日期, 模型) 聚合成 ``Event``。

    ``day_of`` 接收 ``COALESCE(completed_at, started_at)`` 的毫秒时间戳：用量在
    响应完成时才定型，跨午夜的请求归到 token 实际生成完的那天。

    不填 ``cost_cents``：编程套餐是订阅配额，接口不给官方单价。fold 阶段按
    Z.AI 公开牌价补 API-equivalent 的 model cost，raw 保持原样。
    """
    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0,
                 "cache_write": 0, "cache_read": 0})

    for row in rows:
        if row.get("status") != "completed":
            continue
        usage = _usage_of(row)
        if usage is None:
            continue
        bucket = buckets[(day_of(row["at_ms"]), row.get("model_id") or "unknown")]
        bucket["requests"] += 1
        for kind, value in zip(("tokens_in", "tokens_out", "cache_write",
                                "cache_read"), usage):
            bucket[kind] += value

    events = [
        Event(date=date, source=ADE_SOURCE, model=model, **bucket)
        for (date, model), bucket in buckets.items()
    ]
    events.sort(key=lambda e: (e.date, e.model))
    return events


# ---------------------------------------------------------------- 磁盘

def _db_path(cfg: dict) -> Path:
    configured = cfg.get("zcode_db")
    if not configured:
        return Path.home() / DEFAULT_DB
    return expand_user_path(configured)


def _read_db(path: Path) -> list[dict]:
    """把库与 WAL 复制到临时目录再读，返回 ``_SELECT`` 的行。"""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        shutil.copyfile(path, copy)
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        con = sqlite3.connect(copy)
        try:
            names = {r[1] for r in con.execute("PRAGMA table_info(model_usage)")}
            missing = REQUIRED_COLUMNS - names
            if missing:
                raise FormatDrift(f"model_usage 缺少列: {sorted(missing)}")
            cursor = con.execute(_SELECT)
            columns = [d[0] for d in cursor.description]
            rows = [dict(zip(columns, r)) for r in cursor]
        finally:
            con.close()
    return rows


def collect(ctx, cfg: dict, *, rows=None) -> CollectResult:
    """读本机 ZCode 会话库并翻译。负责范围从最早一条用量到今天。

    ``rows`` 是本机库 adapter：``model_usage`` 行的列表。默认读磁盘；测试传入
    内存里的行，不必碰 ``~/.zcode``。

    读库或自检出错时整源放弃、不写 raw：少读一段历史就会把那几天的旧数据覆盖成
    偏小的值，宁可这次不更新。
    """
    empty = CollectResult(events=[], days=[], machine_shard=True)
    if rows is None:
        path = _db_path(cfg)
        if not path.is_file():
            print(f"[zcode] 找不到 {path}，跳过")
            return empty
        try:
            rows = _read_db(path)
        except FormatDrift as exc:
            print(f"[zcode] 存储格式疑似变更，本次不写 raw：{exc}")
            return empty
        except (OSError, sqlite3.Error) as exc:
            print(f"[zcode] 读取会话库失败，本次不写 raw：{exc}")
            return empty

    try:
        events = [e for e in to_events(rows, ctx.day_of) if e.date >= ctx.since]
    except FormatDrift as exc:
        print(f"[zcode] 存储格式疑似变更，本次不写 raw：{exc}")
        return empty

    requests = sum(e.requests for e in events)
    print(f"[zcode] model_usage {len(rows)} 行 → {len(events)} 条日模型记录"
          f"（{requests} 次）")

    if not events:
        return empty
    return CollectResult(
        events=events,
        days=ctx.days_between(min(e.date for e in events), ctx.today()),
        machine_shard=True,
    )
