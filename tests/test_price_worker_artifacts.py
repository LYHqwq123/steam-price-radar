"""Actual worker outputs must exist before a price refresh can publish."""
import unittest

from scripts.verify_price_workers import verify


def artifact(worker, **fields):
    return {"name": f"price-shards-{worker}", "size_in_bytes": 100, "expired": False, **fields}


class WorkerArtifactTests(unittest.TestCase):
    def test_false_success_with_only_baseline_and_pages_artifacts_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing current-run worker artifacts"):
            verify([{"name": "latest-steam-price-shards"}, {"name": "github-pages"}])

    def test_one_missing_worker_is_rejected_instead_of_deploying_partial_old_baseline(self):
        with self.assertRaisesRegex(ValueError, "7"):
            verify([artifact(index) for index in range(7)])

    def test_all_eight_current_workers_are_accepted(self):
        self.assertEqual(verify([artifact(index) for index in range(8)]), set(range(8)))

    def test_expired_empty_and_duplicate_worker_outputs_are_rejected(self):
        for bad in (artifact(7, expired=True), artifact(7, size_in_bytes=0), artifact(6)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                verify([artifact(index) for index in range(7)] + [bad])


if __name__ == "__main__":
    unittest.main()
