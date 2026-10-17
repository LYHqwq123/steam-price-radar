"""Batch Steam CN app price overviews for the synchronized search directory.

This intentionally collects only the app's price overview. Exact editions, DLC,
CDK offers and historical series remain the separate, slower detail collector.
An empty price_overview response does not prove that an app is free.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://store.steampowered.com/api/appdetails"
MAX_BATCH_SIZE = 100
MIN_DELAY = 0.7


class RateLimitedError(RuntimeError):
    """Stop this run instead of retrying every remaining batch."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def app_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        return None
    number = int(value)
    return number if 0 < number <= 2147483647 else None


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2) if math.isfinite(value) and value >= 0 else None


def cents(value):
    # Steam returns integer cents. Do not interpret booleans or fractional cents.
    if isinstance(value, str):
        value = int(value) if value.isascii() and value.isdigit() else None
    number = amount(value)
    return round(number / 100, 2) if number is not None and float(value).is_integer() else None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def request_batch(ids):
    query = urlencode({"appids": ",".join(map(str, ids)), "filters": "price_overview", "cc": "cn", "l": "schinese"})
    request = Request(API_URL + "?" + query, headers={
        "User-Agent": "SteamPriceRadar/1.0 (public prices; read-only)", "Accept": "application/json",
    })
    for attempt in range(2):
        try:
            with urlopen(request, timeout=18) as response:
                payload = json.load(response)
            break
        except HTTPError as error:
            if error.code in (403, 420, 429, 503):
                error.close()
                raise RateLimitedError(f"Steam HTTP {error.code}; stopped without retries") from error
            raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt:
                raise
            time.sleep(MIN_DELAY)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Steam returned no batch result")
    return payload


def blank_record(appid, *, checked_at=None, status="error", note="尚未采集；等待后续批次。"):
    return {"appid": appid, "price": None, "originalPrice": None, "discount": None,
            "checkedAt": checked_at, "status": status, "note": note}


def failed_record(appid, previous, checked_at, note):
    if previous and amount(previous.get("price")) is not None:
        result = copy.deepcopy(previous)
        result.update(status="stale", note=note + " 保留此前报价及其采集时间，仅供参考。")
        return result
    return blank_record(appid, checked_at=checked_at, note=note)


def parse_record(appid, response, checked_at, previous=None):
    if not isinstance(response, dict) or response.get("success") is not True:
        return failed_record(appid, previous, checked_at, "Steam 未返回该条目的有效结果。")
    data = response.get("data")
    overview = data.get("price_overview") if isinstance(data, dict) else None
    if not isinstance(overview, dict) or not overview:
        return blank_record(appid, checked_at=checked_at, status="unavailable",
                            note="Steam 未返回现价；免费、未发售或国区不可售等情况需另行确认。")
    if overview.get("currency") != "CNY":
        return blank_record(appid, checked_at=checked_at, status="unavailable",
                            note="Steam 未返回人民币国区价格，未使用其他地区报价。")
    price, original = cents(overview.get("final")), cents(overview.get("initial"))
    if price is None:
        return failed_record(appid, previous, checked_at, "Steam 国区价格格式无效。")
    discount = round((1 - price / original) * 100) if original and price <= original else None
    return {"appid": appid, "price": price, "originalPrice": original, "discount": discount,
            "checkedAt": checked_at, "status": "ok",
            "note": "Steam 国区应用现价概览；具体购买版本和内容以商店为准。"}


def previous_records(previous):
    if (not isinstance(previous, dict) or previous.get("source") != "steam"
            or previous.get("country") != "CN" or previous.get("currency") != "CNY"):
        return {}
    result = {}
    for raw in previous.get("games", []) if isinstance(previous.get("games"), list) else []:
        if not isinstance(raw, dict) or not (appid := app_id(raw.get("appid"))):
            continue
        row = blank_record(appid)
        row.update({key: copy.deepcopy(raw[key]) for key in row if key in raw})
        row["appid"] = appid
        row["price"], row["originalPrice"] = amount(row["price"]), amount(row["originalPrice"])
        row["discount"] = amount(row["discount"])
        if row["discount"] is not None and row["discount"] > 100:
            row["discount"] = None
        if row["status"] not in ("ok", "unavailable", "stale", "error"):
            row["status"] = "error"
        if row["price"] is None and row["status"] in ("ok", "stale"):
            row["status"] = "error"
        result[appid] = row
    return result


def collect(index, previous=None, *, batch_size=100, max_batches=None, delay=MIN_DELAY,
            fetcher=request_batch, sleeper=time.sleep, clock=now_iso, checkpoint=None, progress=print):
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and 100")
    if max_batches is not None and (not isinstance(max_batches, int) or isinstance(max_batches, bool) or max_batches < 1):
        raise ValueError("max_batches must be positive")
    if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < MIN_DELAY:
        raise ValueError("delay must be at least 0.7 seconds")
    ids = list(dict.fromkeys(appid for row in index.get("games", [])
                             if isinstance(row, dict) and (appid := app_id(row.get("appid")))))
    records = previous_records(previous)
    had_previous = any(row.get("checkedAt") for row in records.values())
    for appid in ids:
        records.setdefault(appid, blank_record(appid))
    state = previous.get("syncState", {}) if isinstance(previous, dict) else {}
    offset = state.get("nextIndex", 0) if isinstance(state, dict) else 0
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        offset = 0
    next_appid = state.get("nextAppid") if isinstance(state, dict) else None
    start = ids.index(next_appid) if next_appid in ids else offset % len(ids) if ids else 0
    ordered = ids[start:] + ids[:start]
    requested, successes, batches, failures = 0, 0, 0, 0
    errors = []
    generated_at = clock()

    def snapshot():
        cursor = (start + requested) % len(ids) if ids else 0
        all_done = requested == len(ids) and bool(ids)
        status = "ok" if all_done and not errors else "partial"
        if not successes and not had_previous:
            status = "error"
        rows = list(records.values())
        return {"schemaVersion": 1, "generatedAt": generated_at, "source": "steam",
                "sourceUrl": API_URL, "country": "CN", "currency": "CNY", "refreshMinutes": 15,
                "status": status, "gameCount": len(rows), "catalogGameCount": len(ids),
                "pricedCount": sum(row["status"] == "ok" and row["price"] is not None for row in rows),
                "scope": "已同步游戏目录的 Steam 国区应用现价概览，并非完整 Steam 游戏库或全部购买版本。",
                "note": "未返回价格不等于免费；具体版本、DLC 和历史由详情采集任务另行补充。",
                "games": rows, "processedCount": requested, "successfulBatches": successes,
                "attemptedBatches": batches, "errors": errors,
                "syncState": {"nextIndex": cursor, "nextAppid": ids[cursor] if ids else None,
                              "lastRunCompletedDirectory": all_done}}

    for position in range(0, len(ordered), batch_size):
        if max_batches is not None and batches >= max_batches:
            break
        if batches:
            sleeper(delay)
        chunk = ordered[position:position + batch_size]
        checked_at = clock()
        stop = False
        try:
            payload = fetcher(chunk)
            if not isinstance(payload, dict) or not payload:
                raise ValueError("Steam returned no batch result")
            for appid in chunk:
                records[appid] = parse_record(appid, payload.get(str(appid)), checked_at, records.get(appid))
            successes += 1
            failures = 0
        except Exception as error:
            failures += 1
            stop = isinstance(error, RateLimitedError) or isinstance(error, HTTPError) and error.code in (403, 420, 429, 503) or failures >= 3
            label = (f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__)
            if isinstance(error, HTTPError):
                error.close()
            errors.append({"batch": batches + 1, "type": label, "stopped": stop})
            for appid in chunk:
                records[appid] = failed_record(appid, records.get(appid), checked_at,
                                               "本轮 Steam 查询失败；等待后续重试。")
        requested += len(chunk)
        batches += 1
        generated_at = clock()
        if progress:
            progress(f"Direct prices: batch={batches} processed={requested}/{len(ids)} successful_batches={successes} errors={len(errors)}", flush=True)
        if checkpoint and batches % 10 == 0:
            checkpoint(snapshot())
        if stop:
            break
    return snapshot()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=ROOT / "dist/data/catalog-index.json")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/data/direct-prices.json")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--delay", type=float, default=MIN_DELAY)
    args = parser.parse_args(argv)
    try:
        index = read_json(args.index)
        if not isinstance(index, dict) or not isinstance(index.get("games"), list):
            raise ValueError("index must contain a games array")
        previous = read_json(args.output, {})
        result = collect(index, previous, batch_size=args.batch_size, max_batches=args.max_batches,
                         delay=args.delay, checkpoint=lambda value: atomic_write(args.output, value))
        atomic_write(args.output, result)
        print(f"Direct prices saved: status={result['status']} priced={result['pricedCount']}/{result['gameCount']}", flush=True)
        return 1 if result["status"] == "error" else 0
    except (ValueError, OSError, TypeError) as error:
        print(f"Direct prices failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
