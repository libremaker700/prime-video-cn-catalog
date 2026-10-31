"""Evidence-bound source identity policy. Provider IDs/cache stay on the caller's device.

No network requests, playback URLs, or credentials are made or stored here. Callers
inject search/detail functions and persist the returned binding cache privately.
"""
import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from catalog import norm


def year(value):
    match = re.search(r'(?:19|20)\d{2}', str(value or ''))
    return int(match[0]) if match else None


def official_id(record):
    return record.get('prime_id') or record.get('official_id') or record.get('id')


@lru_cache(maxsize=1)
def evidence_registry():
    path = Path(__file__).resolve().parents[1] / 'data/release-year-evidence.json'
    return json.loads(path.read_text(encoding='utf-8'))['items'] if path.exists() else []


def with_release_evidence(record):
    out = copy.deepcopy(record)
    for evidence in evidence_registry():
        if evidence.get('officialId') == official_id(record):
            out['releaseYearEvidence'] = evidence
            if len(release_years(out)) < 2:
                out.pop('releaseYearEvidence', None)
            break
    return out


def release_years(record):
    """An exception is scoped to official identity, original, medium and season."""
    display = year(record.get('year') or record.get('details', {}).get('first_air_year'))
    fallback = {display} if display else set()
    evidence = record.get('releaseYearEvidence')
    if not isinstance(evidence, dict):
        return fallback
    kind = 'movie' if record.get('kind') == 'movie' else 'tv'
    season = 0 if kind == 'movie' else record.get('season')
    original = record.get('english_title') or record.get('original_title')
    if (not re.fullmatch(r'[A-Z0-9]{10,26}', str(official_id(record) or ''))
            or evidence.get('officialId') != official_id(record)
            or evidence.get('kind') != kind or evidence.get('season') != season
            or evidence.get('officialYear') != display
            or not norm(original) or norm(evidence.get('originalTitle')) != norm(original)
            or not re.fullmatch(r'tt\d{5,12}', str(evidence.get('imdbId') or ''))):
        return fallback
    if record.get('imdb_id') and record['imdb_id'] != evidence['imdbId']:
        return fallback
    releases = evidence.get('releases')
    if not isinstance(releases, list) or not 2 <= len(releases) <= 4:
        return fallback
    years, urls, hosts = set(), set(), set()
    for release in releases:
        if not isinstance(release, dict):
            return fallback
        value, url = release.get('year'), release.get('url', '')
        try:
            parsed = urlsplit(url)
            if (type(value) is not int or not 1900 <= value <= 2199
                    or not release.get('region') or parsed.scheme != 'https'
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or not parsed.path.startswith('/')
                    or parsed.query and re.search(r'(?i)key|token|signature', parsed.query)
                    or re.search(r'[\s<>"\'\\]', url)):
                return fallback
        except (TypeError, ValueError):
            return fallback
        years.add(value); urls.add(url); hosts.add(parsed.hostname.removeprefix('www.'))
    return years if display in years and len(urls) >= 2 and len(hosts) >= 2 else fallback


def title_base(value):
    return norm(re.sub(r'第[\d一二三四五六七八九十百]+季|\bseason\s*\d+\b', '', str(value or ''), flags=re.I))


def names(record):
    values = [record.get('display_name_zh'), record.get('english_title'), record.get('original_title')]
    values += record.get('aliases_zh', []) + record.get('lookup_verified_aliases', [])
    return {title_base(v) for v in values if isinstance(v, str) and title_base(v)}


def candidate_season(record):
    value = record.get('season')
    if type(value) is int and 1 <= value <= 200:
        return value
    title = record.get('title') or record.get('display_name_zh') or ''
    match = re.search(r'第([\d一二三四五六七八九十百]+)季|\bseason\s*(\d+)\b', title, re.I)
    if not match:
        return None
    value = match[1] or match[2]
    if value.isdigit():
        return int(value)
    digits = dict(zip('一二三四五六七八九', range(1, 10)))
    if '十' in value:
        left, right = value.split('十', 1)
        return (digits.get(left, 1) * 10 + digits.get(right, 0)) if len(left) <= 1 and len(right) <= 1 else None
    return digits.get(value)


def identity_match(reference, candidate):
    """Return an explicit decision; same-title frequency never proves identity."""
    title = candidate.get('title') or candidate.get('display_name_zh') or candidate.get('original_title')
    if re.search(r'解说|解說|预告|短剧|短劇|综艺|新闻|直播', str(candidate.get('kind', '')) + str(title)):
        return 'rejected'
    family = lambda r: 'tv' if r.get('kind') in ('tv', 'series', 'anime') else r.get('kind')
    if family(reference) != family(candidate) or title_base(title) not in names(reference):
        return 'rejected'
    if family(reference) != 'movie':
        wanted, actual = candidate_season(reference), candidate_season(candidate)
        if wanted is None or actual is None:
            return 'unverified'
        if wanted != actual:
            return 'rejected'
    actual_year = year(candidate.get('year'))
    if not actual_year:
        return 'unverified'
    if actual_year not in release_years(reference):
        return 'needs_release_evidence'
    for field in ('imdb_id', 'douban_id'):
        expected = reference.get(field)
        if field == 'imdb_id' and not expected and reference.get('releaseYearEvidence'):
            expected = reference['releaseYearEvidence'].get('imdbId')
        if expected and candidate.get(field) and expected != candidate[field]:
            return 'rejected'
    for field in ('directors', 'actors'):
        expected, actual = reference.get(field, []), candidate.get(field, [])
        if expected and actual and not {norm(v) for v in expected} & {norm(v) for v in actual}:
            return 'rejected'
    return 'matched' if candidate.get('has_routes') is True else 'no_routes'


def select(reference, candidates):
    matched, pending = {}, False
    for candidate in candidates:
        state = identity_match(reference, candidate)
        pending |= state in ('needs_release_evidence', 'unverified')
        ident = str(candidate.get('provider_id') or '')
        if state == 'matched' and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', ident):
            matched[ident] = candidate
    if len(matched) > 1:
        return 'ambiguous', None
    if matched:
        return 'matched', next(iter(matched.values()))
    return ('pending_review' if pending else 'no_match'), None


def binding_key(reference, provider):
    scope = {k: reference.get(k) for k in ('prime_id', 'official_id', 'id', 'english_title', 'original_title', 'kind', 'season', 'year', 'releaseYearEvidence')}
    return hashlib.sha256(json.dumps([provider, scope], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resolve_sources(reference, providers, search, detail, cache, lookup=None):
    """Strict queries first, verified-name fallback second, private ID reuse first.

    Timeout/blocked/transport failures never create a negative binding or erase a
    saved positive one. `lookup` returns verified metadata only; AI text is not it.
    """
    reference = with_release_evidence(reference)
    output, working = [], copy.deepcopy(reference)
    for provider in list(dict.fromkeys(providers))[:18]:
        key = binding_key(reference, provider); saved = cache.get(key)
        if saved:
            try:
                candidate = detail(provider, saved['provider_id'])
                saved_ref = copy.deepcopy(working)
                saved_ref['lookup_verified_aliases'] = saved.get('verified_aliases', [])
                if identity_match(saved_ref, candidate) == 'matched':
                    output.append({'provider': provider, 'status': 'matched', 'provider_id': saved['provider_id'], 'episode_count': max(0, int(candidate.get('episode_count') or 0)), 'reused': True})
                    continue
                cache.pop(key, None)
            except Exception:
                output.append({'provider': provider, 'status': 'unavailable'}); continue
        rows, failed = [], False
        terms = [working.get('display_name_zh'), *(working.get('aliases_zh') or []), working.get('english_title') or working.get('original_title')]
        terms = list(dict.fromkeys(t for t in terms if isinstance(t, str) and t.strip()))
        # Keep the English original reachable within the three-query budget.
        terms = terms[:2] + ([terms[-1]] if len(terms) > 2 else [])
        for term in terms:
            try:
                rows.extend(search(provider, term))
            except Exception:
                failed = True
        state, hit = select(working, rows)
        row = {'provider': provider, 'status': state if hit or not failed else 'unavailable'}
        if hit:
            row['provider_id'] = str(hit['provider_id'])
            row['episode_count'] = max(0, int(hit.get('episode_count') or 0))
            cache[key] = {'provider_id': row['provider_id'], 'verified_aliases': working.get('lookup_verified_aliases', [])}  # No media/session URLs.
        output.append(row)
    if lookup and not any(row['status'] == 'matched' for row in output):
        try:
            patch = lookup(copy.deepcopy(working)) or {}
            aliases = patch.get('aliases_zh', [])
            aliases = [v for v in aliases if isinstance(v, str) and 0 < len(v) <= 100][:6]
            if str(patch.get('name_status', '')).startswith('verified') and aliases:
                working['lookup_verified_aliases'] = aliases
                working['display_name_zh'] = aliases[0]
                working['aliases_zh'] = list(dict.fromkeys(aliases + working.get('aliases_zh', [])))
                return resolve_sources(working, providers, search, detail, cache)
        except Exception:
            # A failed name lookup does not turn an unverified result into a miss.
            output.append({'status': 'unavailable', 'provider': 'name_lookup'})
    return {'sources': output, 'episode_count': max((r.get('episode_count', 0) for r in output), default=0),
            'status': 'matched' if any(r['status'] == 'matched' for r in output) else
                      'unavailable' if any(r['status'] == 'unavailable' for r in output) else
                      'pending_review' if any(r['status'] in ('pending_review', 'ambiguous') for r in output) else 'no_match'}
