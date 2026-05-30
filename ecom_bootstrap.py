from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"


@dataclass(frozen=True)
class BootstrapKit:
    req_tree: type
    req_exec: type
    format_result: Callable[[Any, Any], str]
    req_read: type | None = None


def build_bootstrap_commands(kit: BootstrapKit) -> list[Any]:
    return [
        kit.req_tree(level=2, tool="tree", root="/"),
        kit.req_tree(level=2, tool="tree", root="/docs"),
        kit.req_exec(
            path="/bin/sql",
            tool="exec",
            stdin=(
                "select type, name, sql from sqlite_schema "
                "where type = 'table' and sql is not null order by name;"
            ),
        ),
        kit.req_exec(
            path="/bin/sql",
            tool="exec",
            stdin=(
                "select distinct property_key as key "
                "from product_variant_properties order by property_key;"
            ),
        ),
        kit.req_exec(path="/bin/date", tool="exec"),
        kit.req_exec(path="/bin/id", tool="exec"),
    ]


def build_bootstrap_preface_commands(kit: BootstrapKit) -> list[Any]:
    if kit.req_read is None:
        return []
    return [
        kit.req_read(tool="read", path="/AGENTS.md"),
        kit.req_read(tool="read", path="/AGENTS.MD"),
    ]


def run_bootstrap(call_runtime, log: list[dict], kit: BootstrapKit) -> None:
    for cmd in build_bootstrap_preface_commands(kit):
        try:
            result = call_runtime(cmd)
        except Exception:
            continue
        formatted = kit.format_result(cmd, result)
        print(f"{CLI_GREEN}AUTO{CLI_CLR}: {formatted}")
        log.append({"role": "user", "content": formatted})
        break

    for cmd in build_bootstrap_commands(kit):
        result = call_runtime(cmd)
        formatted = kit.format_result(cmd, result)
        print(f"{CLI_GREEN}AUTO{CLI_CLR}: {formatted}")
        log.append({"role": "user", "content": formatted})
