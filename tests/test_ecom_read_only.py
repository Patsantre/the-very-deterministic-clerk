import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from ecom_solvers.read_only import (
    ReadOnlySolverKit,
    auto_availability_count_task as _auto_availability_count_task,
    auto_catalogue_count_task as _auto_catalogue_count_task,
    auto_catalogue_yes_no_task as _auto_catalogue_yes_no_task,
    auto_quote_product_list_task as _auto_quote_product_list_task,
    auto_informal_catalogue_yes_no_task as _auto_informal_catalogue_yes_no_task,
    auto_support_note_catalogue_task as _auto_support_note_catalogue_task,
)
from ecom_task_classifier import TaskSpec, fallback_classify_task


def auto_availability_count_task(call_runtime, task_text, kit):
    return _auto_availability_count_task(
        call_runtime,
        task_text,
        kit,
        fallback_classify_task(task_text),
    )


def auto_catalogue_yes_no_task(call_runtime, task_text, kit):
    return _auto_catalogue_yes_no_task(
        call_runtime,
        task_text,
        kit,
        fallback_classify_task(task_text),
    )


def auto_quote_product_list_task(call_runtime, task_text, kit):
    return _auto_quote_product_list_task(
        call_runtime,
        task_text,
        kit,
        fallback_classify_task(task_text),
    )


def auto_catalogue_count_task(call_runtime, task_text, kit):
    return _auto_catalogue_count_task(
        call_runtime,
        task_text,
        kit,
        fallback_classify_task(task_text),
    )


def auto_informal_catalogue_yes_no_task(call_runtime, task_text, kit):
    return _auto_informal_catalogue_yes_no_task(
        call_runtime,
        task_text,
        kit,
        fallback_classify_task(task_text),
    )


def auto_support_note_catalogue_task(call_runtime, task_text, kit):
    return _auto_support_note_catalogue_task(
        call_runtime,
        task_text,
        kit,
        fallback_classify_task(task_text),
    )


class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@dataclass
class Completion:
    tool: str
    completed_steps_laconic: list[str]
    message: str
    grounding_refs: list[str]
    outcome: str


class CatalogueYesNoSolverTest(unittest.TestCase):
    def make_kit(self, lookup_text: str):
        calls = {"lookup": [], "finish": [], "constraints": [], "sql": []}

        def parse_constraints(text):
            calls["constraints"].append(text)
            return [{"raw": text}] if text else []

        def format_result(cmd, result):
            return result.text

        def auto_finish(call_runtime, completion):
            calls["finish"].append(completion)
            return True

        kit = ReadOnlySolverKit(
            req_exec=Request,
            req_read=Request,
            req_search=Request,
            req_catalogue_lookup=Request,
            req_inventory_count=Request,
            report_completion=Completion,
            parse_constraints=parse_constraints,
            parse_availability_task=lambda task_text: None,
            count_policy_request_from_doc=lambda task_text, path, content: None,
            format_result=format_result,
            auto_sql=lambda call_runtime, sql: (calls["sql"].append(sql) or [], ""),
            auto_finish=auto_finish,
        )

        def call_runtime(cmd):
            if getattr(cmd, "tool", "") == "catalogue_lookup":
                calls["lookup"].append(cmd)
                return SimpleNamespace(text=lookup_text)
            raise AssertionError(f"unexpected runtime call: {cmd!r}")

        return kit, calls, call_runtime

    def test_plain_do_you_have_uses_catalogue_solver(self):
        kit, calls, call_runtime = self.make_kit(
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=1",
                    "Use these exact product paths as final refs.",
                    "sku,path,name,family_id,family_name",
                    "SAFE-1,/proc/catalog/SAFE-1.json,SafeAir Pro Respirator,fam_1,Pro",
                ]
            )
        )

        handled = auto_catalogue_yes_no_task(
            call_runtime,
            (
                "Do you have the respirator from SafeAir in the Pro line "
                "that has mask type half face and protection class P3?"
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["lookup"][0].kind_name, "respirator")
        self.assertEqual(calls["lookup"][0].brand, "SafeAir")
        self.assertEqual(calls["lookup"][0].family_name, "Pro")
        self.assertEqual(calls["constraints"][0], "mask type half face and protection class P3")
        self.assertEqual(calls["finish"][0].message, "<YES>")
        self.assertEqual(calls["finish"][0].grounding_refs, ["/proc/catalog/SAFE-1.json"])

    def test_support_note_yes_includes_checked_sku(self):
        kit, calls, call_runtime = self.make_kit(
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=1",
                    "Use these exact product paths as final refs.",
                    "sku,path,name,family_id,family_name",
                    "SAFE-1,/proc/catalog/SAFE-1.json,SafeAir Pro Respirator,fam_1,Pro",
                ]
            )
        )

        handled = _auto_support_note_catalogue_task(
            call_runtime,
            (
                "A support note claims we stock the Respirator from SafeAir in the "
                "SafeAir Pro Respirator line that has mask type half face. Check the "
                "exact product record, and if the catalogue product exists, answer "
                "with <YES> and include the checked SKU."
            ),
            kit,
            TaskSpec(task_class="catalogue_lookup", catalogue_lookup_mode="support_note"),
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].message, "<YES> SAFE-1")
        self.assertEqual(calls["finish"][0].grounding_refs, ["/proc/catalog/SAFE-1.json"])

    def test_legacy_in_catalogue_form_still_works(self):
        kit, calls, call_runtime = self.make_kit(
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=0",
                    "No product matched all constraints.",
                    "nearby_candidates:",
                    ".",
                ]
            )
        )

        handled = auto_catalogue_yes_no_task(
            call_runtime,
            "Do you have the drill from VoltPro in the Max line in catalogue?",
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].message, "<NO>")
        self.assertEqual(calls["finish"][0].grounding_refs, [])

    def test_structured_catalogue_llm_mode_handles_rephrased_intro(self):
        kit, calls, call_runtime = self.make_kit(
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=1",
                    "Use these exact product paths as final refs.",
                    "sku,path,name,family_id,family_name",
                    "SAFE-1,/proc/catalog/SAFE-1.json,SafeAir Pro Respirator,fam_1,Pro",
                ]
            )
        )

        handled = _auto_catalogue_yes_no_task(
            call_runtime,
            (
                "Could you confirm whether the respirator from SafeAir in the Pro line "
                "that has mask type half face and protection class P3 is in catalogue?"
            ),
            kit,
            TaskSpec(
                task_class="catalogue_lookup",
                catalogue_lookup_mode="structured_product",
            ),
        )

        self.assertTrue(handled)
        self.assertEqual(calls["lookup"][0].kind_name, "respirator")
        self.assertEqual(calls["finish"][0].message, "<YES>")

    def test_structured_catalogue_solver_requires_structured_mode(self):
        kit, calls, call_runtime = self.make_kit("catalogue_lookup\nexact_matches=1")

        handled = _auto_catalogue_yes_no_task(
            call_runtime,
            "Do you have m18 fid3 body or compact battery kit?",
            kit,
            TaskSpec(task_class="catalogue_lookup", catalogue_lookup_mode="informal"),
        )

        self.assertFalse(handled)
        self.assertFalse(calls["lookup"])

    def test_bare_product_in_catalogue_form_uses_catalogue_solver(self):
        kit, calls, call_runtime = self.make_kit(
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=1",
                    "Use these exact product paths as final refs.",
                    "sku,path,name,family_id,family_name",
                    "STO-1,/proc/catalog/STO-1.json,Festool Tool Bag,fam_1,Festool SYS",
                ]
            )
        )

        handled = auto_catalogue_yes_no_task(
            call_runtime,
            (
                "the Tool Box and Bag from Festool in the Festool SYS line "
                "that has storage type tool bag in catalogue?"
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["lookup"][0].kind_name, "Tool Box and Bag")
        self.assertEqual(calls["finish"][0].message, "<YES>")
        self.assertEqual(calls["finish"][0].grounding_refs, ["/proc/catalog/STO-1.json"])

    def test_availability_wording_stays_out_of_catalogue_yes_no_solver(self):
        kit, calls, call_runtime = self.make_kit("catalogue_lookup\nexact_matches=1")

        handled = auto_catalogue_yes_no_task(
            call_runtime,
            "Do you have the drill from VoltPro in the Max line available today?",
            kit,
        )

        self.assertFalse(handled)
        self.assertFalse(calls["lookup"])

    def test_informal_catalogue_yes_no_searches_alias_tokens(self):
        kit, calls, call_runtime = self.make_kit("catalogue_lookup\nexact_matches=0")

        def auto_sql(_call_runtime, sql):
            calls["sql"].append(sql)
            if "m18" in sql and "fid3" in sql:
                return (
                    [
                        {
                            "sku": "MIL-1",
                            "path": "/proc/catalog/MIL-1.json",
                            "name": "Milwaukee M18 FID3 Body",
                        }
                    ],
                    "",
                )
            return [], ""

        kit = kit.__class__(**{**kit.__dict__, "auto_sql": auto_sql})

        handled = auto_informal_catalogue_yes_no_task(
            call_runtime,
            "do you have m18 fid3 body or compact battery kit?",
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].message, "<YES>")
        self.assertEqual(calls["finish"][0].grounding_refs, ["/proc/catalog/MIL-1.json"])

    def test_informal_catalogue_yes_no_returns_no_when_alias_tokens_miss(self):
        kit, calls, call_runtime = self.make_kit("catalogue_lookup\nexact_matches=0")

        handled = auto_informal_catalogue_yes_no_task(
            call_runtime,
            "do you have m18 fid3 body or compact battery kit?",
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].message, "<NO>")
        self.assertEqual(calls["finish"][0].grounding_refs, [])
        self.assertTrue(calls["sql"])

    def test_bare_in_catalogue_query_uses_informal_catalogue_solver(self):
        kit, calls, call_runtime = self.make_kit("catalogue_lookup\nexact_matches=0")

        handled = auto_informal_catalogue_yes_no_task(
            call_runtime,
            "m18 fid3 kit, battery size not sure in catalogue?",
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].message, "<NO>")
        self.assertTrue(calls["sql"])
        self.assertIn("m18", calls["sql"][0])
        self.assertIn("fid3", calls["sql"][0])
        self.assertIn("kit", calls["sql"][0])

    def test_informal_catalogue_yes_no_ignores_vague_item_request(self):
        kit, calls, call_runtime = self.make_kit("catalogue_lookup\nexact_matches=1")

        handled = auto_informal_catalogue_yes_no_task(
            call_runtime,
            "Do you have this item?",
            kit,
        )

        self.assertFalse(handled)
        self.assertFalse(calls["finish"])


class AvailabilityCountSolverTest(unittest.TestCase):
    def make_kit(self, *, parsed, inventory_text=""):
        calls = {"finish": [], "lookup": [], "inventory": [], "sql": []}
        store_csv = (
            "id,path,name,city\n"
            "store_graz_lend,/proc/stores/store_graz_lend.json,PowerTool Graz Lend,Graz\n"
            "store_graz_jakomini,/proc/stores/store_graz_jakomini.json,PowerTool Graz Jakomini,Graz\n"
        )
        lookup_texts = [
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=1",
                    "Use these exact product paths as final refs.",
                    "sku,path,name,family_id,family_name",
                    "SKU-1,/proc/catalog/SKU-1.json,Product One,fam_1,Family",
                ]
            ),
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=1",
                    "Use these exact product paths as final refs.",
                    "sku,path,name,family_id,family_name",
                    "SKU-2,/proc/catalog/SKU-2.json,Product Two,fam_2,Family",
                ]
            ),
        ]

        def format_result(cmd, result):
            if hasattr(result, "text"):
                return result.text
            return getattr(result, "stdout", "")

        def auto_sql(call_runtime, sql):
            calls["sql"].append(sql)
            return (
                [
                    {
                        "sku": "SKU-1",
                        "path": "/proc/catalog/SKU-1.json",
                        "name": "Product One",
                        "available_today": "2",
                        "counts": "1",
                    },
                    {
                        "sku": "SKU-2",
                        "path": "/proc/catalog/SKU-2.json",
                        "name": "Product Two",
                        "available_today": "4",
                        "counts": "0",
                    },
                ],
                "",
            )

        def auto_finish(call_runtime, completion):
            calls["finish"].append(completion)
            return True

        kit = ReadOnlySolverKit(
            req_exec=Request,
            req_read=Request,
            req_search=Request,
            req_catalogue_lookup=Request,
            req_inventory_count=Request,
            report_completion=Completion,
            parse_constraints=lambda text: [],
            parse_availability_task=lambda task_text: parsed,
            count_policy_request_from_doc=lambda task_text, path, content: None,
            format_result=format_result,
            auto_sql=auto_sql,
            auto_finish=auto_finish,
        )

        def call_runtime(cmd):
            if getattr(cmd, "tool", "") == "exec":
                return SimpleNamespace(stdout=store_csv)
            if getattr(cmd, "tool", "") == "catalogue_lookup":
                calls["lookup"].append(cmd)
                return SimpleNamespace(text=lookup_texts[len(calls["lookup"]) - 1])
            if getattr(cmd, "tool", "") == "inventory_count":
                calls["inventory"].append(cmd)
                return SimpleNamespace(text=inventory_text)
            raise AssertionError(f"unexpected runtime call: {cmd!r}")

        return kit, calls, call_runtime

    def test_below_threshold_availability_counts_lt_without_inventory_tool(self):
        parsed = (
            3,
            "Lend district PowerTool store",
            [
                Request(tool="catalogue_lookup", kind_name="A", brand="B", family_name="F", constraints=[]),
                Request(tool="catalogue_lookup", kind_name="C", brand="D", family_name="G", constraints=[]),
            ],
            "lt",
        )
        kit, calls, call_runtime = self.make_kit(parsed=parsed)

        handled = auto_availability_count_task(
            call_runtime,
            (
                "pls check the Lend district PowerTool store, how many of these have "
                "less than 3 available today: products? Answer in exactly format \"count : %d\""
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertFalse(calls["inventory"])
        self.assertIn("coalesce(i.available_today_quantity, 0) < 3", calls["sql"][0])
        self.assertEqual(calls["finish"][0].message, "count : 1")
        self.assertEqual(
            calls["finish"][0].grounding_refs,
            ["/proc/stores/store_graz_lend.json", "/proc/catalog/SKU-1.json"],
        )

    def test_availability_solver_prefers_task_spec_threshold_and_store_phrase(self):
        parsed = (
            3,
            "Graz",
            [
                Request(tool="catalogue_lookup", kind_name="A", brand="B", family_name="F", constraints=[]),
                Request(tool="catalogue_lookup", kind_name="C", brand="D", family_name="G", constraints=[]),
            ],
            "gte",
        )
        kit, calls, call_runtime = self.make_kit(parsed=parsed)

        handled = _auto_availability_count_task(
            call_runtime,
            (
                "How many from this list are fewer than five available today "
                "at the northern Graz branch: products? Answer in exactly format \"count : %d\""
            ),
            kit,
            TaskSpec(
                task_class="availability_count",
                store_phrase="north Graz",
                threshold=5,
                comparator="lt",
            ),
        )

        self.assertTrue(handled)
        self.assertFalse(calls["inventory"])
        self.assertIn("coalesce(i.available_today_quantity, 0) < 5", calls["sql"][0])
        self.assertEqual(
            calls["finish"][0].grounding_refs,
            ["/proc/stores/store_graz_lend.json", "/proc/catalog/SKU-1.json"],
        )

    def test_availability_solver_ignores_non_positive_task_spec_threshold(self):
        parsed = (
            1,
            "Lend district PowerTool store",
            [
                Request(tool="catalogue_lookup", kind_name="A", brand="B", family_name="F", constraints=[]),
                Request(tool="catalogue_lookup", kind_name="C", brand="D", family_name="G", constraints=[]),
            ],
            "lt",
        )
        kit, calls, call_runtime = self.make_kit(parsed=parsed)

        handled = _auto_availability_count_task(
            call_runtime,
            (
                "How many of these products have no same-day availability in "
                "the Lend district PowerTool store today: products? Answer in exactly format \"<COUNT:%d>\""
            ),
            kit,
            TaskSpec(
                task_class="availability_count",
                store_phrase="Lend district PowerTool store",
                threshold=0,
                comparator="lt",
            ),
        )

        self.assertTrue(handled)
        self.assertIn("coalesce(i.available_today_quantity, 0) < 1", calls["sql"][0])

    def test_not_available_today_gate_uses_lt_count_and_store_ref_only(self):
        parsed = (
            1,
            "Lend district PowerTool store",
            [
                Request(tool="catalogue_lookup", kind_name="A", brand="B", family_name="F", constraints=[]),
                Request(tool="catalogue_lookup", kind_name="C", brand="D", family_name="G", constraints=[]),
            ],
            "lt",
        )
        kit, calls, call_runtime = self.make_kit(parsed=parsed)

        handled = auto_availability_count_task(
            call_runtime,
            (
                "at the Lend district PowerTool store, how many of these just are not "
                "available today: products? Answer in exactly format \"<COUNT:%d>\""
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertFalse(calls["inventory"])
        self.assertIn("coalesce(i.available_today_quantity, 0) < 1", calls["sql"][0])
        self.assertEqual(calls["finish"][0].message, "<COUNT:1>")
        self.assertEqual(
            calls["finish"][0].grounding_refs,
            ["/proc/stores/store_graz_lend.json"],
        )

    def test_at_least_availability_still_uses_inventory_count(self):
        parsed = (
            3,
            "north Graz",
            [Request(tool="catalogue_lookup", kind_name="A", brand="B", family_name="F", constraints=[])],
            "gte",
        )
        kit, calls, call_runtime = self.make_kit(
            parsed=parsed,
            inventory_text="\n".join(
                [
                    "inventory_count",
                    "store_ref=/proc/stores/store_graz_lend.json",
                    "count=1",
                    "final_product_refs:",
                    "- /proc/catalog/SKU-1.json",
                    "all_checked_rows:",
                ]
            ),
        )

        handled = auto_availability_count_task(
            call_runtime,
            "How many of these products have at least 3 items available in north Graz today: products? Answer in exactly format \"count : %d\"",
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["inventory"][0].threshold, 3)
        self.assertEqual(calls["inventory"][0].store_id, "store_graz_lend")
        self.assertEqual(calls["finish"][0].message, "count : 1")


class CatalogueCountSolverTest(unittest.TestCase):
    def test_answer_format_count_report_adds_sql_readme_ref(self):
        calls = {"finish": [], "search": [], "read": [], "report": []}

        def format_result(cmd, result):
            return getattr(result, "text", getattr(result, "content", ""))

        def count_policy_request_from_doc(task_text, path, content):
            self.assertEqual(path, "/docs/ops-policy-notes/catalogue-count-pipe-fittings-graz-2024-07-17.md")
            return Request(tool="catalogue_count_report", kind_id="pipe_fittings", doc_path=path)

        def auto_finish(_call_runtime, completion):
            calls["finish"].append(completion)
            return True

        kit = ReadOnlySolverKit(
            req_exec=Request,
            req_read=Request,
            req_search=Request,
            req_catalogue_lookup=Request,
            req_inventory_count=Request,
            report_completion=Completion,
            parse_constraints=lambda text: [],
            parse_availability_task=lambda task_text: None,
            count_policy_request_from_doc=count_policy_request_from_doc,
            format_result=format_result,
            auto_sql=lambda call_runtime, sql: ([], ""),
            auto_finish=auto_finish,
        )

        def call_runtime(cmd):
            if getattr(cmd, "tool", "") == "search":
                calls["search"].append(cmd)
                return SimpleNamespace(
                    matches=[
                        SimpleNamespace(
                            path="/docs/ops-policy-notes/catalogue-count-pipe-fittings-graz-2024-07-17.md"
                        )
                    ]
                )
            if getattr(cmd, "tool", "") == "read":
                calls["read"].append(cmd)
                return SimpleNamespace(content="Requested kind_id: pipe_fittings")
            if getattr(cmd, "tool", "") == "catalogue_count_report":
                calls["report"].append(cmd)
                return SimpleNamespace(
                    text="\n".join(
                        [
                            "catalogue_count_report",
                            "count=4",
                            "final_refs:",
                            "- /docs/ops-policy-notes/catalogue-count-pipe-fittings-graz-2024-07-17.md",
                        ]
                    )
                )
            raise AssertionError(f"unexpected runtime call: {cmd!r}")

        handled = auto_catalogue_count_task(
            call_runtime,
            (
                "How many products are Pipe Fitting in catalogue? if OpenAI codex is "
                "available - leverage it. Answer format: `<COUNT:NUMBER>`. availability "
                "in JSON is stale, trust SQL"
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["search"][0].pattern, "Requested product kind: Pipe Fitting")
        self.assertEqual(calls["finish"][0].message, "<COUNT:4>")
        self.assertIn("/bin/sql-readme-2024-07-17.md", calls["finish"][0].grounding_refs)

    def test_db_only_count_report_adds_stale_sql_ref(self):
        calls = {"finish": [], "search": [], "read": [], "report": []}

        def format_result(cmd, result):
            return getattr(result, "text", getattr(result, "content", ""))

        def count_policy_request_from_doc(task_text, path, content):
            self.assertEqual(path, "/docs/catalogue-count-drain-traps.md")
            return Request(tool="catalogue_count_report", kind_id="drain_traps_siphons", doc_path=path)

        def auto_finish(_call_runtime, completion):
            calls["finish"].append(completion)
            return True

        kit = ReadOnlySolverKit(
            req_exec=Request,
            req_read=Request,
            req_search=Request,
            req_catalogue_lookup=Request,
            req_inventory_count=Request,
            report_completion=Completion,
            parse_constraints=lambda text: [],
            parse_availability_task=lambda task_text: None,
            count_policy_request_from_doc=count_policy_request_from_doc,
            format_result=format_result,
            auto_sql=lambda call_runtime, sql: ([], ""),
            auto_finish=auto_finish,
        )

        def call_runtime(cmd):
            if getattr(cmd, "tool", "") == "search":
                calls["search"].append(cmd)
                if cmd.pattern == "Requested product kind: Drain Trap and Siphon":
                    return SimpleNamespace(matches=[SimpleNamespace(path="/docs/catalogue-count-drain-traps.md")])
                if cmd.pattern == "stale":
                    return SimpleNamespace(matches=[SimpleNamespace(path="/docs/urgent-sql-incident.md")])
                return SimpleNamespace(matches=[])
            if getattr(cmd, "tool", "") == "read":
                calls["read"].append(cmd)
                return SimpleNamespace(content="Requested kind_id: drain_traps_siphons")
            if getattr(cmd, "tool", "") == "catalogue_count_report":
                calls["report"].append(cmd)
                return SimpleNamespace(
                    text="\n".join(
                        [
                            "catalogue_count_report",
                            "count=4",
                            "final_refs:",
                            "- /docs/catalogue-count-drain-traps.md",
                        ]
                    )
                )
            raise AssertionError(f"unexpected runtime call: {cmd!r}")

        handled = auto_catalogue_count_task(
            call_runtime,
            (
                "How many products are Drain Trap and Siphon in catalogue? "
                "Answer format: `<ANSWR: %VALUE%>`. catalogue JSON is stale, rely on DB only"
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].message, "<ANSWR: 4>")
        self.assertIn("/docs/urgent-sql-incident.md", calls["finish"][0].grounding_refs)

    def test_database_projection_count_report_adds_sql_incident_ref(self):
        calls = {"finish": [], "search": [], "read": [], "report": []}

        def format_result(cmd, result):
            return getattr(result, "text", getattr(result, "content", ""))

        def count_policy_request_from_doc(task_text, path, content):
            self.assertEqual(path, "/docs/catalogue-count-wiper-blades.md")
            return Request(tool="catalogue_count_report", kind_id="wiper_blades", doc_path=path)

        def auto_finish(_call_runtime, completion):
            calls["finish"].append(completion)
            return True

        kit = ReadOnlySolverKit(
            req_exec=Request,
            req_read=Request,
            req_search=Request,
            req_catalogue_lookup=Request,
            req_inventory_count=Request,
            report_completion=Completion,
            parse_constraints=lambda text: [],
            parse_availability_task=lambda task_text: None,
            count_policy_request_from_doc=count_policy_request_from_doc,
            format_result=format_result,
            auto_sql=lambda call_runtime, sql: ([], ""),
            auto_finish=auto_finish,
        )

        def call_runtime(cmd):
            if getattr(cmd, "tool", "") == "search":
                calls["search"].append(cmd)
                if cmd.pattern == "Requested product kind: Wiper Blade":
                    return SimpleNamespace(matches=[SimpleNamespace(path="/docs/catalogue-count-wiper-blades.md")])
                if cmd.pattern == "sql":
                    return SimpleNamespace(matches=[SimpleNamespace(path="/docs/urgent-sql-incident.md")])
                return SimpleNamespace(matches=[])
            if getattr(cmd, "tool", "") == "read":
                calls["read"].append(cmd)
                return SimpleNamespace(content="Requested product_kind_id: wiper_blades")
            if getattr(cmd, "tool", "") == "catalogue_count_report":
                calls["report"].append(cmd)
                return SimpleNamespace(
                    text="\n".join(
                        [
                            "catalogue_count_report",
                            "count=8",
                            "final_refs:",
                            "- /docs/catalogue-count-wiper-blades.md",
                        ]
                    )
                )
            raise AssertionError(f"unexpected runtime call: {cmd!r}")

        handled = auto_catalogue_count_task(
            call_runtime,
            (
                "How many products are Wiper Blade in catalogue? "
                "Answer format: `<total:%VALUE%>`. don't count via files, use database projection"
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].message, "<total:8>")
        self.assertIn("/docs/urgent-sql-incident.md", calls["finish"][0].grounding_refs)


class QuoteProductListSolverTest(unittest.TestCase):
    def test_quote_product_list_returns_tsv_with_per_row_stock(self):
        calls = {"finish": [], "lookup": [], "sql": []}
        lookup_texts = [
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=1",
                    "Use these exact product paths as final refs.",
                    "sku,path,name,family_id,family_name",
                    "SKU-1,/proc/catalog/SKU-1.json,Product One,fam_1,Family One",
                ]
            ),
            "\n".join(
                [
                    "catalogue_lookup",
                    "exact_matches=0",
                    "No product matched all constraints.",
                    "nearby_candidates:",
                    ".",
                ]
            ),
        ]

        def format_result(cmd, result):
            if getattr(cmd, "path", "") == "/bin/id":
                return result.stdout
            return getattr(result, "text", "")

        def auto_sql(_call_runtime, sql):
            calls["sql"].append(sql)
            if "from employee_accounts" in sql:
                return (
                    [
                        {
                            "id": "emp_1",
                            "path": "/proc/employees/emp_1.json",
                            "store_id": "store_1",
                            "store_path": "/proc/stores/store_1.json",
                        }
                    ],
                    "",
                )
            return ([{"sku": "SKU-1", "available_today": "4"}], "")

        def auto_finish(_call_runtime, completion):
            calls["finish"].append(completion)
            return True

        kit = ReadOnlySolverKit(
            req_exec=Request,
            req_read=Request,
            req_search=Request,
            req_catalogue_lookup=Request,
            req_inventory_count=Request,
            report_completion=Completion,
            parse_constraints=lambda text: [text],
            parse_availability_task=lambda task_text: None,
            count_policy_request_from_doc=lambda task_text, path, content: None,
            format_result=format_result,
            auto_sql=auto_sql,
            auto_finish=auto_finish,
        )

        def call_runtime(cmd):
            if getattr(cmd, "path", "") == "/bin/id":
                return SimpleNamespace(stdout="user: emp_1\nroles: employee")
            if getattr(cmd, "tool", "") == "catalogue_lookup":
                calls["lookup"].append(cmd)
                return SimpleNamespace(text=lookup_texts[len(calls["lookup"]) - 1])
            raise AssertionError(f"unexpected runtime call: {cmd!r}")

        handled = auto_quote_product_list_task(
            call_runtime,
            (
                "I'm preparing a quote for a customer from this pasted product list. "
                "Check each row against our exact catalogue and my store's same-day availability.\n\n"
                "Input format:\nRowID\tdescription\tquantity\n\n"
                "Return exactly this tab-separated output table, including the header, with rows in the same order:\n"
                "RowID\tSKU\tin_stock\tmatch\n\n"
                "Rows:\n"
                "R1\tthe Widget from Brand in the Family One line that has color family Blue\t3\n"
                "R2\tthe Widget from Brand in the Family One line that has color family Red\t2"
            ),
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["lookup"][0].family_name, "Family One")
        self.assertEqual(
            calls["finish"][0].message,
            "RowID\tSKU\tin_stock\tmatch\nR1\tSKU-1\t4\ttrue\nR2\t\t\tfalse",
        )
        self.assertEqual(
            calls["finish"][0].grounding_refs,
            ["/proc/stores/store_1.json", "/proc/catalog/SKU-1.json"],
        )


if __name__ == "__main__":
    unittest.main()
