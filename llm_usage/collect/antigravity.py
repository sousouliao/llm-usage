"""Antigravity 采集器：读本机会话库里逐次模型调用的 token。

== 为什么走本地会话库，不走账号接口 ==

Antigravity 没有公开的用量历史接口。客户端调用的 Cloud Code 内部接口
``v1internal:retrieveUserQuotaSummary`` / ``fetchAvailableModels`` 只返回 5 小时 /
每周窗口的 ``remainingFraction``，是余量快照，不是消耗。本地语言服务的
``GetUserAnalyticsSummary`` 能按区间查，但只有对话收发次数、补全接受数这类计数，
没有 token。和 Codex 一样，本机日志是唯一的逐次来源。

== 存储形态 ==

每个会话一个 SQLite：``~/.gemini/antigravity/conversations/<id>.db``（CLI 在同级的
``antigravity-cli`` 下，布局相同）。库用 WAL，只读打开也要写 ``-shm``，所以先把
``.db`` 与 ``-wal`` 复制到临时目录再读。

用量在 protobuf 里，没有公开 schema，字段编号来自实测（2026-09，1925 次调用）：

``steps.metadata``（每个步骤一条）
    1: Timestamp{1: 秒}    9: 用量子消息    12: 消息 ID
用量子消息
    2: 未缓存输入   5: 缓存读   3: 输出   9: 思考输出   10: 正文输出   11: 响应 ID
``gen_metadata.data``（每次生成一条）
    4: 消息 ID    1.19: 模型名，如 ``gemini-3.8-flash``

- 字段 2 **不含**缓存读（和 Codex 的 ``input_tokens`` 相反），不用做减法。
- 3 恒等于 9 + 10。它被当作格式自检：不成立就认为存储格式变了，本次不写 raw，
  而不是把错位的数字静默落盘。
- 缓存写（字段 4）从未出现，``cache_write`` 缺省为 ``None``，不写 0。
- ``gen_metadata`` 里还有一份相同的用量副本，只拿它的模型名，不计入用量；
  同一响应 ID 出现多次也只算一次。

只读 ``metadata`` / ``data`` 两列，不碰 ``step_payload`` 里的对话正文。

这是本机源：会话只存在于产生它的那台机器，删掉会话，那部分历史在下次重采时也会
消失。文件按 machine 分片。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import CollectResult, Event, expand_user_path

ADE_SOURCE = "antigravity"

_DEFAULT_DIRS = (
    Path(".gemini") / "antigravity" / "conversations",
    Path(".gemini") / "antigravity-cli" / "conversations",
)


class FormatDrift(ValueError):
    """会话库的 protobuf 不再符合实测的字段语义。"""


# ---------------------------------------------------------------- protobuf wire

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(buf):
            raise FormatDrift("varint 越界")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, i


def _fields(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """把一段 protobuf 拆成 ``(字段号, wire type, 值)``。不认识的 wire type 视为漂移。"""
    out: list[tuple[int, int, int | bytes]] = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        elif wire == 2:
            size, i = _varint(buf, i)
            value, i = bytes(buf[i:i + size]), i + size
        elif wire == 5:
            value, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        else:
            raise FormatDrift(f"未知 wire type {wire}")
        if i > len(buf):
            raise FormatDrift("字段越界")
        out.append((field, wire, value))
    return out


def _first(buf: bytes, *path: int) -> int | bytes | None:
    """按字段路径取第一个值；中间层必须是子消息。"""
    current: int | bytes | None = buf
    for field in path:
        if not isinstance(current, bytes):
            return None
        current = next((v for f, _, v in _fields(current) if f == field), None)
    return current


def _ints(buf: bytes) -> dict[int, int]:
    return {f: v for f, wire, v in _fields(buf) if wire == 0}


def _text(value: int | bytes | None) -> str | None:
    return value.decode("utf-8") if isinstance(value, bytes) else None


# ---------------------------------------------------------------- 纯函数

def parse_conversation(step_blobs, gen_blobs) -> list[dict]:
    """从一个会话的 ``steps.metadata`` 与 ``gen_metadata.data`` 抽出逐次用量。"""
    models: dict[str, str] = {}
    for blob in gen_blobs:
        message_id = _text(_first(blob, 4))
        model = _text(_first(blob, 1, 19))
        if message_id and model:
            models[message_id] = model

    out: list[dict] = []
    for blob in step_blobs:
        usage = _first(blob, 9)
        seconds = _first(blob, 1, 1)
        if not isinstance(usage, bytes) or not isinstance(seconds, int):
            continue
        counts = _ints(usage)
        out.append({
            "timestamp": seconds,
            "model": models.get(_text(_first(blob, 12)) or "", "unknown"),
            "response_id": _text(_first(usage, 11)),
            "tokens_in": counts.get(2, 0),
            "tokens_out": counts.get(3, 0),
            "cache_read": counts.get(5, 0),
            "thinking": counts.get(9, 0),
            "response": counts.get(10, 0),
        })
    return out


def to_events(rows: list[dict], day_of) -> list[Event]:
    """按 (日期, 模型) 聚合成 ``Event``，同一响应 ID 只算一次。

    不填 ``cost_cents``：订阅配额没有官方单价，fold 时按 Gemini 公开牌价补。
    """
    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cache_read": 0})
    seen: set[str] = set()

    for row in rows:
        if row["thinking"] + row["response"] != row["tokens_out"]:
            raise FormatDrift(
                f"输出 {row['tokens_out']} ≠ 思考 {row['thinking']} + 正文 {row['response']}")
        response_id = row.get("response_id")
        if response_id:
            if response_id in seen:
                continue
            seen.add(response_id)
        if not (row["tokens_in"] or row["tokens_out"] or row["cache_read"]):
            continue
        bucket = buckets[(day_of(row["timestamp"]), row["model"])]
        bucket["requests"] += 1
        for kind in ("tokens_in", "tokens_out", "cache_read"):
            bucket[kind] += row[kind]

    events = [
        Event(date=date, source=ADE_SOURCE, model=model, **bucket)
        for (date, model), bucket in buckets.items()
    ]
    events.sort(key=lambda e: (e.date, e.model))
    return events


# ---------------------------------------------------------------- 磁盘

def _conversation_dirs(cfg: dict) -> list[Path]:
    configured = cfg.get("antigravity_dirs")
    if configured:
        return [expand_user_path(d) for d in configured]
    return [Path.home() / d for d in _DEFAULT_DIRS]


def _conversation_files(dirs: list[Path]) -> list[Path]:
    """各目录下的会话库，同名（同一会话 ID）只取第一个。"""
    files: dict[str, Path] = {}
    for d in dirs:
        if d.is_dir():
            for path in sorted(d.glob("*.db")):
                files.setdefault(path.name, path)
    return list(files.values())


def _read_db(path: Path) -> tuple[list[bytes], list[bytes]]:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        shutil.copyfile(path, copy)
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        con = sqlite3.connect(copy)
        try:
            steps = [r[0] for r in con.execute(
                "select metadata from steps where metadata is not null")]
            gens = [r[0] for r in con.execute(
                "select data from gen_metadata where data is not null")]
        finally:
            con.close()
    return steps, gens


def collect(ctx, cfg: dict, *, conversations=None) -> CollectResult:
    """扫描本机 Antigravity 会话库并翻译。负责范围从最早一条事件到今天。

    ``conversations`` 是本机库 adapter：可迭代的 ``(step_blobs, gen_blobs)``。默认读
    磁盘；测试传入内存里的 protobuf，不必碰 ``~/.gemini``。

    读库或解码出错时整源放弃、不写 raw：少读一个会话就会把那几天的历史覆盖成偏小
    的值，宁可这次不更新。
    """
    empty = CollectResult(events=[], days=[], machine_shard=True)
    n_files = 0
    if conversations is None:
        files = _conversation_files(_conversation_dirs(cfg))
        if not files:
            print("[antigravity] 找不到会话库，跳过")
            return empty
        n_files = len(files)
        conversations = (_read_db(path) for path in files)

    try:
        rows: list[dict] = []
        for step_blobs, gen_blobs in conversations:
            rows.extend(parse_conversation(step_blobs, gen_blobs))
        events = to_events(
            rows, lambda s: datetime.fromtimestamp(s, ctx.tz).strftime("%Y-%m-%d"))
    except FormatDrift as exc:
        print(f"[antigravity] 存储格式疑似变更，本次不写 raw：{exc}")
        return empty
    except (OSError, sqlite3.Error) as exc:
        print(f"[antigravity] 读取会话库失败，本次不写 raw：{exc}")
        return empty

    events = [e for e in events if e.date >= ctx.since]
    requests = sum(e.requests for e in events)
    print(f"[antigravity] {n_files or 'fixture'} 个会话 → {len(events)} 条日模型记录"
          f"（{requests} 次）")

    if not events:
        return empty
    return CollectResult(
        events=events,
        days=ctx.days_between(min(e.date for e in events), ctx.today()),
        machine_shard=True,
    )
