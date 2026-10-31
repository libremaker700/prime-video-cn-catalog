#!/usr/bin/env python3
"""Publish a small, identity-bound source-verification snapshot without source data.

The input is an actual preprocessing result, not evidence that a new check ran.
This publisher never queries providers or advances a record's checkedAt value.
"""
import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 512 * 1024
MAX_ITEMS = 2000
FIELDS = {'officialId', 'originalTitle', 'kind', 'season', 'year', 'status', 'checkedAt'}
STATUSES = {'matched', 'no_match', 'unavailable', 'pending'}
PATH = re.compile(r'source-verification/[a-f0-9]{64}\.json')
MANIFEST_FIELDS = {'schema', 'path', 'sha256', 'count', 'checkedAt', 'publishedAt', 'previousPath'}


def timestamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid source check timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('invalid source check timestamp') from None
    if parsed.tzinfo is None:
        raise ValueError('source check timestamp needs a timezone')
    return parsed.astimezone(timezone.utc)


def validate_snapshot(payload, *, clock=None):
    """Exact allowlists keep provider IDs, endpoints, sessions and keys private."""
    clock = clock or datetime.now(timezone.utc)
    if not isinstance(payload, dict) or set(payload) != {'schema', 'items'} or type(payload.get('schema')) is not int or payload.get('schema') != 1:
        raise ValueError('invalid source verification schema')
    rows = payload.get('items')
    if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
        raise ValueError('source verification item limit')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError('unexpected source verification field')
        ident, original = row['officialId'], row['originalTitle']
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Z0-9]{10,26}', ident):
            raise ValueError('invalid source official identity')
        if (not isinstance(original, str) or not 1 <= len(original.strip()) <= 200
                or re.search(r'[\x00-\x1f<>]|https?://|(?:tvly|AIza)[A-Za-z0-9_-]{12,}', original, re.I)):
            raise ValueError('invalid source original title')
        kind, season = row['kind'], row['season']
        if kind not in ('movie', 'tv') or type(season) is not int or not (season == 0 if kind == 'movie' else 1 <= season <= 200):
            raise ValueError('invalid source medium or season')
        if type(row['year']) is not int or not 1900 <= row['year'] <= 2199:
            raise ValueError('invalid source identity year')
        key = (ident, kind, season)
        if key in seen:
            raise ValueError('duplicate source official identity and season')
        seen.add(key)
        if not isinstance(row['status'], str) or row['status'] not in STATUSES:
            raise ValueError('invalid source verification status')
        checked = timestamp(row['checkedAt'], nullable=row['status'] == 'pending')
        if checked is not None and checked > clock + timedelta(minutes=5):
            raise ValueError('source verification cannot be future dated')
    return payload


def encoded(payload):
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('source verification byte limit')
    return raw


def latest_check(payload):
    checked = [timestamp(row['checkedAt']) for row in payload['items'] if row['checkedAt'] is not None]
    return max(checked).isoformat(timespec='seconds') if checked else None


def merge_snapshots(previous, incoming, *, clock):
    """Partial exports retain old slots; incomplete checks retain fresh positives."""
    def slot(row):
        return row['officialId'], row['kind'], row['season']

    rows = {slot(row): row for row in previous['items']}
    for row in incoming['items']:
        saved = rows.get(slot(row))
        same_identity = (saved is not None and saved['originalTitle'] == row['originalTitle']
                         and saved['year'] == row['year'])
        if (same_identity and saved['status'] == 'matched'
                and row['status'] in ('pending', 'unavailable')
                and clock - timestamp(saved['checkedAt']) < timedelta(days=7)):
            continue
        rows[slot(row)] = row
    merged = {'schema': 1, 'items': list(rows.values())}
    return validate_snapshot(merged, clock=clock)


def safe_path(docs, relative):
    target = docs / relative
    if not target.resolve().is_relative_to(docs.resolve()) or target.is_symlink():
        raise ValueError('unsafe source publication path')
    return target


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('source publication target is a symlink')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def validate_publication(docs, *, clock=None, require_convenience=True):
    docs = Path(docs)
    manifest_path = docs / 'source-verification-version.json'
    convenience = docs / 'source-verification.json'
    if not manifest_path.exists():
        if convenience.exists():
            raise ValueError('source verification has no manifest')
        return None
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or set(manifest) - MANIFEST_FIELDS
            or not (MANIFEST_FIELDS - {'previousPath'}).issubset(manifest)
            or manifest.get('schema') != 1 or type(manifest.get('count')) is not int
            or not PATH.fullmatch(str(manifest.get('path', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(manifest.get('sha256', '')))):
        raise ValueError('invalid source verification manifest')
    if manifest.get('previousPath') and not PATH.fullmatch(manifest['previousPath']):
        raise ValueError('invalid prior source verification path')
    timestamp(manifest['publishedAt'])
    raw = safe_path(docs, manifest['path']).read_bytes()
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != manifest['sha256']:
        raise ValueError('source verification hash mismatch')
    if manifest['path'] != 'source-verification/' + manifest['sha256'] + '.json':
        raise ValueError('source verification path/hash mismatch')
    payload = validate_snapshot(json.loads(raw), clock=clock)
    if manifest['count'] != len(payload['items']) or manifest['checkedAt'] != latest_check(payload):
        raise ValueError('source verification summary mismatch')
    if require_convenience and (not convenience.is_file() or convenience.read_bytes() != raw):
        raise ValueError('source verification convenience copy mismatch')
    return manifest


def publish(root=ROOT, *, input_path=None, clock=None):
    root = Path(root)
    input_path = Path(input_path) if input_path is not None else root / 'data/source-verification.json'
    docs = root / 'docs'
    clock = clock or datetime.now(timezone.utc)
    if not input_path.is_file():
        previous = validate_publication(docs, clock=clock)
        return {'status': 'skipped_no_preprocessing_result', 'count': previous['count'] if previous else 0,
                'network_requests': 0, 'fresh_checks': 0}
    if input_path.stat().st_size > MAX_BYTES:
        raise ValueError('source verification input byte limit')
    payload = validate_snapshot(json.loads(input_path.read_text(encoding='utf-8')), clock=clock)
    previous = validate_publication(docs, clock=clock, require_convenience=False)
    if previous and previous['count'] and not payload['items']:
        raise ValueError('empty source input cannot erase an existing publication')
    if previous:
        saved = json.loads(safe_path(docs, previous['path']).read_bytes())
        payload = merge_snapshots(saved, payload, clock=clock)
    raw = encoded(payload)
    digest = hashlib.sha256(raw).hexdigest()
    relative = 'source-verification/' + digest + '.json'
    if previous and previous['sha256'] == digest:
        convenience = docs / 'source-verification.json'
        if not convenience.is_file() or convenience.read_bytes() != raw:
            atomic_write(convenience, raw)
        return {'status': 'unchanged', 'count': len(payload['items']), 'sha256': digest,
                'network_requests': 0, 'fresh_checks': 0}
    manifest = {'schema': 1, 'path': relative, 'sha256': digest, 'count': len(payload['items']),
                'checkedAt': latest_check(payload), 'publishedAt': clock.astimezone(timezone.utc).isoformat(timespec='seconds')}
    if previous:
        manifest['previousPath'] = previous['path']
    # Immutable content comes first. The App reads this manifest last and verifies bytes.
    atomic_write(safe_path(docs, relative), raw)
    atomic_write(docs / 'source-verification.json', raw)
    atomic_write(docs / 'source-verification-version.json', encoded(manifest))
    validate_publication(docs, clock=clock)
    keep = {relative, manifest.get('previousPath')}
    for path in (docs / 'source-verification').glob('*.json'):
        rel = path.relative_to(docs).as_posix()
        if PATH.fullmatch(rel) and rel not in keep:
            safe_path(docs, rel).unlink()
    return {'status': 'published_preprocessing_result', 'count': len(payload['items']),
            'sha256': digest, 'network_requests': 0, 'fresh_checks': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        manifest = validate_publication(ROOT / 'docs')
        print(json.dumps({'status': 'valid' if manifest else 'not_published', 'count': manifest['count'] if manifest else 0}))
    else:
        print(json.dumps(publish(input_path=args.input)))


if __name__ == '__main__':
    main()
