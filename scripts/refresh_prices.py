"""Refresh core games and a small rotating batch without dropping old prices.

The Steam directory is a search index, not a promise that all games were priced
in this run. Each quote keeps its own observed timestamp. No browser requests,
catalog-wide immediate price scan, or fabricated price estimates are used.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from .collect_prices import collect
    from .providers import SOURCES, now_iso
except ImportError:
    from collect_prices import collect
    from providers import SOURCES, now_iso

ROOT = Path(__file__).resolve().parents[1]


def app_id(value):
    value = str(value or "")
    return int(value) if value.isascii() and value.isdigit() and int(value) > 0 else None


def records(catalog):
    if isinstance(catalog, list):
        return catalog
    if isinstance(catalog, dict):
        for field in ("games", "apps", "items"):
            if isinstance(catalog.get(field), list):
                return catalog[field]
    return []


def normalize_catalog(catalog):
    """Only normalize directory metadata; never infer verified shop mappings."""
    output, seen = [], set()
    for row in records(catalog):
        if not isinstance(row, dict):
            continue
        appid = app_id(row.get("appid"))
        name = str(row.get("name") or row.get("nameEn") or "").strip()
        if not appid or not name or appid in seen:
            continue
        if row.get("type") not in (None, "game"):
            continue
        value = copy.deepcopy(row)
        value.update(appid=appid, name=name, nameEn=str(row.get("nameEn") or name).strip(),
                     aliases=[alias for alias in row.get("aliases", []) if isinstance(alias, str) and alias.strip()]
                     if isinstance(row.get("aliases", []), list) else [])
        output.append(value)
        seen.add(appid)
    return output


def timestamp(value):
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return 0
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0


def last_observation(game):
    return max((timestamp(q.get("checkedAt")) for q in game.get("quotes", []) if isinstance(q, dict)), default=0)


def select_batch(core, directory, previous, batch_size):
    """Try every never-attempted game before retrying the oldest prior attempt."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 0:
        raise ValueError("batch_size must be a nonnegative integer")
    core_ids = {row["appid"] for row in core}
    old = {app_id(row.get("appid")): row for row in previous.get("games", []) if isinstance(row, dict)}
    attempts = previous.get("rotation", {}).get("lastAttempts", {})
    attempts = attempts if isinstance(attempts, dict) else {}

    def priority(row):
        appid = row["appid"]
        attempted_at = timestamp(attempts.get(str(appid)))
        was_seen = appid in old or attempted_at > 0
        # A failed attempt advances the cursor too; it must not trap every
        # subsequent round on a permanently inaccessible first directory row.
        freshness = max(attempted_at, last_observation(old.get(appid, {})))
        return (1 if was_seen else 0, freshness, appid)

    candidates = [row for row in directory if row["appid"] not in core_ids]
    return sorted(candidates, key=priority)[:batch_size]


def merge_daily_history(old, new):
    points = {}
    for row in old + new:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            continue
        day = row["date"]
        if day not in points:
            points[day] = copy.deepcopy(row)
            continue
        for field in ("steam", "cdk"):
            value = row.get(field)
            before = points[day].get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                points[day][field] = min(before, value) if isinstance(before, (int, float)) and not isinstance(before, bool) else value
    return [points[day] for day in sorted(points)]


def merge_game(old, current):
    merged = copy.deepcopy(old)
    merged.update(copy.deepcopy(current))
    merged["history"] = merge_daily_history(old.get("history", []), current.get("history", []))
    before, after = old.get("steamHistory"), current.get("steamHistory")
    if isinstance(before, dict) and isinstance(after, dict) and (
            before.get("country") == after.get("country") == "CN" and
            before.get("currency") == after.get("currency") == "CNY" and
            before.get("source") == after.get("source")):
        points = {point["date"]: copy.deepcopy(point)
                  for point in before.get("points", []) + after.get("points", [])
                  if isinstance(point, dict) and isinstance(point.get("date"), str)}
        if points:
            merged["steamHistory"]["points"] = [points[day] for day in sorted(points)]
            merged["steamHistory"]["coverageStart"] = min(points)
            merged["steamHistory"]["coverageEnd"] = max(points)
    return merged


def source_summaries(games, selected_ids, scheduled_at):
    summaries = []
    for source, name in SOURCES.items():
        relevant = [next((q for q in row.get("quotes", []) if q.get("source") == source), {}) for row in games]
        valid = sum(isinstance(q.get("price"), (int, float)) and not isinstance(q.get("price"), bool)
                    and math.isfinite(q["price"]) and q["price"] >= 0
                    and q.get("status") in ("ok", "unknown") for q in relevant)
        failed = sum(q.get("status") in ("error", "stale") for q in relevant)
        summary = {"id": source, "name": name,
                   "status": "error" if not relevant or failed == len(relevant) else "partial" if failed else "ok",
                   "checkedAt": scheduled_at, "validCount": valid, "totalCount": len(games),
                   "checkedCount": len(selected_ids),
                   "message": f"已采集目录共 {len(games)} 款，{valid} 款有正常报价记录（含旧快照）；本轮检查 {len(selected_ids)} 款，各报价更新时间独立保留"}
        if source == "steampy":
            summary["message"] += "；公开 CDK 参考价，实际卖单需登录，异常价已隐藏"
        elif source == "steamcici":
            summary["message"] += "；平台低价参考，库存及可成交价未确认"
        summaries.append(summary)
    return summaries


def refresh(core_catalog, directory_catalog, previous=None, *, batch_size=3, client=None,
            workers=3, scheduled_at=None, collector=None, refresh_history=False, refresh_content=False):
    previous = copy.deepcopy(previous or {})
    core, directory = normalize_catalog(core_catalog), normalize_catalog(directory_catalog)
    if not core:
        raise ValueError("core catalog must contain at least one valid game")
    scheduled_at = scheduled_at or now_iso()
    if not timestamp(scheduled_at):
        raise ValueError("scheduled_at must be an ISO timestamp with a timezone")
    batch = select_batch(core, directory, previous, batch_size)
    selected = core + batch
    selected_ids = {row["appid"] for row in selected}
    core_ids = {row["appid"] for row in core}
    previous_games = {app_id(row.get("appid")): copy.deepcopy(row)
                      for row in previous.get("games", []) if isinstance(row, dict) and app_id(row.get("appid"))}
    merged = copy.deepcopy(previous_games)
    error, partial = None, {}
    try:
        partial = (collector or collect)(selected, previous, client=client, workers=workers,
                                         refresh_history=refresh_history, refresh_content=refresh_content)
        if not isinstance(partial, dict) or not isinstance(partial.get("games"), list):
            raise ValueError("collector did not return a games array")
        for row in partial["games"]:
            appid = app_id(row.get("appid")) if isinstance(row, dict) else None
            if appid in selected_ids:
                merged[appid] = merge_game(previous_games.get(appid, {}), row)
                merged[appid]["catalogOrigin"] = "curated" if appid in core_ids else "steam_directory"
    except Exception as exc:
        # Record the attempt even on a transport-wide failure; retain every
        # prior quote and history value with its original observed timestamp.
        error = type(exc).__name__
        merged = copy.deepcopy(previous_games)
        partial = {}
    last_attempts = previous.get("rotation", {}).get("lastAttempts", {})
    last_attempts = copy.deepcopy(last_attempts) if isinstance(last_attempts, dict) else {}
    last_attempts.update({str(appid): scheduled_at for appid in selected_ids})
    returned_ids = {app_id(row.get("appid")) for row in partial.get("games", []) if isinstance(row, dict)}
    succeeded_ids = selected_ids.intersection(returned_ids) if not error else set()
    rotation = {"scheduledAt": scheduled_at, "checkedCount": len(selected), "coreCount": len(core),
                "batchCount": len(batch), "newCount": sum(appid not in previous_games for appid in succeeded_ids),
                "coreAppIds": [row["appid"] for row in core], "batchAppIds": [row["appid"] for row in batch],
                "catalogCount": len({row["appid"] for row in directory + core}),
                "collectedCount": len(merged), "returnedCount": len(succeeded_ids), "lastAttempts": last_attempts,
                "status": "error" if error else "partial" if succeeded_ids != selected_ids else "ok"}
    if error:
        rotation["error"] = error
    snapshot = copy.deepcopy(previous)
    snapshot.update({"schemaVersion": 1, "generatedAt": scheduled_at, "refreshMinutes": 15,
                     "country": "CN", "currency": "CNY", "games": list(merged.values()), "rotation": rotation})
    snapshot["sources"] = source_summaries(snapshot["games"], selected_ids, scheduled_at)
    return snapshot


def atomic_write(path, snapshot):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp", prefix=path.name + ".",
                                         dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(snapshot, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "catalog.json")
    parser.add_argument("--index", type=Path, default=ROOT / "dist" / "data" / "catalog-index.json")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "data" / "prices.json")
    parser.add_argument("--batch-size", type=int, default=3, help="additional directory games per run; default 3")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--refresh-history", action="store_true")
    parser.add_argument("--refresh-content", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 0:
        parser.error("--batch-size must be nonnegative")
    read = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
    snapshot = refresh(read(args.catalog), read(args.index) if args.index.exists() else {},
                       read(args.output) if args.output.exists() else {}, batch_size=args.batch_size,
                       workers=args.workers, refresh_history=args.refresh_history, refresh_content=args.refresh_content)
    atomic_write(args.output, snapshot)
    stats = {key: value for key, value in snapshot["rotation"].items() if key != "lastAttempts"}
    print(json.dumps(stats, ensure_ascii=True))
    if snapshot["rotation"]["status"] == "error":
        print("Collection failed; old quotes retained and rotation attempts recorded.", file=sys.stderr)
    # A written failure state still needs committing so future rounds advance.
    return 0


if __name__ == "__main__":
    sys.exit(main())
