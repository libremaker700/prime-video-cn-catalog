"""Bounded preflight on CI; publish identity/status only, never provider bindings."""
import argparse
import copy
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit
import requests
from catalog import norm
from source_matching import select, with_release_evidence
from publish_source_verification import validate_snapshot

ROOT = Path(__file__).resolve().parents[1]


def scope(row):
    return tuple(row.get(k) for k in ('officialId', 'originalTitle', 'kind', 'season', 'year'))


def reference(target):
    return with_release_evidence({'prime_id': target['officialId'], 'english_title': target['originalTitle'],
        'kind': target['kind'], 'season': target['season'], 'year': target['year'],
        'display_name_zh': target.get('displayTitle') or target['originalTitle'],
        'aliases_zh': target.get('aliases', []), **({'releaseYearEvidence': target['releaseYearEvidence']} if target.get('releaseYearEvidence') else {})})


def calibrated_targets(targets, catalog):
    """Reuse newly verified names from this same workflow, without changing identity."""
    updated = copy.deepcopy(targets)
    for target in updated:
        for item in catalog.get('items', []):
            if not (str(item.get('name_status', '')).startswith('verified') or item.get('name_status') == 'user_app_preference'):
                continue
            kind = 'movie' if item.get('kind') == 'movie' else 'tv'
            if kind != target['kind'] or norm(item.get('english_title') or item.get('original_title')) != norm(target['originalTitle']):
                continue
            identities = [(item.get('prime_id'), 0 if kind == 'movie' else 1)]
            identities += [(s.get('official_id'), s.get('season')) for s in item.get('seasons', [])]
            if (target['officialId'], target['season']) not in identities:
                continue
            aliases = [item.get('display_name_zh')] + item.get('aliases_zh', []) + target.get('aliases', [])
            aliases = list(dict.fromkeys(value for value in aliases if isinstance(value, str) and 0 < len(value) <= 100))[:8]
            if aliases:
                target['aliases'] = aliases
                target['displayTitle'] = aliases[0]
    return updated


def native_playlist(row):
    groups = str(row.get('vod_play_url') or '')[:1_000_000].split('$$$')
    for group in groups:
        for episode in group.split('#'):
            if '$' not in episode:
                continue
            try:
                url = urlsplit(episode.split('$', 1)[1].strip())
                if (url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password
                        and re.search(r'\.(m3u8|mp4|mkv|mpd)$', url.path, re.I)):
                    return True
            except ValueError:
                continue
    return False


def candidate(row):
    from source_matching import candidate_season
    label = str(row.get('type_name') or '')
    kind = 'tv' if re.search('剧|劇|动漫|動畫|动画', label) and not re.search('电影|電影|动画片', label) else 'movie'
    result = {'provider_id': str(row.get('vod_id') or ''), 'title': str(row.get('vod_name') or ''),
              'kind': kind, 'year': row.get('vod_year'), 'has_routes': bool(native_playlist(row))}
    season = candidate_season(result)
    if season: result['season'] = season
    if re.search('综艺|綜藝|解说|解說|预告|直播|新闻', label): result['kind'] = 'excluded'
    for incoming, outgoing in [('vod_imdb', 'imdb_id'), ('vod_douban_id', 'douban_id')]:
        value = str(row.get(incoming) or '').strip()
        if value and value != '0': result[outgoing] = value
    return result


class Probe:
    def __init__(self, providers, deadline, session=None):
        self.providers = providers[:18]; self.deadline = deadline
        self.session = session or requests.Session(); self.blocked = set(); self.requests = 0

    def get(self, provider, params):
        url = provider['url']; host = urlsplit(url).hostname
        if host in self.blocked or time.monotonic() >= self.deadline: raise RuntimeError('unavailable')
        self.requests += 1
        remaining = max(.2, min(6, self.deadline - time.monotonic()))
        response = self.session.get(url, params=params, timeout=(min(3, remaining), remaining),
            allow_redirects=False, stream=True, headers={'User-Agent': 'PrimeCatalogVerification/1.0'})
        try:
            if response.status_code in (401, 403, 429): self.blocked.add(host)
            response.raise_for_status()
            if response.status_code != 200: raise RuntimeError('unavailable')
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(chunks) + len(chunk) > 2_000_000 or time.monotonic() >= self.deadline:
                    raise RuntimeError('unavailable')
                chunks.extend(chunk)
            payload = json.loads(chunks.decode('utf-8-sig'))
        finally:
            response.close()
        if not isinstance(payload, dict) or not isinstance(payload.get('list'), list): raise RuntimeError('unavailable')
        return payload['list'][:100]

    def check(self, target):
        ref = reference(target); terms = []
        for name in [ref['display_name_zh']] + ref.get('aliases_zh', []):
            if isinstance(name, str) and re.search(r'[\u3400-\u9fff]', name) and name not in terms: terms.append(name)
        terms = terms[:2]
        if target['originalTitle'] not in terms: terms.append(target['originalTitle'])
        incomplete, pending = False, False
        for provider in self.providers:
            try:
                rows = []
                for term in terms:
                    found = self.get(provider, {'ac': 'detail', 'wd': term})
                    for source in found:
                        hit = candidate(source)
                        if not hit['has_routes'] and hit['provider_id'].isdigit():
                            # Fetch only plausible same-title details, never every search hit.
                            from source_matching import title_base, names
                            if title_base(hit['title']) in names(ref):
                                details = self.get(provider, {'ac': 'detail', 'ids': hit['provider_id']})
                                rows.extend(candidate(item) for item in details)
                        else: rows.append(hit)
                    status, match = select(ref, rows)
                    if status == 'matched': return 'matched'
                    pending |= status in ('pending_review', 'ambiguous')
            except Exception:
                incomplete = True
        return 'unavailable' if incomplete else 'pending' if pending else 'no_match'


def configured(value):
    raw = json.loads(value or '[]')
    if not isinstance(raw, list) or len(raw) > 18: raise ValueError('invalid source configuration')
    result = []
    for source in raw:
        if not isinstance(source, dict): raise ValueError('invalid source configuration')
        parsed = urlsplit(source.get('url', ''))
        if source.get('adapter', 'maccms') != 'maccms': continue
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('invalid source configuration')
        result.append({'url': source['url']})
    return result


def run(root=ROOT, limit=12, budget=120, providers=None, probe=None, clock=None):
    targets = json.loads((root / 'data/source-verification-targets.json').read_text(encoding='utf-8'))['items']
    catalog_path = root / 'docs/catalog.json'
    if catalog_path.exists():
        targets = calibrated_targets(targets, json.loads(catalog_path.read_text(encoding='utf-8')))
    path = root / 'data/source-verification.json'
    old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema': 1, 'items': []}
    clock = clock or datetime.now(timezone.utc)
    validate_snapshot(old, clock=clock)
    prior = {scope(row): row for row in old['items']}
    now = clock.isoformat(timespec='seconds'); providers = configured(os.getenv('SOURCE_PROVIDERS_JSON', '')) if providers is None else providers
    if not providers:
        print(json.dumps({'status': 'not_configured', 'attempted': 0, 'changed': False})); return old
    output = []
    for target in targets:
        saved = prior.get(scope(target))
        row = copy.deepcopy(saved) if saved else {k: target[k] for k in ('officialId','originalTitle','kind','season','year')}
        if not saved:
            row.update(status='pending', checkedAt=None)
        output.append(row)
    validate_snapshot({'schema': 1, 'items': output}, clock=clock)
    deadline = time.monotonic() + max(15, min(int(budget), 600)); probe = probe or Probe(providers, deadline)
    attempted = changed = 0
    # All old positives are reused for seven days; incomplete checks cannot erase one.
    for row in sorted(output, key=lambda item: item.get('checkedAt') or ''):
        if attempted >= max(0, min(int(limit), 30)) or time.monotonic() >= deadline: break
        if row.get('checkedAt'):
            age = clock - datetime.fromisoformat(row['checkedAt'].replace('Z', '+00:00'))
            if age < timedelta(days=7 if row['status'] == 'matched' else 1): continue
        target = next(target for target in targets if scope(target) == scope(row)); attempted += 1
        status = probe.check(target)
        if status == 'matched' or row['status'] != 'matched':
            previous = dict(row); row.update(status=status, checkedAt=now); changed += row != previous
    if output != old['items']:
        validate_snapshot({'schema': 1, 'items': output}, clock=clock)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': 1, 'items': output}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'completed', 'attempted': attempted, 'changed': changed, 'requests': getattr(probe, 'requests', 0)}))
    return {'schema': 1, 'items': output}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--limit', type=int, default=12); parser.add_argument('--budget-seconds', type=int, default=120)
    args = parser.parse_args()
    try: run(limit=args.limit, budget=args.budget_seconds)
    except Exception as exc:
        print('Source preflight stopped: ' + type(exc).__name__ + ' (private transport details suppressed)'); raise SystemExit(1)
