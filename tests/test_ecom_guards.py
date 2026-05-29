import unittest

from ecom_guards import GuardState, guard_before_execution, guard_request, is_catalogue_count


class Req_Exec:
    def __init__(self, path, args=None, stdin=""):
        self.path = path
        self.args = args or []
        self.stdin = stdin


class Req_Read:
    def __init__(self, path):
        self.path = path


class Req_InventoryCount:
    def __init__(self, store_id, skus):
        self.store_id = store_id
        self.skus = skus


class Req_CatalogueLookup:
    def __init__(self, key="lookup-key"):
        self.key = key

    def model_dump_json(self):
        return self.key


class Req_StoreLookup:
    pass


class ReportTaskCompletion:
    def __init__(self, message, grounding_refs=None):
        self.message = message
        self.grounding_refs = grounding_refs or []


class GuardRequestTest(unittest.TestCase):
    def test_exec_blocks_relative_path(self):
        message = guard_request(Req_Exec("sql"), "task")

        self.assertIn("absolute runtime executable", message)

    def test_exec_blocks_prose_sql_stdin(self):
        message = guard_request(
            Req_Exec("/bin/sql", stdin="please check the product table"),
            "task",
        )

        self.assertIn("must contain only SQL", message)

    def test_inventory_blocks_unresolved_skus(self):
        message = guard_request(
            Req_InventoryCount("store_vienna_meidling", ["garden trap slug"]),
            "task",
        )

        self.assertIn("exact catalogue `sku` values", message)

    def test_exact_format_blocks_explanatory_completion(self):
        state = GuardState(task_inspections=1)
        message = guard_before_execution(
            ReportTaskCompletion("There are 3 available", ["/docs/policy.md"]),
            'Answer in exactly format "<COUNT:%d>"',
            state,
            task_completed=True,
        )

        self.assertIn("exact answer format", message)

    def test_multi_product_availability_requires_inventory_count_before_final(self):
        state = GuardState(task_inspections=1)
        message = guard_before_execution(
            ReportTaskCompletion("<COUNT:2>", ["/proc/catalog/A.json"]),
            "How many of these products are available today? Answer in exactly format.",
            state,
            task_completed=True,
        )

        self.assertIn("require calling `inventory_count`", message)

    def test_repeat_read_blocks(self):
        state = GuardState(task_inspections=1, read_path_counts={"/docs/policy.md": 2})
        message = guard_before_execution(
            Req_Read("/docs/policy.md"),
            "task",
            state,
        )

        self.assertIn("already been read twice", message)

    def test_repeat_catalogue_lookup_blocks(self):
        state = GuardState(task_inspections=1, catalogue_lookup_counts={"lookup-key": 2})
        message = guard_before_execution(
            Req_CatalogueLookup(),
            "task",
            state,
        )

        self.assertIn("already returned", message)

    def test_resolved_store_blocks_repeat_store_lookup_for_availability(self):
        state = GuardState(
            task_inspections=1,
            resolved_store=(
                "store_vienna_meidling",
                "/proc/stores/store_vienna_meidling.json",
            ),
        )
        message = guard_before_execution(
            Req_StoreLookup(),
            "How many of these products have at least 2 items available today?",
            state,
        )

        self.assertIn("store is already resolved", message)

    def test_support_note_pack_count_is_not_catalogue_count_report(self):
        self.assertFalse(
            is_catalogue_count(
                "A support note claims we stock the Wood and Drywall Screw from Heco "
                "that has screw type drywall screw and has pack count 50 pcs. "
                "Check the actual catalogue item."
            )
        )

    def test_catalogue_count_report_still_detects(self):
        self.assertTrue(
            is_catalogue_count(
                "For the catalogue count report, how many products are Manual Garden Tool?"
            )
        )

    def test_support_note_no_requires_checked_sku_in_message(self):
        state = GuardState(task_inspections=1)
        message = guard_before_execution(
            ReportTaskCompletion("<NO>", ["/proc/catalog/Bondex/PNT-3APVSF7J.json"]),
            "A support note claims we stock the Wood Stain and Deck Oil from Bondex. "
            "Check the actual catalogue item, cite the exact product record, and if "
            "the base product exists but that extra catalogue claim is absent, answer "
            "with <NO> and include the checked SKU.",
            state,
            task_completed=True,
        )

        self.assertIn("<NO> PNT-3APVSF7J", message)

    def test_support_note_no_accepts_checked_sku_in_message(self):
        state = GuardState(task_inspections=1)
        message = guard_before_execution(
            ReportTaskCompletion(
                "<NO> PNT-3APVSF7J",
                ["/proc/catalog/Bondex/PNT-3APVSF7J.json"],
            ),
            "A support note claims we stock the Wood Stain and Deck Oil from Bondex. "
            "Check the actual catalogue item, cite the exact product record, and if "
            "the base product exists but that extra catalogue claim is absent, answer "
            "with <NO> and include the checked SKU.",
            state,
            task_completed=True,
        )

        self.assertIsNone(message)


if __name__ == "__main__":
    unittest.main()
