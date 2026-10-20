"""Sharded collection, retries and ownership behavior without live requests."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from scripts.direct_prices import (RateLimitedError, cents, collect, main, parse_record,
                                   request_batch, worker_appids, worker_shards, write_worker_shards)
from scripts.price_format import load_shard_records, read_shard


STAMP = "2026-10-17T09:00:00Z"
OLD_STAMP = "2026-10-16T09:00:00Z"


def response(final=2475, initial=5500, currency="CNY"):
    return {"success": True, "data": {"price_overview": {
        "currency": currency, "initial": initial, "final": final, "discount_percent": 55}}}


def old_record(appid=1, checked=OLD_STAMP):
    return {"appid": appid, "price": 55.0, "originalPrice": 55.0, "discount": 0,
            "checkedAt": checked, "status": "ok", "note": "previous"}


def run_collection(directory, old=None, **kwargs):
    return collect(directory, old, sleeper=lambda _: None, clock=lambda: STAMP,
                   progress=None, **kwargs)


class RequestTests(unittest.TestCase):
    def test_temporary_transport_failure_retries_then_succeeds(self):
        payload = {"1": response()}
        with patch("scripts.direct_prices.urlopen",
                   side_effect=[URLError("temporary"), io.BytesIO(json.dumps(payload).encode())]) as opening, \
                patch("scripts.direct_prices.time.sleep") as sleeper:
            self.assertEqual(request_batch([1]), payload)
        self.assertEqual(opening.call_count, 2)
        sleeper.assert_called_once_with(2)

    def test_request_batch_uses_100_ids_price_only_and_cn(self):
        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"1":{"success":true,"data":[]}}'

        with patch("scripts.direct_prices.urlopen", return_value=Reply()) as mocked:
            request_batch(list(range(1, 101)))
        query = parse_qs(urlparse(mocked.call_args.args[0].full_url).query)
        self.assertEqual(len(query["appids"][0].split(",")), 100)
        self.assertEqual(query["filters"], ["price_overview"])
        self.assertEqual(query["cc"], ["cn"])

    def test_rate_limit_retries_with_backoff_then_stops(self):
        errors = [HTTPError("url", 429, "limited", {"Retry-After": "5"}, None) for _ in range(3)]
        with patch("scripts.direct_prices.urlopen", side_effect=errors) as opening, \
                patch("scripts.direct_prices.time.sleep") as sleeper:
            with self.assertRaises(RateLimitedError):
                request_batch([1])
        self.assertEqual(opening.call_count, 3)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [5, 5])

    def test_permanent_http_error_is_not_retried(self):
        with patch("scripts.direct_prices.urlopen",
                   side_effect=HTTPError("url", 400, "bad", {}, None)) as opening:
            with self.assertRaises(HTTPError):
                request_batch([1])
        self.assertEqual(opening.call_count, 1)


class RecordTests(unittest.TestCase):
    def test_cents_and_genuine_zero_are_preserved(self):
        row = parse_record(1, response(0, 5500), STAMP)
        self.assertEqual((row["price"], row["originalPrice"], row["discount"]), (0, 55, 100))
        self.assertEqual(cents("2475"), 24.75)
        self.assertEqual(parse_record(1, response(), STAMP)["price"], 24.75)

    def test_empty_array_and_missing_overview_do_not_guess_free(self):
        for data in ([], {}, {"is_free": True}):
            with self.subTest(data=data):
                row = parse_record(1, {"success": True, "data": data}, STAMP)
                self.assertEqual(row["status"], "unavailable")
                self.assertIsNone(row["price"])

    def test_foreign_currency_bool_nan_and_fractional_cents_rejected(self):
        for currency in ("USD", "HKD", None):
            row = parse_record(1, response(currency=currency), STAMP)
            self.assertIsNone(row["price"])
        for invalid in (True, False, float("nan"), float("inf"), -1, 2475.5, "nan"):
            with self.subTest(value=invalid):
                row = parse_record(1, response(invalid), STAMP)
                self.assertEqual(row["status"], "error")
                self.assertIsNone(row["price"])


class CollectTests(unittest.TestCase):
    def test_failed_batch_preserves_price_and_original_timestamp(self):
        def fail(_):
            raise OSError("offline")
        old = {1: old_record()}
        result = run_collection([1, 2], old, fetcher=fail)
        self.assertEqual(result["status"], "error")
        first, second = result["updates"][1], result["updates"][2]
        self.assertEqual((first["price"], first["checkedAt"], first["status"]), (55, OLD_STAMP, "stale"))
        self.assertEqual((second["price"], second["status"]), (None, "error"))
        self.assertEqual(old, {1: old_record()})

    def test_batch_limit_returns_only_attempted_updates(self):
        calls = []

        def fetch(ids):
            calls.append(ids)
            return {str(appid): response() for appid in ids}

        result = run_collection(range(1, 6), fetcher=fetch, batch_size=2, max_batches=1)
        self.assertEqual(calls, [[1, 2]])
        self.assertEqual(result["attemptedAppids"], [1, 2])
        self.assertEqual(set(result["updates"]), {1, 2})
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["stopped"])
        self.assertFalse(result["completed"])

    def test_stalest_and_never_checked_games_are_attempted_first(self):
        calls = []

        def fetch(ids):
            calls.append(ids)
            return {str(appid): response() for appid in ids}

        old = {1: old_record(1, OLD_STAMP), 2: old_record(2, STAMP)}
        result = run_collection([1, 2, 3], old, fetcher=fetch, batch_size=1, max_batches=2)
        self.assertEqual(calls, [[3], [1]])
        self.assertTrue(result["stopped"])

    def test_full_run_batches_100_and_marks_itself_complete(self):
        calls = []

        def fetch(ids):
            calls.append(ids)
            return {str(appid): response() for appid in ids}

        result = run_collection(range(1, 202), fetcher=fetch)
        self.assertEqual(list(map(len, calls)), [100, 100, 1])
        self.assertEqual(result["attemptedCount"], 201)
        self.assertTrue(result["completed"])
        self.assertEqual(result["status"], "ok")

    def test_rate_limit_stops_after_the_current_batch(self):
        calls = []

        def fetch(ids):
            calls.append(ids)
            if len(calls) == 2:
                raise RateLimitedError("429")
            return {str(appid): response() for appid in ids}

        result = run_collection(range(1, 501), fetcher=fetch, batch_size=100)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["attemptedCount"], 200)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["stopped"])

    def test_repeated_transport_errors_are_bounded(self):
        calls = []

        def fail(ids):
            calls.append(ids)
            raise OSError("offline")

        result = run_collection(range(1, 1001), fetcher=fail, batch_size=100)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["attemptedCount"], 300)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["stopped"])

    def test_invalid_limits_rejected(self):
        for kwargs in ({"batch_size": 101}, {"batch_size": 0}, {"max_batches": 0},
                       {"max_seconds": 0}, {"delay": 0.6}, {"delay": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_collection([1], fetcher=lambda _: {}, **kwargs)

    def test_empty_ids_rejected(self):
        with self.assertRaises(ValueError):
            run_collection([], fetcher=lambda _: {})


class ShardOwnershipTests(unittest.TestCase):
    def test_worker_class_matches_public_shard_indices(self):
        catalog = {"games": [[appid, f"Game {appid}"] for appid in range(1, 129)]}
        all_ids, selected = worker_appids(catalog, worker_index=0, worker_count=8)
        self.assertEqual(all_ids, list(range(1, 129)))
        self.assertEqual(selected, [appid for appid in all_ids if appid % 64 < 8])

    def test_eight_workers_cover_even_steam_ids_once_with_balanced_shards(self):
        catalog = {"games": [[appid, f"Game {appid}"] for appid in range(10, 6410, 10)]}
        selections = [worker_appids(catalog, index, 8)[1] for index in range(8)]
        flattened = [appid for selection in selections for appid in selection]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(set(flattened), set(range(10, 6410, 10)))
        self.assertEqual([len(selection) for selection in selections], [80] * 8)
        for index, selection in enumerate(selections):
            self.assertTrue(all(appid % 64 in worker_shards(64, index, 8) for appid in selection))

    def test_writer_touches_only_attempted_shards_and_keeps_unattempted_rows(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            shard_dir = Path(folder) / "price-shards"
            catalog_ids = [4, 8, 12, 16, 2, 6, 10, 14]
            previous = {appid: old_record(appid) for appid in catalog_ids}
            updates = {4: old_record(4, STAMP)}
            written = write_worker_shards(shard_dir, 4, 0, 2, catalog_ids, previous, updates, [4], STAMP)
            self.assertEqual([entry["index"] for entry in written], [0])
            self.assertFalse((shard_dir / "price-02.json").exists())
            records, shard = load_shard_records(shard_dir / "price-00.json", 0, 4)
            self.assertIsNotNone(shard)
            self.assertEqual(set(records), {4, 8, 12, 16})
            self.assertEqual(records[8]["checkedAt"], OLD_STAMP)
            self.assertEqual(records[4]["checkedAt"], STAMP)

    def test_writer_rejects_another_workers_consecutive_shard(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            with self.assertRaises(ValueError):
                write_worker_shards(Path(folder), 64, 0, 8, [10], {}, {10: old_record(10)}, [10], STAMP)

    def test_main_writes_manifest_and_atomic_shards(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            directory = Path(folder)
            catalog = directory / "catalog.json"
            catalog.write_text(json.dumps({"schemaVersion": 2, "games": [
                [appid, f"游戏 {appid}"] for appid in range(1, 9)]}), encoding="utf-8")
            output = directory / "data"

            def fetch(ids):
                return {str(appid): response() for appid in ids}

            code = main(["--index", str(catalog), "--output-dir", str(output), "--shard-count", "2",
                         "--worker-index", "0", "--worker-count", "1", "--delay", "0.7",
                         "--write-manifest"], fetcher=fetch, sleeper=lambda _: None, clock=lambda: STAMP)
            self.assertEqual(code, 0)
            manifest = json.loads((output / "price-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual((manifest["catalogGameCount"], manifest["gameCount"], manifest["pricedCount"]),
                             (8, 8, 8))
            self.assertTrue(manifest["shards"][0]["available"])
            self.assertEqual(list((output / "price-shards").glob("*.tmp")), [])
            for index in (0, 1):
                self.assertEqual(read_shard(output / "price-shards" / f"price-{index:02d}.json",
                                            index, 2)["shard"], index)

    def test_main_rejects_non_divisible_worker_topology(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            directory = Path(folder)
            catalog = directory / "catalog.json"
            catalog.write_text('{"games":[[1,"游戏"]]}', encoding="utf-8")
            self.assertEqual(main(["--index", str(catalog), "--output-dir", str(directory / "data"),
                                   "--shard-count", "3", "--worker-count", "2", "--delay", "0.7"]), 1)


if __name__ == "__main__":
    unittest.main()
