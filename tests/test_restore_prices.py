"""A previous Actions snapshot must preserve progress across partial runs."""
from pathlib import Path
import tempfile
import unittest

from scripts.direct_prices import collect
from scripts.price_format import load_shard_records, write_shard
from scripts.restore_prices import restore


OLD = "2026-10-17T09:00:00Z"
NEW = "2026-10-20T09:00:00Z"


def row(appid, price, checked):
    return {"appid": appid, "price": price, "originalPrice": price,
            "discount": 0, "status": "ok", "checkedAt": checked}


class RestoreTests(unittest.TestCase):
    def test_merge_preserves_newer_baseline_and_adds_saved_progress(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            source, target = Path(directory) / "saved", Path(directory) / "baseline"
            write_shard(source / "price-00.json", 0, 2, NEW,
                        [row(2, 20, NEW), row(4, 40, OLD), row(6, 60, NEW), row(8, 80, NEW)])
            write_shard(target / "price-00.json", 0, 2, OLD,
                        [row(2, 10, OLD), row(4, 45, NEW), row(10, 100, OLD)])
            self.assertEqual(restore(source, target, catalog_ids=[2, 4, 6, 10], shard_count=2), 2)
            records, _ = load_shard_records(target / "price-00.json", 0, 2)
            self.assertEqual({appid: record["price"] for appid, record in records.items()},
                             {2: 20, 4: 45, 6: 60, 10: 100})
            self.assertEqual(records[2]["checkedAt"], NEW)
            self.assertEqual(records[10]["checkedAt"], OLD)

    def test_restored_partial_run_advances_the_next_bounded_batch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            source, target = Path(directory) / "saved", Path(directory) / "baseline"
            write_shard(source / "price-00.json", 0, 2, NEW,
                        [row(2, 20, NEW), row(4, 40, OLD)])
            write_shard(target / "price-00.json", 0, 2, OLD,
                        [row(2, 10, OLD), row(4, 40, OLD)])
            restore(source, target, catalog_ids=[2, 4], shard_count=2)
            previous, _ = load_shard_records(target / "price-00.json", 0, 2)
            response = {"4": {"success": True, "data": {"price_overview": {
                "currency": "CNY", "final": 4000, "initial": 4000}}}}
            result = collect([2, 4], previous, fetcher=lambda _: response,
                             batch_size=1, max_batches=1, sleeper=lambda _: None)
            self.assertEqual(result["attemptedAppids"], [4])

    def test_absent_or_malformed_previous_artifact_leaves_baseline_intact(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            source, target = Path(directory) / "saved", Path(directory) / "baseline"
            write_shard(target / "price-00.json", 0, 2, OLD, [row(2, 20, OLD)])
            source.mkdir()
            (source / "price-00.json").write_text("broken", encoding="utf-8")
            self.assertEqual(restore(source, target, catalog_ids=[2], shard_count=2), 0)
            records, _ = load_shard_records(target / "price-00.json", 0, 2)
            self.assertEqual(records[2]["price"], 20)


if __name__ == "__main__":
    unittest.main()
