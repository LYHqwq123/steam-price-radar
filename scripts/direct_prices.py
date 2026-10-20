"""Collect Steam CN current prices for the full directory in sharded batches.

Each worker owns a consecutive range of public ``appid % shard_count`` shards.
With 64 public shards and 8 workers, one worker refreshes eight shard files.
Consecutive ranges balance Steam AppIDs, which are predominantly even numbers.
An empty ``price_overview`` response does not prove that an app is free, and a
failed request preserves the previous amount with its original checked time.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import math
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .catalog_format import catalog_appids, read_catalog
    from .price_format import (SHARD_COUNT, SOURCE_URL, build_manifest, legacy_records,
                               load_shard_records, shard_filename, timestamp, write_manifest, write_shard)
except ImportError:
    from catalog_format import catalog_appids, read_catalog
    from price_format import (SHARD_COUNT, SOURCE_URL, build_manifest, legacy_records,
                              load_shard_records, shard_filename, timestamp, write_manifest, write_shard)

ROOT = Path(__file__).resolve().parents[1]
API_URL = SOURCE_URL
MAX_BATCH_SIZE = 100
MIN_DELAY = 0.7
RETRY_ATTEMPTS = 3
RATE_LIMIT_CODES = {403, 420, 429, 503}
TRANSIENT_CODES = {500, 502, 504}


class RateLimitedError(RuntimeError):
    """Stop this worker instead of retrying every remaining batch."""


def iso_now():
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


def retry_after(error):
    try:
        value = int(error.headers.get("Retry-After", ""))
    except (AttributeError, TypeError, ValueError):
        return None
    return max(1, min(value, 90))


def request_batch(ids, *, attempts=RETRY_ATTEMPTS, sleeper=None):
    sleeper = sleeper or time.sleep
    query = urlencode({"appids": ",".join(map(str, ids)), "filters": "price_overview",
                       "cc": "cn", "l": "schinese"})
    request = Request(API_URL + "?" + query, headers={
        "User-Agent": "SteamPriceRadar/2.0 (public prices; read-only)", "Accept": "application/json",
    })
    last_error = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not payload:
                raise ValueError("Steam returned no batch result")
            return payload
        except HTTPError as error:
            code = error.code
            retry_seconds = retry_after(error)
            error.close()
            last_error = error
            if code in RATE_LIMIT_CODES:
                if attempt + 1 >= attempts:
                    raise RateLimitedError(f"Steam HTTP {code}; stopped without further retries") from None
                sleeper(min(90, retry_seconds or 20 * (attempt + 1)))
                continue
            if code in TRANSIENT_CODES:
                if attempt + 1 >= attempts:
                    raise
                sleeper(min(30, retry_seconds or 2 ** (attempt + 1)))
                continue
            raise
        except (URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            if attempt + 1 >= attempts:
                raise
            sleeper(min(30, 2 ** (attempt + 1)))
    raise last_error or RuntimeError("Steam batch request failed")


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
    """Normalize worker-owned records or a legacy schema v1 snapshot."""
    if isinstance(previous, dict) and isinstance(previous.get("games"), list):
        return legacy_records(previous)
    result = {}
    for key, raw in (previous or {}).items():
        if not isinstance(raw, dict):
            continue
        appid = app_id(raw.get("appid") if raw.get("appid") is not None else key)
        if not appid:
            continue
        row = blank_record(appid)
        row.update({field: copy.deepcopy(raw[field]) for field in row if field in raw})
        row["appid"] = appid
        row["price"], row["originalPrice"] = amount(row["price"]), amount(row["originalPrice"])
        row["discount"] = amount(row["discount"])
        if row["discount"] is not None and row["discount"] > 100:
            row["discount"] = None
        if row["status"] not in ("ok", "unavailable", "stale", "error"):
            row["status"] = "error"
        if row["price"] is None and row["status"] in ("ok", "stale"):
            row["status"] = "error"
        if not isinstance(row["checkedAt"], str) or not timestamp(row["checkedAt"]):
            row["checkedAt"] = None
        result[appid] = row
    return result


def validate_limits(batch_size=MAX_BATCH_SIZE, max_batches=None, max_seconds=None, delay=MIN_DELAY):
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and 100")
    if max_batches is not None and (isinstance(max_batches, bool) or not isinstance(max_batches, int) or max_batches < 1):
        raise ValueError("max_batches must be positive")
    if max_seconds is not None and (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                                    or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError("max_seconds must be positive")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < MIN_DELAY:
        raise ValueError("delay must be at least 0.7 seconds")


def collect(appids, previous=None, *, fetcher=request_batch, sleeper=time.sleep, clock=iso_now,
            batch_size=MAX_BATCH_SIZE, max_batches=None, max_seconds=None, delay=MIN_DELAY,
            progress=None):
    """Fetch explicit AppIDs. Unattempted AppIDs are never rewritten."""
    validate_limits(batch_size, max_batches, max_seconds, delay)
    ids = list(dict.fromkeys(appid for row in appids if (appid := app_id(row))))
    if not ids:
        raise ValueError("at least one positive AppID is required")
    known = previous_records(previous)
    ordered = sorted(ids, key=lambda value: ((known.get(value) or {}).get("checkedAt") or "", value))
    records, attempted, errors = {}, [], []
    batches = successes = consecutive_failures = 0
    generated_at = clock()
    started = time.monotonic()
    broken = False
    for position in range(0, len(ordered), batch_size):
        if max_batches is not None and batches >= max_batches:
            break
        if max_seconds is not None and batches and time.monotonic() - started >= max_seconds:
            break
        if batches and delay:
            sleeper(delay)
        chunk = ordered[position:position + batch_size]
        checked_at = clock()
        stop = False
        try:
            payload = fetcher(chunk)
            if not isinstance(payload, dict) or not payload:
                raise ValueError("Steam returned no batch result")
            for value in chunk:
                records[value] = parse_record(value, payload.get(str(value)), checked_at, known.get(value))
            successes += 1
            consecutive_failures = 0
        except Exception as error:
            consecutive_failures += 1
            stop = isinstance(error, RateLimitedError) or consecutive_failures >= 3
            label = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
            if isinstance(error, HTTPError):
                error.close()
            errors.append({"batch": batches + 1, "type": label, "stopped": stop})
            for value in chunk:
                records[value] = failed_record(value, known.get(value), checked_at,
                                               "本轮 Steam 查询失败；等待后续重试。")
        attempted.extend(chunk)
        batches += 1
        generated_at = clock()
        if progress:
            progress(f"prices worker: batch={batches} attempted={len(attempted)}/{len(ids)} "
                     f"successful={successes} errors={len(errors)}")
        if stop:
            broken = True
            break
    completed = len(attempted) == len(ids) and not errors
    status = "error" if not successes else "ok" if completed else "partial"
    updates = {value: records[value] for value in attempted if value in records}
    return {"generatedAt": generated_at, "status": status, "selectedCount": len(ids),
            "attemptedCount": len(attempted), "batchCount": batches, "successfulBatches": successes,
            "errorCount": len(errors), "errors": errors, "completed": completed,
            "stopped": broken or len(attempted) < len(ids),
            "attemptedAppids": attempted, "updates": updates}


def worker_shards(shard_count, worker_index=0, worker_count=1):
    if (isinstance(worker_count, bool) or not isinstance(worker_count, int) or worker_count < 1
            or isinstance(worker_index, bool) or not isinstance(worker_index, int)
            or not 0 <= worker_index < worker_count):
        raise ValueError("worker_index and worker_count must describe a valid worker")
    if shard_count % worker_count:
        raise ValueError("shard_count must be divisible by worker_count")
    width = shard_count // worker_count
    return range(worker_index * width, (worker_index + 1) * width)


def worker_appids(catalog, worker_index=0, worker_count=1, shard_count=SHARD_COUNT):
    owned = set(worker_shards(shard_count, worker_index, worker_count))
    ids = catalog_appids(catalog)
    return ids, [value for value in ids if value % shard_count in owned]


def read_worker_shards(shard_dir, shard_count, worker_index=0, worker_count=1):
    records = {}
    for index in worker_shards(shard_count, worker_index, worker_count):
        found, shard = load_shard_records(Path(shard_dir) / shard_filename(index), index, shard_count)
        if shard:
            records.update(found)
    return records


def write_worker_shards(shard_dir, shard_count, worker_index, worker_count, catalog_ids,
                        previous, updates, attempted, checked_at):
    owned = set(worker_shards(shard_count, worker_index, worker_count))
    touched = sorted({value % shard_count for value in attempted})
    if any(index not in owned for index in touched):
        raise ValueError("worker attempted an AppID outside its shard class")
    wanted = {index: [] for index in touched}
    for value in catalog_ids:
        index = value % shard_count
        if index in wanted:
            wanted[index].append(value)
    written = []
    for index in touched:
        rows = [updates.get(value) or previous.get(value) for value in wanted[index]]
        rows = [row for row in rows if row]
        written.append(write_shard(Path(shard_dir) / shard_filename(index), index, shard_count,
                                   checked_at, rows))
    return written


def main(argv=None, *, fetcher=request_batch, sleeper=time.sleep, clock=iso_now):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=ROOT / "dist/data/catalog-index.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/data")
    parser.add_argument("--shard-count", type=int, default=SHARD_COUNT)
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--prefix", default="price-shards")
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--refresh-minutes", type=int, default=120)
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate_limits(args.batch_size, args.max_batches, args.max_seconds, args.delay)
        if not 1 <= args.shard_count <= 1000:
            raise ValueError("shard-count must be between 1 and 1000")
        if args.worker_count < 1 or not 0 <= args.worker_index < args.worker_count:
            raise ValueError("worker-index must be within worker-count")
        if args.shard_count % args.worker_count:
            raise ValueError("shard-count must be divisible by worker-count")
        if args.refresh_minutes < 1:
            raise ValueError("refresh-minutes must be positive")
        catalog = read_catalog(args.index)
        all_ids, ids = worker_appids(catalog, args.worker_index, args.worker_count, args.shard_count)
        if not all_ids:
            raise ValueError("catalog contains no valid games")
        shard_dir = args.output_dir / args.prefix
        previous = read_worker_shards(shard_dir, args.shard_count, args.worker_index, args.worker_count)
        result = collect(ids, previous, fetcher=fetcher, sleeper=sleeper, clock=clock,
                         batch_size=args.batch_size, max_batches=args.max_batches,
                         max_seconds=args.max_seconds, delay=args.delay,
                         progress=lambda message: print(message, flush=True))
        written = write_worker_shards(shard_dir, args.shard_count, args.worker_index, args.worker_count,
                                      all_ids, previous, result["updates"], result["attemptedAppids"],
                                      result["generatedAt"])
        if args.write_manifest:
            manifest = build_manifest(shard_dir, catalog_appids=all_ids, output_dir=args.output_dir,
                                      refresh_minutes=args.refresh_minutes, generated_at=result["generatedAt"],
                                      prefix=args.prefix, shard_count=args.shard_count)
            write_manifest(args.output_dir / "price-manifest.json", manifest)
        stats = {key: result[key] for key in ("generatedAt", "status", "selectedCount", "attemptedCount",
                                              "batchCount", "successfulBatches", "errorCount", "completed", "stopped")}
        stats.update(workerIndex=args.worker_index, workerCount=args.worker_count,
                     shardCount=args.shard_count, writtenShards=[entry["index"] for entry in written],
                     writtenRows=sum(entry["count"] for entry in written), errors=result["errors"])
        print(json.dumps(stats, ensure_ascii=True))
        return 0
    except (ValueError, OSError, TypeError) as error:
        print(f"Steam price worker failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
