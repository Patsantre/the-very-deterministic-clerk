from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ecom_task_classifier import TaskSpec


@dataclass(frozen=True)
class RefundSolverKit:
    req_exec: type
    report_completion: type
    runtime_identity: Callable[[Callable[[object], object]], tuple[str, set[str]]]
    payment_id_from_task: Callable[[str], str]
    auto_sql: Callable[[Callable[[object], object], str], tuple[list[dict[str, str]], str]]
    auto_call: Callable[[Callable[[object], object], object], tuple[object, str]]
    auto_finish: Callable[[Callable[[object], object], object], bool]
    sql_literal: Callable[[str], str]
    security_refs: Callable[..., list[str]]


def _return_id_from_task(task_text: str) -> str:
    match = re.search(r"\bret[_ -](\d+)\b", task_text, re.I)
    return f"ret_{match.group(1)}" if match else ""


def _amount_cents_from_task(task_text: str) -> int | None:
    currency = r"(?:eur|euros?|€)"
    match = re.search(rf"{currency}\s*([0-9]+)(?:[,.]([0-9]{{2}}))?", task_text, re.I)
    if not match:
        match = re.search(rf"\b([0-9]+)(?:[,.]([0-9]{{2}}))?\s*{currency}\b", task_text, re.I)
    if not match:
        return None
    return int(match.group(1)) * 100 + int(match.group(2) or "0")


def _clean_refs(refs: list[str]) -> list[str]:
    return [ref for ref in refs if ref]


def auto_refund_task(
    call_runtime,
    task_text: str,
    kit: RefundSolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "refund":
        return False

    user, roles = kit.runtime_identity(call_runtime)
    payment_id = task_spec.payment_id or kit.payment_id_from_task(task_text)
    return_id = _return_id_from_task(task_text)

    if payment_id:
        rows, _ = kit.auto_sql(
            call_runtime,
            """
select payment_id as id, record_path as path, customer_id, payment_status as status
from payment_transactions
where payment_id = {payment_id}
limit 1;
""".format(payment_id=kit.sql_literal(payment_id)).strip(),
        )
        if not rows:
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=["looked up requested payment"],
                    message=f"Refund is not supported because payment {payment_id} was not found.",
                    grounding_refs=["/docs/returns.md"],
                    outcome="OUTCOME_NONE_UNSUPPORTED",
                ),
            )
        return_rows, _ = kit.auto_sql(
            call_runtime,
            """
select return_id as id, record_path as path, customer_id, return_status as status
from return_requests
where payment_id = {payment_id}
order by return_created_at desc
limit 1;
""".format(payment_id=kit.sql_literal(payment_id)).strip(),
        )
        refs = ["/docs/returns.md", rows[0].get("path", "")]
        if return_rows:
            refs.append(return_rows[0].get("path", ""))
        if user.startswith("emp_") and "refund_manager" not in roles:
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked refund payment target",
                        "refused refund approval without refund role",
                    ],
                    message="Request denied: refund approval requires an authorized refund_manager role.",
                    grounding_refs=kit.security_refs("/docs/returns.md"),
                    outcome="OUTCOME_DENIED_SECURITY",
                ),
            )
        if (
            return_rows
            and user.startswith("cust_")
            and rows[0].get("customer_id") == user
            and rows[0].get("status") == "paid"
            and return_rows[0].get("status") == "refund_pending"
        ):
            refund_cmd = kit.req_exec(
                tool="exec",
                path="/bin/payments",
                args=["refund", return_rows[0]["id"]],
            )
            kit.auto_call(call_runtime, refund_cmd)
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked refund payment target",
                        "verified customer-owned paid payment and refund-pending return",
                        "finalized customer refund",
                    ],
                    message=f"Refund finalized for return {return_rows[0]['id']}.",
                    grounding_refs=kit.security_refs(*_clean_refs(refs)),
                    outcome="OUTCOME_OK",
                ),
            )
        if (
            return_rows
            and user.startswith("emp_")
            and "refund_manager" in roles
            and rows[0].get("status") == "paid"
            and return_rows[0].get("status") == "approved"
        ):
            approve_cmd = kit.req_exec(
                tool="exec",
                path="/bin/payments",
                args=["approve-refund", return_rows[0]["id"]],
            )
            kit.auto_call(call_runtime, approve_cmd)
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked refund payment target",
                        "verified approved return and refund_manager role",
                        "approved refund workflow",
                    ],
                    message=f"Refund approved for return {return_rows[0]['id']}.",
                    grounding_refs=kit.security_refs(*_clean_refs(refs)),
                    outcome="OUTCOME_OK",
                ),
            )
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["looked up requested payment refund target"],
                message=f"Refund approval for payment {payment_id} is not supported by this request.",
                grounding_refs=_clean_refs(refs),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    if return_id:
        rows, _ = kit.auto_sql(
            call_runtime,
            """
select r.return_id as id, r.record_path as path, r.basket_id, r.customer_id, r.payment_id,
       r.return_status as status,
       p.record_path as payment_path, p.payment_status as payment_status
from return_requests r
left join payment_transactions p on p.payment_id = r.payment_id
where r.return_id = {return_id}
limit 1;
""".format(return_id=kit.sql_literal(return_id)).strip(),
        )
        if not rows:
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=["looked up requested return"],
                    message=f"Refund is not supported because return {return_id} was not found.",
                    grounding_refs=["/docs/returns.md"],
                    outcome="OUTCOME_NONE_UNSUPPORTED",
                ),
            )
        if user.startswith("emp_") and "refund_manager" not in roles:
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked refund return target",
                        "refused refund approval without refund role",
                    ],
                    message="Request denied: refund approval requires an authorized refund_manager role.",
                    grounding_refs=kit.security_refs("/docs/returns.md"),
                    outcome="OUTCOME_DENIED_SECURITY",
                ),
            )
        if (
            user.startswith("emp_")
            and "refund_manager" in roles
            and rows[0].get("status") == "approved"
            and rows[0].get("payment_status") == "paid"
        ):
            approve_cmd = kit.req_exec(
                tool="exec",
                path="/bin/payments",
                args=["approve-refund", rows[0]["id"]],
            )
            kit.auto_call(call_runtime, approve_cmd)
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked refund return target",
                        "verified approved return and refund_manager role",
                        "approved refund workflow",
                    ],
                    message=f"Refund approved for return {rows[0]['id']}.",
                    grounding_refs=kit.security_refs(
                        "/docs/returns.md",
                        rows[0].get("path", ""),
                        rows[0].get("payment_path", ""),
                    ),
                    outcome="OUTCOME_OK",
                ),
            )
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["looked up requested return refund target"],
                message=f"Refund approval for return {return_id} is not supported by this request.",
                grounding_refs=_clean_refs(
                    [
                        "/docs/returns.md",
                        rows[0].get("path", ""),
                        rows[0].get("payment_path", ""),
                    ]
                ),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    amount_cents = _amount_cents_from_task(task_text)
    if amount_cents is not None:
        rows, _ = kit.auto_sql(
            call_runtime,
            """
select r.return_id as return_id, r.record_path as return_path, r.return_status as return_status,
       r.customer_id as return_customer_id,
       p.payment_id as payment_id, p.record_path as payment_path, p.payment_status as payment_status,
       b.basket_id as basket_id, b.record_path as basket_path, b.customer_id as basket_customer_id
from return_requests r
join payment_transactions p on p.payment_id = r.payment_id
left join shopping_baskets b on b.basket_id = p.basket_id
where r.customer_id = {user}
  and p.payment_amount_cents = {cents}
order by r.return_created_at desc
limit 5;
""".format(user=kit.sql_literal(user), cents=amount_cents).strip(),
        )
        refs = ["/docs/returns.md"]
        refs.extend(row.get("return_path", "") for row in rows)
        refs.extend(row.get("payment_path", "") for row in rows)
        refs.extend(row.get("basket_path", "") for row in rows)
        for row in rows:
            eligible = (
                user.startswith("cust_")
                and row.get("return_customer_id") == user
                and row.get("basket_customer_id") == user
                and row.get("payment_status") == "paid"
                and row.get("return_status") == "refund_pending"
            )
            if not eligible:
                continue
            refund_cmd = kit.req_exec(
                tool="exec",
                path="/bin/payments",
                args=["refund", row["return_id"]],
            )
            kit.auto_call(call_runtime, refund_cmd)
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "matched refund request by customer and amount",
                        "verified refund-pending return and paid payment",
                        "finalized customer refund",
                    ],
                    message=f"Refund finalized for return {row['return_id']}.",
                    grounding_refs=_clean_refs(["/docs/security.md", *refs]),
                    outcome="OUTCOME_OK",
                ),
            )
        if not rows:
            rows, _ = kit.auto_sql(
                call_runtime,
                """
select r.return_id as return_id, r.record_path as return_path, r.return_status as return_status,
       p.payment_id as payment_id, p.record_path as payment_path
from return_requests r
left join payment_transactions p on p.payment_id = r.payment_id
where r.customer_id = {user}
order by r.return_created_at desc
limit 5;
""".format(user=kit.sql_literal(user)).strip(),
            )
            refs.extend(row.get("return_path", "") for row in rows)
            refs.extend(row.get("payment_path", "") for row in rows)
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["looked up refund request by customer and amount"],
                message="Refund is not supported without an approved return workflow.",
                grounding_refs=_clean_refs(refs),
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=["checked refund request target"],
            message="Refund is not supported without an existing payment or return record to act on.",
            grounding_refs=["/docs/returns.md"],
            outcome="OUTCOME_NONE_UNSUPPORTED",
        ),
    )
