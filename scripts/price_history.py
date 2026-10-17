"""Read the recorded Steam CN price changes exposed by Heybox's public API.

The endpoint returns yuan, not cents. Its days parameter is not a reliable
coverage limit, so expose the dates actually returned instead of promising a
complete lifetime history. No historical series is inferred from today's quote.
"""
from __future__ import annotations

import copy
import math
import urllib.parse
from datetime import datetime, timedelta, timezone

try:
    from .providers import money, now_iso
except ImportError:
    from providers import money, now_iso

SHANGHAI = timezone(timedelta(hours=8))
CACHE_HOURS = 24
HISTORY_NOTE = "小黑盒收录的 Steam 国区人民币历史变价记录；覆盖范围以返回记录为准，不保证自发售起完整。同日多次变价保留最后一次。"


def parse_timestamp(value):
    """Accept the API's seconds strings/numbers, but never boolean/NaN dates."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        stamp = float(value)
        if not math.isfinite(stamp) or stamp < 0:
            return None
        return datetime.fromtimestamp(stamp, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def normalize_points(rows, *, today=None):
    """Native CNY only; chronological final observation per Shanghai date."""
    today = today or datetime.now(SHANGHAI).date().isoformat()
    points = {}
    ordered = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict) or row.get("currency") != "CNY":
            continue
        timestamp = parse_timestamp(row.get("date"))
        price = money(row.get("price"))
        if timestamp is None or price is None:
            continue
        day = timestamp.astimezone(SHANGHAI).date().isoformat()
        if day > today:
            continue
        ordered.append((timestamp, index, day, price))
    for _, _, day, price in sorted(ordered):
        points[day] = {"date": day, "price": price}
    return [points[day] for day in sorted(points)]


def _base(appid, fetched_at):
    return {
        "source": "heybox", "sourceName": "小黑盒", "country": "CN", "currency": "CNY",
        "fetchedAt": fetched_at, "status": "unavailable", "scope": "recorded", "points": [],
        "note": HISTORY_NOTE, "sourceUrl": f"https://www.xiaoheihe.cn/app/topic/game/pc/{int(appid)}",
        "coverageStart": None, "coverageEnd": None,
    }


def fetch_history(client, appid, *, fetched_at=None):
    fetched_at = fetched_at or now_iso()
    result = _base(appid, fetched_at)
    # A lowercase cc is essential: the endpoint returns an empty CN series for CN.
    query = urllib.parse.urlencode({"appid": int(appid), "platf": "steam", "cc": "cn", "days": 720})
    payload = client.get("https://api.xiaoheihe.cn/game/get_game_prices/history/v2?" + query,
                         headers={"Referer": result["sourceUrl"], "User-Agent": "Mozilla/5.0"})
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError("Heybox history endpoint did not return success")
    body = payload.get("result")
    if not isinstance(body, dict) or not isinstance(body.get("prices"), list):
        raise ValueError("Heybox history endpoint did not return a price series")
    # Do not trust lowest_info_v2: it can disagree with the returned observations.
    current_day = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).astimezone(SHANGHAI).date().isoformat()
    points = normalize_points(body["prices"], today=current_day)
    result["points"] = points
    if points:
        result.update(status="ok", coverageStart=points[0]["date"], coverageEnd=points[-1]["date"])
    else:
        result["note"] = "小黑盒未返回可用的 Steam 国区人民币历史记录；未生成模拟走势。"
    return result


def _same_series(previous):
    return (isinstance(previous, dict) and previous.get("source") == "heybox"
            and previous.get("country") == "CN" and previous.get("currency") == "CNY")


def _fresh(previous, timestamp):
    if not _same_series(previous) or previous.get("status") not in ("ok", "unavailable"):
        return False
    try:
        fetched = datetime.fromisoformat(previous["fetchedAt"].replace("Z", "+00:00"))
        now = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = (now - fetched).total_seconds()
        return 0 <= age < CACHE_HOURS * 3600
    except (KeyError, TypeError, ValueError):
        return False


def load_history(client, appid, previous=None, *, refresh=False, fetched_at=None):
    """Cache history for 24h; preserve real timestamps and points on failure."""
    timestamp = fetched_at or now_iso()
    if not refresh and _fresh(previous, timestamp):
        return copy.deepcopy(previous)
    try:
        result = fetch_history(client, appid, fetched_at=timestamp)
        if result["points"] or not (_same_series(previous) and previous.get("points")):
            return result
        reason = "小黑盒本轮未返回历史记录"
    except Exception as exc:
        reason = "小黑盒历史接口本轮采集失败（" + type(exc).__name__ + "）"
    if _same_series(previous) and previous.get("points"):
        value = copy.deepcopy(previous)
        value.update(status="stale", note=reason + "；保留上次真实记录与原更新时间。" + HISTORY_NOTE)
        return value
    value = _base(appid, timestamp)
    value.update(status="error", note=reason + "；暂无历史记录，未生成模拟走势。")
    return value
