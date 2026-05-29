from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(__file__).with_name("config")


def _load_json_config(name: str) -> Any:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing ECOM config file: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _normalize_label(value: str) -> str:
    return re.sub(r"[\s_-]+", "_", value.strip().lower())


def _label_regex(label: str) -> str:
    parts = [re.escape(part) for part in re.split(r"[\s_-]+", label.strip()) if part]
    return r"[\s_-]+".join(parts)


def _unit_pattern(unit: str) -> str:
    if not unit:
        return r"\b"
    normalized = unit.lower()
    aliases = {normalized}
    if normalized == "pcs":
        aliases.update({"pc", "piece", "pieces"})
    elif normalized == "mm2":
        aliases.add("mm²")
    alias_pattern = "|".join(
        re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)
    )
    return rf"\s*(?:{alias_pattern})\b"


def _clean_text_constraint_value(value: str) -> str:
    cleaned = " ".join(value.strip().rstrip("?.").split())
    cleaned = re.sub(
        r"\s+(?:in\s+(?:the\s+)?catalogue|from\s+you|from\s+your\s+store)$",
        "",
        cleaned,
        flags=re.I,
    )
    return cleaned


RAW_TEXT_CONSTRAINT_KEYS = _load_json_config("property_aliases.json")["property_aliases"]
TEXT_CONSTRAINT_KEYS = {
    _normalize_label(label): key for label, key in RAW_TEXT_CONSTRAINT_KEYS.items()
}
TEXT_CONSTRAINT_ITEMS = tuple(
    sorted(RAW_TEXT_CONSTRAINT_KEYS.items(), key=lambda kv: -len(_normalize_label(kv[0])))
)
NUMERIC_CONSTRAINTS = tuple(
    (item["label"], item["key"], item.get("unit", ""))
    for item in _load_json_config("numeric_constraints.json")["numeric_constraints"]
)
NUMERIC_CONSTRAINT_ITEMS = tuple(
    sorted(NUMERIC_CONSTRAINTS, key=lambda item: -len(_normalize_label(item[0])))
)
STORE_ALIASES = tuple(_load_json_config("store_aliases.json")["store_aliases"])
STORE_METADATA_KEYS = frozenset(
    {
        "area",
        "district",
        "direction",
        "locality",
        "neighborhood",
        "neighbourhood",
        "region",
        "store_area",
        "zone",
    }
)


@dataclass(frozen=True)
class ParsedConstraint:
    key: str
    value_text: str = ""
    value_number: float | None = None


@dataclass(frozen=True)
class ParsedCatalogueLookup:
    kind_name: str
    brand: str
    family_name: str
    constraints: list[ParsedConstraint]


@dataclass(frozen=True)
class ParsedAvailabilityTask:
    threshold: int
    store_phrase: str
    products: list[ParsedCatalogueLookup]
    comparator: str


def basket_id_from_task(task_text: str) -> str:
    match = re.search(r"\bbasket[_ -](\d+)\b", task_text, re.I)
    return f"basket_{match.group(1)}" if match else ""


def payment_id_from_task(task_text: str) -> str:
    match = re.search(r"\bpay[_ -](\d+)\b", task_text, re.I)
    return f"pay_{match.group(1)}" if match else ""


def requested_count_kind(task_text: str) -> str:
    patterns = (
        r"how many catalogue products are\s+(.+?)(?=\?|\.?\s+answer\b|$)",
        r"catalogue count report,\s+how many products are\s+(.+?)(?=\?|\.?\s+answer\b|$)",
        r"how many products are\s+(.+?)(?=\?|\.?\s+answer\b|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, task_text, re.I)
        if match:
            kind = " ".join(match.group(1).split())
            kind = re.sub(r"\s+in\s+catalogue$", "", kind, flags=re.I)
            return kind
    return ""


def exact_count_message(task_text: str, count: int) -> str:
    def apply_count_template(template: str) -> str:
        return (
            template
            .replace("%d", str(count))
            .replace("%VALUE%", str(count))
            .replace("the_actual_number", str(count))
            .replace("NUMBER", str(count))
            .replace("VALUE", str(count))
            .replace("\\t", "\t")
        )

    match = re.search(r'answer in exactly format\s+"([^"]+)"', task_text, re.I)
    if match:
        return apply_count_template(match.group(1))
    match = re.search(r"answer (?:format|pattern):\s*`?\"?([^`\"\n.]+)\"?`?", task_text, re.I)
    if match:
        return apply_count_template(match.group(1).strip())
    return str(count)


def exact_quantity_message(task_text: str, count: int) -> str:
    match = re.search(r'answer exactly as\s+"([^"]+)"', task_text, re.I)
    if match:
        return match.group(1).replace("%d", str(count))
    return exact_count_message(task_text, count)


def parse_constraints(text: str) -> list[ParsedConstraint]:
    constraints: list[ParsedConstraint] = []
    normalized = text.strip().rstrip("?")
    parts = re.split(r",\s*(?:and\s+)?|\s+and\s+", normalized)
    for part in parts:
        item = " ".join(part.strip().split())
        if not item:
            continue
        if item.lower().startswith("has "):
            item = item[4:].strip()
        lowered = item.lower()

        volume = re.match(r"volume\s+(\d+(?:\.\d+)?)\s*(ml|l)\b", lowered)
        if volume:
            unit = volume.group(2)
            constraints.append(
                ParsedConstraint(
                    key=f"volume_{unit}",
                    value_number=float(volume.group(1)),
                )
            )
            continue

        length = re.match(r"length\s+(\d+(?:\.\d+)?)\s*(mm|m)\b", lowered)
        if length:
            unit = length.group(2)
            constraints.append(
                ParsedConstraint(
                    key=f"length_{unit}",
                    value_number=float(length.group(1)),
                )
            )
            continue

        matched_numeric = False
        for label, key, unit in NUMERIC_CONSTRAINT_ITEMS:
            match = re.match(
                rf"{_label_regex(label)}\s+(\d+(?:\.\d+)?){_unit_pattern(unit)}",
                item,
                re.I,
            )
            if match:
                constraints.append(
                    ParsedConstraint(key=key, value_number=float(match.group(1)))
                )
                matched_numeric = True
                break
        if matched_numeric:
            continue

        for label, key in TEXT_CONSTRAINT_ITEMS:
            match = re.match(rf"{_label_regex(label)}\s+(.+)$", item, re.I)
            if match:
                constraints.append(
                    ParsedConstraint(
                        key=key,
                        value_text=_clean_text_constraint_value(match.group(1)),
                    )
                )
                break
    return constraints


def _parse_availability_products(products_blob: str) -> list[ParsedCatalogueLookup] | None:
    product_parts = re.split(r",\s*the\s+", products_blob)
    products = []
    for idx, part in enumerate(product_parts):
        text = part.strip()
        if idx > 0 and not text.lower().startswith("the "):
            text = "the " + text
        product = re.match(
            r"the\s+(.+?)\s+from\s+(.+?)\s+in\s+the\s+(.+?)\s+line\s+that\s+has\s+(.+)$",
            text,
            re.I | re.S,
        )
        if not product:
            return None
        products.append(
            ParsedCatalogueLookup(
                kind_name=" ".join(product.group(1).split()),
                brand=" ".join(product.group(2).split()),
                family_name=" ".join(product.group(3).split()),
                constraints=parse_constraints(product.group(4)),
            )
        )
    return products


def parse_availability_task(task_text: str):
    no_same_day_match = re.search(
        r"how many of these products have no same-day availability in\s+"
        r"(?:the\s+)?(.+?)\s+today:\s+(.+?)(?:\?\s*)?answer",
        task_text,
        re.I | re.S,
    )
    if no_same_day_match:
        products = _parse_availability_products(no_same_day_match.group(2))
        if products is None:
            return None
        return ParsedAvailabilityTask(
            threshold=1,
            store_phrase=" ".join(no_same_day_match.group(1).split()),
            products=products,
            comparator="lt",
        )

    no_available_match = re.search(
        r"(?:^|.*?\b)(?:at|in)\s+(?:the\s+)?(.+?),\s*how many of these(?:\s+just)?\s+"
        r"are\s+not\s+available\s+today:\s+(.+?)(?:\?\s*)?answer",
        task_text,
        re.I | re.S,
    )
    if no_available_match:
        products = _parse_availability_products(no_available_match.group(2))
        if products is None:
            return None
        return ParsedAvailabilityTask(
            threshold=1,
            store_phrase=" ".join(no_available_match.group(1).split()),
            products=products,
            comparator="lt",
        )

    patterns = (
        (
            "gte",
            r"how many of these products have at least\s+(\d+)\s+items?\s+available in\s+"
            r"(?:the\s+)?(.+?)\s+today:\s+(.+?)(?:\?\s*)?answer",
            1,
            2,
            3,
        ),
        (
            "gte",
            r"how many of these products have\s+(\d+)\s+or more ready in\s+"
            r"(?:the\s+)?(.+?)\s+today:\s+(.+?)(?:\?\s*)?answer",
            1,
            2,
            3,
        ),
        (
            "gte",
            r"(?:^|.*?\b)check\s+(?:the\s+)?(.+?)\s+today\s+and\s+tell\s+me\s+how many of these "
            r"have\s+(\d+)\s+or more ready:\s+(.+?)(?:\?\s*)?answer",
            2,
            1,
            3,
        ),
        (
            "lt",
            r"how many of these products have\s+(?:less|fewer)\s+than\s+"
            r"(\d+)\s+items?\s+available in\s+(?:the\s+)?(.+?)\s+today:\s+"
            r"(.+?)(?:\?\s*)?answer",
            1,
            2,
            3,
        ),
        (
            "lt",
            r"how many\s+(?:of these products|from this list)\s+(?:are\s+)?(?:below|less than|fewer than)\s+"
            r"(\d+)\s+available\s+today\s+at\s+(?:the\s+)?(.+?):\s+(.+?)(?:\?\s*)?answer",
            1,
            2,
            3,
        ),
        (
            "lt",
            r"(?:^|.*?\b)check\s+(?:the\s+)?(.+?),\s*how many of these have\s+"
            r"(?:less|fewer)\s+than\s+(\d+)\s+available\s+today:\s+(.+?)(?:\?\s*)?answer",
            2,
            1,
            3,
        ),
    )
    for comparator, pattern, threshold_group, store_group, products_group in patterns:
        match = re.search(pattern, task_text, re.I | re.S)
        if not match:
            continue
        products = _parse_availability_products(match.group(products_group))
        if products is None:
            return None
        return ParsedAvailabilityTask(
            threshold=int(match.group(threshold_group)),
            store_phrase=" ".join(match.group(store_group).split()),
            products=products,
            comparator=comparator,
        )
    return None


def _normalize_store_phrase(value: str) -> str:
    value = value.lower()
    replacements = {
        "northern": "north",
        "southern": "south",
        "western": "west",
        "eastern": "east",
        "centre": "center",
        "centre-side": "center",
        "center-side": "center",
        "west-side": "west side",
        "east-side": "east side",
    }
    for source, target in replacements.items():
        value = re.sub(rf"\b{re.escape(source)}\b", target, value)
    return " ".join(re.split(r"[^a-z0-9]+", value))


def _alias_matches(phrase: str, text: str) -> bool:
    phrase = _normalize_store_phrase(phrase)
    text = _normalize_store_phrase(text)
    if not text:
        return False
    if phrase in text or text in phrase:
        return True
    phrase_tokens = {token for token in phrase.split() if token}
    text_tokens = {token for token in text.split() if token}
    return bool(phrase_tokens) and phrase_tokens.issubset(text_tokens)


def store_name_alias(city: str, task_text: str, current_name_contains: str = "") -> str:
    if _store_aliases_disabled():
        return ""
    city_lower = city.lower()
    current = current_name_contains.lower()
    for alias in STORE_ALIASES:
        name_contains = alias["name_contains"]
        if alias["city"].lower() == city_lower and _alias_matches(alias["phrase"], task_text):
            if name_contains.lower() not in current:
                return name_contains
    return ""


def _store_aliases_disabled() -> bool:
    return os.getenv("ECOM_DISABLE_STORE_ALIASES", "").lower() in {"1", "true", "yes"}


def _store_row_metadata_score(row: dict[str, str], phrase: str) -> int:
    score = 0
    for key, value in row.items():
        if not value:
            continue
        lowered_key = key.lower()
        if lowered_key in STORE_METADATA_KEYS and _alias_matches(value, phrase):
            score += 6
        elif lowered_key in {"alias", "aliases", "description", "descriptor"} and _alias_matches(value, phrase):
            score += 4
    return score


def select_store(store_rows: list[dict[str, str]], store_phrase: str):
    phrase = _normalize_store_phrase(store_phrase)
    best_score = -1
    best_has_store_signal = False
    best = None
    city_matches = []
    for row in store_rows:
        city = row.get("city", "").lower()
        name = row.get("name", "").lower()
        store_id = row.get("id", "").lower()
        score = 0
        has_store_signal = False
        city_in_phrase = bool(city and city in phrase)
        if city_in_phrase:
            score += 5
            city_matches.append(row)
        metadata_score = _store_row_metadata_score(row, phrase)
        if metadata_score:
            score += metadata_score
            has_store_signal = True
        for token in re.split(r"[^a-z0-9]+", name):
            if token and token not in {"powertool", city} and token in phrase:
                score += 4
                has_store_signal = True
        for token in re.split(r"[^a-z0-9]+", store_id):
            if token and token not in {"store", "powertool", city} and token in phrase:
                score += 3
                has_store_signal = True
        if not _store_aliases_disabled():
            for alias in STORE_ALIASES:
                if alias["city"].lower() != city:
                    continue
                if _alias_matches(alias["phrase"], phrase) and alias["store_id"].lower() == store_id:
                    score += 3
                    has_store_signal = True
        if score > best_score:
            best_score = score
            best_has_store_signal = has_store_signal
            best = row
    if best is not None and not best_has_store_signal:
        unique_city_ids = {row.get("id", "") for row in city_matches}
        if len(unique_city_ids) == 1:
            return city_matches[0]
        return None
    if best_score < 4:
        return None
    return best
