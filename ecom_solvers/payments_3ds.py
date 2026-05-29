from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ecom_task_classifier import TaskSpec


@dataclass(frozen=True)
class ThreeDsSolverKit:
    req_exec: type
    req_read: type
    req_search: type
    report_completion: type
    basket_id_from_task: Callable[[str], str]
    payment_id_from_task: Callable[[str], str]
    runtime_identity: Callable[[Callable[[object], object]], tuple[str, set[str]]]
    auto_sql: Callable[[Callable[[object], object], str], tuple[list[dict[str, str]], str]]
    auto_call: Callable[[Callable[[object], object], object], tuple[object, str]]
    auto_finish: Callable[[Callable[[object], object], object], bool]
    sql_literal: Callable[[str], str]
    security_refs: Callable[..., list[str]]


def _policy_refs_and_retry_window(
    call_runtime,
    payment_id: str,
    kit: ThreeDsSolverKit,
) -> tuple[list[str], str]:
    policy_refs: list[str] = []
    retry_available_at = ""
    search_cmd = kit.req_search(tool="search", root="/docs", pattern=payment_id, limit=10)
    search_result, _ = kit.auto_call(call_runtime, search_cmd)
    for match in getattr(search_result, "matches", []):
        path = getattr(match, "path", "")
        if not path.startswith("/docs/") or path in policy_refs:
            continue
        policy_refs.append(path)
        read_cmd = kit.req_read(tool="read", path=path, start_line=1, end_line=80)
        read_result, _ = kit.auto_call(call_runtime, read_cmd)
        content = getattr(read_result, "content", "")
        retry_match = re.search(r"retry_available_at:\s*(\S+)", content)
        if not retry_match:
            retry_match = re.search(
                r"only after\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)",
                content,
                re.I,
            )
        if retry_match:
            retry_available_at = retry_match.group(1)
    return policy_refs, retry_available_at


def auto_3ds_recovery_task(
    call_runtime,
    task_text: str,
    kit: ThreeDsSolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "three_ds_recovery":
        return False

    basket_id = task_spec.basket_id or kit.basket_id_from_task(task_text)
    payment_id = task_spec.payment_id or kit.payment_id_from_task(task_text)
    if not payment_id and not basket_id:
        return False
    user, _roles = kit.runtime_identity(call_runtime)

    where = (
        f"p.payment_id = {kit.sql_literal(payment_id)}"
        if payment_id
        else f"p.basket_id = {kit.sql_literal(basket_id)}"
    )
    rows, _ = kit.auto_sql(
        call_runtime,
        f"""
select
  p.payment_id as id,
  p.record_path as path,
  p.basket_id,
  p.customer_id,
  p.payment_status as status,
  p.three_ds_status,
  p.three_ds_failure_reason,
  p.three_ds_attempts,
  p.three_ds_max_attempts,
  b.record_path as basket_path,
  b.basket_status as basket_status,
  b.customer_id as basket_customer_id
from payment_transactions p
join shopping_baskets b on b.basket_id = p.basket_id
where {where}
order by p.payment_created_at desc
limit 1;
""".strip(),
    )
    if not rows:
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["looked up requested 3DS payment"],
                message="No matching payment was found for 3DS recovery.",
                grounding_refs=kit.security_refs(
                    "/docs/checkout.md",
                    "/docs/payments/3ds.md",
                ),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )
    payment = rows[0]
    policy_refs, retry_available_at = _policy_refs_and_retry_window(
        call_runtime,
        payment["id"],
        kit,
    )

    if basket_id and payment.get("basket_id") != basket_id:
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["checked payment against requested basket"],
                message="The payment does not belong to the requested basket.",
                grounding_refs=kit.security_refs(
                    "/docs/checkout.md",
                    "/docs/payments/3ds.md",
                    *policy_refs,
                    payment.get("path", ""),
                    payment.get("basket_path", ""),
                ),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )
    if user != payment.get("customer_id") or user != payment.get("basket_customer_id"):
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=[
                    "checked authenticated customer against basket and payment",
                    "refused cross-customer payment recovery",
                ],
                message="Request denied: the authenticated user does not own this basket/payment.",
                grounding_refs=kit.security_refs(
                    "/docs/checkout.md",
                    "/docs/payments/3ds.md",
                    *policy_refs,
                ),
                outcome="OUTCOME_DENIED_SECURITY",
            ),
        )

    if retry_available_at:
        date_cmd = kit.req_exec(tool="exec", path="/bin/date")
        date_result, _ = kit.auto_call(call_runtime, date_cmd)
        now = getattr(date_result, "stdout", "").strip()
        if now and now < retry_available_at:
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked dated 3DS retry policy",
                        "found retry window is still locked",
                    ],
                    message=(
                        f"3DS recovery for {payment['id']} is not supported until "
                        f"{retry_available_at}."
                    ),
                    grounding_refs=kit.security_refs(
                        "/docs/checkout.md",
                        "/docs/payments/3ds.md",
                        *policy_refs,
                        payment.get("path", ""),
                        payment.get("basket_path", ""),
                    ),
                    outcome="OUTCOME_NONE_UNSUPPORTED",
                ),
            )

    try:
        attempts = int(payment.get("three_ds_attempts") or "0")
        max_attempts = int(payment.get("three_ds_max_attempts") or "0")
    except ValueError:
        attempts = max_attempts = 0
    eligible = (
        payment.get("basket_status") == "checked_out"
        and payment.get("status") == "requires_3ds_action"
        and payment.get("three_ds_status") in {"3ds-status2", "3ds-status3"}
        and attempts < max_attempts
    )
    if not eligible:
        if payment.get("status") == "paid":
            message = f"Payment {payment['id']} is already paid, so 3DS recovery is not supported."
        else:
            message = "This payment is not eligible for 3DS recovery."
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["checked 3DS recovery eligibility"],
                message=message,
                grounding_refs=kit.security_refs(
                    "/docs/checkout.md",
                    "/docs/payments/3ds.md",
                    *policy_refs,
                    payment.get("path", ""),
                    payment.get("basket_path", ""),
                ),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    recover_cmd = kit.req_exec(tool="exec", path="/bin/payments", args=["recover-3ds", payment["id"]])
    kit.auto_call(call_runtime, recover_cmd)
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "verified basket/payment ownership",
                "checked recoverable 3DS status and attempts",
                "started a fresh 3DS challenge",
            ],
            message=f"3DS recovery started for {payment['id']}.",
            grounding_refs=kit.security_refs(
                "/docs/checkout.md",
                "/docs/payments/3ds.md",
                *policy_refs,
                payment.get("path", ""),
                payment.get("basket_path", ""),
            ),
            outcome="OUTCOME_OK",
        ),
    )
