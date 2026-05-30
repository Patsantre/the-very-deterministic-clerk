import unittest
from types import SimpleNamespace

from main import ECOM_DEV_TASK_ORDER, ECOM_PROD_TASK_ORDER, filtered_trial_ids


class MainRunnerFilterTest(unittest.TestCase):
    def test_unfiltered_returns_all_trial_ids(self):
        trial_ids, source = filtered_trial_ids([], ["trial_1", "trial_2"])

        self.assertEqual(trial_ids, ["trial_1", "trial_2"])
        self.assertEqual(source, "all")

    def test_benchmark_index_filters_requested_trials(self):
        benchmark_tasks = [
            SimpleNamespace(task_id="t01"),
            SimpleNamespace(task_id="t46"),
            SimpleNamespace(task_id="t02"),
        ]

        trial_ids, source = filtered_trial_ids(
            ["t46"],
            ["trial_t01", "trial_t46", "trial_t02"],
            benchmark_tasks=benchmark_tasks,
            bench_id="custom",
        )

        self.assertEqual(trial_ids, ["trial_t46"])
        self.assertEqual(source, "benchmark index")

    def test_ecom_dev_fallback_uses_current_53_task_order(self):
        trial_ids = [f"trial_{task_id}" for task_id in ECOM_DEV_TASK_ORDER]

        filtered, source = filtered_trial_ids(
            ["t44", "t45", "t46", "t48"],
            trial_ids,
            bench_id="bitgn/ecom1-dev",
        )

        self.assertEqual(filtered, ["trial_t44", "trial_t45", "trial_t46", "trial_t48"])
        self.assertEqual(source, "built-in task order")
        self.assertEqual(ECOM_DEV_TASK_ORDER[-1], "t53")

    def test_ecom_prod_fallback_uses_100_task_order(self):
        trial_ids = [f"trial_{task_id}" for task_id in ECOM_PROD_TASK_ORDER]

        filtered, source = filtered_trial_ids(
            ["t01", "t53", "t100"],
            trial_ids,
            bench_id="bitgn/ecom1-prod",
        )

        self.assertEqual(filtered, ["trial_t01", "trial_t53", "trial_t100"])
        self.assertEqual(source, "built-in task order")
        self.assertEqual(ECOM_PROD_TASK_ORDER[-1], "t100")

    def test_unknown_benchmark_falls_back_to_scan(self):
        trial_ids, source = filtered_trial_ids(
            ["t46"],
            ["trial_1", "trial_2"],
            bench_id="unknown",
        )

        self.assertEqual(trial_ids, ["trial_1", "trial_2"])
        self.assertEqual(source, "scan")


if __name__ == "__main__":
    unittest.main()
