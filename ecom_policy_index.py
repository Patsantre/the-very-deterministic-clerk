from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


DEFAULT_PATH_BY_KEY = {
    "agents.root": "/AGENTS.md",
    "security.identity": "/docs/security.md",
    "checkout.inventory": "/docs/checkout.md",
    "discount.service_recovery": "/docs/discounts.md",
    "payment.3ds": "/docs/payments/3ds.md",
    "returns.refund": "/docs/returns.md",
    "store.exception": "/docs/store-associate-exception-handbook.md",
    "sql.incident": "/bin/sql-readme-2024-07-17.md",
}

FALLBACK_PATH_TO_KEY = {path: key for key, path in DEFAULT_PATH_BY_KEY.items()}

POLICY_PATH_NEEDLES = (
    "security",
    "identity",
    "checkout",
    "discount",
    "return",
    "refund",
    "3ds",
    "payment",
    "exception",
    "handbook",
    "sql",
    "incident",
)


@dataclass(frozen=True)
class PolicyDoc:
    path: str
    sha256: str
    keys: tuple[str, ...]
    title: str = ""
    content: str = ""


@dataclass(frozen=True)
class PolicyIndex:
    docs_by_key: dict[str, tuple[PolicyDoc, ...]] = field(default_factory=dict)
    docs_by_sha: dict[str, PolicyDoc] = field(default_factory=dict)
    scanned_paths: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "PolicyIndex":
        return cls()

    def refs(self, *items: str) -> list[str]:
        final: list[str] = []
        for item in items:
            if not item:
                continue
            key = FALLBACK_PATH_TO_KEY.get(item, item)
            docs = self.docs_by_key.get(key, ())
            if docs:
                for doc in docs:
                    _append_ref(final, doc.path)
                continue
            fallback = DEFAULT_PATH_BY_KEY.get(key)
            if fallback:
                _append_ref(final, fallback)
            elif item.startswith("/"):
                _append_ref(final, item)
        return final

    def security_refs(self, *items: str) -> list[str]:
        return self.refs("security.identity", *items)

    def path_for(self, key: str, fallback: str = "") -> str:
        docs = self.docs_by_key.get(key, ())
        if docs:
            return docs[0].path
        return fallback or DEFAULT_PATH_BY_KEY.get(key, "")

    def known_sha(self, sha256: str) -> bool:
        return bool(sha256 and sha256 in self.docs_by_sha)


def _append_ref(refs: list[str], ref: str) -> None:
    if ref and ref not in refs:
        refs.append(ref)


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def classify_policy_doc(path: str, content: str) -> tuple[str, ...]:
    lower_path = _canonical_path(path).lower()
    name = lower_path.rsplit("/", 1)[-1]
    lower = content.lower()
    keys: list[str] = []

    if lower_path == "/agents.md" or lower_path.endswith("/agents.md"):
        keys.append("agents.root")
    if "security" in name or "identity" in name:
        keys.append("security.identity")
    if "checkout" in name:
        keys.append("checkout.inventory")
    if "discount" in name:
        keys.append("discount.service_recovery")
    if "3ds" in name:
        keys.append("payment.3ds")
    if "return" in name or "refund" in name:
        keys.append("returns.refund")
    if "exception" in name or "handbook" in name:
        keys.append("store.exception")
    if "sql" in name or "incident" in name:
        keys.append("sql.incident")

    if not keys:
        if "identity" in lower and ("role" in lower or "ownership" in lower):
            keys.append("security.identity")
        if "checkout" in lower and "basket" in lower:
            keys.append("checkout.inventory")
        if "discount" in lower and "service" in lower:
            keys.append("discount.service_recovery")
        if "3ds" in lower:
            keys.append("payment.3ds")
        if "refund" in lower and "return" in lower:
            keys.append("returns.refund")
        if "store" in lower and "exception" in lower:
            keys.append("store.exception")
        if "sql" in lower or "stale" in lower or "db only" in lower or "rely on db" in lower:
            keys.append("sql.incident")

    return tuple(dict.fromkeys(keys))


def build_policy_index_from_documents(
    documents: Iterable[tuple[str, str, str | None]],
) -> PolicyIndex:
    by_key: dict[str, list[PolicyDoc]] = {}
    by_sha: dict[str, PolicyDoc] = {}
    scanned: list[str] = []

    for path, content, sha in documents:
        path = _canonical_path(path) if path else ""
        if not path or path in scanned:
            continue
        scanned.append(path)
        sha256 = sha or sha256_text(content)
        keys = classify_policy_doc(path, content)
        if not keys:
            continue
        title = _first_markdown_title(content)
        doc = PolicyDoc(path=path, sha256=sha256, keys=keys, title=title, content=content)
        by_sha[sha256] = doc
        for key in keys:
            by_key.setdefault(key, []).append(doc)

    return PolicyIndex(
        docs_by_key={key: tuple(docs) for key, docs in by_key.items()},
        docs_by_sha=by_sha,
        scanned_paths=tuple(scanned),
    )


def candidate_policy_paths_from_tree(root: Any, base_path: str = "/docs") -> list[str]:
    paths: list[str] = []

    def walk(node: Any, path: str) -> None:
        children = list(getattr(node, "children", []) or [])
        if not children:
            if _looks_like_policy_path(path):
                _append_ref(paths, _canonical_path(path))
            return
        for child in children:
            name = getattr(child, "name", "")
            if not name:
                continue
            child_path = f"{path.rstrip('/')}/{str(name).lstrip('/')}"
            walk(child, child_path)

    walk(root, base_path)
    return paths


def _looks_like_policy_path(path: str) -> bool:
    lower = _canonical_path(path).lower()
    if not (lower.startswith("/docs/") or lower.startswith("/bin/")):
        return False
    name = lower.rsplit("/", 1)[-1]
    return any(needle in name for needle in POLICY_PATH_NEEDLES)


def _canonical_path(path: str) -> str:
    if path.startswith("/"):
        return path
    return "/" + path.lstrip("./")


def _first_markdown_title(content: str) -> str:
    for line in content.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return ""
