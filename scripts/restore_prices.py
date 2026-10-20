"""Merge a saved Actions price snapshot with the Git baseline, without requests.

Compare the observation time of each AppID rather than copying entire files:
an older successful artifact must never replace a newer baseline observation.
"""
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .catalog_format import catalog_appids, read_catalog
    from .price_format import SHARD_COUNT, load_shard_records, now_iso, shard_filename, timestamp, write_shard
except ImportError:
    from catalog_format import catalog_appids, read_catalog
    from price_format import SHARD_COUNT, load_shard_records, now_iso, shard_filename, timestamp, write_shard


def observed(record):
    stamp = timestamp(record.get("checkedAt")) if record else None
    return stamp.timestamp() if stamp else float("-inf")


def restore(source, target, *, catalog_ids=None, shard_count=SHARD_COUNT):
    wanted = set(catalog_ids) if catalog_ids is not None else None
    restored = 0
    for index in range(shard_count):
        saved, source_shard = load_shard_records(Path(source) / shard_filename(index), index, shard_count)
        if not source_shard:
            continue
        current, _ = load_shard_records(Path(target) / shard_filename(index), index, shard_count)
        changed = False
        for appid, record in saved.items():
            if appid % shard_count != index or wanted is not None and appid not in wanted:
                continue
            if appid not in current or observed(record) >= observed(current[appid]):
                if record != current.get(appid):
                    changed = True
                    restored += 1
                    current[appid] = record
        if changed:
            rows = [record for appid, record in current.items()
                    if appid % shard_count == index and (wanted is None or appid in wanted)]
            write_shard(Path(target) / shard_filename(index), index, shard_count, now_iso(), rows)
    return restored


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path("dist/data/price-shards"))
    parser.add_argument("--catalog", type=Path, default=Path("dist/data/catalog-index.json"))
    args = parser.parse_args(argv)
    ids = catalog_appids(read_catalog(args.catalog))
    if not ids:
        parser.error("catalog contains no valid AppIDs")
    count = restore(args.source, args.target, catalog_ids=ids)
    print(f"Restored {count} newer price records from the latest successful Actions snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
