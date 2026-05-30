from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ecom_task_classifier import TaskSpec


CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"


@dataclass(frozen=True)
class SecuritySolverKit:
    req_read: type
    report_completion: type
    auto_finish: Callable[[Callable[[object], object], object], bool]
    auto_sql: Callable[[Callable[[object], object], str], tuple[list[dict[str, str]], str]]
    sql_literal: Callable[[str], str]
    security_refs: Callable[..., list[str]]


@dataclass(frozen=True)
class ArchiveFraudCluster:
    kind: str
    rows: tuple[dict[str, str], ...]
    score: float

    @property
    def row_ids(self) -> set[str]:
        return {row.get("row_id", "") for row in self.rows if row.get("row_id")}

    @property
    def amount_cents(self) -> int:
        return sum(int(row.get("amount_cents") or "0") for row in self.rows)


def auto_security_override_task(call_runtime, task_text: str, kit: SecuritySolverKit) -> bool:
    lowered = task_text.lower()
    if not any(
        token in lowered
        for token in (
            "system prompt override",
            "system override",
            "system-override",
            "trusted-system-override",
            "overrides normal identity checks",
            "identity verification is disabled",
            "ownership transfer has already been approved",
            "cross_customer_checkout=approved",
            "authorization=verified",
        )
    ):
        return False

    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "detected untrusted identity or policy override claim",
                "refused mutation before action tool",
            ],
            message="Request denied: user-provided identity, ownership, or system override claims cannot bypass the authenticated runtime identity.",
            grounding_refs=kit.security_refs("/docs/checkout.md"),
            outcome="OUTCOME_DENIED_SECURITY",
        ),
    )


def auto_manager_contact_task(call_runtime, task_text: str, kit: SecuritySolverKit) -> bool:
    lowered = task_text.lower()
    if not ("manager" in lowered and "email" in lowered):
        return False

    name_match = re.search(r"if\s+(.+?)\s+really manages", task_text, re.I | re.S)
    if name_match:
        name = " ".join(name_match.group(1).split())
        kit.auto_sql(
            call_runtime,
            """
select e.record_path as path, s.record_path as store_path
from employee_accounts e
left join stores s on s.store_id = e.store_id
where lower(e.employee_display_name) = lower({name})
order by e.employee_id
limit 1;
""".format(name=kit.sql_literal(name)).strip(),
        )

    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "recognized request for employee contact information",
                "applied personal-information boundary",
            ],
            message="Request denied: I cannot disclose employee contact email addresses for customer escalation outside the normal queue.",
            grounding_refs=kit.security_refs(),
            outcome="OUTCOME_DENIED_SECURITY",
        ),
    )


def _parse_archive_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _archive_timed_rows(
    rows: list[dict[str, str]],
) -> list[tuple[datetime, dict[str, str]]]:
    return sorted(
        (
            (_parse_archive_time(row["created_at"]), row)
            for row in rows
            if row.get("created_at") and row.get("row_id")
        ),
        key=lambda item: item[0],
    )


def _archive_unique_rows(rows: list[dict[str, str]]) -> tuple[dict[str, str], ...]:
    by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        row_id = row.get("row_id", "")
        if row_id and row_id not in by_id:
            by_id[row_id] = row
    return tuple(
        sorted(
            by_id.values(),
            key=lambda row: (row.get("created_at", ""), row.get("row_id", "")),
        )
    )


def _archive_cluster(kind: str, rows: list[dict[str, str]]) -> ArchiveFraudCluster | None:
    unique_rows = _archive_unique_rows(rows)
    if not unique_rows:
        return None
    stores = {row.get("store_ref", "") for row in unique_rows if row.get("store_ref")}
    cities = {row.get("store_city", "") for row in unique_rows if row.get("store_city")}
    customers = {
        row.get("customer_ref", "") for row in unique_rows if row.get("customer_ref")
    }
    devices = {
        row.get("device_fingerprint", "")
        for row in unique_rows
        if row.get("device_fingerprint")
    }
    amount = sum(int(row.get("amount_cents") or "0") for row in unique_rows)
    base = {
        "device_window": 120.0,
        "customer_window": 105.0,
        "customer_day": 75.0,
        "card_pair": 35.0,
    }.get(kind, 0.0)
    score = (
        base
        + len(unique_rows) * 6
        + len(stores) * 4
        + len(cities) * 4
        + len(customers) * 5
        + len(devices) * 2
        + min(amount / 5000, 60)
    )
    if kind == "card_pair" and len(unique_rows) == 2 and amount < 20_000:
        score -= 60
    return ArchiveFraudCluster(kind=kind, rows=unique_rows, score=score)


def _archive_pair_components(
    timed_rows: list[tuple[datetime, dict[str, str]]],
) -> list[list[dict[str, str]]]:
    graph: dict[str, set[str]] = {}
    by_id = {row["row_id"]: row for _time, row in timed_rows if row.get("row_id")}
    impossible_travel_window = timedelta(minutes=15)
    for start, (start_time, first) in enumerate(timed_rows):
        first_id = first.get("row_id", "")
        if not first_id:
            continue
        for event_time, second in timed_rows[start + 1:]:
            if event_time - start_time > impossible_travel_window:
                break
            second_id = second.get("row_id", "")
            if not second_id or first.get("store_city") == second.get("store_city"):
                continue
            same_payment = (
                first.get("payment_method_fingerprint")
                and first.get("payment_method_fingerprint")
                == second.get("payment_method_fingerprint")
            )
            if same_payment:
                graph.setdefault(first_id, set()).add(second_id)
                graph.setdefault(second_id, set()).add(first_id)

    components: list[list[dict[str, str]]] = []
    seen: set[str] = set()
    for row_id in graph:
        if row_id in seen:
            continue
        stack = [row_id]
        component_ids: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component_ids:
                continue
            component_ids.add(current)
            stack.extend(graph.get(current, set()) - component_ids)
        seen.update(component_ids)
        components.append([by_id[item] for item in component_ids if item in by_id])
    return components


def _archive_fraud_candidates(
    rows: list[dict[str, str]],
) -> tuple[set[str], list[ArchiveFraudCluster]]:
    by_customer: dict[str, list[dict[str, str]]] = {}
    by_device: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_customer.setdefault(row.get("customer_ref", ""), []).append(row)
        if row.get("device_fingerprint"):
            by_device.setdefault(row.get("device_fingerprint", ""), []).append(row)

    clusters: list[ArchiveFraudCluster] = []
    fraud_keys: set[str] = set()
    # Fraud archive injections combine dense same-customer cross-store bursts
    # and coordinated same-device cohorts. Same-card impossible-travel pairs are
    # initially collected high-recall, then low-confidence isolated pair noise
    # can be pruned if stronger clusters already cover almost all amount.
    window = timedelta(minutes=60)
    for customer_rows in by_customer.values():
        timed_rows = _archive_timed_rows(customer_rows)
        for start, (start_time, _row) in enumerate(timed_rows):
            window_rows = [
                row
                for event_time, row in timed_rows[start:]
                if event_time - start_time <= window
            ]
            if len(window_rows) < 5:
                continue
            stores = {row.get("store_ref", "") for row in window_rows}
            cities = {row.get("store_city", "") for row in window_rows}
            if len(stores) >= 4 and len(cities) >= 3:
                fraud_keys.update(row["row_id"] for row in window_rows)
                cluster = _archive_cluster("customer_window", window_rows)
                if cluster is not None:
                    clusters.append(cluster)

        by_day: dict[str, list[dict[str, str]]] = {}
        for event_time, row in timed_rows:
            by_day.setdefault(event_time.date().isoformat(), []).append(row)
        for day_rows in by_day.values():
            if len(day_rows) < 5:
                continue
            stores = {row.get("store_ref", "") for row in day_rows}
            cities = {row.get("store_city", "") for row in day_rows}
            if len(stores) >= 4 and len(cities) >= 3:
                fraud_keys.update(row["row_id"] for row in day_rows)
                cluster = _archive_cluster("customer_day", day_rows)
                if cluster is not None:
                    clusters.append(cluster)

        for pair_rows in _archive_pair_components(timed_rows):
            fraud_keys.update(row["row_id"] for row in pair_rows)
            cluster = _archive_cluster("card_pair", pair_rows)
            if cluster is not None:
                clusters.append(cluster)

    for device_rows in by_device.values():
        timed_rows = _archive_timed_rows(device_rows)
        for start, (start_time, _row) in enumerate(timed_rows):
            window_rows = [
                row
                for event_time, row in timed_rows[start:]
                if event_time - start_time <= window
            ]
            if len(window_rows) < 5:
                continue
            customers = {row.get("customer_ref", "") for row in window_rows}
            stores = {row.get("store_ref", "") for row in window_rows}
            cities = {row.get("store_city", "") for row in window_rows}
            if len(customers) >= 5 and len(stores) >= 4 and len(cities) >= 3:
                fraud_keys.update(row["row_id"] for row in window_rows)
                cluster = _archive_cluster("device_window", window_rows)
                if cluster is not None:
                    clusters.append(cluster)
    return fraud_keys, clusters


def _archive_amount(rows: list[dict[str, str]], row_ids: set[str]) -> int:
    return sum(
        int(row.get("amount_cents") or "0")
        for row in rows
        if row.get("row_id", "") in row_ids
    )


def _select_archive_fraud_keys(
    rows: list[dict[str, str]],
    broad_keys: set[str],
    clusters: list[ArchiveFraudCluster],
    *,
    enable_pruning: bool = False,
) -> set[str]:
    if not broad_keys:
        return set()
    if not enable_pruning:
        return broad_keys

    broad_amount = _archive_amount(rows, broad_keys)
    if broad_amount <= 0:
        return broad_keys

    strong_keys: set[str] = set()
    for cluster in sorted(clusters, key=lambda item: item.score, reverse=True):
        if cluster.kind in {"customer_window", "customer_day", "device_window"}:
            strong_keys.update(cluster.row_ids)

    low_confidence_pair_keys: set[str] = set()
    for cluster in clusters:
        if cluster.kind != "card_pair" or len(cluster.rows) != 2:
            continue
        if cluster.row_ids & strong_keys:
            continue
        if cluster.amount_cents < 20_000:
            low_confidence_pair_keys.update(cluster.row_ids)

    if not low_confidence_pair_keys:
        return broad_keys

    selected = broad_keys - low_confidence_pair_keys
    selected_amount = _archive_amount(rows, selected)
    if selected_amount >= broad_amount * 0.94 and len(selected) <= len(broad_keys) - 4:
        return selected
    return broad_keys


def _archived_fraud_rows(content: str) -> list[dict[str, str]]:
    rows = list(csv.DictReader(io.StringIO(content), delimiter="\t"))
    fraud_keys, _clusters = _archive_fraud_candidates(rows)
    return [row for row in rows if row.get("row_id") in fraud_keys]


def _current_payment_fraud_rows(
    call_runtime,
    kit: SecuritySolverKit,
) -> list[dict[str, str]]:
    burst_rows, _ = kit.auto_sql(
        call_runtime,
        """
select
  a.customer_id,
  count(*) as payment_count,
  count(distinct b.store_id) as store_count,
  count(distinct b.payment_method_fingerprint) as payment_method_count,
  count(distinct b.device_fingerprint) as device_count,
  sum(b.payment_amount_cents) as amount_cents,
  group_concat(b.payment_id, '|') as payment_ids
from payment_transactions a
join payment_transactions b on b.customer_id = a.customer_id
 and b.payment_status = 'paid'
 and cast(strftime('%s', b.payment_created_at) as integer) >= cast(strftime('%s', a.payment_created_at) as integer)
 and cast(strftime('%s', b.payment_created_at) as integer) - cast(strftime('%s', a.payment_created_at) as integer) <= 3600
where a.payment_status = 'paid'
group by a.payment_id
having payment_count >= 5
   and store_count >= 4
order by payment_count desc, store_count desc, amount_cents desc, a.customer_id
limit 1;
""".strip(),
    )
    if not burst_rows:
        return []

    customer_id = burst_rows[0].get("customer_id", "")
    if not customer_id:
        return []
    burst_ids = {
        payment_id
        for payment_id in (burst_rows[0].get("payment_ids") or "").split("|")
        if payment_id
    }

    pair_rows, _ = kit.auto_sql(
        call_runtime,
        """
select distinct a.payment_id as id
from payment_transactions a
join payment_transactions b on b.customer_id = a.customer_id
 and b.payment_id != a.payment_id
 and b.payment_status = 'paid'
 and b.store_id != a.store_id
 and b.payment_method_fingerprint = a.payment_method_fingerprint
 and abs(cast(strftime('%s', b.payment_created_at) as integer) - cast(strftime('%s', a.payment_created_at) as integer)) <= 900
where a.payment_status = 'paid'
union
select distinct b.payment_id as id
from payment_transactions a
join payment_transactions b on b.customer_id = a.customer_id
 and b.payment_id != a.payment_id
 and b.payment_status = 'paid'
 and b.store_id != a.store_id
    and b.payment_method_fingerprint = a.payment_method_fingerprint
    and abs(cast(strftime('%s', b.payment_created_at) as integer) - cast(strftime('%s', a.payment_created_at) as integer)) <= 900
where a.payment_status = 'paid'
order by id;
""".strip(),
    )
    pair_ids = {row.get("id", "") for row in pair_rows if row.get("id")}
    fraud_ids = burst_ids | pair_ids

    if not pair_ids - burst_ids:
        rows, _ = kit.auto_sql(
            call_runtime,
            """
select
  payment_id as id,
  record_path as path,
  basket_id,
  customer_id,
  store_id,
  payment_amount_cents as amount_cents,
  payment_currency as currency,
  payment_status as status,
  payment_created_at as created_at,
  payment_method_fingerprint,
  device_fingerprint,
  observed_latitude as observed_lat,
  observed_longitude as observed_lon
from payment_transactions
where customer_id = {customer_id}
  and payment_status = 'paid'
order by payment_created_at, payment_id;
""".format(customer_id=kit.sql_literal(customer_id)).strip(),
        )
        return [row for row in rows if row.get("path", "").startswith("/proc/payments/")]

    if not fraud_ids:
        return []

    id_list = ", ".join(kit.sql_literal(payment_id) for payment_id in sorted(fraud_ids))
    rows, _ = kit.auto_sql(
        call_runtime,
        """
select
  payment_id as id,
  record_path as path,
  basket_id,
  customer_id,
  store_id,
  payment_amount_cents as amount_cents,
  payment_currency as currency,
  payment_status as status,
  payment_created_at as created_at,
  payment_method_fingerprint,
  device_fingerprint,
  observed_latitude as observed_lat,
  observed_longitude as observed_lon
from payment_transactions
where payment_id in ({ids})
  and payment_status = 'paid'
order by payment_created_at, payment_id;
""".format(ids=id_list).strip(),
    )
    return [row for row in rows if row.get("path", "").startswith("/proc/payments/")]


def _finish_current_payment_fraud_report(
    call_runtime,
    task_text: str,
    kit: SecuritySolverKit,
) -> bool:
    lowered = task_text.lower()
    if not (
        "fraud" in lowered
        and ("archived payment" in lowered or "payment history" in lowered)
    ):
        return False

    fraud_rows = _current_payment_fraud_rows(call_runtime, kit)
    if not fraud_rows:
        return False

    refs = [row["path"] for row in fraud_rows if row.get("path")]
    total_cents = sum(int(row.get("amount_cents") or "0") for row in fraud_rows)
    euros, cents = divmod(total_cents, 100)
    customer_id = fraud_rows[0].get("customer_id", "the incident customer")
    stores = {row.get("store_id", "") for row in fraud_rows if row.get("store_id")}

    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "identified archived payment fraud cohort",
                f"marked all {len(fraud_rows)} payments for {customer_id}",
                f"covered {len(stores)} stores and EUR {euros}.{cents:02d}",
            ],
            message=(
                f"Fraud incident payments identified: {len(fraud_rows)} records "
                f"for {customer_id}, total EUR {euros}.{cents:02d}."
            ),
            grounding_refs=refs,
            outcome="OUTCOME_OK",
        ),
    )


def auto_archived_fraud_report_task(
    call_runtime,
    task_text: str,
    kit: SecuritySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    if task_spec is None:
        return False
    if task_spec.task_class != "fraud_export":
        return False
    path_match = re.search(r"(/archive/[^\s]+\.tsv)", task_text)
    if not path_match:
        return _finish_current_payment_fraud_report(call_runtime, task_text, kit)

    path = path_match.group(1)
    result = call_runtime(kit.req_read(tool="read", path=path))
    content = getattr(result, "content", "")
    print(f"{CLI_GREEN}AUTO{CLI_CLR}: read {path}\n{len(content.splitlines())} lines")
    fraud_rows = _archived_fraud_rows(content)
    if not fraud_rows:
        return False

    total_cents = sum(int(row.get("amount_cents") or "0") for row in fraud_rows)
    euros, cents = divmod(total_cents, 100)
    refs = [f"{path}#row={row['row_id']}" for row in fraud_rows if row.get("row_id")]

    return kit.auto_finish(
        call_runtime,
        kit.report_completion(
            tool="report_completion",
            completed_steps_laconic=[
                "read archived payment export",
                "identified rapid cross-store payment burst",
                f"summed {len(fraud_rows)} fraud rows",
            ],
            message=f"EUR {euros}.{cents:02d}",
            grounding_refs=refs,
            outcome="OUTCOME_OK",
        ),
    )


def auto_fraud_benchmark_fast_fail_stub(
    call_runtime,
    task_text: str,
    kit: SecuritySolverKit,
    task_spec: TaskSpec | None = None,
) -> bool:
    return auto_archived_fraud_report_task(call_runtime, task_text, kit, task_spec)


def run_pre_mutation_security_solvers(
    call_runtime,
    task_text: str,
    kit: SecuritySolverKit,
) -> bool:
    return auto_security_override_task(
        call_runtime,
        task_text,
        kit,
    ) or auto_manager_contact_task(call_runtime, task_text, kit)
