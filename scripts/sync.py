#!/usr/bin/env python3
"""Refresh due records; publish a complete valid catalogue retaining failed entries."""
import argparse
import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from catalog import match, merge, now, publish, repair, write_json
from sources import Clients, SourceError
from title_lookup import apply_cache
from metadata_resolution import apply_summary_cache
from app_metadata import apply_app_metadata

ROOT = Path(__file__).resolve().parent.parent


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def due(version, interval=604800, clock=None):
    """Bad/future timestamps cannot indefinitely suppress refreshes."""
    try:
        if version.get('refresh_status') == 'partial' and version.get('last_attempt_at'):
            attempted = datetime.fromisoformat(version['last_attempt_at'])
            current = clock or datetime.now(timezone.utc)
            if attempted.tzinfo is not None:
                elapsed = (current - attempted).total_seconds()
                return elapsed < 0 or elapsed >= 21600
        last = datetime.fromisoformat(version["last_success_at"])
        clock = clock or datetime.now(timezone.utc)
        if last.tzinfo is None:
            return True
        age = clock - last
        return age.total_seconds() < 0 or age >= timedelta(seconds=interval)
    except (KeyError, ValueError, TypeError):
        return True


def record_due(record, interval, clock=None):
    current = clock or datetime.now(timezone.utc)
    status = record.get('refresh_status')
    key = 'metadata_checked_at' if status == 'checked' else 'metadata_attempted_at'
    wait = interval if status in ('checked', 'pending_source_or_identity') else 21600
    try:
        attempted = datetime.fromisoformat(record[key])
        if attempted.tzinfo is None:
            return True
        age = (current - attempted).total_seconds()
        return age < 0 or age >= wait
    except (KeyError, TypeError, ValueError):
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
    out['metadata_attempted_at'] = now()
    out["refresh_status"] = ("partial" if checked else "failed") if failures else ("checked" if checked else "pending_source_or_identity")
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
    app_asset = load(root / 'data/app-metadata.json', None)
    if app_asset:
        items = apply_app_metadata(items, app_asset)
    title_cache = load(root / "data/title-cache.json", {"records": {}})
    items = [apply_summary_cache(apply_cache(item, title_cache), title_cache) for item in items]
    seeds = yaml.safe_load((root / "config/seeds.yaml").read_text(encoding="utf-8"))["seeds"]
    items = add_seeds(items, seeds)
    if args.repair:
        version = publish(docs, items, previous, reason="restore_snapshot_identity_fields")
        write_json(root / "logs/last_repair.json", {"status": "repaired", "version": version, "note": "No network crawl; last_success_at was not advanced."})
        print(f'Repaired {len(items)} records; version={version["version"]}; success timestamp preserved.')
        return 0
    started = now()
    clients = clients_factory(docs, print)
    budget = int(os.getenv('SYNC_BUDGET_SECONDS', '1200'))
    if not 60 <= budget <= 1800:
        raise ValueError('SYNC_BUDGET_SECONDS must be between 60 and 1800')
    clients.deadline = time.monotonic() + budget
    refreshed, reports = [], []
    # Resume unvisited/oldest attempts first, while keeping the published order stable.
    worklist = sorted(items, key=lambda item: str(item.get('metadata_attempted_at') or ''))
    for item in worklist:
        if not args.force and not record_due(item, interval):
            record, report = copy.deepcopy(item), {'id': item['id'], 'checked': [], 'failures': [], 'status': 'reused', 'previous_status': item.get('refresh_status')}
        elif time.monotonic() >= clients.deadline:
            record, report = copy.deepcopy(item), {'id': item['id'], 'checked': [], 'failures': [], 'status': 'deferred', 'reason': 'refresh time budget reached'}
        else:
            record, report = refresh_record(item, clients)
        refreshed.append(record)
        reports.append(report)
        print(f'{len(reports)}/{len(items)} {item["id"]}: {report["status"]}', flush=True)
    order = {item['id']: n for n, item in enumerate(items)}
    refreshed.sort(key=lambda item: order[item['id']])
    reports.sort(key=lambda report: order[report['id']])
    budget_reached = time.monotonic() >= clients.deadline
    queue, additions, discovery_failures = ([], [], []) if args.no_discovery or budget_reached else discover(refreshed, clients, int(os.getenv("DISCOVERY_LIMIT", "20")))
    failures = sum(len(r["failures"]) for r in reports) + len(discovery_failures)
    checked = sum(bool(r["checked"]) for r in reports)
    reused = sum(r['status'] == 'reused' for r in reports)
    deferred = sum(r['status'] == 'deferred' for r in reports)
    pending = sum(r['status'] in ('pending_source_or_identity', 'deferred') or (r['status'] == 'reused' and r.get('previous_status') != 'checked') for r in reports)
    complete = not failures and not pending and all(r['status'] == 'checked' or (r['status'] == 'reused' and r.get('previous_status') == 'checked') for r in reports)
    status = 'success' if complete else 'partial' if checked or additions else 'failed'
    report = {"started_at": started, "finished_at": now(), "status": status,
              "processed": len(items), "checked": checked, "retained_without_refresh": len(items) - checked,
              "reused": reused, "pending": pending, "deferred": deferred,
              "attempted": len(items) - reused - deferred, "budget_seconds": budget,
              "new_verified_titles": len(additions), "failures": failures, "records": reports,
              "discovery_failures": discovery_failures, "discovery_queue": queue}
    if not checked and not additions:
        write_json(root / "logs/last_attempt.json", report)
        if not failures:
            print('No new verified source updates; published catalogue unchanged.')
            return 0
        print(f"Refresh incomplete: {failures} errors, {checked} checked. Published files and last_success_at remain unchanged.")
        return 2
    refreshed += additions
    if len(refreshed) < len(current["items"]):
        raise ValueError("refresh would drop existing records")
    refreshed = repair(refreshed, baseline, rules)
    refreshed = [apply_cache(item, title_cache) for item in refreshed]
    version = publish(docs, refreshed, previous, success=complete, reason='sync' if complete else 'partial_sync', refresh=report)
    report["version"] = version
    write_json(root / "logs/last_run.json", report)
    write_json(root / "data/discovery_queue.json", queue)
    print(f'Published {len(refreshed)} records ({status}, {failures} source errors retained), {version["image_count"]} images, version={version["version"]}.')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Publication aborted: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
