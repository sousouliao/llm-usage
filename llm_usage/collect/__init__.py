"""采集层：Event 契约、落盘、adapter 登记。

每个采集器暴露 ``collect(ctx, cfg, **ports) -> CollectResult``，只负责把某个源
的用量翻译成 ``Event``。是否按机器分片由 ``CollectResult.machine_shard`` 声明，
落盘走 ``persist``——编排不再识别源类型。

== 为什么没有 unit 字段 ==

早先的契约有个 ``unit`` 字段（tokens / requests / sessions / credits），聚合时按它
分桶，防止把 token 和请求数加到一起。现在改成具名字段：token 的四个分类各占一个
字段，请求数和成本各占一个。这样「不同口径不能相加」变成了类型层面的事实——
``tokens_in`` 和 ``requests`` 是两个字段，根本没有把它们加起来的路径，不需要一层
分桶逻辑去拦。

拿不到 token 的源把四个 token 字段留 ``None``（不是 0）：``None`` 是「这个源不报
token」，0 是「报了，但确实是零」。展示层据此决定显示数字还是横线。

== 为什么 Event 没有 machine 字段 ==

Cursor 和 DeepSeek 的用量来自账号级接口，两台机器采到的是同一份数据，按机器分片
会导致重复计数。所以账号级源的原始数据只按源和月份分片。

Codex、Antigravity 这类源相反：会话日志只存在于产生它的那台机器。它们在文件系统上按
``data/raw/<source>/<machine>/<月>.json`` 分片，避免两台机器互相覆盖；但
``Event.source`` 仍然是 ADE 名（``cursor`` / ``codex`` / ``antigravity`` / ``deepseek``），
machine 不进公开契约。展示与聚合都看不见机器。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from llm_usage.contract import TOKEN_KINDS

__all__ = [
    "TOKEN_KINDS",
    "Event",
    "CollectContext",
    "CollectResult",
    "write_events",
    "read_all_events",
    "persist",
    "expand_user_path",
    "COLLECTORS",
]

_WINDOWS_ENV = re.compile(r"%([^%]+)%")


def expand_user_path(value: str) -> Path:
    """展开配置里的本机路径：``~``、``$HOME`` / ``${HOME}`` 与 Windows 的 ``%USERPROFILE%``。"""
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    expanded = _WINDOWS_ENV.sub(
        lambda match: os.environ.get(match.group(1), match.group(0)), expanded)
    return Path(expanded)


@dataclass(frozen=True)
class Event:
    """某一天、某个源、某个模型上的用量。

    ``requests`` 是这一格里的调用次数，任何源都该能给出。四个 token 字段与
    ``cost_cents`` 只有能拿到真实明细的源才填。
    """

    date: str                        # YYYY-MM-DD，按配置时区计算
    source: str                      # ADE 名，如 cursor / codex / deepseek
    model: str
    requests: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_write: int | None = None
    cache_read: int | None = None
    # token 按各模型单价折算出的成本，单位为「分」。刻意用浮点：单次调用的成本常
    # 在一分以下，取整会让求和系统性偏小。展示时才四舍五入到分。
    cost_cents: float | None = None
    note: str | None = None          # 口径说明，供日后回看，不参与计算

    @property
    def tokens_total(self) -> int | None:
        """四类 token 之和。四个字段全为 None 时返回 None，表示该源不报 token。"""
        parts = [getattr(self, k) for k in TOKEN_KINDS]
        if all(p is None for p in parts):
            return None
        return sum(p or 0 for p in parts)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CollectContext:
    """采集器需要的环境信息，由 cli 组装后传入。"""

    tz: tzinfo
    root: Path
    # 采集起点。接口型源每次重采 [since, as_of 或今天] 的全部数据并覆盖写回，
    # 所以采集幂等：跑一次和跑十次结果相同，漏跑补跑都能自愈。
    since: str = "2026-01-01"
    # 本机标识，只给「会话在本机」的源当文件分片键。账号级源忽略它。
    machine: str | None = None
    # 采集终点。None 表示配置时区的「现在」。测试传入固定日期，窗口才不随
    # 日历翻月而多写出空分片。fold 的 ``now`` 是同一类缝：时钟是输入，不是
    # 藏在实现里的 datetime.now()。
    as_of: str | None = None

    def day_of(self, ms: int) -> str:
        """毫秒时间戳 → 配置时区下的 YYYY-MM-DD。"""
        return datetime.fromtimestamp(ms / 1000, self.tz).strftime("%Y-%m-%d")

    def today(self) -> str:
        if self.as_of is not None:
            return self.as_of
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    def since_ms(self) -> int:
        """采集起点零点的毫秒时间戳，供接口的区间参数使用。"""
        d = datetime.strptime(self.since, "%Y-%m-%d")
        return int(datetime(d.year, d.month, d.day, tzinfo=self.tz).timestamp() * 1000)

    def days_between(self, start: str, end: str) -> list[str]:
        """[start, end] 的日期列表，升序。"""
        first = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
        if last < first:
            return []
        return [(first + timedelta(days=i)).isoformat()
                for i in range((last - first).days + 1)]

    def days_since(self) -> list[str]:
        """[since, 今天] 的日期列表，升序。"""
        return self.days_between(self.since, self.today())


@dataclass(frozen=True)
class CollectResult:
    """一次采集的产物。``machine_shard`` 为真时按 ``ctx.machine`` 分片落盘，
    并按 ``Event.source`` 分组（一次采集可能拆出多个 ADE）。"""

    events: list[Event]
    days: list[str]
    machine_shard: bool


# ---------------------------------------------------------------- 原始数据读写

_MONTH_FILE = re.compile(r"^\d{4}-\d{2}\.json$")


def _raw_path(root: Path, source: str, month: str, shard: str | None = None) -> Path:
    if shard:
        return root / "data" / "raw" / source / shard / f"{month}.json"
    return root / "data" / "raw" / source / f"{month}.json"


def write_events(root: Path, source: str, events: list[Event],
                 days: list[str], shard: str | None = None) -> None:
    """把 ``events`` 写入按月分片的原始文件，并覆盖 ``days`` 覆盖到的那些天。

    ``days`` 是本次采集「负责」的日期列表：这些天在文件里的旧内容会被整体替换，
    没被列出的天原样保留。这让采集可以重复运行而不产生重复记录，也不会因为某天
    恰好没有用量就把旧数据留成幽灵——负责范围内没有用量的那天会留一个空数组。

    ``shard`` 是本机标识。账号级源不传；本机源传了之后写到
    ``data/raw/<source>/<shard>/<月>.json``，两台机器互不覆盖。
    """
    by_month: dict[str, dict[str, list[dict]]] = {}
    for day in days:
        by_month.setdefault(day[:7], {})[day] = []
    for e in events:
        by_month.setdefault(e.date[:7], {}).setdefault(e.date, []).append(e.to_dict())

    for month, fresh_days in by_month.items():
        path = _raw_path(root, source, month, shard)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc: dict = {"source": source, "month": month, "days": {}}
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[warn] {path} 解析失败（{exc}），按空文件重建")
                doc = {"source": source, "month": month, "days": {}}
        doc.setdefault("days", {}).update(fresh_days)
        doc["days"] = {d: doc["days"][d] for d in sorted(doc["days"])}
        doc["source"], doc["month"] = source, month
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


def read_all_events(root: Path) -> list[dict]:
    """读取 data/raw 下所有源、所有月份的事件，供聚合使用。

    认两种布局：

    - ``<source>/<月>.json``：账号级源（Cursor）
    - ``<source>/<machine>/<月>.json``：本机源（Codex）

    旧布局 ``<machine>/<source>/<月>.json`` 会被跳过：文件里的 ``source`` 对不上
    第一段路径。那批文件是第一轮按机器分片留下的，不能再折进总量。
    """
    out: list[dict] = []
    raw = root / "data" / "raw"
    if not raw.exists():
        return out
    for path in sorted(raw.rglob("*.json")):
        if not _MONTH_FILE.match(path.name):
            continue
        rel = path.relative_to(raw).parts
        if len(rel) not in (2, 3):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[warn] 读取 {path} 失败: {exc}")
            continue
        if len(rel) == 3 and doc.get("source") and doc["source"] != rel[0]:
            continue
        for day, rows in (doc.get("days") or {}).items():
            for row in rows:
                row.setdefault("date", day)
                row.setdefault("source", doc.get("source", "unknown"))
                out.append(row)
    return out


def persist(ctx: CollectContext, result: CollectResult) -> int:
    """按 ``CollectResult`` 声明的策略把事件写入 raw。返回写入的事件数。"""
    if not result.days:
        return 0
    shard = None
    if result.machine_shard:
        if not ctx.machine:
            raise SystemExit(
                "本机源需要在 sources.yaml 里设置 machine"
                "（如 work-mac / home-win），两台机器必须用不同的名字。")
        shard = ctx.machine
    grouped: dict[str, list[Event]] = {}
    for event in result.events:
        grouped.setdefault(event.source, []).append(event)
    for source, group in grouped.items():
        write_events(ctx.root, source, group, result.days, shard=shard)
    return len(result.events)


from . import antigravity, chatgpt, cursor, deepseek  # noqa: E402

COLLECTORS = {
    "cursor": cursor,
    "chatgpt": chatgpt,
    "antigravity": antigravity,
    "deepseek": deepseek,
}
