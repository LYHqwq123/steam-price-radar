"""History integrity, currency/region matching, date boundaries and caching."""
import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.price_history import fetch_history, load_history, normalize_points


def stamp(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def row(date="2026-10-16T16:00:00Z", price="298", currency="CNY", **extra):
    return {"date": str(stamp(date)), "price": price, "currency": currency, **extra}


class Client:
    def __init__(self, rows=None, error=None, payload=None):
        self.payload = payload if payload is not None else {"status": "ok", "result": {"prices": rows or []}}
        self.error = error
        self.calls = []

    def get(self, url, data=None, headers=None):
        self.calls.append((url, headers))
        if self.error:
            raise self.error
        return copy.deepcopy(self.payload)


class HistoryTests(unittest.TestCase):
    NOW = "2026-10-17T02:00:00Z"

    def previous(self):
        return fetch_history(Client([row()]), 1245620, fetched_at=self.NOW)

    def test_dates_use_china_midnight_not_utc_date(self):
        points = normalize_points([row()], today="2026-10-17")
        self.assertEqual(points, [{"date": "2026-10-17", "price": 298.0}])

    def test_prices_are_yuan_and_require_native_cny(self):
        rows = [row(price="298", rmb_price="0.01"), row(currency="USD", price="1", rmb_price="7")]
        self.assertEqual(normalize_points(rows, today="2026-10-17"), [{"date": "2026-10-17", "price": 298.0}])

    def test_same_day_keeps_chronological_last_not_minimum_or_api_order(self):
        rows = [row("2026-10-17T05:00:00Z", "100"), row("2026-10-16T16:00:00Z", "50"),
                row("2026-10-17T05:00:00Z", "110")]
        self.assertEqual(normalize_points(rows, today="2026-10-17"), [{"date": "2026-10-17", "price": 110.0}])

    def test_invalid_values_and_future_days_are_rejected_but_free_is_valid(self):
        rows = [row(price=value) for value in [True, None, {}, "NaN", "Infinity", "-1"]]
        rows += [dict(row(), date=value) for value in [True, None, {}, "NaN", "Infinity", "-1"]]
        rows += [row("2026-10-18T00:00:00Z", "1"), {}, None, row(price="0")]
        self.assertEqual(normalize_points(rows, today="2026-10-17"), [{"date": "2026-10-17", "price": 0.0}])

    def test_recorded_coverage_sorted_and_not_artificially_limited_to_days_parameter(self):
        rows = [row(), row("2022-02-24T16:00:00Z", "178.8")]
        client = Client(rows)
        result = fetch_history(client, 1245620, fetched_at=self.NOW)
        self.assertEqual(result["coverageStart"], "2022-02-25")
        self.assertEqual(result["coverageEnd"], "2026-10-17")
        self.assertEqual(result["scope"], "recorded")
        self.assertEqual(result["country"], "CN")
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(parse_qs(urlparse(client.calls[0][0]).query)["cc"], ["cn"])
        self.assertEqual(client.calls[0][1]["Referer"], result["sourceUrl"])

    def test_summary_low_is_not_injected_into_real_series(self):
        payload = {"status": "ok", "result": {"prices": [row(price="178.8")], "lowest_info_v2": {"price": "1"}}}
        result = fetch_history(Client(payload=payload), 1245620, fetched_at=self.NOW)
        self.assertEqual(result["points"][0]["price"], 178.8)

    def test_empty_history_is_explicit_without_synthetic_current_point(self):
        result = fetch_history(Client(), 1245620, fetched_at=self.NOW)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["points"], [])
        self.assertIsNone(result["coverageStart"])

    def test_cache_avoids_requests_for_twenty_four_hours(self):
        previous = self.previous()
        client = Client(error=RuntimeError("Must not request"))
        result = load_history(client, 1245620, previous, fetched_at="2026-10-18T01:59:59Z")
        self.assertEqual(client.calls, [])
        self.assertEqual(result, previous)
        result["points"][0]["price"] = 1
        self.assertEqual(previous["points"][0]["price"], 298)

    def test_cache_expires_at_twenty_four_hours(self):
        client = Client([row(price="200")])
        result = load_history(client, 1245620, self.previous(), fetched_at="2026-10-18T02:00:00Z")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["points"][0]["price"], 200)

    def test_force_refresh_bypasses_cache(self):
        client = Client([row(price="200")])
        result = load_history(client, 1245620, self.previous(), refresh=True, fetched_at=self.NOW)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["points"][0]["price"], 200)

    def test_failure_preserves_previous_points_and_fetch_timestamp(self):
        previous = self.previous()
        result = load_history(Client(error=TimeoutError()), 1245620, previous, refresh=True, fetched_at="2026-10-19T00:00:00Z")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["points"], previous["points"])
        self.assertEqual(result["fetchedAt"], previous["fetchedAt"])
        self.assertEqual(previous["status"], "ok")

    def test_empty_response_does_not_erase_previous_history(self):
        previous = self.previous()
        result = load_history(Client(), 1245620, previous, refresh=True, fetched_at=self.NOW)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["points"], previous["points"])
        self.assertEqual(result["fetchedAt"], previous["fetchedAt"])

    def test_no_previous_history_and_failure_has_no_points(self):
        result = load_history(Client(error=TimeoutError()), 1245620, fetched_at=self.NOW)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["points"], [])

    def test_foreign_currency_previous_history_is_never_reused(self):
        previous = self.previous()
        previous["currency"] = "USD"
        result = load_history(Client(error=TimeoutError()), 1245620, previous, fetched_at=self.NOW)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["points"], [])

    def test_bad_api_payload_is_a_failure_not_successful_empty_history(self):
        for payload in ({"status": "error"}, {"status": "ok", "result": {}}, []):
            with self.subTest(payload=payload):
                result = load_history(Client(payload=payload), 1245620, fetched_at=self.NOW)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["points"], [])


if __name__ == "__main__":
    unittest.main()
