"""配置加载。刻意分成两个文件，因为它们的读者不同。

``sources.yaml``（不提交）：本机专属信息——机器名、各源的 base_url 与 key 环境变量名。
只有采集阶段需要，只在本机存在。

``config/aggregate.yaml``（提交）：时区、模型别名、订阅源、API 牌价、人民币汇率。
聚合与渲染阶段需要，且不含任何密钥，所以 CI 拿着仓库就能重新生成 stats.json
和 SVG，不必配置 secret。DeepSeek 采集也读这里的 ``usd_cny``，把人民币账单折成
美元分。

把两者混在一份 gitignore 掉的文件里，CI 就永远拿不到别名表；混在提交的文件里，
base_url 之类的信息又会进公开仓库。
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import yaml

from llm_usage import REPO_ROOT

SOURCES_PATH = REPO_ROOT / "sources.yaml"
AGGREGATE_PATH = REPO_ROOT / "config" / "aggregate.yaml"

DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_USD_CNY = 7.2


def load_aggregate_config() -> dict:
    """读取公共聚合配置。文件缺失时退回内置默认值，保证 CI 不会因此失败。"""
    if not AGGREGATE_PATH.exists():
        return {"timezone": DEFAULT_TZ, "model_aliases": {},
                "subscription_sources": [], "list_prices": {},
                "price_aliases": {}, "usd_cny": DEFAULT_USD_CNY}
    cfg = yaml.safe_load(AGGREGATE_PATH.read_text(encoding="utf-8")) or {}
    cfg.setdefault("timezone", DEFAULT_TZ)
    cfg.setdefault("model_aliases", {})
    cfg.setdefault("subscription_sources", [])
    cfg.setdefault("list_prices", {})
    cfg.setdefault("price_aliases", {})
    cfg.setdefault("usd_cny", DEFAULT_USD_CNY)
    return cfg


def load_sources_config() -> dict:
    if not SOURCES_PATH.exists():
        raise SystemExit(
            f"找不到 {SOURCES_PATH}，请复制 config/sources.example.yaml 为 sources.yaml")
    cfg = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    cfg.setdefault("sources", [])
    # YAML 会把不加引号的 2026-01-01 解析成 date 对象。统一转回字符串，免得每个
    # 用到 since 的地方都要判类型，也不必要求手写配置的人记得加引号。
    since = cfg.get("since") or "2026-01-01"
    cfg["since"] = since.isoformat() if hasattr(since, "isoformat") else str(since)
    return cfg


def timezone() -> ZoneInfo:
    return ZoneInfo(load_aggregate_config()["timezone"])


def model_aliases() -> dict[str, str]:
    """模型别名表，用于把同一模型在不同机器上的标签归一。"""
    raw = load_aggregate_config().get("model_aliases") or {}
    return {str(k): str(v) for k, v in raw.items()}


def subscription_sources() -> list[str]:
    """订阅制源名。raw 没有官方成本；对不上牌价的模型，金额列写 Subscription。"""
    raw = load_aggregate_config().get("subscription_sources") or []
    return [str(name) for name in raw]


def list_prices() -> dict[str, dict[str, float]]:
    """公开 API 牌价，美元 / 百万 token。"""
    raw = load_aggregate_config().get("list_prices") or {}
    return {str(model): {str(k): float(v) for k, v in rates.items()}
            for model, rates in raw.items()}


def price_aliases() -> dict[str, str]:
    """计价别名。只影响牌价查找，不改展示用的模型名。"""
    raw = load_aggregate_config().get("price_aliases") or {}
    return {str(k): str(v) for k, v in raw.items()}


def usd_cny() -> float:
    """1 美元兑多少人民币。DeepSeek 账单是 CNY，采集时折成 cost_cents。"""
    value = float(load_aggregate_config().get("usd_cny", DEFAULT_USD_CNY))
    if value <= 0:
        raise SystemExit("usd_cny 必须为正数")
    return value


def normalize_model(model: str, aliases: dict[str, str] | None = None) -> str:
    """归一模型名。未登记的名字原样返回，不做猜测。"""
    aliases = model_aliases() if aliases is None else aliases
    return aliases.get(model, model)
