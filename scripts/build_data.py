"""Validate published price shards and write the browser-facing manifest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .catalog_format import catalog_appids, read_catalog
    from .price_format import SHARD_COUNT, build_manifest, write_manifest
except ImportError:
    from catalog_format import catalog_appids, read_catalog
    from price_format import SHARD_COUNT, build_manifest, write_manifest

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "dist/data/catalog-index.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "dist/data")
    parser.add_argument("--output", type=Path, help="manifest path; defaults to DATA_DIR/price-manifest.json")
    parser.add_argument("--shard-count", type=int, default=SHARD_COUNT)
    parser.add_argument("--refresh-minutes", type=int, default=120)
    parser.add_argument("--prefix", default="price-shards")
    parser.add_argument("--require-complete", action="store_true",
                        help="fail unless every catalog game has a fresh usable price")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.shard_count <= 1000:
            raise ValueError("shard-count must be between 1 and 1000")
        if args.refresh_minutes < 1:
            raise ValueError("refresh-minutes must be positive")
        catalog = read_catalog(args.catalog)
        ids = catalog_appids(catalog)
        if not ids:
            raise ValueError("catalog contains no valid games")
        manifest = build_manifest(args.data_dir / args.prefix, catalog_appids=ids,
                                  output_dir=args.data_dir, refresh_minutes=args.refresh_minutes,
                                  prefix=args.prefix, shard_count=args.shard_count)
        write_manifest(args.output or args.data_dir / "price-manifest.json", manifest)
        summary = {key: manifest[key] for key in (
            "generatedAt", "checkedAt", "status", "gameCount", "catalogGameCount", "pricedCount",
            "missingCount", "staleRecordCount", "duplicateCount", "invalidCount",
            "availableShardCount", "shardCount")}
        summary["statusCounts"] = manifest["statusCounts"]
        print(json.dumps(summary, ensure_ascii=True))
        if manifest["status"] == "error":
            return 1
        if args.require_complete and manifest["status"] != "ok":
            return 1
        return 0
    except (ValueError, OSError, TypeError) as error:
        print(f"Price manifest build failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
