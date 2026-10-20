"""Compact shard rows and manifest coverage checks."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.build_data import main as build_main
from scripts.price_format import (build_manifest, legacy_records, load_shard_records, pack_record,
                                  read_shard, unpack_record, write_manifest, write_shard)


NOW = "2026-10-17T12:00:00Z"
OLD = "2026-10-15T12:00:00Z"


def record(appid, price=24.75, status="ok", checked=NOW, original=55):
    return {"appid": appid, "price": price, "originalPrice": original, "discount": 55,
            "status": status, "checkedAt": checked}


class RowTests(unittest.TestCase):
    def test_round_trip_uses_cents_and_shard_time_override(self):
        row = pack_record(record(10), NOW)
        self.assertEqual(row, [10, 2475, 5500, 55, 0])
        self.assertEqual(unpack_record(row, NOW), record(10))
        override = pack_record(record(11, checked=OLD), NOW)
        self.assertEqual(override[-1], OLD)
        self.assertEqual(unpack_record(override, NOW)["checkedAt"], OLD)

    def test_free_zero_price_and_invalid_amounts(self):
        self.assertEqual(pack_record(record(5, 0, original=0), NOW)[1], 0)
        self.assertEqual(unpack_record(pack_record(record(5, 0, original=0), NOW), NOW)["price"], 0)
        for invalid in (True, False, float("nan"), -1, "12"):
            self.assertIsNone(pack_record({"appid": 6, "price": invalid, "status": "ok"}, NOW)[1])
        self.assertEqual(pack_record({"appid": 6, "price": 10, "status": "unavailable"}, NOW)[1], None)
        self.assertEqual(pack_record({"appid": 6, "price": 10, "status": "ok", "discount": 150}, NOW)[3], None)

    def test_invalid_appid_and_status_are_rejected(self):
        for appid in (True, 0, -1, "abc", None):
            self.assertIsNone(pack_record({"appid": appid, "price": 1, "status": "ok"}, NOW))
        self.assertEqual(pack_record({"appid": "1", "price": 1, "status": "ok"}, NOW)[0], 1)
        self.assertEqual(unpack_record([1, 100, 100, 0, 99], NOW), None)
        self.assertEqual(unpack_record([1, 100], NOW), None)

    def test_legacy_records_keep_only_valid_rows(self):
        rows = legacy_records({"games": [{"appid": 1, "price": 10, "status": "ok", "checkedAt": NOW},
                                         {"appid": True, "price": 1}, "bad"]})
        self.assertEqual(list(rows), [1])
        self.assertEqual(rows[1]["price"], 10)
        self.assertEqual(rows[1]["checkedAt"], NOW)


class ShardTests(unittest.TestCase):
    def test_write_read_and_index_validation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            path = Path(folder) / "price-00.json"
            write_shard(path, 0, 4, NOW, [record(4), record(8)])
            shard = read_shard(path, 0, 4)
            self.assertEqual(shard["checkedAt"], NOW)
            self.assertIsNone(read_shard(path, 1, 4))
            self.assertIsNone(read_shard(path, 0, 8))
            records, _ = load_shard_records(path, 0, 4)
            self.assertEqual(set(records), {4, 8})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class ManifestTests(unittest.TestCase):
    def make_shards(self, folder, catalog, rows_by_shard, checked=NOW):
        for index, rows in rows_by_shard.items():
            write_shard(folder / f"price-{index:02d}.json", index, 4, checked, rows)

    def test_missing_and_old_records_make_partial_but_keep_real_counts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            shard_dir = Path(folder) / "price-shards"
            catalog = list(range(1, 10))
            self.make_shards(shard_dir, catalog, {
                1: [record(1), record(5, checked=OLD)],
                2: [record(2, status="unavailable", price=None)],
                3: [record(3, status="error", price=None)],
            })
            manifest = build_manifest(shard_dir, catalog_appids=catalog, output_dir=folder,
                                      generated_at=NOW, shard_count=4)
            self.assertEqual((manifest["catalogGameCount"], manifest["gameCount"], manifest["pricedCount"]),
                             (9, 4, 2))
            self.assertEqual(manifest["missingCount"], 5)
            self.assertEqual(manifest["statusCounts"], {"ok": 2, "unavailable": 1, "stale": 0, "error": 1})
            self.assertEqual(manifest["staleRecordCount"], 1)
            self.assertEqual(manifest["status"], "partial")
            self.assertEqual(len(manifest["shards"]), 4)
            self.assertEqual(manifest["shards"][0]["available"], False)
            self.assertEqual(manifest["shards"][1]["file"], "price-shards/price-01.json")

    def test_complete_catalog_manifest_is_ok(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            shard_dir = Path(folder) / "price-shards"
            catalog = list(range(1, 9))
            rows = {0: [record(4), record(8)], 1: [record(1), record(5)],
                    2: [record(2), record(6)], 3: [record(3), record(7)]}
            self.make_shards(shard_dir, catalog, rows)
            manifest = build_manifest(shard_dir, catalog_appids=catalog, output_dir=folder,
                                      generated_at=NOW, shard_count=4)
            self.assertEqual(manifest["status"], "ok")
            self.assertEqual(manifest["pricedCount"], 8)
            self.assertEqual(manifest["checkedAt"], NOW)
            self.assertEqual(manifest["availableShardCount"], 4)

    def test_invalid_and_duplicate_rows_are_reported(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            shard_dir = Path(folder) / "price-shards"
            write_shard(shard_dir / "price-00.json", 0, 2, NOW, [record(2), record(4)])
            # AppID 2 does not belong to shard 1 even though it is a duplicate.
            write_shard(shard_dir / "price-01.json", 1, 2, NOW, [record(2), record(1)])
            manifest = build_manifest(shard_dir, catalog_appids=[1, 2, 4], output_dir=folder,
                                      generated_at=NOW, shard_count=2)
            self.assertEqual(manifest["invalidCount"], 1)
            self.assertEqual(manifest["gameCount"], 3)
            self.assertEqual(manifest["statusCounts"]["ok"], 3)
            self.assertNotEqual(manifest["status"], "ok")

    def test_no_rows_is_an_error_and_build_cli_writes_manifest(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            directory = Path(folder)
            catalog = directory / "catalog.json"
            catalog.write_text(json.dumps({"games": [[1, "一"], [3, "三"]]}), encoding="utf-8")
            output = directory / "data"
            data = output / "price-shards"
            write_shard(data / "price-01.json", 1, 2, NOW, [record(1), record(3)])
            self.assertEqual(build_main(["--catalog", str(catalog), "--data-dir", str(output),
                                         "--shard-count", "2"]), 0)
            manifest = json.loads((output / "price-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["pricedCount"], 2)
            self.assertEqual(manifest["shards"][0]["available"], False)

            empty = directory / "empty"
            empty.mkdir()
            catalog.write_text(json.dumps({"games": []}), encoding="utf-8")
            self.assertEqual(build_main(["--catalog", str(catalog), "--data-dir", str(empty)]), 1)

            catalog.write_text(json.dumps({"games": [[1, "一"]]}), encoding="utf-8")
            self.assertEqual(build_main(["--catalog", str(catalog), "--data-dir", str(empty),
                                         "--require-complete"]), 1)

    def test_manifest_round_trip(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            path = Path(folder) / "manifest.json"
            write_manifest(path, {"schemaVersion": 2, "status": "ok"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"schemaVersion": 2, "status": "ok"})


if __name__ == "__main__":
    unittest.main()
