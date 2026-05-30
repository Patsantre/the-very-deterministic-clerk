import unittest
from types import SimpleNamespace

from ecom_domain_tools import (
    catalogue_count_report,
    catalogue_paths_from_output,
    count_report_summary_from_output,
    csv_rows,
    exec_sql,
    first_catalogue_row,
    inventory_refs_from_output,
    inventory_summary_from_output,
    single_store_from_lookup_output,
    sku_from_catalogue_output,
    sql_in,
    sql_literal,
    store_lookup,
)


class SqlHelperTest(unittest.TestCase):
    def test_sql_literal_escapes_quotes(self):
        self.assertEqual(sql_literal("O'Reilly"), "'O''Reilly'")

    def test_sql_in(self):
        self.assertEqual(sql_in(["A", "B's"]), "'A', 'B''s'")

    def test_csv_rows(self):
        self.assertEqual(
            csv_rows("id,name\n1,A\n2,B\n"),
            [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
        )

    def test_exec_sql_tries_alternate_tmpdir_after_no_space(self):
        class FakeVm:
            def __init__(self):
                self.calls = []

            def exec(self, request):
                args = list(getattr(request, "args", []))
                self.calls.append(args)
                if args == ["--tmpdir", "/tmp/mount"]:
                    return SimpleNamespace(stdout="count\n10\n", stderr="")
                return SimpleNamespace(stdout="", stderr="no space left on device")

        vm = FakeVm()
        result = exec_sql(vm, "select 1;")

        self.assertEqual(result.stdout, "count\n10\n")
        self.assertEqual(vm.calls, [[], ["--tmpdir", "/work/tmp"], ["--tmpdir", "/tmp/mount"]])

    def test_exec_sql_tries_tmpdir_after_empty_select_output(self):
        class FakeVm:
            def __init__(self):
                self.calls = []

            def exec(self, request):
                args = list(getattr(request, "args", []))
                self.calls.append(args)
                if args == ["--tmpdir", "/work/tmp"]:
                    return SimpleNamespace(stdout="id,path\n", stderr="")
                return SimpleNamespace(stdout="", stderr="")

        vm = FakeVm()
        result = exec_sql(vm, "select id, path from stores;")

        self.assertEqual(result.stdout, "id,path\n")
        self.assertEqual(vm.calls, [[], ["--tmpdir", "/work/tmp"]])

    def test_catalogue_count_report_exclude_literal_is_not_rewritten(self):
        class FakeVm:
            def __init__(self):
                self.sql = []

            def exec(self, request):
                sql = request.stdin
                self.sql.append(sql)
                if "count(*) as count" in sql:
                    return SimpleNamespace(stdout="count\n0\n", stderr="")
                if "select product_sku as sku" in sql:
                    return SimpleNamespace(stdout="sku,path,name\n", stderr="")
                raise AssertionError(f"unexpected SQL: {sql}")

        vm = FakeVm()
        result = catalogue_count_report(
            vm,
            SimpleNamespace(
                kind_id="wiper_blades",
                kind_name="",
                city="",
                doc_path="/docs/count-policy.md",
                threshold=1,
                exclude_family_ids=["fam_family_id_x"],
            ),
        )

        self.assertIn("count=0", result)
        self.assertTrue(vm.sql)
        for sql in vm.sql:
            self.assertIn("product_family_id not in ('fam_family_id_x')", sql)
            self.assertNotIn("fam_product_family_id_x", sql)


class OutputParserTest(unittest.TestCase):
    def test_catalogue_output_helpers(self):
        text = "\n".join(
            [
                "catalogue_lookup",
                "exact_matches=1",
                "sku,path,name,family_id,family_name",
                "ABC-123,/proc/catalog/ABC-123.json,Demo Product,fam_1,Demo Family",
            ]
        )

        self.assertEqual(sku_from_catalogue_output(text), "ABC-123")
        self.assertEqual(catalogue_paths_from_output(text), ["/proc/catalog/ABC-123.json"])
        self.assertEqual(first_catalogue_row(text)["family_id"], "fam_1")

    def test_inventory_refs_from_output(self):
        text = "\n".join(
            [
                "inventory_count",
                "count=2",
                "final_product_refs:",
                "- /proc/catalog/A.json",
                "- /proc/catalog/B.json",
                "all_checked_rows:",
                "sku,path",
            ]
        )

        self.assertEqual(
            inventory_refs_from_output(text),
            {"/proc/catalog/A.json", "/proc/catalog/B.json"},
        )

    def test_inventory_summary_from_output(self):
        text = "\n".join(
            [
                "inventory_count",
                "store_ref=/proc/stores/store_vienna_meidling.json",
                "count=1",
                "final_product_refs:",
                "- /proc/catalog/A.json",
                "all_checked_rows:",
            ]
        )

        self.assertEqual(
            inventory_summary_from_output(text),
            (1, "/proc/stores/store_vienna_meidling.json", ["/proc/catalog/A.json"]),
        )

    def test_count_report_summary_from_output(self):
        text = "\n".join(
            [
                "catalogue_count_report",
                "count=7",
                "final_refs:",
                "- /docs/policy.md",
                "- /proc/stores/store_graz_lend.json",
                "open_store_rows:",
            ]
        )

        self.assertEqual(
            count_report_summary_from_output(text),
            (7, ["/docs/policy.md", "/proc/stores/store_graz_lend.json"]),
        )

    def test_single_store_from_lookup_output(self):
        text = "\n".join(
            [
                "store_lookup",
                "matches=1",
                "Use the `id` value as inventory_count.store_id and the `path` as the final store ref.",
                "id,path,name,city,is_open",
                "store_graz_lend,/proc/stores/store_graz_lend.json,PowerTool Graz Lend,Graz,1",
            ]
        )

        self.assertEqual(
            single_store_from_lookup_output(text),
            ("store_graz_lend", "/proc/stores/store_graz_lend.json"),
        )


class StoreLookupTest(unittest.TestCase):
    def test_store_lookup_uses_config_alias_after_empty_first_query(self):
        class FakeVm:
            def __init__(self):
                self.queries = []

            def exec(self, request):
                self.queries.append(request.stdin)
                if "Meidling" in request.stdin:
                    return SimpleNamespace(
                        stdout=(
                            "id,path,name,city,is_open\n"
                            "store_vienna_meidling,/proc/stores/store_vienna_meidling.json,"
                            "PowerTool Vienna Meidling,Vienna,1\n"
                        )
                    )
                return SimpleNamespace(stdout="id,path,name,city,is_open\n")

        result = store_lookup(FakeVm(), SimpleNamespace(city="Vienna", name_contains="west"))

        self.assertIn("matches=1", result)
        self.assertIn("store_vienna_meidling", result)


class CatalogueCountReportTest(unittest.TestCase):
    def test_city_count_falls_back_when_open_flag_filters_every_store(self):
        class FakeVm:
            def __init__(self):
                self.queries = []

            def exec(self, request):
                self.queries.append(request.stdin)
                sql = request.stdin
                if "from stores" in sql and "is_open" in sql:
                    return SimpleNamespace(stdout="id,path,name,city\n")
                if "from stores" in sql:
                    return SimpleNamespace(
                        stdout=(
                            "id,path,name,city\n"
                            "store_linz_hauptplatz,/proc/stores/store_linz_hauptplatz.json,"
                            "PowerTool Linz Hauptplatz,Linz\n"
                        )
                    )
                if "count(distinct p.product_sku)" in sql:
                    return SimpleNamespace(stdout="count\n4\n")
                if "select distinct" in sql:
                    return SimpleNamespace(
                        stdout=(
                            "sku,path,name\n"
                            "PLB-1,/proc/catalog/PLB-1.json,Valve and Connector\n"
                        )
                    )
                raise AssertionError(f"unexpected SQL: {sql}")

        result = catalogue_count_report(
            FakeVm(),
            SimpleNamespace(
                kind_id="valves_connectors",
                kind_name="",
                city="Linz",
                doc_path="/docs/count-policy.md",
                threshold=1,
                exclude_family_ids=[],
            ),
        )

        self.assertIn("count=4", result)
        self.assertIn("/proc/stores/store_linz_hauptplatz.json", result)


if __name__ == "__main__":
    unittest.main()
