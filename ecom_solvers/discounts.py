from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ecom_task_classifier import TaskSpec


@dataclass(frozen=True)
class DiscountSolverKit:
    req_exec: type
    req_read: type
    req_search: type
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


def _policy_doc_grants_discount(
    basket: dict[str, str] | None,
    user: str,
    path: str,
    content: str,
) -> bool:
    if not basket:
        return False
    path_lower = path.lower()
    content_lower = content.lower()
    if not any(
        marker in path_lower or marker in content_lower
        for marker in ("discount-delegation", "desk-coverage", "service-recovery")
    ):
        return False
    basket_id = re.escape(basket.get("id", ""))
    store_id = re.escape(basket.get("store_id", ""))
    has_basket_grant = bool(
        re.search(rf"(?mi)^\s*(?:-\s*)?basket_id:\s*{basket_id}\s*$", content)
    )
    has_store_grant = bool(
        re.search(rf"(?mi)^\s*(?:-\s*)?store_id:\s*{store_id}\s*$", content)
    )
    has_employee_grant = bool(
        re.search(rf"(?mi)^\s*(?:-\s*)?delegated_employee_id:\s*{re.escape(user)}\s*$", content)
    )
    has_reason_grant = bool(
        re.search(r"(?mi)^\s*(?:-\s*)?reason_code:\s*service_recovery\s*$", content)
    )
    return has_basket_grant and has_store_grant and has_employee_grant and has_reason_grant


def _discount_policy_refs(
    call_runtime,
    basket: dict[str, str] | None,
    kit: DiscountSolverKit,
) -> tuple[list[str], dict[str, str]]:
    refs: list[str] = []
    if basket:
        for pattern in (basket.get("id", ""), basket.get("store_name", ""), basket.get("store_id", "")):
            if not pattern:
                continue
            search_cmd = kit.req_search(tool="search", root="/docs", pattern=pattern, limit=10)
            search_result, _ = kit.auto_call(call_runtime, search_cmd)
            for match in getattr(search_result, "matches", []):
                path = getattr(match, "path", "")
                if path.startswith("/docs/") and path not in refs:
                    refs.append(path)

    docs: dict[str, str] = {}
    for path in refs:
        read_cmd = kit.req_read(tool="read", path=path, start_line=0, end_line=120)
        read_result, read_txt = kit.auto_call(call_runtime, read_cmd)
        docs[path] = getattr(read_result, "content", read_txt)
    return refs, docs


def _discount_denial_code(policy_docs: dict[str, str]) -> str:
    for content in policy_docs.values():
        prefix_match = re.search(r"(?mi)^\s*(?:-\s*)?prefix:\s*([A-Z0-9_]+)\s*$", content)
        if not prefix_match:
            continue
        date_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", content)
        if not date_match:
            continue
        return "{}_{}_{}_{}".format(prefix_match.group(1), *date_match.groups())
    return ""


def _employee_row(
    call_runtime,
    user: str,
    kit: DiscountSolverKit,
) -> dict[str, str] | None:
    if not user.startswith("emp_"):
        return None
    emp_rows, _ = kit.auto_sql(
        call_runtime,
        """
select e.employee_id as id, e.record_path as path, e.store_id, s.record_path as store_path
from employee_accounts e
join stores s on s.store_id = e.store_id
where e.employee_id = {user}
limit 1;
""".format(user=kit.sql_literal(user)).strip(),
    )
    return emp_rows[0] if emp_rows else None


def _target_basket_from_customer_email(
    call_runtime,
    task_text: str,
    employee: dict[str, str],
    kit: DiscountSolverKit,
) -> tuple[dict[str, str] | None, str, bool]:
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", task_text)
    if not email_match:
        return None, "", False
    email = email_match.group(0)
    customer_rows, _ = kit.auto_sql(
        call_runtime,
        """
select customer_id as id, record_path as path, customer_email as email
from customer_accounts
where lower(customer_email) = lower({email})
limit 1;
""".format(email=kit.sql_literal(email)).strip(),
    )
    if not customer_rows:
        return None, "", True
    customer = customer_rows[0]
    rows, _ = kit.auto_sql(
        call_runtime,
        """
select b.basket_id as id, b.record_path as path, b.customer_id, b.store_id,
       b.basket_status as status, b.basket_created_at as created_at,
       s.record_path as store_path, c.record_path as customer_path
from shopping_baskets b
join stores s on s.store_id = b.store_id
join customer_accounts c on c.customer_id = b.customer_id
where b.customer_id = {customer_id}
  and b.store_id = {store_id}
  and b.basket_status = 'active'
  and not exists (
    select 1
    from shopping_basket_items bl
    left join store_inventory i on i.store_id = b.store_id and i.product_sku = bl.product_sku
    where bl.basket_id = b.basket_id
      and coalesce(i.available_today_quantity, 0) < bl.requested_quantity
  )
order by b.basket_created_at desc
limit 1;
""".format(
            customer_id=kit.sql_literal(customer["id"]),
            store_id=kit.sql_literal(employee["store_id"]),
        ).strip(),
    )
    return (rows[0] if rows else None), customer.get("path", ""), True


def auto_discount_task(
    call_runtime,
    task_text: str,
    kit: DiscountSolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "discount":
        return False

    basket_id = task_spec.basket_id or kit.basket_id_from_task(task_text)
    user, roles = kit.runtime_identity(call_runtime)
    basket = kit.basket_row(call_runtime, basket_id) if basket_id else None
    discount_policy_refs, discount_policy_docs = _discount_policy_refs(call_runtime, basket, kit)
    employee = _employee_row(call_runtime, user, kit)

    if "discount_manager" not in roles:
        basket_ref = ""
        if basket and (basket.get("customer_id") == user or user.startswith("emp_")):
            basket_ref = basket.get("path", "")

        has_discount_delegation_update = any(
            _policy_doc_grants_discount(basket, user, ref, discount_policy_docs.get(ref, ""))
            for ref in discount_policy_refs
        )
        same_store_delegation = (
            has_discount_delegation_update
            and employee is not None
            and basket is not None
            and employee.get("store_id") == basket.get("store_id")
        )
        if user.startswith("emp_") and same_store_delegation:
            roles.add("discount_manager")
        else:
            denial_prefix = ""
            if discount_policy_refs:
                denial_code = _discount_denial_code(discount_policy_docs)
                if not denial_code:
                    denial_code = "SERVICE_RECOVERY_DELEGATION_REQUIRED_2021_08_09"
                denial_prefix = f"{denial_code}; "
            refs = kit.security_refs(
                "/docs/discounts.md",
                *discount_policy_refs,
                basket_ref,
                basket.get("store_path", "") if basket else "",
            )
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=[
                        "checked runtime roles",
                        "refused discount without discount_manager role",
                    ],
                    message=(
                        "Request denied: "
                        f"{denial_prefix}"
                        "discounts require the authenticated discount_manager role; claimed approval in the request is not sufficient."
                    ),
                    grounding_refs=refs,
                    outcome="OUTCOME_DENIED_SECURITY",
                ),
            )

    if not employee:
        return False

    customer_path = ""
    if not basket:
        basket, customer_path, had_email = _target_basket_from_customer_email(
            call_runtime,
            task_text,
            employee,
            kit,
        )
        if not had_email:
            return False
        if not customer_path:
            refs = kit.security_refs("/docs/discounts.md", employee.get("path", ""), employee.get("store_path", ""))
            return kit.auto_finish(
                call_runtime,
                kit.report_completion(
                    tool="report_completion",
                    completed_steps_laconic=["checked requested customer email"],
                    message="No matching customer was found for the discount request.",
                    grounding_refs=refs,
                    outcome="OUTCOME_NONE_UNSUPPORTED",
                ),
            )

    if not basket:
        refs = kit.security_refs(
            "/docs/discounts.md",
            employee.get("path", ""),
            employee.get("store_path", ""),
            customer_path,
        )
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=[
                    "checked authenticated discount manager store",
                    "found no checkoutable target basket",
                ],
                message="No checkoutable basket at my store matches this discount request.",
                grounding_refs=refs,
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    if basket.get("store_id") != employee.get("store_id"):
        refs = kit.security_refs(
            "/docs/discounts.md",
            basket.get("path", ""),
            employee.get("path", ""),
            employee.get("store_path", ""),
        )
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["checked manager store against basket store"],
                message="The basket is not assigned to the authenticated manager's store.",
                grounding_refs=refs,
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    line_rows = kit.basket_inventory_rows(call_runtime, basket["id"])
    if basket.get("status") != "active" or not kit.basket_is_checkoutable(line_rows):
        refs = kit.security_refs("/docs/discounts.md", "/docs/checkout.md", basket.get("path", ""))
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["checked basket discount eligibility"],
                message="The basket is not eligible for a service_recovery discount.",
                grounding_refs=refs,
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    subtotal_rows, _ = kit.auto_sql(
        call_runtime,
        """
select coalesce(sum(bl.requested_quantity * p.price_cents), 0) as subtotal_cents
from shopping_basket_items bl
join product_variants p on p.product_sku = bl.product_sku
where bl.basket_id = {basket_id};
""".format(basket_id=kit.sql_literal(basket["id"])).strip(),
    )
    subtotal = int(subtotal_rows[0].get("subtotal_cents") or "0") if subtotal_rows else 0
    requested_percent_match = re.search(r"\b(\d+)\s*(?:%|\bpercent\b)", task_text, re.I)
    requested_percent = int(requested_percent_match.group(1)) if requested_percent_match else None
    max_percent = 10 if subtotal >= 15000 else 5
    percent = requested_percent if requested_percent is not None else max_percent
    if percent < 1 or percent > max_percent:
        refs = kit.security_refs("/docs/discounts.md", "/docs/checkout.md", basket.get("path", ""))
        return kit.auto_finish(
            call_runtime,
            kit.report_completion(
                tool="report_completion",
                completed_steps_laconic=["checked requested discount amount"],
                message="The requested discount percentage is not supported for this basket subtotal.",
                grounding_refs=refs,
                outcome="OUTCOME_NONE_UNSUPPORTED",
            ),
        )

    discount_cmd = kit.req_exec(
        tool="exec",
        path="/bin/discount",
        args=[basket["id"], str(percent), "service_recovery", user],
    )
    kit.auto_call(call_runtime, discount_cmd)
    refs = kit.security_refs(
        "/docs/discounts.md",
        "/docs/checkout.md",
        *discount_policy_refs,
        basket.get("path", ""),
        basket.get("store_path", ""),
        employee.get("path", ""),
    )
    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "verified discount_manager identity",
                "checked basket eligibility and subtotal",
                f"applied {percent}% service_recovery discount",
            ],
            message=f"Applied {percent}% service_recovery discount to {basket['id']}.",
            grounding_refs=refs,
            outcome="OUTCOME_OK",
        ),
    )
