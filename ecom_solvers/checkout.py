from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ecom_task_classifier import TaskSpec


@dataclass(frozen=True)
class CheckoutSolverKit:
    req_exec: type
    report_completion: type
    basket_id_from_task: Callable[[str], str]
    runtime_identity: Callable[[Callable[[object], object]], tuple[str, set[str]]]
    basket_row: Callable[[Callable[[object], object], str], dict[str, str] | None]
    basket_inventory_rows: Callable[[Callable[[object], object], str], list[dict[str, str]]]
    basket_is_checkoutable: Callable[[list[dict[str, str]]], bool]
    auto_sql: Callable[[Callable[[object], object], str], tuple[list[dict[str, str]], str]]
    auto_call: Callable[[Callable[[object], object], object], tuple[object, str]]
    auto_finish: Callable[[Callable[[object], object], object], bool]
    sql_literal: Callable[[str], str]
    security_refs: Callable[..., list[str]]


def auto_checkout_task(
    call_runtime,
    task_text: str,
    kit: CheckoutSolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "checkout":
        return False

    lowered = task_text.lower()
    explicit_basket_id = kit.basket_id_from_task(task_text)
    basket_id = task_spec.basket_id or explicit_basket_id
    user, _roles = kit.runtime_identity(call_runtime)
    wants_latest_open = any(
        phrase in lowered
        for phrase in (
            "newest open basket",
            "newest active basket",
            "most recent open basket",
            "most recent active basket",
            "started most recently",
            "started most recent",
        )
    )
    if wants_latest_open and not explicit_basket_id:
        basket_id = ""

    if not basket_id:
        if "my basket" not in lowered and not wants_latest_open:
            return False
        rows, _ = kit.auto_sql(
            call_runtime,
            """
select id, path, store_id, status, created_at
from (
  select basket_id as id, record_path as path, store_id, basket_status as status,
         basket_created_at as created_at, customer_id
  from shopping_baskets
)
where customer_id = {user}
  and status = 'active'
order by created_at desc;
""".format(user=kit.sql_literal(user)).strip(),
        )
        if wants_latest_open and rows:
            basket_id = rows[0]["id"]
        elif wants_latest_open:
            refs = kit.security_refs("/docs/checkout.md")
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked active baskets for authenticated customer",
                        "found no checkout target",
                    ],
                    message="No active basket was found for checkout.",
                    grounding_refs=refs,
                    outcome="OUTCOME_NONE_UNSUPPORTED",
                ),
            )
        else:
            if len(rows) != 1:
                refs = kit.security_refs("/docs/checkout.md", *(row.get("path", "") for row in rows))
                return kit.auto_finish(
                    call_runtime,
                    kit.report_completion(
                        tool="report_completion",
                        completed_steps_laconic=[
                            "checked active baskets for authenticated customer",
                            "found ambiguous checkout target",
                        ],
                        message="Which basket should I check out?",
                        grounding_refs=refs,
                        outcome="OUTCOME_NONE_CLARIFICATION",
                    ),
                )
            basket_id = rows[0]["id"]

    basket = kit.basket_row(call_runtime, basket_id)
    if not basket:
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["looked up requested basket"],
                message="The requested basket was not found.",
                grounding_refs=kit.security_refs("/docs/checkout.md"),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    if user != basket.get("customer_id"):
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=[
                    "checked authenticated customer against basket owner",
                    "refused cross-customer checkout",
                ],
                message="Request denied: the authenticated user does not own this basket.",
                grounding_refs=kit.security_refs("/docs/checkout.md"),
                outcome="OUTCOME_DENIED_SECURITY",
            ),
        )

    if basket.get("status") != "active":
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["checked basket status before checkout"],
                message="The basket is not active, so checkout is not supported.",
                grounding_refs=kit.security_refs("/docs/checkout.md", basket.get("path", "")),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    line_rows = kit.basket_inventory_rows(call_runtime, basket_id)
    if not kit.basket_is_checkoutable(line_rows):
        refs = kit.security_refs(
            "/docs/checkout.md",
            basket.get("path", ""),
            *(row.get("product_path", "") for row in line_rows),
        )
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=[
                    "checked basket inventory",
                    "found at least one unavailable line",
                ],
                message="Checkout is not supported because at least one basket line is unavailable.",
                grounding_refs=refs,
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    checkout_cmd = kit.req_exec(tool="exec", path="/bin/checkout", args=[basket_id])
    kit.auto_call(call_runtime, checkout_cmd)
    refs = kit.security_refs(
        "/docs/checkout.md",
        basket.get("path", ""),
        basket.get("store_path", ""),
        *(row.get("product_path", "") for row in line_rows),
    )
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "verified basket ownership",
                "checked all basket lines against current inventory",
                "ran checkout for the requested basket",
            ],
            message=f"Checkout completed for {basket_id}.",
            grounding_refs=refs,
            outcome="OUTCOME_OK",
        ),
    )
