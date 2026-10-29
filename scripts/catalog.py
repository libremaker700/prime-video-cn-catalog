"""Catalogue identities, lossless merging, validation and versioned publication."""
import copy
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value or "").casefold() if c.isalnum())


def family(kind):
    return "tv" if kind in ("tv", "anime") else kind


def match(record, records):
    """Ambiguous title matches never choose an arbitrary record or cross media/year."""
    for field in ("id", "prime_id", "seed_id"):
        value = record.get(field)
        hits = [r for r in records if value and r.get(field) == value]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
    titles = {norm(record.get(k)) for k in ("original_title", "english_title")} - {""}
    hits = []
    for r in records:
        if family(r.get("kind")) != family(record.get("kind")):
            continue
        y1 = record.get("year") or record.get("details", {}).get("first_air_year")
        y2 = r.get("year") or r.get("details", {}).get("first_air_year")
        if y1 and y2 and str(y1) != str(y2):
            continue
        if titles & {norm(r.get(k)) for k in ("original_title", "english_title")}:
            hits.append(r)
    return hits[0] if len(hits) == 1 else None


def nonempty(value):
    return value is not None and value != "" and value != [] and value != {}


def merge(old, fresh):
    """Only supplied, nonempty fields replace existing values. Identity stays stable."""
    out = copy.deepcopy(old)
    for key, value in fresh.items():
        if key in ("id", "slug", "seed_id", "prime_id", "official_id") and out.get(key):
            if key in ("prime_id", "official_id") and value and value != out[key]:
                raise ValueError(f"conflicting {key}")
            continue
        if key == "seasons":
            seasons = {s["season"]: copy.deepcopy(s) for s in out.get(key, [])}
            for s in value:
                sn = s["season"]
                seasons[sn] = merge(seasons.get(sn, {}), s)
            out[key] = [seasons[n] for n in sorted(seasons)]
        elif key == "sources":
            sources = {(s.get("type"), s.get("url")): s for s in out.get(key, [])}
            for source in value:
                sources[(source.get("type"), source.get("url"))] = copy.deepcopy(source)
            out[key] = list(sources.values())
        elif key == "aliases_zh":
            out[key] = list(dict.fromkeys(out.get(key, []) + value))
        elif isinstance(value, dict) and key != "poster":
            out[key] = merge(out.get(key) or {}, value)
        elif nonempty(value):
            out[key] = copy.deepcopy(value)
    if old.get("name_status") == "user_app_preference":
        out["display_name_zh"] = old["display_name_zh"]
        out["name_status"] = old["name_status"]
        alternative = fresh.get("display_name_zh")
        if alternative and alternative != out["display_name_zh"]:
            out["aliases_zh"] = list(dict.fromkeys(out.get("aliases_zh", []) + [alternative]))
    if old.get("synopsis_zh") and not fresh.get("synopsis_zh"):
        for key in ("synopsis_status", "synopsis_source_type", "synopsis_source_url"):
            if key in old:
                out[key] = old[key]
    if old.get("poster") and not fresh.get("poster"):
        out["poster_status"] = old.get("poster_status", "ok")
        out["poster_scope"] = old.get("poster_scope")
    return out


def repair(items, baseline, overrides):
    repaired = []
    for item in items:
        snapshot = match(item, baseline)
        # Baseline restores lost fields; current nonempty text and images take precedence.
        out = merge(snapshot, item) if snapshot else copy.deepcopy(item)
        out["identity_status"] = "verified_official_mapping" if out.get("prime_id") else "pending_review"
        for s in out.get("seasons", []):
            s["id"] = f'{out["id"]}:s{s["season"]}'
            s["synopsis_status"] = "ok" if s.get("synopsis_zh") else ("translation_pending" if s.get("synopsis_original") else "missing")
            s.pop("status", None)
        for rule in overrides:
            if norm(rule["originalTitle"]) in {norm(out.get("original_title")), norm(out.get("english_title"))} and family(rule["mediaType"]) == family(out["kind"]):
                old_name = out.get("display_name_zh")
                out["display_name_zh"] = rule["preferredTitleZh"]
                out["name_status"] = "user_app_preference"
                out["aliases_zh"] = list(dict.fromkeys(out.get("aliases_zh", []) + rule.get("aliasesZh", []) + ([old_name] if old_name and old_name != rule["preferredTitleZh"] else [])))
        repaired.append(out)
    counts = {}
    for r in repaired:
        counts[r["slug"]] = counts.get(r["slug"], 0) + 1
    for r in repaired:
        if counts[r["slug"]] > 1:
            r["slug"] += "-" + hashlib.sha256(r["id"].encode()).hexdigest()[:8]
    return repaired


def poster_refs(items):
    for item in items:
        if item.get("poster"):
            yield item["id"], None, item["poster"], item.get("poster_scope")
        for s in item.get("seasons", []):
            if s.get("poster"):
                yield item["id"], s["season"], s["poster"], s.get("poster_scope")


def validate(catalog, docs, version=None, raw=None):
    errors = []
    items = catalog.get("items", [])
    if catalog.get("schema") != "prime-video-cn-catalog/1" or not items:
        errors.append("invalid or empty catalogue")
    if catalog.get("item_count") != len(items):
        errors.append("catalogue count mismatch")
    for field in ("id", "slug"):
        values = [x.get(field) for x in items]
        if not all(values) or len(set(values)) != len(values):
            errors.append(f"missing/duplicate {field}")
    official_ids = [i["prime_id"] for i in items if i.get("prime_id")]
    if len(set(official_ids)) != len(official_ids):
        errors.append("duplicate official root ID")
    for item in items:
        if not item.get("display_name_zh") or not item.get("identity_status"):
            errors.append(f'{item.get("id")}: missing identity/name status')
        if item.get("prime_id") and not re.fullmatch(r"(?:[A-Z0-9]{10}|[A-Z0-9]{26})", item["prime_id"]):
            errors.append(f'{item["id"]}: invalid official ID')
        sns = [s.get("season") for s in item.get("seasons", [])]
        if len(set(sns)) != len(sns) or any(type(n) is not int or n < 0 for n in sns):
            errors.append(f'{item["id"]}: invalid/duplicate season')
    checked = {}
    for owner, sn, poster, scope in poster_refs(items):
        try:
            path = poster["path"]
            if not re.fullmatch(r"images/[a-zA-Z0-9_-]+\.(?:jpg|png)", path):
                raise ValueError("unsafe image path")
            if scope != ("season" if sn is not None else "series"):
                raise ValueError("invalid image scope")
            if sn is not None and poster.get("season_number") not in (None, sn):
                raise ValueError("image belongs to a different season")
            if path not in checked:
                file = (docs / path).resolve()
                if not file.is_relative_to(docs.resolve()):
                    raise ValueError("image escapes docs")
                data = file.read_bytes()
                with Image.open(file) as im:
                    im.load()
                    w, h = im.size
                if w < 960 or abs(w / h - 16 / 9) > 0.03:
                    raise ValueError("not a >=960px landscape image")
                checked[path] = (hashlib.sha256(data).hexdigest(), w, h)
            digest, w, h = checked[path]
            if (digest, w, h) != (poster.get("sha256"), poster.get("width"), poster.get("height")):
                raise ValueError("image hash/dimensions mismatch")
        except (KeyError, OSError, ValueError) as exc:
            errors.append(f"{owner} S{sn}: {exc}")
    if version:
        if raw is None or hashlib.sha256(raw).hexdigest() != version.get("catalog_sha256"):
            errors.append("catalogue hash mismatch")
        if version.get("version") != version.get("catalog_sha256", "")[:16]:
            errors.append("version/hash mismatch")
        if version.get("item_count") != len(items) or version.get("image_count") != len(checked):
            errors.append("manifest counts mismatch")
        rel = version.get("catalog_path")
        if rel:
            if not re.fullmatch(r"releases/[a-f0-9]{64}/catalog\.json", rel) or not (docs / rel).is_file() or (docs / rel).read_bytes() != raw:
                errors.append("immutable release mismatch")
    return errors


def write_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)
    return data


def publish(docs, items, previous, *, success=False, reason="sync"):
    catalog = {"schema": "prime-video-cn-catalog/1", "generated_at": now(), "item_count": len(items), "items": items}
    failures = validate(catalog, docs)
    if failures:
        raise ValueError("; ".join(failures))
    raw = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    rel = f"releases/{sha}/catalog.json"
    version = {"version": sha[:16], "catalog_sha256": sha, "catalog_path": rel,
               "generated_at": catalog["generated_at"],
               "last_success_at": catalog["generated_at"] if success else previous.get("last_success_at"),
               "publication_reason": reason, "item_count": len(items),
               "image_count": len({p["path"] for _, _, p, _ in poster_refs(items)})}
    release = docs / rel
    release.parent.mkdir(parents=True, exist_ok=True)
    if release.exists() and release.read_bytes() != raw:
        raise ValueError("immutable release already exists with different bytes")
    if not release.exists():
        temp_release = release.with_name(".catalog.tmp")
        temp_release.write_bytes(raw)
        os.replace(temp_release, release)
    # Readers fetch manifest -> immutable release -> hash. Manifest is the final pointer.
    temp = docs / ".catalog.tmp"
    temp.write_bytes(raw)
    os.replace(temp, docs / "catalog.json")
    write_json(docs / "version.json", version)
    return version
