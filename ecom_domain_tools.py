from __future__ import annotations

import csv
import io

from bitgn.vm.ecom.ecom_pb2 import ExecRequest

from ecom_parsers import store_name_alias


SQL_TMPDIR_FALLBACKS = ("/work/tmp", "/tmp/mount")


PROPERTY_KEY_ALIASES = {
    "volume_l": ["volume_l", "tank_volume_l"],
    "cleaner_type": ["cleaner_type", "cleaning_type"],
    "cleaning_type": ["cleaning_type", "cleaner_type"],
}


def render_command(command: str, body: str) -> str:
    return f"{command}\n{body}"


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_in(values) -> str:
    return ", ".join(sql_literal(value) for value in values)


def property_keys(key: str) -> list[str]:
    return PROPERTY_KEY_ALIASES.get(key, [key])


def csv_rows(stdout: str) -> list[dict[str, str]]:
    if not stdout.strip():
        return []
    return list(csv.DictReader(io.StringIO(stdout)))


def exec_sql(vm, sql: str):
    result = vm.exec(ExecRequest(path="/bin/sql", stdin=sql))
    stderr = (getattr(result, "stderr", "") or "").lower()
    stdout = getattr(result, "stdout", "") or ""
    if stdout and "no space left on device" not in stderr:
        return result
    last_result = result
    for tmpdir in SQL_TMPDIR_FALLBACKS:
        retry = vm.exec(ExecRequest(path="/bin/sql", args=["--tmpdir", tmpdir], stdin=sql))
        retry_stderr = (getattr(retry, "stderr", "") or "").lower()
        retry_stdout = getattr(retry, "stdout", "") or ""
        last_result = retry
        if retry_stdout and "no space left on device" not in retry_stderr:
            return retry
    return last_result


def catalogue_lookup(vm, cmd) -> str:
    where = [f"lower(f.product_family_name) = lower({sql_literal(cmd.family_name)})"]
    if cmd.brand:
        where.append(f"lower(f.brand) = lower({sql_literal(cmd.brand)})")
    if cmd.kind_name:
        where.append(f"lower(k.product_kind_name) = lower({sql_literal(cmd.kind_name)})")

    exists = []
    for constraint in cmd.constraints:
        keys = sql_in(property_keys(constraint.key))
        if constraint.value_number is not None:
            exists.append(
                "exists (select 1 from product_variant_properties pp "
                f"where pp.product_sku = p.product_sku and pp.property_key in ({keys}) "
                f"and pp.property_value_number = {constraint.value_number})"
            )
        elif constraint.value_text:
            exists.append(
                "exists (select 1 from product_variant_properties pp "
                f"where pp.product_sku = p.product_sku and pp.property_key in ({keys}) "
                f"and lower(pp.property_value_text) = lower({sql_literal(constraint.value_text)}))"
            )

    exact_sql = (
        "select p.product_sku as sku, p.record_path as path, p.product_name as name, "
        "f.product_family_id as family_id, f.product_family_name as family_name "
        "from product_variants p "
        "join product_families f on f.product_family_id = p.product_family_id "
        "join product_kinds k on k.product_kind_id = f.product_kind_id "
        "where "
        + " and ".join(where + exists)
        + " order by p.product_sku;"
    )
    exact = exec_sql(vm, exact_sql)
    exact_rows = csv_rows(exact.stdout)
    if exact_rows:
        return render_command(
            "catalogue_lookup",
            "\n".join(
                [
                    f"exact_matches={len(exact_rows)}",
                    "Use these exact product paths as final refs.",
                    exact.stdout.rstrip(),
                ]
            ),
        )

    family_sql = (
        "select p.product_sku as sku, p.record_path as path, p.product_name as name, "
        "pp.property_key as key, pp.property_value_text as value_text, "
        "pp.property_value_number as value_number "
        "from product_variants p "
        "join product_families f on f.product_family_id = p.product_family_id "
        "join product_kinds k on k.product_kind_id = f.product_kind_id "
        "left join product_variant_properties pp on pp.product_sku = p.product_sku "
        "where "
        + " and ".join(where)
    )
    if cmd.constraints:
        candidate_keys = []
        for constraint in cmd.constraints:
            candidate_keys.extend(property_keys(constraint.key))
        family_sql += (
            " and (pp.property_key is null or pp.property_key in ("
            + sql_in(candidate_keys)
            + "))"
        )
    family_sql += " order by p.product_sku, pp.property_key;"
    nearby = exec_sql(vm, family_sql)
    return render_command(
        "catalogue_lookup",
        "\n".join(
            [
                "exact_matches=0",
                "No product matched all constraints. Inspect nearby_candidates before answering <NO>.",
                "nearby_candidates:",
                nearby.stdout.rstrip() or ".",
            ]
        ),
    )


def inventory_count(vm, cmd) -> str:
    if not cmd.skus:
        return render_command("inventory_count", "count=0\nfinal_product_refs=.")
    store = exec_sql(
        vm,
        "select store_id as id, record_path as path, store_name as name "
        f"from stores where store_id = {sql_literal(cmd.store_id)};",
    )
    store_rows = csv_rows(store.stdout)
    store_ref = store_rows[0]["path"] if store_rows else ""
    sql = f"""
select
  p.sku,
  p.path,
  p.name,
  coalesce(i.available_today_quantity, 0) as available_today,
  case when coalesce(i.available_today_quantity, 0) >= {cmd.threshold} then 1 else 0 end as counts
from (
  select product_sku as sku, record_path as path, product_name as name
  from product_variants
) p
left join store_inventory i on i.product_sku = p.sku and i.store_id = {sql_literal(cmd.store_id)}
where p.sku in ({sql_in(cmd.skus)})
order by p.sku;
""".strip()
    result = exec_sql(vm, sql)
    rows = csv_rows(result.stdout)
    counted = [row for row in rows if row.get("counts") == "1"]
    refs = [row["path"] for row in counted if row.get("path")]
    body = [
        f"store_id={cmd.store_id}",
        f"store_ref={store_ref or '.'}",
        f"threshold={cmd.threshold}",
        f"count={len(counted)}",
        "final_product_refs:",
    ]
    body.extend(f"- {ref}" for ref in refs) if refs else body.append(".")
    body.extend(["all_checked_rows:", result.stdout.rstrip() or "."])
    return render_command("inventory_count", "\n".join(body))


def store_lookup(vm, cmd) -> str:
    def query(name_contains: str):
        where = []
        if cmd.city:
            where.append(f"lower(city) = lower({sql_literal(cmd.city)})")
        if name_contains:
            where.append(f"lower(store_name) like lower({sql_literal('%' + name_contains + '%')})")
        sql = "select store_id as id, record_path as path, store_name as name, city, is_open from stores"
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by city, name limit 20;"
        return exec_sql(vm, sql)

    result = query(cmd.name_contains)
    rows = csv_rows(result.stdout)

    alias = "" if rows else store_name_alias(cmd.city, cmd.name_contains)
    if alias:
        result = query(alias)
        rows = csv_rows(result.stdout)

    return render_command(
        "store_lookup",
        "\n".join(
            [
                f"matches={len(rows)}",
                "Use the `id` value as inventory_count.store_id and the `path` as the final store ref.",
                result.stdout.rstrip() or ".",
            ]
        ),
    )


def _normalize_kind_id(kind_id: str) -> str:
    return (kind_id or "").strip().replace("-", "_")


def _kind_exists(vm, kind_id: str) -> bool:
    if not kind_id:
        return False
    result = exec_sql(
        vm,
        "select product_kind_id as id from product_kinds "
        f"where product_kind_id = {sql_literal(kind_id)} "
        "limit 1;",
    )
    return bool(csv_rows(result.stdout))


def _resolve_kind_id(vm, kind_id: str, kind_name: str) -> tuple[str, str | None]:
    normalized = _normalize_kind_id(kind_id)
    if normalized and normalized == kind_id and not kind_name:
        return normalized, None
    for candidate in (
        normalized,
        f"{normalized}s" if normalized and not normalized.endswith("s") else "",
    ):
        if candidate and _kind_exists(vm, candidate):
            return candidate, None

    if kind_name:
        kind_result = exec_sql(
            vm,
            "select product_kind_id as id, product_kind_name as name from product_kinds "
            f"where lower(product_kind_name) = lower({sql_literal(kind_name)}) "
            "order by product_kind_id limit 5;",
        )
        kind_rows = csv_rows(kind_result.stdout)
        if kind_rows:
            return kind_rows[0]["id"], None
        return "", kind_result.stdout.rstrip() or "."

    return normalized, None


def catalogue_count_report(vm, cmd) -> str:
    kind_id, kind_debug = _resolve_kind_id(vm, cmd.kind_id, cmd.kind_name)
    if not kind_id:
        return render_command(
            "catalogue_count_report",
            "\n".join(
                [
                    "kind_matches=0",
                    "No product kind matched kind_id or kind_name exactly; inspect product_kinds.",
                    kind_debug or ".",
                ]
            ),
        )

    exclude_clause = ""
    if cmd.exclude_family_ids:
        exclude_clause = f" and family_id not in ({sql_in(cmd.exclude_family_ids)})"

    if not cmd.city:
        count_result = exec_sql(
            vm,
            "select count(*) as count from product_variants "
            f"where product_kind_id = {sql_literal(kind_id)}"
            f"{exclude_clause.replace('family_id', 'product_family_id')};",
        )
        count_rows = csv_rows(count_result.stdout)
        count = int(count_rows[0]["count"]) if count_rows and count_rows[0].get("count") else 0
        products = exec_sql(
            vm,
            "select product_sku as sku, record_path as path, product_name as name "
            "from product_variants "
            f"where product_kind_id = {sql_literal(kind_id)}"
            f"{exclude_clause.replace('family_id', 'product_family_id')} "
            "order by product_sku limit 50;",
        )
        refs = [cmd.doc_path]
        body = [
            f"doc_ref={cmd.doc_path}",
            f"kind_id={kind_id}",
            "city=.",
            "threshold=.",
            f"exclude_family_ids={','.join(cmd.exclude_family_ids) or '.'}",
            f"count={count}",
            "final_refs:",
        ]
        body.extend(f"- {ref}" for ref in refs)
        body.extend(["counted_product_rows:", products.stdout.rstrip() or "."])
        return render_command("catalogue_count_report", "\n".join(body))

    threshold = max(cmd.threshold, 1)
    store_sql = (
        "select store_id as id, record_path as path, store_name as name, city from stores "
        f"where (lower(city) = lower({sql_literal(cmd.city)}) "
        f"or lower(store_name) like lower({sql_literal('%' + cmd.city + '%')})) "
        "and (is_open = 1 or lower(cast(is_open as text)) in ('true', 'yes', 'open')) "
        "and lower(store_name) like lower('%PowerTool%') "
        "order by store_id;"
    )
    stores = exec_sql(vm, store_sql)
    store_rows = csv_rows(stores.stdout)
    if not store_rows:
        store_sql = (
            "select store_id as id, record_path as path, store_name as name, city from stores "
            f"where (lower(city) = lower({sql_literal(cmd.city)}) "
            f"or lower(store_name) like lower({sql_literal('%' + cmd.city + '%')})) "
            "and lower(store_name) like lower('%PowerTool%') "
            "order by store_id;"
        )
        stores = exec_sql(vm, store_sql)
        store_rows = csv_rows(stores.stdout)
    store_ids = [row["id"] for row in store_rows]
    store_refs = [row["path"] for row in store_rows if row.get("path")]

    if not store_ids:
        body = [
            f"doc_ref={cmd.doc_path}",
            f"kind_id={kind_id}",
            f"city={cmd.city}",
            f"threshold={cmd.threshold}",
            "count=0",
            "final_refs:",
            f"- {cmd.doc_path}",
            "open_store_rows:",
            stores.stdout.rstrip() or ".",
        ]
        return render_command("catalogue_count_report", "\n".join(body))

    count_sql = f"""
select count(distinct p.product_sku) as count
from product_variants p
join store_inventory i on i.product_sku = p.product_sku
where p.product_kind_id = {sql_literal(kind_id)}
  {"and p.product_family_id not in (" + sql_in(cmd.exclude_family_ids) + ")" if cmd.exclude_family_ids else ""}
  and i.store_id in ({sql_in(store_ids)})
  and coalesce(i.available_today_quantity, 0) >= {threshold};
""".strip()
    count_result = exec_sql(vm, count_sql)
    count_rows = csv_rows(count_result.stdout)
    count = int(count_rows[0]["count"]) if count_rows and count_rows[0].get("count") else 0

    sql = f"""
select distinct
  p.product_sku as sku,
  p.record_path as path,
  p.product_name as name
from product_variants p
join store_inventory i on i.product_sku = p.product_sku
where p.product_kind_id = {sql_literal(kind_id)}
  {"and p.product_family_id not in (" + sql_in(cmd.exclude_family_ids) + ")" if cmd.exclude_family_ids else ""}
  and i.store_id in ({sql_in(store_ids)})
  and coalesce(i.available_today_quantity, 0) >= {threshold}
order by p.product_sku
limit 50;
""".strip()
    products = exec_sql(vm, sql)
    refs = [cmd.doc_path] + store_refs
    body = [
        f"doc_ref={cmd.doc_path}",
        f"kind_id={kind_id}",
        f"city={cmd.city}",
        f"threshold={threshold}",
        f"exclude_family_ids={','.join(cmd.exclude_family_ids) or '.'}",
        f"count={count}",
        "final_refs:",
    ]
    body.extend(f"- {ref}" for ref in refs)
    body.extend(["open_store_rows:", stores.stdout.rstrip() or "."])
    body.extend(["counted_product_rows:", products.stdout.rstrip() or "."])
    return render_command("catalogue_count_report", "\n".join(body))


def inventory_refs_from_output(text: str) -> set[str]:
    refs: set[str] = set()
    in_refs = False
    for line in text.splitlines():
        if line == "final_product_refs:":
            in_refs = True
            continue
        if line == "all_checked_rows:":
            break
        if in_refs and line.startswith("- "):
            refs.add(line[2:].strip())
    return refs


def inventory_summary_from_output(text: str) -> tuple[int | None, str, list[str]]:
    count: int | None = None
    store_ref = ""
    refs: list[str] = []
    in_refs = False
    for line in text.splitlines():
        if line.startswith("count="):
            try:
                count = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("store_ref="):
            store_ref = line.split("=", 1)[1].strip()
            if store_ref == ".":
                store_ref = ""
        elif line == "final_product_refs:":
            in_refs = True
            continue
        elif line == "all_checked_rows:":
            in_refs = False
        elif in_refs and line.startswith("- "):
            refs.append(line[2:].strip())
    return count, store_ref, refs


def count_report_summary_from_output(text: str) -> tuple[int | None, list[str]]:
    count: int | None = None
    refs: list[str] = []
    in_refs = False
    for line in text.splitlines():
        if line.startswith("count="):
            try:
                count = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line == "final_refs:":
            in_refs = True
            continue
        elif line in {"open_store_rows:", "counted_product_rows:"}:
            in_refs = False
        elif in_refs and line.startswith("- "):
            refs.append(line[2:].strip())
    return count, refs


def single_store_from_lookup_output(text: str) -> tuple[str, str] | None:
    if "matches=1" not in text:
        return None
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line == "id,path,name,city,is_open" and idx + 1 < len(lines):
            row = next(csv.DictReader([line, lines[idx + 1]]), None)
            if row and row.get("id") and row.get("path"):
                return row["id"], row["path"]
    return None


def sku_from_catalogue_output(text: str) -> str | None:
    if "exact_matches=1" not in text:
        return None
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("sku,path,name") and idx + 1 < len(lines):
            row = next(csv.DictReader([line, lines[idx + 1]]), None)
            if row and row.get("sku"):
                return row["sku"]
    return None


def catalogue_paths_from_output(text: str) -> list[str]:
    paths: list[str] = []
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("sku,path,name") and idx + 1 < len(lines):
            for row in csv.DictReader(lines[idx:]):
                path = row.get("path", "")
                if path.startswith("/proc/catalog/"):
                    paths.append(path)
            break
    return paths


def first_catalogue_row(text: str) -> dict[str, str] | None:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("sku,path,name") and idx + 1 < len(lines):
            return next(csv.DictReader([line, lines[idx + 1]]), None)
    return None
