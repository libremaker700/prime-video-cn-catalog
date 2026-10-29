#!/usr/bin/env python3
"""Refresh every catalogue record; publish only validated, complete batch results."""
import argparse
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from catalog import match, merge, now, publish, repair, write_json
from sources import Clients, SourceError

ROOT = Path(__file__).resolve().parent.parent


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def due(version, interval=604800, clock=None):
    """Bad/future timestamps cannot indefinitely suppress refreshes."""
    try:
        last = datetime.fromisoformat(version["last_success_at"])
        clock = clock or datetime.now(timezone.utc)
        if last.tzinfo is None:
            return True
        age = clock - last
        return age.total_seconds() < 0 or age >= timedelta(seconds=interval)
    except (KeyError, ValueError, TypeError):
        return True


def add_seeds(items, seeds):
    """Curated seeds extend the full catalogue; they never replace its worklist."""
    for seed in seeds:
        rec = {"id": f'prime:{seed["prime_id"]}' if seed.get("prime_id") else f'seed:{seed["slug"]}',
               "slug": seed["slug"], "kind": seed["kind"], "year": seed.get("year"),
               "original_title": seed.get("original_title"), "english_title": seed.get("english_title"),
               "prime_id": seed.get("prime_id"), "display_name_zh": seed.get("english_title") or seed["original_title"],
               "name_status": "pending_review", "identity_status": "pending_review", "seasons": []}
        existing = match(rec, items)
        if existing:
            rec = existing
        else:
            items.append(rec)
        sns = {s["season"] for s in rec.get("seasons", [])}
        for sn in seed.get("seasons", []):
            if sn not in sns:
                rec.setdefault("seasons", []).append({"season": sn, "id": f'{rec["id"]}:s{sn}', "synopsis_status": "missing"})
    return items


def refresh_record(record, clients):
    out = copy.deepcopy(record)
    failures, checked = [], []
    # Root ID can identify the selected season, not the series as a whole.
    targets = {}
    if out.get("prime_id"):
        targets[out["prime_id"]] = None
    for season in out.get("seasons", []):
        season_id = season.get("official_id") or season.get("official_compact_id")
        if season_id:
            targets[season_id] = season["season"]
    visited = set()
    while any(asin not in visited for asin in targets):
        asin = next(asin for asin in targets if asin not in visited)
        expected = targets[asin]
        visited.add(asin)
        try:
            try:
                data = clients.prime(asin, expected)
            except SourceError:
                # The official selector may expose a working compact URL for a stale ASIN.
                mapping = next((s for s in out.get("seasons", []) if s.get("official_id") == asin), {})
                alternative = mapping.get("official_compact_id")
                if not alternative or alternative == asin:
                    raise
                data = clients.prime(alternative, mapping["season"])
            sn = int(data.get("seasonNumber") or 0)
            previous = next((s for s in out.get("seasons", []) if s["season"] == sn), out)
            out = merge(out, clients.prime_patch(asin, data, previous))
            checked.append("prime_official:" + asin)
            # The official selector can expose seasons absent from the imported snapshot.
            for season in out.get("seasons", []):
                season_id = season.get("official_id") or season.get("official_compact_id")
                if season_id:
                    targets[season_id] = season["season"]
        except SourceError as exc:
            failures.append({"source": "prime_official", "official_id": asin, "reason": str(exc)})
    try:
        extra = clients.tmdb(out)
        if extra:
            if out.get("synopsis_source_type") == "prime_official":
                for key in ("synopsis_zh", "synopsis_source_type", "synopsis_source_url", "synopsis_status"):
                    extra.pop(key, None)
            for season in extra.get("seasons", []):
                old = next((s for s in out.get("seasons", []) if s["season"] == season["season"]), {})
                if old.get("synopsis_source_type") == "prime_official":
                    for key in ("synopsis_zh", "synopsis_source_type", "synopsis_source_url", "synopsis_status"):
                        season.pop(key, None)
            out = merge(out, extra)
            checked.append("tmdb")
    except SourceError as exc:
        failures.append({"source": "tmdb", "reason": str(exc)})
    if out.get("name_status") in ("pending_review", "existing_app_curated_not_fresh_web_verified"):
        try:
            extra = clients.wiki(out)
            if extra:
                if out.get("synopsis_zh"):
                    for key in ("synopsis_zh", "synopsis_source_type", "synopsis_source_url", "synopsis_status"):
                        extra.pop(key, None)
                out = merge(out, extra)
                checked.append("wikipedia")
        except SourceError as exc:
            failures.append({"source": "wikipedia", "reason": str(exc)})
    out["refresh_status"] = "failed" if failures else ("checked" if checked else "pending_source_or_identity")
    if checked:
        out["metadata_checked_at"] = now()
        out["updated_at"] = now()
    return out, {"id": record["id"], "checked": checked, "failures": failures, "status": out["refresh_status"]}


def discover(items, clients, limit):
    queue, added, failures = [], [], []
    known = {i.get("prime_id") for i in items}
    known |= {s.get("official_id") for i in items for s in i.get("seasons", [])}
    try:
        candidates = clients.discover(limit)
    except SourceError as exc:
        return [], [], [{"source": "prime_storefront", "reason": str(exc)}]
    for candidate in candidates:
        asin = candidate["prime_id"]
        if asin in known:
            continue
        entry = dict(candidate, status="pending_review")
        try:
            data = clients.prime(asin)
            television = data["entityType"] == "TV Show"
            title = data.get("parentTitle") or data["title"]
            same_name = [i for i in items + added if title in (i.get("display_name_zh"), i.get("prime_official_name_zh")) or title in i.get("aliases_zh", [])]
            rec = {"id": f"prime:{asin}", "slug": "prime-" + asin.lower(), "prime_id": asin,
                   "kind": "tv" if television else "movie", "identity_status": "verified_official_mapping",
                   "display_name_zh": title, "name_status": "verified_prime_official",
                   "prime_official_name_zh": data["title"], "seasons": [], "sources": [],
                   "original_title": data.get("originalTitle"), "aliases_zh": [], "details": {}}
            prime_patch = clients.prime_patch(asin, data)
            series_id = prime_patch.get("prime_series_id")
            series_matches = [i for i in items + added if series_id and i.get("prime_series_id") == series_id]
            if series_matches or same_name or (television and not series_id):
                entry["reason"] = "possible existing series/new season; identity needs confirmation"
            else:
                rec = merge(rec, prime_patch)
                rec["discovery_source"] = candidate["discovery_source"]
                added.append(rec)
                known.add(asin)
                entry["status"] = "verified_public_prime_page"
        except SourceError as exc:
            entry["reason"] = str(exc)
            failures.append({"source": "discovery_detail", "official_id": asin, "reason": str(exc)})
        queue.append(entry)
    return queue, added, failures


def run(root=ROOT, argv=None, clients_factory=Clients):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--due", action="store_true")
    ap.add_argument("--repair", action="store_true", help="Restore snapshot fields; do not claim a successful crawl")
    ap.add_argument("--no-discovery", action="store_true", help="Refresh existing catalogue only")
    args = ap.parse_args(argv)
    docs = root / "docs"
    previous = load(docs / "version.json", {})
    interval = int(os.getenv("UPDATE_INTERVAL_SECONDS", "604800"))
    if interval < 3600:
        raise ValueError("UPDATE_INTERVAL_SECONDS must be >=3600")
    if args.due and not args.force and not args.repair and not due(previous, interval):
        print("Not due: less than 7 days since last completed successful refresh. No files changed.")
        return 0
    current = load(docs / "catalog.json", {"items": []})
    baseline = load(root / "data/fallback_catalog.json", {"items": []})["items"]
    rules = load(root / "data/title-overrides.json", {"rules": []})["rules"]
    items = repair(current["items"], baseline, rules)
    seeds = yaml.safe_load((root / "config/seeds.yaml").read_text(encoding="utf-8"))["seeds"]
    items = add_seeds(items, seeds)
    if args.repair:
        version = publish(docs, items, previous, reason="restore_snapshot_identity_fields")
        write_json(root / "logs/last_repair.json", {"status": "repaired", "version": version, "note": "No network crawl; last_success_at was not advanced."})
        print(f'Repaired {len(items)} records; version={version["version"]}; success timestamp preserved.')
        return 0
    started = now()
    clients = clients_factory(docs, print)
    refreshed, reports = [], []
    for item in items:
        record, report = refresh_record(item, clients)
        refreshed.append(record)
        reports.append(report)
        print(f'{len(reports)}/{len(items)} {item["id"]}: {report["status"]}', flush=True)
    queue, additions, discovery_failures = ([], [], []) if args.no_discovery else discover(refreshed, clients, int(os.getenv("DISCOVERY_LIMIT", "20")))
    failures = sum(len(r["failures"]) for r in reports) + len(discovery_failures)
    checked = sum(bool(r["checked"]) for r in reports)
    report = {"started_at": started, "finished_at": now(), "status": "failed" if failures or not checked else "success",
              "processed": len(items), "checked": checked, "retained_without_refresh": len(items) - checked,
              "new_verified_titles": len(additions), "failures": failures, "records": reports,
              "discovery_failures": discovery_failures, "discovery_queue": queue}
    if failures or not checked:
        write_json(root / "logs/last_attempt.json", report)
        print(f"Refresh incomplete: {failures} errors, {checked} checked. Published files and last_success_at remain unchanged.")
        return 2
    refreshed += additions
    if len(refreshed) < len(current["items"]):
        raise ValueError("refresh would drop existing records")
    refreshed = repair(refreshed, baseline, rules)
    version = publish(docs, refreshed, previous, success=True)
    report["version"] = version
    write_json(root / "logs/last_run.json", report)
    write_json(root / "data/discovery_queue.json", queue)
    print(f'Published {len(refreshed)} records, {version["image_count"]} unique images, version={version["version"]}.')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Publication aborted: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
