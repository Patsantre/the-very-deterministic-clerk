from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import BaseModel


TaskClass = Literal[
    "availability_count",
    "catalogue_lookup",
    "checkout",
    "discount",
    "refund",
    "three_ds_recovery",
    "fraud_export",
    "quote_tsv",
    "receipt_price_check",
    "count_report",
    "city_quantity",
    "unknown",
]
Comparator = Literal["gte", "lt"]
CatalogueLookupMode = Literal["structured_product", "support_note", "informal", "unknown"]
# Structural task formats are safer than LLM routing: if the local parser sees
# these exact markers, the deterministic solver should run.
HIGH_CONFIDENCE_FALLBACK_CLASSES = frozenset({
    "checkout",
    "three_ds_recovery",
    "quote_tsv",
    "receipt_price_check",
})


class TaskSpec(BaseModel):
    task_class: TaskClass
    catalogue_lookup_mode: CatalogueLookupMode = "unknown"
    store_phrase: str = ""
    threshold: int | None = None
    comparator: Comparator | None = None
    basket_id: str = ""
    payment_id: str = ""


CLASSIFIER_SYSTEM_PROMPT = """Classify one ECOM benchmark task.
Return only the structured TaskSpec. Do not solve the task and do not include business reasoning.

Classes:
- availability_count: count products from a list by same-day availability, including at-least, below, fewer-than, less-than, or not-available-today wording.
- catalogue_lookup: yes/no catalogue existence checks, including support-note catalogue checks and terse product-name queries ending with "in catalogue?".
- checkout: complete/check out/put through a basket or finish an order, excluding bank/3DS/payment verification tasks.
- discount: apply or refuse service_recovery/discount workflow.
- refund: refund or refund-approval workflow.
- three_ds_recovery: bank verification, card verification, 3DS, card security, or payment verification recovery.
- fraud_export: fraud review over current/archived payment history or /archive/*.tsv batch exports.
- quote_tsv: pasted product list tasks that require RowID/SKU/in_stock/match TSV output.
- receipt_price_check: OCR/uploaded receipt tasks comparing old receipt total excluding VAT to today's catalogue prices.
- count_report: count catalogue products/product kinds, possibly using catalogue count policy docs.
- city_quantity: total units of one product across every branch in a city.
- unknown: none of the above.

Set comparator to "gte" for at least / or more / ready thresholds, and "lt" for below / fewer than / less than / not available today.
If a task says "how many of these" or "how many from this list" with a store/branch phrase and an availability threshold, use availability_count, not city_quantity.
Use city_quantity only for one product summed across every branch in a city.
Extract basket_id and payment_id only when explicit in text, normalized as basket_123 and pay_123.
store_phrase should be the verbatim city/store phrase when obvious, otherwise empty.
For catalogue_lookup, set catalogue_lookup_mode:
- structured_product: text names "the KIND from BRAND in the FAMILY line" and asks if it exists.
- support_note: a support note or claim must be checked against the actual catalogue.
- informal: terse/slang/free-text catalogue checks without the structured KIND/BRAND/FAMILY shape.
- unknown: not catalogue_lookup or not clear.

Examples:
- "m18 fid3 kit, battery size not sure in catalogue?" -> catalogue_lookup
- "Store desk note says the handbook allows a manager-waved-through exception for my basket basket_150; check it out now" -> checkout
- "Look at the old receipt in /uploads/. If we were to sell these products today, would the total price excluding VAT stay within 3 EUR?" -> receipt_price_check
- "do we stock the x from brand y in catalogue?" -> catalogue_lookup
- "How many products are Pipe Fitting in catalogue? Answer format: <COUNT:NUMBER>" -> count_report
- "For the catalogue count report, how many products are Work Jacket? answer pattern: <QTY:%VALUE%>" -> count_report
"""


def fallback_classify_task(task_text: str) -> TaskSpec:
    """Local classifier used only when the structured model call is unavailable.

    This keeps the agent functional during model/schema timeouts while the normal
    path remains the LLM-produced TaskSpec.
    """
    lowered = task_text.lower()
    basket_id = _id_from_text(task_text, "basket")
    payment_id = _id_from_text(task_text, "pay")
    threshold, comparator = _availability_threshold(task_text)
    store_phrase = _store_phrase(task_text)
    catalogue_lookup_mode: CatalogueLookupMode = "unknown"

    task_class: TaskClass = "unknown"
    if "pasted product list" in lowered and "rowid" in lowered and "sku" in lowered:
        task_class = "quote_tsv"
    elif _looks_like_receipt_price_check(lowered):
        task_class = "receipt_price_check"
    elif "fraud" in lowered and (
        "/archive/" in lowered
        or "archive export" in lowered
        or "archived payment" in lowered
        or "payment history" in lowered
    ):
        task_class = "fraud_export"
    elif _looks_like_three_ds(lowered):
        task_class = "three_ds_recovery"
    elif "refund" in lowered:
        task_class = "refund"
    elif "discount" in lowered or "service_recovery" in lowered:
        task_class = "discount"
    elif _looks_like_checkout(lowered):
        task_class = "checkout"
    elif _looks_like_city_quantity(lowered):
        task_class = "city_quantity"
    elif _looks_like_availability_count(lowered):
        task_class = "availability_count"
    elif _looks_like_count_report(lowered):
        task_class = "count_report"
    elif _looks_like_catalogue_lookup(lowered):
        task_class = "catalogue_lookup"
        catalogue_lookup_mode = _catalogue_lookup_mode(task_text)

    return TaskSpec(
        task_class=task_class,
        catalogue_lookup_mode=catalogue_lookup_mode,
        store_phrase=store_phrase,
        threshold=threshold,
        comparator=comparator,
        basket_id=basket_id,
        payment_id=payment_id,
    )


def classify_task(task_text: str, llm_client) -> TaskSpec:
    fallback = fallback_classify_task(task_text)
    if llm_client is None:
        return fallback

    try:
        client = llm_client
        timeout = os.getenv("ECOM_TASK_CLASSIFIER_TIMEOUT_S")
        if timeout and hasattr(llm_client, "with_options"):
            client = llm_client.with_options(timeout=float(timeout))
        response = client.beta.chat.completions.parse(
            model=os.getenv("ECOM_TASK_CLASSIFIER_MODEL") or os.getenv("MODEL_ID") or "gpt-4.1-mini",
            response_format=TaskSpec,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": task_text},
            ],
            max_completion_tokens=2048,
            temperature=0,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            return fallback
        return _merge_with_fallback(parsed, fallback)
    except Exception as exc:
        print(f"\x1B[33mTASK_CLASSIFIER fallback: {type(exc).__name__}: {exc}\x1B[0m", flush=True)
        return fallback


def _merge_with_fallback(parsed: TaskSpec, fallback: TaskSpec) -> TaskSpec:
    updates: dict[str, object] = {}
    if fallback.task_class != "unknown" and (
        parsed.task_class == "unknown"
        or (
            parsed.task_class != fallback.task_class
            and fallback.task_class in HIGH_CONFIDENCE_FALLBACK_CLASSES
        )
    ):
        updates["task_class"] = fallback.task_class
    if not parsed.store_phrase and fallback.store_phrase:
        updates["store_phrase"] = fallback.store_phrase
    if parsed.catalogue_lookup_mode == "unknown" and fallback.catalogue_lookup_mode != "unknown":
        updates["catalogue_lookup_mode"] = fallback.catalogue_lookup_mode
    if (
        (parsed.threshold is None or parsed.threshold <= 0)
        and fallback.threshold is not None
    ):
        updates["threshold"] = fallback.threshold
    if parsed.comparator is None and fallback.comparator is not None:
        updates["comparator"] = fallback.comparator
    if parsed.basket_id != fallback.basket_id:
        updates["basket_id"] = fallback.basket_id
    if parsed.payment_id != fallback.payment_id:
        updates["payment_id"] = fallback.payment_id
    return parsed.model_copy(update=updates) if updates else parsed


def _id_from_text(task_text: str, prefix: str) -> str:
    match = re.search(rf"\b{re.escape(prefix)}[_ -](\d+)\b", task_text, re.I)
    return f"{prefix}_{match.group(1)}" if match else ""


def _availability_threshold(task_text: str) -> tuple[int | None, Comparator | None]:
    lowered = task_text.lower()
    if re.search(r"\bnot\s+available\s+today\b|\bno\s+same-day\s+availability\b", lowered):
        return 1, "lt"
    match = re.search(
        r"\b(?:below|less than|fewer than)\s+(\d+)(?:\s+items?)?\s+available\b",
        lowered,
    )
    if match:
        return int(match.group(1)), "lt"
    match = re.search(r"\bat least\s+(\d+)\s+items?\s+available\b", lowered)
    if match:
        return int(match.group(1)), "gte"
    match = re.search(r"\b(\d+)\s+or more\s+ready\b", lowered)
    if match:
        return int(match.group(1)), "gte"
    return None, None


def _store_phrase(task_text: str) -> str:
    patterns = (
        r"available in\s+(?:the\s+)?(.+?)\s+today:",
        r"available today at\s+(?:the\s+)?(.+?):",
        r"at\s+(?:the\s+)?(.+?),\s*how many of these",
        r"check\s+(?:the\s+)?(.+?)\s+today\s+and\s+tell",
        r"check\s+(?:the\s+)?(.+?),\s*how many of these",
        r"any PowerTool branch in\s+(.+?)\s+today",
        r"across every\s+(.+?)\s+branch",
    )
    for pattern in patterns:
        match = re.search(pattern, task_text, re.I | re.S)
        if match:
            return " ".join(match.group(1).split())
    return ""


def _looks_like_three_ds(lowered: str) -> bool:
    return any(
        term in lowered
        for term in (
            "3ds",
            "3-ds",
            "3 d secure",
            "3-d secure",
            "3-dsecure",
            "bank verification",
            "bank approval",
            "approval pop-up",
            "card verification",
            "card security",
            "payment verification",
        )
    )


def _looks_like_checkout(lowered: str) -> bool:
    return (
        (
            "check out" in lowered
            or "check it out" in lowered
            or "checkout" in lowered
            or "put through" in lowered
            or "finish my order" in lowered
        )
        and not _looks_like_three_ds(lowered)
        and "discount" not in lowered
    )


def _looks_like_city_quantity(lowered: str) -> bool:
    return (
        re.search(r"\b(?:any|every|each)\s+(?:powertool\s+|hardware\s+)?branch\s+in\b", lowered)
        is not None
        and "how many units of product" in lowered
        and "available today" in lowered
    )


def _looks_like_availability_count(lowered: str) -> bool:
    return (
        (
            "how many of these products" in lowered
            or "how many from this list" in lowered
            or "how many of these have" in lowered
            or "how many of these" in lowered
        )
        and ("available" in lowered or "ready" in lowered)
    )


def _looks_like_count_report(lowered: str) -> bool:
    return (
        "catalogue" in lowered
        and (
            "how many catalogue products" in lowered
            or "catalogue count" in lowered
            or "count report" in lowered
            or "how many products are" in lowered
        )
    )


def _looks_like_receipt_price_check(lowered: str) -> bool:
    return (
        ("/uploads" in lowered or "receipt" in lowered or "ocr" in lowered)
        and "receipt" in lowered
        and ("excluding vat" in lowered or "exkl. mwst" in lowered or "ex-vat" in lowered)
        and ("today" in lowered or "current" in lowered)
    )


def _looks_like_catalogue_lookup(lowered: str) -> bool:
    return (
        "actual catalogue" in lowered
        or "in catalogue" in lowered
        or lowered.startswith("do you have ")
    )


def _catalogue_lookup_mode(task_text: str) -> CatalogueLookupMode:
    lowered = task_text.lower()
    if "actual catalogue" in lowered or "support note" in lowered:
        return "support_note"
    if re.search(
        r"\bthe\s+.+?\s+from\s+.+?\s+in\s+the\s+.+?\s+line\b",
        task_text,
        re.I | re.S,
    ):
        return "structured_product"
    return "informal"
