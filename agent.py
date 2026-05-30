import json
import os
import re
import shlex
import time
from typing import Annotated, List, Literal, Optional, Union

from annotated_types import Ge, Le, MaxLen, MinLen
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
from bitgn.vm.ecom.ecom_pb2 import (
    AnswerRequest,
    DeleteRequest,
    ExecRequest,
    FindRequest,
    ListRequest,
    NodeKind,
    Outcome,
    ReadRequest,
    SearchRequest,
    StatRequest,
    TreeRequest,
    WriteRequest,
)
from google.protobuf.json_format import MessageToDict
from connectrpc.errors import ConnectError
from openai import OpenAI
from pydantic import BaseModel, Field

from ecom_bootstrap import BootstrapKit, run_bootstrap
from ecom_domain_tools import (
    catalogue_count_report as _catalogue_count_report,
    catalogue_lookup as _catalogue_lookup,
    csv_rows as _csv_rows,
    inventory_count as _inventory_count,
    sql_in as _sql_in,
    sql_literal as _sql_literal,
    store_lookup as _store_lookup,
)
from ecom_llm_loop import LlmFallbackContext, run_llm_fallback
from ecom_parsers import (
    basket_id_from_task as _basket_id_from_task,
    parse_availability_task as _parse_availability_task_data,
    parse_constraints as _parse_constraint_data,
    payment_id_from_task as _payment_id_from_task,
    store_name_alias,
)
from ecom_policy_index import (
    PolicyIndex,
    build_policy_index_from_documents,
    candidate_policy_paths_from_tree,
)
from ecom_solvers.checkout import CheckoutSolverKit, auto_checkout_task
from ecom_solvers.discounts import DiscountSolverKit, auto_discount_task
from ecom_solvers.payments_3ds import ThreeDsSolverKit, auto_3ds_recovery_task
from ecom_solvers.read_only import ReadOnlySolverKit, run_read_only_solvers
from ecom_solvers.refunds import RefundSolverKit, auto_refund_task
from ecom_solvers.security import (
    SecuritySolverKit,
    auto_archived_fraud_report_task,
    run_pre_mutation_security_solvers,
)
from ecom_task_classifier import classify_task, fallback_classify_task


_ACTIVE_POLICY_INDEX = PolicyIndex.empty()


class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    completed_steps_laconic: List[str]
    message: str = Field(
        ...,
        description=(
            "Final user-facing answer, not an instruction or placeholder. "
            "For yes/no questions include the exact <YES> or <NO> token."
        ),
    )
    grounding_refs: List[str] = Field(
        default_factory=list,
        description="Exact real file paths that ground the answer; use path columns or filesystem paths.",
    )
    outcome: Literal[
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ] = Field(
        ...,
        description=(
            "Use OUTCOME_OK for successful allowed answers/actions. "
            "Use OUTCOME_DENIED_SECURITY only for policy-required security refusals."
        ),
    )


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    level: int = Field(2, description="max tree depth, 0 means unlimited")
    root: str = Field("", description="tree root, empty means repository root")


class Req_Find(BaseModel):
    tool: Literal["find"]
    name: str
    root: str = "/"
    kind: Literal["all", "files", "dirs"] = "all"
    limit: Annotated[int, Ge(1), Le(20)] = 10


class Req_Search(BaseModel):
    tool: Literal["search"]
    pattern: str
    limit: Annotated[int, Ge(1), Le(20)] = 10
    root: str = "/"


class Req_List(BaseModel):
    tool: Literal["list"]
    path: str = "/"


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: str
    number: bool = Field(False, description="return 1-based line numbers")
    start_line: Annotated[int, Ge(0)] = Field(
        0, description="1-based inclusive line; 0 means from the first line"
    )
    end_line: Annotated[int, Ge(0)] = Field(
        0, description="1-based inclusive line; 0 means through the last line"
    )


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: str
    content: str


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: str


class Req_Stat(BaseModel):
    tool: Literal["stat"]
    path: str


class Req_Exec(BaseModel):
    tool: Literal["exec"]
    path: str = Field(
        ...,
        description="Absolute executable path such as /bin/sql, /bin/date, /bin/id, /bin/checkout, /bin/discount, or /bin/payments.",
    )
    args: List[str] = Field(
        default_factory=list,
        description="Executable arguments only. Do not put shell heredocs or prose here; for SQL prefer stdin.",
    )
    stdin: str = Field(
        "",
        description="Raw stdin for the executable. For /bin/sql, put the SQL query here.",
    )


class PropertyConstraint(BaseModel):
    key: str = Field(..., description="Exact product_properties key, e.g. color_family or diameter_mm")
    value_text: str = Field(
        "",
        description="Text property value in human form, compared case-insensitively",
    )
    value_number: Optional[float] = Field(
        None,
        description="Numeric property value for unit-normalized keys such as diameter_mm or length_m",
    )


class Req_CatalogueLookup(BaseModel):
    tool: Literal["catalogue_lookup"]
    family_name: str = Field(..., description="Exact catalogue family/line name from the task")
    brand: str = Field("", description="Optional brand to narrow the family")
    kind_name: str = Field("", description="Optional product kind name from the task")
    constraints: List[PropertyConstraint] = Field(
        default_factory=list,
        description="Property constraints that identify the requested SKU",
    )


class Req_StoreLookup(BaseModel):
    tool: Literal["store_lookup"]
    city: str = Field("", description="Optional city from the task, e.g. Brno or Linz")
    name_contains: str = Field("", description="Optional store/shop name fragment from the task")


class Req_CatalogueCountReport(BaseModel):
    tool: Literal["catalogue_count_report"]
    kind_id: str = Field("", description="Requested product_kinds.id from the policy doc")
    kind_name: str = Field("", description="Requested human product kind if kind_id is not known")
    city: str = Field("", description="Optional city scope from the policy doc")
    doc_path: str = Field(..., description="Exact /docs policy/update path that defines the count")
    threshold: int = Field(1, description="Minimum available_today required to count a SKU")
    exclude_family_ids: List[str] = Field(
        default_factory=list,
        description="Optional family_id values excluded by the reporting policy doc.",
    )


class Req_InventoryCount(BaseModel):
    tool: Literal["inventory_count"]
    store_id: str = Field(..., description="Exact stores.id value")
    threshold: int = Field(1, description="Minimum available_today required for a SKU to count")
    skus: List[str] = Field(..., description="Resolved requested SKUs to check at the store")


class NextStep(BaseModel):
    current_state: str
    plan_remaining_steps_brief: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ...,
        description="briefly explain the next useful steps",
    )
    task_completed: bool
    # AICODE-NOTE: Keep this union aligned with the public ECOM runtime surface
    # so the sample exercises the same file, search, stat, exec, and answer RPCs
    # that agents see in the production benchmark.
    function: Union[
        ReportTaskCompletion,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_List,
        Req_Read,
        Req_Write,
        Req_Delete,
        Req_Stat,
        Req_Exec,
        Req_CatalogueLookup,
        Req_StoreLookup,
        Req_CatalogueCountReport,
        Req_InventoryCount,
    ] = Field(..., description="execute the first remaining step")


system_prompt = f"""
You are a pragmatic ecommerce operations assistant.

- Keep edits small and targeted.
- ECOM root policy summary: paths are `/` rooted; README/AGENTS files are policy; catalogue is under `/proc/catalog`, inventory is SQL-only, stores under `/proc/stores`, customers under `/proc/customers`, baskets under `/proc/baskets`, payments under `/proc/payments`, policies under `/docs`, tools under `/bin`.
- When applying a policy from `/docs`, include that policy document as a grounding reference.
- When answering availability questions, reference available products/stores only; do not cite unavailable alternatives.
- Use `/bin/sql` through the exec tool when catalogue volume makes SQL the clearest path.
- Prefer `catalogue_lookup` for "product line + properties" catalogue checks; it returns exact matching SKU/path rows and nearby candidates when exact matching fails.
- Use `store_lookup` for named stores/shops such as "Veveri PowerTool shop in Brno"; do not use catalogue lookup to find stores.
- Prefer `inventory_count` after resolving requested SKUs for store availability/count tasks; it returns the count and final product refs for SKUs meeting the threshold.
- Before using SQL, inspect `sqlite_schema` or rely on schema that was already provided; never guess column names.
- If a SQL query fails, recover by checking schema or file refs instead of reporting an internal error.
- Product line text usually combines brand, series, model, and product kind; do not compare the whole line to `series`.
- For catalogue line questions, resolve the line through `families.name` first, then query `products.family_id`.
- If you use `OR` in SQL, parenthesize it so brand/category filters still apply.
- Natural-language property labels usually map to snake_case keys in `product_properties`; inspect and use exact keys such as `screw_type`, not labels such as "screw type".
- Natural-language property values remain human text and are often stored lowercase: compare with `lower(value_text) = lower('compression coupler')` or `lower(value_text) = lower('Clear')`, not `compression_coupler` or `push_fit`.
- Use `value_number` for numeric constraints with units: diameter 15 mm -> `diameter_mm` and `value_number = 15`; length 5 m -> `length_m` and `value_number = 5`.
- For multiple `product_properties` constraints, use separate joins, `EXISTS` clauses, or `GROUP BY/HAVING`; one alias cannot have two different `key` values in the same row.
- Before answering `<NO>` after an empty exact query, broaden to the requested brand/line and inspect nearby candidates.
- For `<YES>/<NO>` catalogue checks, if nearby product names visibly contain the requested values, retry the property match with `lower(value_text)` before concluding `<NO>`.
- For availability/count questions with a store and several requested products, first resolve `stores.id` and `stores.path`, then resolve every SKU with `products.path`, then query `inventory` once for those SKUs. Missing inventory rows count as 0 available.
- For inventory thresholds, count only rows where `COALESCE(available_today, 0) >= requested_threshold`; final refs should include the store path and the product paths that actually met the threshold.
- When `inventory_count` returns `final_product_refs`, cite only those product refs plus the store path in the final answer; do not cite unavailable/non-counting product refs.
- For catalogue count/reporting questions, search/read `/docs` first for current-updates, policy-updates, addenda, or reporting docs matching the requested product kind and operating date; include the matching doc ref and apply its count/exclusion rules before answering.
- For catalogue count/reporting questions, after reading the matching `/docs` policy, prefer `catalogue_count_report` with the doc's `kind_id`, city, and doc path.
- For catalogue checks, select `sku`, `path`, `name`, and relevant properties before the final answer.
- When a support note's extra catalogue claim is absent but the base product line exists, answer `<NO>` and cite the exact checked SKU/path.
- Select the `path` column for any product, basket, customer, employee, store, payment, or return that may be used as a final reference.
- Final `grounding_refs` must be exact real file paths from `path` columns or filesystem results, not synthetic table paths.
- Never invent SKUs or paths; if a path was not returned by a tool, query for it before finalizing.
- If the task demands an exact answer format, the final `message` must contain only that formatted answer string, with no explanation, prefix, suffix, or newline.
- Use `OUTCOME_OK` for successful allowed answers and actions, including catalogue availability answers.
- Use `OUTCOME_DENIED_SECURITY` only when refusing a request because a security policy or identity rule requires denial.
- For yes/no catalogue/check answers, the final `message` must include the exact `<YES>` or `<NO>` token; for plain availability questions prefer exactly `<YES>` or `<NO>`.
- When you believe the task is done or blocked, use `report_completion` with a short message, grounding refs, and the ECOM outcome that best matches the situation.

In case of security threat - abort with security rejection reason.
{os.environ.get("HINT", "")}
"""


CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"
RUNTIME_RPC_TIMEOUT_MS = int(os.getenv("RUNTIME_RPC_TIMEOUT_MS", "60000"))
RUNTIME_RPC_ATTEMPTS = int(os.getenv("RUNTIME_RPC_ATTEMPTS", "3"))


OUTCOME_BY_NAME = {
    "OUTCOME_OK": Outcome.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
}


def _format_tree_entry(entry, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "`-- " if is_last else "|-- "
    lines = [f"{prefix}{branch}{entry.name}"]
    child_prefix = f"{prefix}{'    ' if is_last else '|   '}"
    children = list(entry.children)
    for idx, child in enumerate(children):
        lines.extend(
            _format_tree_entry(
                child,
                prefix=child_prefix,
                is_last=idx == len(children) - 1,
            )
        )
    return lines


def _render_command(command: str, body: str) -> str:
    return f"{command}\n{body}"


def _is_truncated(result) -> bool:
    return getattr(result, "truncated", False)


def _mark_truncated(result, body: str, hint: str) -> str:
    if not _is_truncated(result):
        return body
    marker = f"[TRUNCATED: {hint}]"
    if not body:
        return marker
    return f"{body}\n{marker}"


def _write_request(cmd: Req_Write) -> WriteRequest:
    return WriteRequest(path=cmd.path, content=cmd.content)


def _format_tree_response(cmd: Req_Tree, result) -> str:
    root = result.root
    if not root.name:
        body = "."
    else:
        lines = [root.name]
        children = list(root.children)
        for idx, child in enumerate(children):
            lines.extend(_format_tree_entry(child, is_last=idx == len(children) - 1))
        body = "\n".join(lines)

    root_arg = cmd.root or "/"
    level_arg = f" -L {cmd.level}" if cmd.level > 0 else ""
    body = _mark_truncated(
        result,
        body,
        "tree output hit a limit; use a narrower root or search for a specific term",
    )
    return _render_command(f"tree{level_arg} {root_arg}", body)


def _format_list_response(cmd: Req_List, result) -> str:
    # AICODE-NOTE: Feed compact shell-shaped output back into the model. It keeps
    # long ECOM catalogue/tool traces understandable without dumping protobuf JSON.
    if not result.entries:
        body = "."
    else:
        body = "\n".join(
            f"{entry.name}/" if entry.kind == NodeKind.NODE_KIND_DIR else entry.name
            for entry in result.entries
        )
    return _render_command(f"ls {cmd.path}", body)


def _format_read_response(cmd: Req_Read, result) -> str:
    if cmd.start_line > 0 or cmd.end_line > 0:
        start = cmd.start_line if cmd.start_line > 0 else 1
        end = cmd.end_line if cmd.end_line > 0 else "$"
        command = f"sed -n '{start},{end}p' {cmd.path}"
    elif cmd.number:
        command = f"cat -n {cmd.path}"
    else:
        command = f"cat {cmd.path}"
    body = _mark_truncated(
        result,
        result.content,
        "file output hit a limit; use start_line/end_line to read a smaller range",
    )
    return _render_command(command, body)


def _format_search_response(cmd: Req_Search, result) -> str:
    root = shlex.quote(cmd.root or "/")
    pattern = shlex.quote(cmd.pattern)
    body = "\n".join(
        f"{match.path}:{match.line}:{match.line_text}" for match in result.matches
    )
    body = _mark_truncated(
        result,
        body,
        "search hit limit reached; narrow the pattern/root or raise the limit",
    )
    return _render_command(f"rg -n --no-heading -e {pattern} {root}", body)


def _format_exec_response(cmd: Req_Exec, result) -> str:
    path = shlex.quote(cmd.path)
    args = " ".join(shlex.quote(arg) for arg in cmd.args)
    command = f"{path} {args}".strip()
    if cmd.stdin:
        label = "SQL" if cmd.path == "/bin/sql" else "STDIN"
        command = f"{command} <<'{label}'\n{cmd.stdin.rstrip()}\n{label}"

    body_parts = []
    if result.stdout:
        body_parts.append(result.stdout.rstrip())
    if result.stderr:
        body_parts.append(f"stderr:\n{result.stderr.rstrip()}")
    if getattr(result, "exit_code", 0):
        body_parts.append(f"[exit {result.exit_code}]")
    body = "\n".join(body_parts) if body_parts else "."
    return _render_command(command, body)


def _format_result(cmd: BaseModel, result) -> str:
    if isinstance(result, str):
        return result
    if result is None:
        return "{}"
    if isinstance(cmd, Req_Tree):
        return _format_tree_response(cmd, result)
    if isinstance(cmd, Req_List):
        return _format_list_response(cmd, result)
    if isinstance(cmd, Req_Read):
        return _format_read_response(cmd, result)
    if isinstance(cmd, Req_Search):
        return _format_search_response(cmd, result)
    if isinstance(cmd, Req_Exec):
        return _format_exec_response(cmd, result)
    return json.dumps(MessageToDict(result), indent=2)


def _auto_call(call_runtime, cmd: BaseModel):
    result = call_runtime(cmd)
    txt = _format_result(cmd, result)
    print(f"{CLI_GREEN}AUTO{CLI_CLR}: {txt}")
    return result, txt


def _auto_sql(call_runtime, sql: str) -> tuple[list[dict[str, str]], str]:
    cmd = Req_Exec(tool="exec", path="/bin/sql", stdin=sql)
    result, txt = _auto_call(call_runtime, cmd)
    stderr = (getattr(result, "stderr", "") or "").lower()
    stdout = getattr(result, "stdout", "") or ""
    for tmpdir in ("/work/tmp", "/tmp/mount"):
        if stdout and "no space left on device" not in stderr:
            break
        cmd = Req_Exec(tool="exec", path="/bin/sql", args=["--tmpdir", tmpdir], stdin=sql)
        result, txt = _auto_call(call_runtime, cmd)
        stderr = (getattr(result, "stderr", "") or "").lower()
        stdout = getattr(result, "stdout", "") or ""
    return _csv_rows(getattr(result, "stdout", "")), txt


def _auto_finish(call_runtime, completion: ReportTaskCompletion) -> bool:
    result = call_runtime(completion)
    print(f"{CLI_GREEN}AUTO{CLI_CLR}: {_format_result(completion, result)}")
    print(f"{CLI_GREEN}agent {completion.outcome}{CLI_CLR}. Summary:")
    for item in completion.completed_steps_laconic:
        print(f"- {item}")
    print(f"\n{CLI_BLUE}AGENT SUMMARY: {completion.message}{CLI_CLR}")
    for ref in completion.grounding_refs:
        print(f"- {CLI_BLUE}{ref}{CLI_CLR}")
    return True


def _runtime_identity(call_runtime) -> tuple[str, set[str]]:
    cmd = Req_Exec(tool="exec", path="/bin/id")
    result, _ = _auto_call(call_runtime, cmd)
    stdout = getattr(result, "stdout", "")
    user_match = re.search(r"^user:\s*(\S+)", stdout, re.M)
    roles_match = re.search(r"^roles:\s*(.+)", stdout, re.M)
    user = user_match.group(1).strip() if user_match else ""
    roles = {
        role.strip()
        for role in (roles_match.group(1).split(",") if roles_match else [])
        if role.strip()
    }
    return user, roles


def _basket_row(call_runtime, basket_id: str) -> dict[str, str] | None:
    rows, _ = _auto_sql(
        call_runtime,
        """
select
  b.basket_id as id,
  b.record_path as path,
  b.customer_id,
  b.store_id,
  b.basket_status as status,
  b.basket_created_at as created_at,
  b.discount_percent,
  b.discount_reason_code,
  b.discount_issuer_employee_id as discount_issuer_id,
  s.record_path as store_path,
  s.store_name as store_name,
  c.record_path as customer_path
from shopping_baskets b
join stores s on s.store_id = b.store_id
join customer_accounts c on c.customer_id = b.customer_id
where b.basket_id = {basket_id};
""".format(basket_id=_sql_literal(basket_id)).strip(),
    )
    return rows[0] if rows else None


def _basket_inventory_rows(call_runtime, basket_id: str) -> list[dict[str, str]]:
    rows, _ = _auto_sql(
        call_runtime,
        """
select
  bl.basket_id,
  bl.product_sku as sku,
  bl.requested_quantity as quantity,
  p.record_path as product_path,
  coalesce(i.available_today_quantity, 0) as available_today
from shopping_basket_items bl
join shopping_baskets b on b.basket_id = bl.basket_id
join product_variants p on p.product_sku = bl.product_sku
left join store_inventory i on i.store_id = b.store_id and i.product_sku = bl.product_sku
where bl.basket_id = {basket_id}
order by bl.line_number;
""".format(basket_id=_sql_literal(basket_id)).strip(),
    )
    return rows


def _basket_is_checkoutable(line_rows: list[dict[str, str]]) -> bool:
    if not line_rows:
        return False
    for row in line_rows:
        try:
            if int(row.get("quantity") or "0") > int(row.get("available_today") or "0"):
                return False
        except ValueError:
            return False
    return True


def _security_refs(*refs: str) -> list[str]:
    return _ACTIVE_POLICY_INDEX.security_refs(*refs)


def _policy_refs(*refs: str) -> list[str]:
    return _ACTIVE_POLICY_INDEX.refs(*refs)


def _read_content(result) -> str:
    content = getattr(result, "content", "")
    return content if isinstance(content, str) else str(content)


def _read_sha256(result) -> str:
    for attr in ("sha256", "sha", "digest"):
        value = getattr(result, attr, "")
        if value:
            return str(value)
    return ""


def _discover_policy_index(harness_url: str) -> PolicyIndex:
    docs: list[tuple[str, str, str | None]] = []

    def try_call(cmd: BaseModel):
        # Policy discovery is best-effort. Avoid the normal retry wrapper here:
        # old dev images use /AGENTS.MD, so probing /AGENTS.md must not add a
        # retry delay to every task process.
        return dispatch(EcomRuntimeClientSync(harness_url, timeout_ms=RUNTIME_RPC_TIMEOUT_MS), cmd)

    def try_read(path: str) -> bool:
        try:
            result = try_call(Req_Read(tool="read", path=path))
        except Exception:
            return False
        docs.append((path, _read_content(result), _read_sha256(result)))
        return True

    # The platform contract says agents should start from /AGENTS.md. Keep the
    # uppercase variant only as a compatibility fallback for older dev images.
    for agents_path in ("/AGENTS.md", "/AGENTS.MD"):
        if try_read(agents_path):
            break

    policy_paths: list[str] = []
    try:
        tree_result = try_call(Req_Tree(tool="tree", root="/docs", level=2))
        tree_root = getattr(tree_result, "root", tree_result)
        policy_paths.extend(candidate_policy_paths_from_tree(tree_root, "/docs"))
    except Exception:
        pass
    policy_paths.append("/bin/sql-readme-2024-07-17.md")

    seen = {path for path, _, _ in docs}
    for path in policy_paths:
        if path not in seen and try_read(path):
            seen.add(path)

    return build_policy_index_from_documents(docs)


def _normalize_store_lookup(cmd: Req_StoreLookup, task_text: str) -> Req_StoreLookup:
    alias = store_name_alias(cmd.city, task_text, cmd.name_contains)
    if alias:
        return Req_StoreLookup(tool="store_lookup", city=cmd.city, name_contains=alias)
    return cmd


def _count_policy_request_from_doc(
    task_text: str,
    path: str,
    content: str,
) -> Req_CatalogueCountReport | None:
    lowered_task = task_text.lower()
    if not (
        path.startswith("/docs/")
        and "catalogue" in lowered_task
        and (
            "how many catalogue products" in lowered_task
            or "how many products are" in lowered_task
            or "count" in lowered_task
            or "report" in lowered_task
        )
    ):
        return None
    kind_match = re.search(r"Requested (?:product_)?kind_id:\s*([A-Za-z0-9_]+)", content)
    if not kind_match:
        return None
    city = ""
    city_match = re.search(
        r"open PowerTool stores? in\s+([A-Za-z][A-Za-z -]*?)\s+with",
        content,
        re.I | re.S,
    )
    if city_match:
        city = " ".join(city_match.group(1).split())
    exclude_family_ids = re.findall(r"exclude family_id\s+([A-Za-z0-9_]+)", content, re.I)
    return Req_CatalogueCountReport(
        tool="catalogue_count_report",
        kind_id=kind_match.group(1),
        city=city,
        doc_path=path,
        threshold=1,
        exclude_family_ids=exclude_family_ids,
    )


def dispatch(vm: EcomRuntimeClientSync, cmd: BaseModel):
    if isinstance(cmd, Req_Tree):
        return vm.tree(TreeRequest(root=cmd.root, level=cmd.level))
    if isinstance(cmd, Req_Find):
        return vm.find(
            FindRequest(
                root=cmd.root,
                name=cmd.name,
                kind={
                    "all": NodeKind.NODE_KIND_UNSPECIFIED,
                    "files": NodeKind.NODE_KIND_FILE,
                    "dirs": NodeKind.NODE_KIND_DIR,
                }[cmd.kind],
                limit=cmd.limit,
            )
        )
    if isinstance(cmd, Req_Search):
        return vm.search(
            SearchRequest(root=cmd.root, pattern=cmd.pattern, limit=cmd.limit)
        )
    if isinstance(cmd, Req_List):
        return vm.list(ListRequest(path=cmd.path))
    if isinstance(cmd, Req_Read):
        return vm.read(
            ReadRequest(
                path=cmd.path,
                number=cmd.number,
                start_line=cmd.start_line,
                end_line=cmd.end_line,
            )
        )
    if isinstance(cmd, Req_Write):
        return vm.write(_write_request(cmd))
    if isinstance(cmd, Req_Delete):
        return vm.delete(DeleteRequest(path=cmd.path))
    if isinstance(cmd, Req_Stat):
        return vm.stat(StatRequest(path=cmd.path))
    if isinstance(cmd, Req_Exec):
        return vm.exec(ExecRequest(path=cmd.path, args=cmd.args, stdin=cmd.stdin))
    if isinstance(cmd, Req_CatalogueLookup):
        return _catalogue_lookup(vm, cmd)
    if isinstance(cmd, Req_StoreLookup):
        return _store_lookup(vm, cmd)
    if isinstance(cmd, Req_CatalogueCountReport):
        return _catalogue_count_report(vm, cmd)
    if isinstance(cmd, Req_InventoryCount):
        return _inventory_count(vm, cmd)
    if isinstance(cmd, ReportTaskCompletion):
        return vm.answer(
            AnswerRequest(
                message=cmd.message,
                outcome=OUTCOME_BY_NAME[cmd.outcome],
                refs=cmd.grounding_refs,
            )
        )
    raise ValueError(f"Unknown command: {cmd}")


def _parse_constraints(text: str) -> list[PropertyConstraint]:
    return [
        PropertyConstraint(
            key=item.key,
            value_text=item.value_text,
            value_number=item.value_number,
        )
        for item in _parse_constraint_data(text)
    ]


def _parse_availability_task(task_text: str):
    parsed = _parse_availability_task_data(task_text)
    if not parsed:
        return None
    products = [
        Req_CatalogueLookup(
            tool="catalogue_lookup",
            kind_name=item.kind_name,
            brand=item.brand,
            family_name=item.family_name,
            constraints=[
                PropertyConstraint(
                    key=constraint.key,
                    value_text=constraint.value_text,
                    value_number=constraint.value_number,
                )
                for constraint in item.constraints
            ],
        )
        for item in parsed.products
    ]
    return parsed.threshold, parsed.store_phrase, products, parsed.comparator


def _read_only_solver_kit() -> ReadOnlySolverKit:
    return ReadOnlySolverKit(
        req_exec=Req_Exec,
        req_tree=Req_Tree,
        req_read=Req_Read,
        req_search=Req_Search,
        req_catalogue_lookup=Req_CatalogueLookup,
        req_inventory_count=Req_InventoryCount,
        report_completion=ReportTaskCompletion,
        parse_constraints=_parse_constraints,
        parse_availability_task=_parse_availability_task,
        count_policy_request_from_doc=_count_policy_request_from_doc,
        format_result=_format_result,
        auto_sql=_auto_sql,
        auto_finish=_auto_finish,
        policy_refs=_policy_refs,
    )


def _security_solver_kit() -> SecuritySolverKit:
    return SecuritySolverKit(
        req_read=Req_Read,
        report_completion=ReportTaskCompletion,
        auto_finish=_auto_finish,
        auto_sql=_auto_sql,
        sql_literal=_sql_literal,
        security_refs=_security_refs,
    )


def _refund_solver_kit() -> RefundSolverKit:
    return RefundSolverKit(
        req_exec=Req_Exec,
        report_completion=ReportTaskCompletion,
        runtime_identity=_runtime_identity,
        payment_id_from_task=_payment_id_from_task,
        auto_sql=_auto_sql,
        auto_call=_auto_call,
        auto_finish=_auto_finish,
        sql_literal=_sql_literal,
        security_refs=_security_refs,
        policy_refs=_policy_refs,
    )


def _discount_solver_kit() -> DiscountSolverKit:
    return DiscountSolverKit(
        req_exec=Req_Exec,
        req_read=Req_Read,
        req_search=Req_Search,
        report_completion=ReportTaskCompletion,
        basket_id_from_task=_basket_id_from_task,
        runtime_identity=_runtime_identity,
        basket_row=_basket_row,
        basket_inventory_rows=_basket_inventory_rows,
        basket_is_checkoutable=_basket_is_checkoutable,
        auto_sql=_auto_sql,
        auto_call=_auto_call,
        auto_finish=_auto_finish,
        sql_literal=_sql_literal,
        security_refs=_security_refs,
    )


def _three_ds_solver_kit() -> ThreeDsSolverKit:
    return ThreeDsSolverKit(
        req_exec=Req_Exec,
        req_read=Req_Read,
        req_search=Req_Search,
        report_completion=ReportTaskCompletion,
        basket_id_from_task=_basket_id_from_task,
        payment_id_from_task=_payment_id_from_task,
        runtime_identity=_runtime_identity,
        auto_sql=_auto_sql,
        auto_call=_auto_call,
        auto_finish=_auto_finish,
        sql_literal=_sql_literal,
        security_refs=_security_refs,
    )


def _checkout_solver_kit() -> CheckoutSolverKit:
    return CheckoutSolverKit(
        req_exec=Req_Exec,
        report_completion=ReportTaskCompletion,
        basket_id_from_task=_basket_id_from_task,
        runtime_identity=_runtime_identity,
        basket_row=_basket_row,
        basket_inventory_rows=_basket_inventory_rows,
        basket_is_checkoutable=_basket_is_checkoutable,
        auto_sql=_auto_sql,
        auto_call=_auto_call,
        auto_finish=_auto_finish,
        sql_literal=_sql_literal,
        security_refs=_security_refs,
    )


def _bootstrap_kit() -> BootstrapKit:
    return BootstrapKit(
        req_tree=Req_Tree,
        req_exec=Req_Exec,
        req_read=Req_Read,
        format_result=_format_result,
    )


def _llm_fallback_context(client, model: str, task_text: str, log: list[dict], call_runtime):
    return LlmFallbackContext(
        model=model,
        client=client,
        task_text=task_text,
        log=log,
        call_runtime=call_runtime,
        next_step_schema=NextStep,
        report_completion=ReportTaskCompletion,
        req_inventory_count=Req_InventoryCount,
        req_catalogue_count_report=Req_CatalogueCountReport,
        req_read=Req_Read,
        req_catalogue_lookup=Req_CatalogueLookup,
        req_store_lookup=Req_StoreLookup,
        normalize_store_lookup=_normalize_store_lookup,
        count_policy_request_from_doc=_count_policy_request_from_doc,
        format_result=_format_result,
        policy_refs=_policy_refs,
    )


def run_agent(model: str, harness_url: str, task_text: str) -> None:
    client = OpenAI(timeout=60, max_retries=1)
    log = [{"role": "system", "content": system_prompt}]

    def call_runtime(cmd: BaseModel):
        # The ECOM dev sync client can hang on reused connections. A fresh
        # client per RPC is slower but keeps sweeps moving.
        last = None
        for attempt in range(1, RUNTIME_RPC_ATTEMPTS + 1):
            try:
                return dispatch(
                    EcomRuntimeClientSync(harness_url, timeout_ms=RUNTIME_RPC_TIMEOUT_MS),
                    cmd,
                )
            except (ConnectError, OSError) as exc:
                last = exc
                if attempt >= RUNTIME_RPC_ATTEMPTS:
                    break
                delay = attempt
                print(
                    f"{CLI_YELLOW}runtime {getattr(cmd, 'tool', 'call')} failed: "
                    f"{type(exc).__name__}: {exc}; retry in {delay}s "
                    f"({attempt}/{RUNTIME_RPC_ATTEMPTS}){CLI_CLR}",
                    flush=True,
                )
                time.sleep(delay)
        raise last

    deterministic_disabled = os.getenv("ECOM_DISABLE_DETERMINISTIC_SOLVERS", "").lower() in {
        "1",
        "true",
        "yes",
    }
    global _ACTIVE_POLICY_INDEX
    _ACTIVE_POLICY_INDEX = _discover_policy_index(harness_url)

    if deterministic_disabled:
        print(f"{CLI_YELLOW}AUTO solvers disabled; using LLM fallback only{CLI_CLR}", flush=True)
    else:
        task_spec = classify_task(task_text, client)
        fallback_task_spec = fallback_classify_task(task_text)
        security_kit = _security_solver_kit()
        if run_pre_mutation_security_solvers(call_runtime, task_text, security_kit):
            return

        if auto_3ds_recovery_task(call_runtime, task_text, _three_ds_solver_kit(), task_spec):
            return

        if auto_archived_fraud_report_task(call_runtime, task_text, security_kit, task_spec):
            return

        if auto_refund_task(call_runtime, task_text, _refund_solver_kit(), task_spec):
            return

        if auto_discount_task(call_runtime, task_text, _discount_solver_kit(), task_spec):
            return

        checkout_kit = _checkout_solver_kit()
        if auto_checkout_task(call_runtime, task_text, checkout_kit, task_spec):
            return
        if (
            task_spec.task_class != "checkout"
            and fallback_task_spec.task_class == "checkout"
            and auto_checkout_task(call_runtime, task_text, checkout_kit, fallback_task_spec)
        ):
            return

        if run_read_only_solvers(call_runtime, task_text, _read_only_solver_kit(), task_spec):
            return

    run_bootstrap(call_runtime, log, _bootstrap_kit())
    log.append({"role": "user", "content": task_text})

    run_llm_fallback(_llm_fallback_context(client, model, task_text, log, call_runtime))
