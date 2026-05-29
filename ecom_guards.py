from __future__ import annotations

import re
from dataclasses import dataclass, field


SQL_STARTS = (
    "select",
    "with",
    "pragma",
    "insert",
    "update",
    "delete",
    "create",
    "explain",
)

ALLOWED_EXEC_PATHS = {
    "/bin/checkout",
    "/bin/date",
    "/bin/discount",
    "/bin/id",
    "/bin/payments",
    "/bin/sql",
}


@dataclass
class GuardState:
    task_inspections: int = 0
    inventory_final_refs: set[str] | None = None
    read_path_counts: dict[str, int] = field(default_factory=dict)
    catalogue_lookup_counts: dict[str, int] = field(default_factory=dict)
    resolved_store: tuple[str, str] | None = None


def is_multi_product_availability(task_text: str) -> bool:
    lowered = task_text.lower()
    return "how many of these products" in lowered and "available" in lowered


def is_catalogue_count(task_text: str) -> bool:
    lowered = task_text.lower()
    if "pasted product list" in lowered and "rowid" in lowered:
        return False
    if "support note claims" in lowered or "actual catalogue item" in lowered:
        return False
    return bool(
        re.search(r"\bhow many catalogue products\b", lowered)
        or re.search(r"\bcatalogue count(?:ing)?(?: report)?\b", lowered)
        or re.search(r"\bcatalogue .* reporting\b", lowered)
    )


def _is_sql(text: str) -> bool:
    return text.lstrip().lower().startswith(SQL_STARTS)


def _cmd_kind(cmd) -> str:
    return cmd.__class__.__name__


def _cmd_json(cmd) -> str:
    if hasattr(cmd, "model_dump_json"):
        return cmd.model_dump_json()
    return repr(cmd)


def _sku_from_catalog_ref(ref: str) -> str | None:
    match = re.search(r"/proc/catalog/(?:[^/\s]+/)?([A-Z0-9]+-[A-Z0-9]+)\.json$", ref)
    return match.group(1) if match else None


def guard_before_execution(
    cmd,
    task_text: str,
    state: GuardState,
    *,
    task_completed: bool | None = None,
) -> str | None:
    kind = _cmd_kind(cmd)

    if kind == "ReportTaskCompletion" and state.task_inspections == 0:
        return (
            "report_completion blocked: inspect current task data with read, search, "
            "or exec before finalizing. Use exact tool-returned paths/SKUs only."
        )

    if kind == "ReportTaskCompletion" and task_completed is False:
        return (
            "report_completion blocked: task_completed is false. Continue inspecting "
            "and use report_completion only for the final answer."
        )

    if kind == "Req_Read":
        path = getattr(cmd, "path", "")
        previous_reads = state.read_path_counts.get(path, 0)
        if previous_reads >= 2:
            return (
                "read blocked: this file has already been read twice. Use the policy "
                "already in context to run the next SQL/count/action step, "
                "`catalogue_count_report`, or finalize."
            )

    if kind == "Req_CatalogueLookup":
        lookup_key = _cmd_json(cmd)
        previous_lookups = state.catalogue_lookup_counts.get(lookup_key, 0)
        if previous_lookups >= 2:
            return (
                "catalogue_lookup blocked: this exact product lookup already returned "
                "a result twice. Use different catalogue constraints for the remaining "
                "products, or use `store_lookup` to resolve the named shop/store."
            )

    if (
        kind == "Req_StoreLookup"
        and state.resolved_store is not None
        and is_multi_product_availability(task_text)
    ):
        store_id, store_path = state.resolved_store
        return (
            "store_lookup blocked: the store is already resolved. "
            f"Use store_id `{store_id}` and store_ref `{store_path}`; "
            "do not call store_lookup again. The next useful step must be "
            "`catalogue_lookup` for unresolved products or `inventory_count` "
            "with exact resolved SKUs."
        )

    return guard_request(cmd, task_text, state.inventory_final_refs)


def guard_request(
    cmd,
    task_text: str,
    inventory_final_refs: set[str] | None = None,
) -> str | None:
    kind = _cmd_kind(cmd)
    lowered_task = task_text.lower()
    multi_product_availability = is_multi_product_availability(task_text)

    if kind == "Req_Read":
        path = getattr(cmd, "path", "")
        if path.rstrip("/") in {"/proc/catalog", "/proc/stores"}:
            return (
                "read blocked: that path is a directory. Use `tree`/`list` for filesystem "
                "navigation or `/bin/sql` for catalogue, store, inventory, product, and family data."
            )
        if multi_product_availability and path.startswith("/docs"):
            return (
                "read blocked: this is a concrete multi-product availability/count task, "
                "not a catalogue reporting-policy task. Do not read `/docs`; resolve exact "
                "product SKUs with `catalogue_lookup`, resolve the store with `store_lookup`, "
                "then call `inventory_count`."
            )

    if kind == "Req_InventoryCount":
        store_id = getattr(cmd, "store_id", "")
        skus = getattr(cmd, "skus", [])
        if not store_id.startswith("store_"):
            return (
                "inventory_count blocked: resolve the exact `stores.id` first. "
                "It must look like `store_city_area`, not a placeholder or prose."
            )
        if not skus:
            return "inventory_count blocked: pass the exact resolved SKUs to check."
        bad_skus = [
            sku
            for sku in skus
            if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+", sku)
        ]
        if bad_skus:
            return (
                "inventory_count blocked: skus must be exact catalogue `sku` values "
                "returned by catalogue_lookup or SQL, such as `PWR-123ABC`. "
                f"Resolve these product descriptions/slugs first: {bad_skus[:3]}"
            )
        return None

    if kind == "Req_CatalogueCountReport":
        doc_path = getattr(cmd, "doc_path", "")
        kind_id = getattr(cmd, "kind_id", "")
        kind_name = getattr(cmd, "kind_name", "")
        if not doc_path.startswith("/docs/"):
            return (
                "catalogue_count_report blocked: read the matching `/docs` count/report "
                "policy first and pass its exact path as doc_path."
            )
        if not kind_id and not kind_name:
            return (
                "catalogue_count_report blocked: pass the requested product kind_id from "
                "the policy doc, or the exact human kind_name from the task."
            )
        return None

    if kind == "Req_Exec":
        path = getattr(cmd, "path", "")
        stdin = getattr(cmd, "stdin", "")
        args = getattr(cmd, "args", [])
        if not path.startswith("/"):
            return (
                "exec blocked: path must be an absolute runtime executable such as "
                "`/bin/sql`, not a shell command name. Retry with `/bin/sql` for SQL."
            )
        if path.startswith("/bin/") and path not in ALLOWED_EXEC_PATHS:
            return (
                "exec blocked: unsupported runtime executable. Available tools are "
                "`/bin/sql`, `/bin/date`, `/bin/id`, `/bin/checkout`, `/bin/discount`, "
                "and `/bin/payments`; use `/bin/sql` for stores/catalogue/inventory."
            )
        if path == "/bin/sql":
            if stdin.strip() and not _is_sql(stdin):
                return (
                    "exec blocked: stdin for `/bin/sql` must contain only SQL, not prose. "
                    "Put the SQL query in stdin and leave args empty."
                )
            if not stdin.strip() and not _is_sql(" ".join(args)):
                return (
                    "exec blocked: `/bin/sql` requires a SQL query. "
                    "Use SELECT/WITH/PRAGMA/etc. in stdin, not a plan or note."
                )
            sql_text = (stdin or " ".join(args)).lower()
            if multi_product_availability and "inventory" in sql_text:
                return (
                    "exec blocked: for multi-product availability/count tasks, use the "
                    "`inventory_count` tool instead of raw inventory SQL. Pass the resolved "
                    "store_id, threshold, and exact SKUs; it returns count and final_product_refs."
                )
        return None

    if kind != "ReportTaskCompletion":
        return None

    msg = getattr(cmd, "message", "")
    refs = getattr(cmd, "grounding_refs", [])

    if "answer in exactly format" in lowered_task:
        looks_explanatory = (
            "\n" in msg
            or len(msg.strip()) > 40
            or "answer:" in msg.lower()
            or "there are" in msg.lower()
            or "available" in msg.lower()
            or "still" in msg.lower()
            or "resolving" in msg.lower()
            or "working" in msg.lower()
        )
        if msg != msg.strip() or looks_explanatory:
            return (
                "report_completion blocked: the task requires an exact answer format. "
                "Final message must be only the formatted answer string, with no explanation."
            )

    is_yes_no_task = (
        lowered_task.startswith("do you have")
        or "<yes>" in lowered_task
        or "<no>" in lowered_task
        or "answer with <no>" in lowered_task
    )
    if is_yes_no_task and "<YES>" not in msg and "<NO>" not in msg:
        return (
            "report_completion blocked: this is a yes/no catalogue check. "
            "Final message must include the exact `<YES>` or `<NO>` token."
        )

    support_note_requires_checked_sku = (
        "support note claims" in lowered_task
        and "include the checked sku" in lowered_task
    )
    if support_note_requires_checked_sku and "<NO>" in msg:
        checked_skus = [
            sku
            for ref in refs
            if (sku := _sku_from_catalog_ref(ref)) is not None
        ]
        if checked_skus and not any(sku in msg for sku in checked_skus):
            return (
                "report_completion blocked: this support-note task requires the "
                "final `<NO>` message to include the checked SKU text. Retry with "
                f"`<NO> {checked_skus[0]}` and cite the same exact product record."
            )

    if multi_product_availability and inventory_final_refs is None:
        return (
            "report_completion blocked: multi-product availability/count tasks require "
            "calling `inventory_count` with the resolved store_id, threshold, and SKUs before final."
        )

    if multi_product_availability:
        bad_inventory_refs = [
            ref
            for ref in refs
            if ref.startswith("/proc/catalog") and ref not in inventory_final_refs
        ]
        if bad_inventory_refs:
            return (
                "report_completion blocked: after `inventory_count`, product refs must be "
                "only the `final_product_refs` returned by that tool. Do not cite unavailable "
                f"or non-counting product refs. Bad refs: {bad_inventory_refs[:3]}"
            )

    bad_refs = [
        ref
        for ref in refs
        if not ref.startswith("/")
        or "\n" in ref
        or "/proc/catalog/products/" in ref
        or " by " in ref
        or "kind_id:" in ref
    ]
    if bad_refs:
        return (
            "report_completion blocked: grounding refs must be exact real filesystem paths "
            "returned by tools or SQL `path` columns, not synthetic descriptions. "
            f"Bad refs: {bad_refs[:3]}"
        )

    if is_catalogue_count(task_text) and not any(ref.startswith("/docs/") for ref in refs):
        return (
            "report_completion blocked: catalogue count/reporting tasks require checking "
            "matching `/docs` current-updates, policy-updates, addenda, or reporting docs "
            "and citing the applicable document."
        )

    return None
