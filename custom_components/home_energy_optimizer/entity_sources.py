"""Construct entity IDs from user configuration, without embedded site identifiers."""

from __future__ import annotations


def discover_unique_entity(entity_ids, prefix: str, suffix: str) -> str:
    matches = sorted({
        value for value in entity_ids
        if isinstance(value, str) and value.startswith(prefix)
        and value.endswith(suffix) and len(value) > len(prefix) + len(suffix)
    })
    return matches[0] if len(matches) == 1 else ""


def hanchu_entity(serial: str, suffix: str) -> str:
    slug = str(serial or "").strip().lower()
    return f"sensor.hanchuess_{slug}_{suffix}" if slug and suffix else ""


def meter_derived_entity(consumption_entity: str, original_suffix: str, target_suffix: str) -> str:
    if not consumption_entity or not consumption_entity.endswith(original_suffix):
        return ""
    return consumption_entity[:-len(original_suffix)] + "_" + target_suffix.lstrip("_")
