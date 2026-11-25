"""Match the reviewed Prime exclusion registry against base catalog records."""

import json
import re
from pathlib import Path
from uuid import UUID


REGISTRY = Path(__file__).resolve().parents[1] / "data" / "base-catalog-exclusions.json"
PREFIX = "amzn1.dv.gti."
GTI = re.compile(r"amzn1\.dv\.gti\.[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}")
COMPACT = re.compile(r"[0-9A-Z]{26}")
NAMESPACE = 1 << 128


def official_id(value):
    if not isinstance(value, str):
        return ""
    if GTI.fullmatch(value):
        return value
    if COMPACT.fullmatch(value):
        number = int(value, 36) - NAMESPACE
        if 0 <= number < NAMESPACE:
            return PREFIX + str(UUID(int=number))
    return ""


def valid_entry(entry):
    if not isinstance(entry, list) or len(entry) != 4:
        return False
    gti, kind, season, year = entry
    return (isinstance(gti, str) and bool(GTI.fullmatch(gti)) and kind in ("movie", "tv")
            and type(season) is int and (season == 0 if kind == "movie" else season > 0)
            and type(year) is int and 1800 <= year <= 2200)


def read_registry(path=REGISTRY):
    registry = json.loads(path.read_bytes())
    entries = registry.get("entries") if isinstance(registry, dict) and registry.get("schema") == 1 else None
    if not entries or not isinstance(entries, list) or not all(map(valid_entry, entries)):
        raise ValueError("Missing or malformed reviewed catalog exclusions")
    result = {gti: (kind, season, year) for gti, kind, season, year in entries}
    if len(result) != len(entries):
        raise ValueError("Duplicate reviewed catalog exclusion")
    return result


def excluded(record, kind, number, registry):
    ids = {gti for field in ("official_catalog_id", "official_id", "official_compact_id", "prime_id")
           if (gti := official_id(record.get(field)))}
    matches = ids & registry.keys()
    if not matches:
        return False
    if len(ids) != 1:
        raise ValueError("Conflicting official IDs for a reviewed catalog exclusion")
    rule_kind, rule_season, rule_year = registry[matches.pop()]
    if (rule_kind, rule_season) != (kind, number):
        raise ValueError("Type or season conflict for a reviewed catalog exclusion")
    year = record.get("year")
    if year and str(year) != str(rule_year):
        raise ValueError("Year conflict for a reviewed catalog exclusion")
    return True


def filter_items(items, registry):
    kept = []
    for item in items:
        if item.get("kind") == "movie":
            if not excluded(item, "movie", 0, registry):
                kept.append(item)
            continue
        seasons = item.get("seasons", [])
        retained = [season for season in seasons if not excluded(season, "tv", season["season"], registry)]
        if len(retained) == len(seasons):
            kept.append(item)
        elif retained:
            # A series poster can be inherited from its default season.
            # Rebind only when it is the poster of a season just removed.
            updated = {**item, "seasons": retained}
            poster_path = (item.get("poster") or {}).get("path")
            if (poster_path and any((s.get("poster") or {}).get("path") == poster_path for s in seasons)
                    and not any((s.get("poster") or {}).get("path") == poster_path for s in retained)):
                substitute = next((s["poster"] for s in retained if s.get("poster")), None)
                if substitute is None:
                    updated.pop("poster", None)
                    updated.pop("poster_scope", None)
                else:
                    updated["poster"] = substitute
            kept.append(updated)
    return kept


def require_filtered(catalog, registry):
    if filter_items(catalog["items"], registry) != catalog["items"]:
        raise ValueError("Published catalog contains reviewed exclusions")
