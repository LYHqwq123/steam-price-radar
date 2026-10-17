"""Batch coverage, currency and resumable failure behavior without live requests."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from scripts.direct_prices import (RateLimitedError, atomic_write, cents, collect,
                                  parse_record, request_batch)


STAMP = "2026-10-17T09:00:00Z"
OLD_STAMP = "2026-10-16T09:00:00Z"


def response(final=2475, initial=5500, currency="CNY"):
    return {"success": True, "data": {"price_overview": {
        "currency": currency, "initial": initial, "final": final, "discount_percent": 55}}}


def index(size):
    return {"games": [{"appid": number} for number in range(1, size + 1)]}


def previous():
    return {"source": "steam", "country": "CN", "currency": "CNY", "games": [
        {"appid": 1, "price": 55, "originalPrice": 55, "discount": 0,
         "checkedAt": OLD_STAMP, "status": "ok", "note": "previous"}]}


def run_collection(directory, old=None, **kwargs):
    return collect(directory, old, sleeper=lambda _: None, clock=lambda: STAMP,
                   progress=None, **kwargs)


class DirectPriceTests(unittest.TestCase):
    def test_temporary_transport_failure_retries_once(self):
        payload = {"1": response()}
        with patch("scripts.direct_prices.urlopen", side_effect=[URLError("temporary"), io.BytesIO(json.dumps(payload).encode())]) as opening, patch("scripts.direct_prices.time.sleep") as sleeper:
            self.assertEqual(request_batch([1]), payload)
        self.assertEqual(opening.call_count, 2)
        sleeper.assert_called_once_with(0.7)

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

    def test_batch_failure_preserves_price_and_original_timestamp(self):
        def fail(_):
            raise OSError("offline")
        old = previous()
        result = run_collection(index(2), old, fetcher=fail)
        self.assertEqual(result["status"], "partial")
        row, new = result["games"]
        self.assertEqual((row["price"], row["checkedAt"], row["status"]), (55, OLD_STAMP, "stale"))
        self.assertEqual((new["price"], new["status"]), (None, "error"))
        self.assertEqual(old, previous())

    def test_page_limit_cursor_resumes_then_wraps(self):
        calls = []
        def fetch(ids):
            calls.append(ids)
            return {str(appid): response() for appid in ids}
        first = run_collection(index(5), fetcher=fetch, batch_size=2, max_batches=1)
        self.assertEqual(first["syncState"]["nextAppid"], 3)
        self.assertEqual(first["status"], "partial")
        second = run_collection(index(5), first, fetcher=fetch, batch_size=2, max_batches=1)
        third = run_collection(index(5), second, fetcher=fetch, batch_size=2, max_batches=1)
        self.assertEqual(calls, [[1, 2], [3, 4], [5, 1]])
        self.assertEqual(third["syncState"]["nextAppid"], 2)
        self.assertEqual(third["pricedCount"], 5)

    def test_full_run_pages_100_and_retains_old_removed_records(self):
        old = previous()
        old["games"][0]["appid"] = 9999
        calls = []
        def fetch(ids):
            calls.append(ids)
            return {str(appid): response() for appid in ids}
        result = run_collection(index(201), old, fetcher=fetch)
        self.assertEqual(list(map(len, calls)), [100, 100, 1])
        self.assertEqual(result["gameCount"], 202)
        self.assertIn(9999, [row["appid"] for row in result["games"]])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["syncState"]["lastRunCompletedDirectory"])

    def test_rate_limit_stops_immediately_and_retains_partial_results(self):
        calls = []
        def fetch(ids):
            calls.append(ids)
            if len(calls) == 2:
                raise RateLimitedError("429")
            return {str(appid): response() for appid in ids}
        result = run_collection(index(500), fetcher=fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["pricedCount"], 100)
        self.assertEqual(result["syncState"]["nextAppid"], 201)
        self.assertEqual(result["status"], "partial")

    def test_http_429_adapter_stops_and_repeated_transport_errors_are_bounded(self):
        with patch("scripts.direct_prices.urlopen", side_effect=HTTPError("url", 429, "limited", {}, None)):
            with self.assertRaises(RateLimitedError):
                request_batch([1])
        calls = []
        def fail(ids):
            calls.append(ids)
            raise OSError("offline")
        result = run_collection(index(1000), fetcher=fail)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["attemptedBatches"], 3)

    def test_checkpoint_after_ten_batches_is_atomic_valid_json(self):
        checkpoints = []
        result = run_collection(index(1001), fetcher=lambda ids: {str(appid): response() for appid in ids},
                                checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["processedCount"], 1000)
        qa = Path(__file__).resolve().parents[1] / ".qa"
        qa.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=qa) as folder:
            target = Path(folder) / "direct-prices.json"
            atomic_write(target, result)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), result)
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_invalid_batch_limits_and_short_delays_rejected(self):
        for kwargs in ({"batch_size": 101}, {"batch_size": 0}, {"max_batches": 0},
                       {"delay": 0.6}, {"delay": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_collection(index(1), fetcher=lambda _: {}, **kwargs)


if __name__ == "__main__":
    unittest.main()
