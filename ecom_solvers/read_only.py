from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ecom_domain_tools import (
    catalogue_paths_from_output,
    count_report_summary_from_output,
    csv_rows,
    first_catalogue_row,
    inventory_summary_from_output,
    sku_from_catalogue_output,
    sql_in,
    sql_literal,
)
from ecom_parsers import (
    exact_count_message,
    exact_quantity_message,
    requested_count_kind,
    select_store,
)
from ecom_task_classifier import TaskSpec


CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"


@dataclass(frozen=True)
class ReadOnlySolverKit:
    req_exec: type
    req_read: type
    req_search: type
    req_catalogue_lookup: type
    req_inventory_count: type
    report_completion: type
    parse_constraints: Callable[[str], list]
    parse_availability_task: Callable[[str], object]
    count_policy_request_from_doc: Callable[[str, str, str], object]
    format_result: Callable[[object, object], str]
    auto_sql: Callable[[Callable[[object], object], str], tuple[list[dict[str, str]], str]]
    auto_finish: Callable[[Callable[[object], object], object], bool]
    req_tree: type | None = None
    policy_refs: Callable[..., list[str]] | None = None


@dataclass(frozen=True)
class QuoteRow:
    row_id: str
    quantity: int
    lookup: object


@dataclass(frozen=True)
class ReceiptItem:
    sku: str
    quantity: int


def _call_and_print(kit: ReadOnlySolverKit, call_runtime, cmd):
    result = call_runtime(cmd)
    text = kit.format_result(cmd, result)
    print(f"{CLI_GREEN}AUTO{CLI_CLR}: {text}")
    return result, text


def _wants_exact_count_answer(task_text: str) -> bool:
    lowered = task_text.lower()
    return (
        "answer in exactly format" in lowered
        or "answer format:" in lowered
        or "answer pattern:" in lowered
    )


def _needs_sql_policy_ref(task_text: str) -> bool:
    lowered = task_text.lower()
    return (
        "codex" in lowered
        or "claude" in lowered
        or "trust sql" in lowered
        or "sql" in lowered
        or "stale" in lowered
        or "db only" in lowered
        or "rely on db" in lowered
        or "database projection" in lowered
        or "use database" in lowered
        or "count via files" in lowered
    )


def _count_report_refs(task_text: str, refs: list[str], sql_policy_refs: list[str] | None = None) -> list[str]:
    final = list(refs)
    if _needs_sql_policy_ref(task_text):
        extras = sql_policy_refs or ["/bin/sql-readme-2024-07-17.md"]
        for ref in extras:
            if ref and ref not in final:
                final.append(ref)
    return final


def _sql_policy_refs(call_runtime, task_text: str, kit: ReadOnlySolverKit) -> list[str]:
    if not _needs_sql_policy_ref(task_text):
        return []
    lowered = task_text.lower()
    patterns = ["sql"]
    if "stale" in lowered or "db only" in lowered or "rely on db" in lowered:
        patterns = ["stale", "sql"]
    refs: list[str] = []
    for pattern in patterns:
        search_cmd = kit.req_search(tool="search", root="/docs", pattern=pattern, limit=20)
        search_result, _ = _call_and_print(kit, call_runtime, search_cmd)
        for match in getattr(search_result, "matches", []):
            path = getattr(match, "path", "")
            if path.startswith("/docs/") and "sql" in path.lower() and path not in refs:
                refs.append(path)
    if refs:
        return refs
    if kit.policy_refs is not None:
        policy_refs = kit.policy_refs("sql.incident")
        if policy_refs:
            return policy_refs
    return ["/bin/sql-readme-2024-07-17.md"]


def _receipt_upload_path(task_text: str, tree_text: str) -> str:
    direct = re.search(r"(/uploads/receipt_ocr_[A-Za-z0-9]+\.txt)\b", task_text)
    if direct:
        return direct.group(1)
    match = re.search(r"\breceipt_ocr_[A-Za-z0-9]+\.txt\b", tree_text)
    return f"/uploads/{match.group(0)}" if match else ""


def _strip_line_number(line: str) -> str:
    return re.sub(r"^\s*\d+\t", "", line)


def _money_to_cents(raw: str) -> int:
    cleaned = raw.strip().replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    euros, cents = (cleaned.split(".", 1) + ["00"])[:2]
    return int(euros) * 100 + int((cents + "00")[:2])


def _receipt_threshold_cents(task_text: str) -> int:
    match = re.search(r"\bwithin\s+(\d+)\s*(?:eur|€)\b", task_text, re.I)
    return int(match.group(1)) * 100 if match else 300


def _receipt_total_cents(content: str) -> int | None:
    patterns = (
        r"\bSUB\s*T[O0]TAL\b[^\d]*(\d+(?:[.,]\d{2}))",
        r"\bSubtotal\b\s*EUR\s*(\d+(?:[.,]\d{2}))",
        r"\bTotal\s*\(exkl\.\s*MwSt\)\s*EUR\s*(\d+(?:[.,]\d{2}))",
        r"\bTotal\s*\(excl\.?\s*VAT\)\s*EUR\s*(\d+(?:[.,]\d{2}))",
    )
    for pattern in patterns:
        match = re.search(pattern, content, re.I)
        if match:
            return _money_to_cents(match.group(1))
    return None


def _receipt_items(content: str) -> list[ReceiptItem]:
    items: list[ReceiptItem] = []
    pending_qty: int | None = None
    for raw_line in content.splitlines():
        line = _strip_line_number(raw_line).strip()
        if not line:
            continue
        row_match = re.match(r"^(\d+)\s+([A-Z0-9]{3}-[A-Z0-9]{6,16})\b", line)
        if row_match:
            items.append(ReceiptItem(sku=row_match.group(2).upper(), quantity=int(row_match.group(1))))
            pending_qty = None
            continue
        qty_match = re.match(r"^(\d+)\s+.+?\s+\d+[,.]\d{2}\s*$", line)
        if qty_match and "art" not in line.lower():
            pending_qty = int(qty_match.group(1))
            continue
        qty_before_eur = re.match(r"^.+?\s+(\d+)\s+EUR\s+\d+[,.]\d{2}\s*$", line, re.I)
        if qty_before_eur:
            pending_qty = int(qty_before_eur.group(1))
            continue
        sku_match = re.search(
            r"\b(?:Art\.?\s*Nr\.?|SKU/REF)\s+([A-Z0-9]{3}-[A-Z0-9]{6,16})\b",
            line,
            re.I,
        )
        if sku_match:
            items.append(ReceiptItem(sku=sku_match.group(1).upper(), quantity=pending_qty or 1))
            pending_qty = None
    deduped: list[ReceiptItem] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        key = (item.sku, item.quantity)
        if key not in seen:
            deduped.append(item)
            seen.add(key)
    return deduped


def _sku_distance(left: str, right: str) -> int:
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b)
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if a == b else 1),
                )
            )
        previous = current
    return previous[-1]


def _sku_ocr_key(value: str) -> str:
    return value.translate(str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"}))


def _sku_ocr_distance(left: str, right: str) -> int:
    return _sku_distance(_sku_ocr_key(left), _sku_ocr_key(right))


def _closest_receipt_sku_row(item_sku: str, candidates: list[dict[str, str]]) -> dict[str, str] | None:
    if not candidates:
        return None
    row = min(
        candidates,
        key=lambda candidate: (
            _sku_ocr_distance(item_sku, candidate.get("sku", "")),
            _sku_distance(item_sku, candidate.get("sku", "")),
        ),
    )
    return row if _sku_ocr_distance(item_sku, row.get("sku", "")) <= 2 else None


def _ocr_prefix_variants(prefix: str) -> list[str]:
    aliases = {
        "0": ("0", "O"),
        "O": ("O", "0"),
        "1": ("1", "I", "L"),
        "I": ("I", "1"),
        "L": ("L", "1"),
        "5": ("5", "S"),
        "S": ("S", "5"),
        "8": ("8", "B"),
        "B": ("B", "8"),
    }
    variants = [""]
    for char in prefix:
        variants = [base + value for base in variants for value in aliases.get(char, (char,))]
    return list(dict.fromkeys(variants))


def _resolve_receipt_prices(
    call_runtime,
    kit: ReadOnlySolverKit,
    items: list[ReceiptItem],
) -> tuple[int | None, list[str], list[str]]:
    if not items:
        return None, [], []

    rows, _ = kit.auto_sql(
        call_runtime,
        """
select product_sku as sku, record_path as path, product_name as name, price_cents
from product_variants
where product_sku in ({skus})
order by product_sku;
""".format(skus=sql_in([item.sku for item in items])).strip(),
    )
    by_sku = {row.get("sku", ""): row for row in rows}
    refs: list[str] = []
    missing: list[str] = []
    total = 0

    for item in items:
        row = by_sku.get(item.sku)
        if row is None:
            candidates, _ = kit.auto_sql(
                call_runtime,
                """
select product_sku as sku, record_path as path, product_name as name, price_cents
from product_variants
where product_sku like {prefix}
order by product_sku
limit 25;
""".format(prefix=sql_literal(item.sku[:8] + "%")).strip(),
            )
            row = _closest_receipt_sku_row(item.sku, candidates)
            if row is None:
                prefix_clauses = " or ".join(
                    f"product_sku like {sql_literal(prefix + '%')}"
                    for prefix in _ocr_prefix_variants(item.sku[:4])
                )
                candidates, _ = kit.auto_sql(
                    call_runtime,
                    """
select product_sku as sku, record_path as path, product_name as name, price_cents
from product_variants
where {prefix_clauses}
order by product_sku
limit 500;
""".format(prefix_clauses=prefix_clauses).strip(),
                )
                row = _closest_receipt_sku_row(item.sku, candidates)
        if row is None:
            missing.append(item.sku)
            continue
        try:
            total += item.quantity * int(row.get("price_cents") or "0")
        except ValueError:
            missing.append(item.sku)
            continue
        path = row.get("path", "")
        if path.startswith("/proc/catalog/") and path not in refs:
            refs.append(path)

    return (None if missing else total), refs, missing


def auto_receipt_price_check_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "receipt_price_check":
        return False
    if kit.req_tree is None:
        return False

    receipt_path = _receipt_upload_path(task_text, "")
    if not receipt_path:
        tree_result, tree_txt = _call_and_print(
            kit,
            call_runtime,
            kit.req_tree(tool="tree", root="/uploads", level=1),
        )
        receipt_path = _receipt_upload_path(task_text, tree_txt or getattr(tree_result, "content", ""))
    if not receipt_path:
        return False

    read_cmd = kit.req_read(tool="read", path=receipt_path, start_line=1, end_line=120)
    read_result, read_txt = _call_and_print(kit, call_runtime, read_cmd)
    content = getattr(read_result, "content", read_txt)
    items = _receipt_items(content)
    old_total = _receipt_total_cents(content)
    if not items or old_total is None:
        return False

    current_total, product_refs, missing = _resolve_receipt_prices(call_runtime, kit, items)
    if current_total is None:
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=[
                    "read receipt OCR file",
                    f"could not resolve receipt SKUs: {', '.join(missing)}",
                ],
                message="<NO>",
                grounding_refs=[receipt_path] + product_refs,
                outcome="OUTCOME_OK",
            ),
        )

    threshold = _receipt_threshold_cents(task_text)
    diff = abs(current_total - old_total)
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "read receipt OCR file",
                f"resolved {len(items)} receipt SKUs to current catalogue prices",
                f"compared current total {current_total} cents with receipt total {old_total} cents",
            ],
            message="<YES>" if diff <= threshold else "<NO>",
            grounding_refs=[receipt_path] + product_refs,
            outcome="OUTCOME_OK",
        ),
    )


def _user_id_from_id_output(text: str) -> str:
    match = re.search(r"\buser:\s*([A-Za-z0-9_]+)", text)
    return match.group(1) if match else ""


def _parse_quote_rows(task_text: str, kit: ReadOnlySolverKit) -> list[QuoteRow] | None:
    lowered = task_text.lower()
    if not (
        "pasted product list" in lowered
        and "rowid" in lowered
        and "sku\tin_stock\tmatch" in lowered
        and "same-day availability" in lowered
    ):
        return None

    rows_blob = task_text.split("Rows:", 1)[-1]
    rows: list[QuoteRow] = []
    for raw_line in rows_blob.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        row_match = re.match(r"^([A-Za-z0-9]+)\t(.+)\t(\d+)$", line)
        if not row_match:
            continue
        product = re.match(
            r"the\s+(.+?)\s+from\s+(.+?)\s+in\s+the\s+(.+?)\s+line\s+that\s+has\s+(.+)$",
            row_match.group(2),
            re.I | re.S,
        )
        if not product:
            return None
        rows.append(
            QuoteRow(
                row_id=row_match.group(1),
                quantity=int(row_match.group(3)),
                lookup=kit.req_catalogue_lookup(
                    tool="catalogue_lookup",
                    kind_name=" ".join(product.group(1).split()),
                    brand=" ".join(product.group(2).split()),
                    family_name=" ".join(product.group(3).split()),
                    constraints=kit.parse_constraints(product.group(4)),
                ),
            )
        )
    return rows or None


def auto_quote_product_list_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "quote_tsv":
        return False
    quote_rows = _parse_quote_rows(task_text, kit)
    if not quote_rows:
        return False

    id_result, id_txt = _call_and_print(
        kit,
        call_runtime,
        kit.req_exec(tool="exec", path="/bin/id"),
    )
    user_id = _user_id_from_id_output(getattr(id_result, "stdout", "") or id_txt)
    if not user_id:
        return False

    employee_rows, _ = kit.auto_sql(
        call_runtime,
        """
select e.employee_id as id, e.record_path as path, e.store_id, s.record_path as store_path
from employee_accounts e
join stores s on s.store_id = e.store_id
where e.employee_id = {user_id}
limit 1;
""".format(user_id=sql_literal(user_id)).strip(),
    )
    if not employee_rows:
        return False
    store_id = employee_rows[0].get("store_id", "")
    if not store_id:
        return False
    store_path = employee_rows[0].get("store_path", "")

    resolved: list[dict[str, object]] = []
    matched_skus: list[str] = []
    product_refs: list[str] = []
    for row in quote_rows:
        _, lookup_txt = _call_and_print(kit, call_runtime, row.lookup)
        exact = "exact_matches=0" not in lookup_txt
        product_row = first_catalogue_row(lookup_txt) if exact else {}
        sku = product_row.get("sku", "") if product_row else ""
        path = product_row.get("path", "") if product_row else ""
        if sku:
            matched_skus.append(sku)
        if path.startswith("/proc/catalog/") and path not in product_refs:
            product_refs.append(path)
        resolved.append(
            {
                "row_id": row.row_id,
                "quantity": row.quantity,
                "sku": sku,
                "path": path,
            }
        )

    availability: dict[str, int] = {}
    if matched_skus:
        inventory_rows, _ = kit.auto_sql(
            call_runtime,
            """
select p.product_sku as sku, coalesce(i.available_today_quantity, 0) as available_today
from product_variants p
left join store_inventory i on i.product_sku = p.product_sku and i.store_id = {store_id}
where p.product_sku in ({skus})
order by p.product_sku;
""".format(
                store_id=sql_literal(store_id),
                skus=sql_in(matched_skus),
            ).strip(),
        )
        for row in inventory_rows:
            try:
                availability[row.get("sku", "")] = int(row.get("available_today") or "0")
            except ValueError:
                availability[row.get("sku", "")] = 0

    output = ["RowID\tSKU\tin_stock\tmatch"]
    for row in resolved:
        sku = str(row["sku"])
        if not sku:
            output.append(f"{row['row_id']}\t\t\tfalse")
            continue
        in_stock = availability.get(sku, 0)
        match = "true" if in_stock >= int(row["quantity"]) else "false"
        output.append(f"{row['row_id']}\t{sku}\t{in_stock}\t{match}")

    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "resolved employee store",
                "checked each row against exact catalogue records",
                "queried same-day inventory for matched SKUs",
            ],
            message="\n".join(output),
            grounding_refs=([store_path] if store_path else []) + product_refs,
            outcome="OUTCOME_OK",
        ),
    )


CATALOGUE_QUERY_STOPWORDS = {
    "a",
    "an",
    "any",
    "catalog",
    "catalogue",
    "can",
    "carry",
    "check",
    "confirm",
    "do",
    "does",
    "if",
    "have",
    "in",
    "is",
    "item",
    "items",
    "me",
    "please",
    "product",
    "products",
    "really",
    "stock",
    "the",
    "this",
    "whether",
    "you",
}


def auto_single_product_city_quantity_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "city_quantity":
        return False
    match = re.search(
        r"any PowerTool branch in\s+([A-Za-z][A-Za-z -]*?)\s+today\..*?"
        r"product \(the\s+(.+?)\s+from\s+(.+?)\s+in\s+the\s+(.+?)\s+line"
        r"\s+that\s+has\s+(.+?)\)\s+are available today\?",
        task_text,
        re.I | re.S,
    )
    if not match:
        return False

    city = " ".join(match.group(1).split())
    lookup = kit.req_catalogue_lookup(
        tool="catalogue_lookup",
        kind_name=" ".join(match.group(2).split()),
        brand=" ".join(match.group(3).split()),
        family_name=" ".join(match.group(4).split()),
        constraints=kit.parse_constraints(match.group(5)),
    )
    _, lookup_txt = _call_and_print(kit, call_runtime, lookup)
    row = first_catalogue_row(lookup_txt)
    sku = row.get("sku", "") if row else ""
    product_path = row.get("path", "") if row else ""
    if not sku or not product_path:
        return False

    store_rows, _ = kit.auto_sql(
        call_runtime,
        """
select store_id as id, record_path as path, store_name as name, city
from stores
where lower(city) = lower({city})
  and lower(store_name) like lower('%PowerTool%')
order by store_id;
""".format(city=sql_literal(city)).strip(),
    )
    if not store_rows:
        return False
    store_ids = [row["id"] for row in store_rows]
    qty_rows, _ = kit.auto_sql(
        call_runtime,
        """
select coalesce(sum(coalesce(i.available_today_quantity, 0)), 0) as qty
from stores s
left join store_inventory i on i.store_id = s.store_id and i.product_sku = {sku}
where s.store_id in ({store_ids});
""".format(
            sku=sql_literal(sku),
            store_ids=sql_in(store_ids),
        ).strip(),
    )
    try:
        qty = int(qty_rows[0].get("qty") or "0") if qty_rows else 0
    except ValueError:
        return False

    refs = [row["path"] for row in store_rows if row.get("path")] + [product_path]
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "resolved exact product SKU",
                f"summed availability across every PowerTool branch in {city}",
                f"reported quantity {qty}",
            ],
            message=exact_quantity_message(task_text, qty),
            grounding_refs=refs,
            outcome="OUTCOME_OK",
        ),
    )


def auto_catalogue_count_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "count_report":
        return False
    if not _wants_exact_count_answer(task_text):
        return False

    requested_kind = requested_count_kind(task_text)
    if not requested_kind:
        return False

    search_cmd = kit.req_search(
        tool="search",
        root="/docs",
        pattern=f"Requested product kind: {requested_kind}",
        limit=10,
    )
    search_result, _ = _call_and_print(kit, call_runtime, search_cmd)

    paths = []
    for match in getattr(search_result, "matches", []):
        path = getattr(match, "path", "")
        if path.startswith("/docs/") and path not in paths:
            paths.append(path)

    for path in paths:
        read_cmd = kit.req_read(tool="read", path=path, start_line=0, end_line=100)
        read_result, read_txt = _call_and_print(kit, call_runtime, read_cmd)
        count_report = kit.count_policy_request_from_doc(
            task_text,
            path,
            getattr(read_result, "content", read_txt),
        )
        if count_report is None:
            continue
        _, report_txt = _call_and_print(kit, call_runtime, count_report)
        count, refs = count_report_summary_from_output(report_txt)
        if count is None:
            continue
        sql_policy_refs = _sql_policy_refs(call_runtime, task_text, kit)
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=[
                    "read matching catalogue count policy",
                    "counted qualifying catalogue SKUs",
                    f"reported count {count}",
                ],
                message=exact_count_message(task_text, count),
                grounding_refs=_count_report_refs(task_text, refs, sql_policy_refs),
                outcome="OUTCOME_OK",
            ),
        )

    return False


def auto_support_note_catalogue_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "catalogue_lookup":
        return False
    if task_spec.catalogue_lookup_mode != "support_note":
        return False
    match = re.search(
        r"claims we stock the\s+(.+?)\s+from\s+(.+?)\s+in\s+the\s+(.+?)\s+line"
        r"\s+that\s+has\s+(.+?)\.",
        task_text,
        re.I | re.S,
    )
    if not match:
        return False
    constraint_text = match.group(4)
    constraints = kit.parse_constraints(constraint_text)
    unsupported_extra_claim = bool(
        re.search(
            r"\b(?:built[- ]in|gps|tracking|bluetooth|wi[- ]?fi|supports?|voice control)\b",
            constraint_text,
            re.I,
        )
    )
    lookup = kit.req_catalogue_lookup(
        tool="catalogue_lookup",
        kind_name=" ".join(match.group(1).split()),
        brand=" ".join(match.group(2).split()),
        family_name=" ".join(match.group(3).split()),
        constraints=constraints,
    )
    _, lookup_txt = _call_and_print(kit, call_runtime, lookup)
    row = first_catalogue_row(lookup_txt)
    exact = "exact_matches=0" not in lookup_txt and not unsupported_extra_claim

    if not exact and len(constraints) > 1 and not row:
        for idx in range(len(constraints)):
            reduced = constraints[:idx] + constraints[idx + 1 :]
            base_lookup = lookup.model_copy(update={"constraints": reduced})
            _, base_txt = _call_and_print(kit, call_runtime, base_lookup)
            row = first_catalogue_row(base_txt)
            if row and "exact_matches=0" not in base_txt:
                break
    elif not exact and len(constraints) > 1 and not unsupported_extra_claim:
        base_lookup = lookup.model_copy(update={"constraints": constraints[:-1]})
        _, base_txt = _call_and_print(kit, call_runtime, base_lookup)
        row = first_catalogue_row(base_txt) or row

    sku = row.get("sku", "") if row else ""
    path = row.get("path", "") if row else ""
    message = f"<YES> {sku}".strip() if exact else f"<NO> {sku}".strip()
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=["checked catalogue product record against support note"],
            message=message,
            grounding_refs=[path] if path.startswith("/proc/catalog/") else [],
            outcome="OUTCOME_OK",
        ),
    )


def auto_catalogue_yes_no_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "catalogue_lookup":
        return False
    if task_spec.catalogue_lookup_mode != "structured_product":
        return False
    lowered = task_text.lower()
    if any(term in lowered for term in (" available", " basket", " checkout", " refund", " payment", " discount")):
        return False
    match = re.search(
        r"(?:do you have\s+)?the\s+(.+?)\s+from\s+(.+?)\s+in\s+the\s+(.+?)\s+line"
        r"(?:\s+that\s+has\s+(.+?))?(?:\s+in catalogue)?\?",
        task_text,
        re.I | re.S,
    )
    if not match:
        return False
    lookup = kit.req_catalogue_lookup(
        tool="catalogue_lookup",
        kind_name=" ".join(match.group(1).split()),
        brand=" ".join(match.group(2).split()),
        family_name=" ".join(match.group(3).split()),
        constraints=kit.parse_constraints(match.group(4) or ""),
    )
    _, lookup_txt = _call_and_print(kit, call_runtime, lookup)
    exact = "exact_matches=0" not in lookup_txt
    refs = catalogue_paths_from_output(lookup_txt)
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=["checked catalogue product line and properties"],
            message="<YES>" if exact else "<NO>",
            grounding_refs=refs[:1],
            outcome="OUTCOME_OK",
        ),
    )


def _informal_catalogue_query_terms(task_text: str) -> list[list[str]]:
    query = re.sub(r"^\s*do\s+you\s+have\s+", "", task_text, flags=re.I).strip()
    query = query.split("?", 1)[0]
    groups: list[list[str]] = []
    for part in re.split(r"\s+(?:or|/)\s+|,", query, flags=re.I):
        part = re.split(r"\bnot\s+sure\b", part, flags=re.I)[0]
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", part.lower())
            if token not in CATALOGUE_QUERY_STOPWORDS
        ]
        if len(tokens) >= 2:
            groups.append(tokens[:6])
    return groups


def auto_informal_catalogue_yes_no_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "catalogue_lookup":
        return False
    if task_spec.catalogue_lookup_mode != "informal":
        return False
    lowered = task_text.lower()
    if any(
        term in lowered
        for term in (
            " available",
            " basket",
            " checkout",
            " discount",
            " payment",
            " refund",
            " store",
            " branch",
        )
    ):
        return False

    term_groups = _informal_catalogue_query_terms(task_text)
    if not term_groups:
        return False

    haystack = (
        "lower(p.product_name || ' ' || f.product_family_name || ' ' || f.brand || ' ' || "
        "f.series || ' ' || f.model || ' ' || k.product_kind_name || ' ' || p.properties)"
    )
    for terms in term_groups:
        where = " and ".join(f"{haystack} like {sql_literal('%' + term + '%')}" for term in terms)
        rows, _ = kit.auto_sql(
            call_runtime,
            f"""
select p.product_sku as sku, p.record_path as path, p.product_name as name
from product_variants p
join product_families f on f.product_family_id = p.product_family_id
join product_kinds k on k.product_kind_id = p.product_kind_id
where {where}
order by p.product_sku
limit 5;
""".strip(),
        )
        refs = [row.get("path", "") for row in rows if row.get("path", "").startswith("/proc/catalog/")]
        if refs:
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=["searched catalogue product names for requested aliases"],
                    message="<YES>",
                    grounding_refs=refs[:1],
                    outcome="OUTCOME_OK",
                ),
            )

    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=["searched catalogue product names for requested aliases"],
            message="<NO>",
            grounding_refs=[],
            outcome="OUTCOME_OK",
        ),
    )


def auto_availability_count_task(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "availability_count":
        return False
    if not _wants_exact_count_answer(task_text):
        return False
    parsed = kit.parse_availability_task(task_text)
    if parsed is None:
        return False
    threshold, store_phrase, lookups, comparator = parsed
    threshold = (
        task_spec.threshold
        if task_spec.threshold is not None and task_spec.threshold > 0
        else threshold
    )
    store_phrase = task_spec.store_phrase or store_phrase
    comparator = task_spec.comparator or comparator

    store_cmd = kit.req_exec(
        tool="exec",
        path="/bin/sql",
        stdin=(
            "select store_id as id, record_path as path, store_name as name, city "
            "from stores order by city, store_name;"
        ),
    )
    store_result, _ = _call_and_print(kit, call_runtime, store_cmd)
    store = select_store(csv_rows(store_result.stdout), store_phrase)
    if not store:
        return False

    skus: list[str] = []
    for lookup in lookups:
        _, lookup_txt = _call_and_print(kit, call_runtime, lookup)
        sku = sku_from_catalogue_output(lookup_txt)
        if not sku:
            return False
        skus.append(sku)

    if comparator == "lt":
        rows, _ = kit.auto_sql(
            call_runtime,
            """
select
  p.product_sku as sku,
  p.record_path as path,
  p.product_name as name,
  coalesce(i.available_today_quantity, 0) as available_today,
  case when coalesce(i.available_today_quantity, 0) < {threshold} then 1 else 0 end as counts
from product_variants p
left join store_inventory i on i.product_sku = p.product_sku and i.store_id = {store_id}
where p.product_sku in ({skus})
order by p.product_sku;
""".format(
                threshold=threshold,
                store_id=sql_literal(store["id"]),
                skus=sql_in(skus),
            ).strip(),
        )
        counted = [row for row in rows if row.get("counts") == "1"]
        counted_refs = []
        if threshold > 1:
            for row in counted:
                try:
                    available_today = int(row.get("available_today") or "0")
                except ValueError:
                    available_today = 0
                path = row.get("path", "")
                if available_today > 0 and path.startswith("/proc/catalog/"):
                    counted_refs.append(path)
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=[
                    "resolved store",
                    "resolved requested product SKUs",
                    f"counted {len(counted)} products below the threshold",
                ],
                message=exact_count_message(task_text, len(counted)),
                grounding_refs=[store.get("path", "")] + counted_refs,
                outcome="OUTCOME_OK",
            ),
        )

    inventory = kit.req_inventory_count(
        tool="inventory_count",
        store_id=store["id"],
        threshold=threshold,
        skus=skus,
    )
    _, inventory_txt = _call_and_print(kit, call_runtime, inventory)
    count, store_ref, refs = inventory_summary_from_output(inventory_txt)
    if count is None:
        return False
    final_refs = ([store_ref] if store_ref else []) + refs
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "resolved store",
                "resolved requested product SKUs",
                f"counted {count} products meeting the threshold",
            ],
            message=exact_count_message(task_text, count),
            grounding_refs=final_refs,
            outcome="OUTCOME_OK",
        ),
    )


AUTO_SOLVERS = (
    auto_receipt_price_check_task,
    auto_quote_product_list_task,
    auto_single_product_city_quantity_task,
    auto_availability_count_task,
    auto_support_note_catalogue_task,
    auto_catalogue_yes_no_task,
    auto_informal_catalogue_yes_no_task,
    auto_catalogue_count_task,
)


def run_read_only_solvers(
    call_runtime,
    task_text: str,
    kit: ReadOnlySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    for solver in AUTO_SOLVERS:
        if solver(call_runtime, task_text, kit, task_spec):
            return True
    return False
