"""Persistent, bounded title lookup. Search snippets are candidates, not identities."""
import argparse
import copy
import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from catalog import merge, norm, publish, write_json

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'display_name_zh', 'aliases_zh', 'name_status', 'name_checked_at', 'sources'}


def chinese(value):
    return isinstance(value, str) and bool(re.search(r'[\u3400-\u9fff]', value))


def year(record):
    value = record.get('details', {}).get('first_air_year') or record.get('year')
    return int(value) if re.fullmatch(r'(19|20)\d{2}', str(value)) else None


def identity(record):
    return {k: record.get(k) for k in ('id', 'prime_id', 'kind', 'original_title', 'english_title')} | {'first_air_year': year(record)}


def cache_key(record):
    return hashlib.sha256(json.dumps(identity(record), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def name_verified(record):
    state = record.get('name_status', '')
    return chinese(record.get('display_name_zh')) and (state.startswith('verified') or state == 'user_app_preference')


def apply_cache(record, cache):
    entry = cache.get('records', {}).get(cache_key(record), {})
    if entry.get('status') != 'verified' or entry.get('identity') != identity(record):
        return copy.deepcopy(record)
    patch = {k: v for k, v in entry.get('patch', {}).items() if k in FIELDS}
    if not chinese(patch.get('display_name_zh')) or not patch.get('sources'):
        return copy.deepcopy(record)
    return merge(record, patch)


def source(kind, url, checked_at):
    return {'type': kind, 'url': url, 'retrieved_at': checked_at}


def mentions_original(title, text):
    title = unicodedata.normalize('NFKC', title or '').casefold()
    text = unicodedata.normalize('NFKC', text).casefold()
    if re.search(r'[a-z]', title):
        words = re.findall(r'\w+', title)
        return bool(words and re.search(r'(?<![a-z0-9])' + r'\W+'.join(map(re.escape, words)) + r'(?![a-z0-9])', text))
    return bool(norm(title) and norm(title) in norm(text))


def sanitize(value, key):
    text = json.dumps(value, ensure_ascii=False)
    for secret in key if isinstance(key, (list, tuple)) else [key]:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return json.loads(re.sub(r'tvly-[A-Za-z0-9_-]{12,}', '[REDACTED]', text))


def wiki_patch(record, page, checked_at):
    """Verify the defining paragraph, never an unrelated mention later in an article."""
    lead = page.get('extract', '').split('\n', 1)[0][:1000]
    names = [record.get(k) for k in ('original_title', 'english_title') if record.get(k)]
    expected = year(record)
    if not names or not expected or not any(mentions_original(n, lead[:500]) for n in names):
        return None
    from source_matching import release_years,with_release_evidence
    if not any(str(y) in lead for y in release_years(with_release_evidence(record))):
        return None
    words = {'movie': ('电影', '電影', '影片'), 'tv': ('电视剧', '電視劇', '影集', '剧集', '劇集', '情境喜剧', '情境喜劇', '情景喜剧', '情景喜劇'), 'anime': ('电视动画', '電視動畫', '动画影集', '動畫影集', '动画剧集', '動畫劇集')}.get(record.get('kind'), ())
    title = re.match(r'^《([^》]{1,90})》', lead)
    url = page.get('fullurl', '')
    if not title or not chinese(title[1]) or not any(w in lead for w in words) or urlsplit(url).hostname != 'zh.wikipedia.org':
        return None
    # A combined manga article starts by defining a manga; don't pass it as animation.
    if record.get('kind') == 'anime' and re.search(r'漫画|漫畫', lead[:150]) and not re.search(r'电视动画|電視動畫', lead[:150]):
        return None
    display = title[1]
    old = record.get('display_name_zh', '')
    if chinese(old):
        if norm(old) not in norm(lead):
            return None  # Different regional name: retain old display until reviewed.
        display = old
    patch = {'display_name_zh': display, 'aliases_zh': [title[1]],
            'name_status': 'verified_title_sources', 'name_checked_at': checked_at,
            'sources': [source('wikipedia_title', url, checked_at)]}
    # A series article may describe the series, never an arbitrary requested season.
    if not record.get('season'):
        from sources import chinese as chinese_summary
        summary=page.get('extract','').strip()[:5000]
        if chinese_summary(summary):
            patch.update(synopsis_zh=summary,synopsis_status='ok',synopsis_source_type='wikipedia',synopsis_source_url=url)
    return patch


def bangumi_patch(record, subject, checked_at):
    expected = year(record)
    if record.get('kind') != 'anime' or not expected or subject.get('type') != 2 or subject.get('platform') != 'TV':
        return None
    if str(subject.get('date', ''))[:4] != str(expected):
        return None
    # Exact original Japanese/English title; no fuzzy match to manga or another season.
    originals = {norm(record.get(k)) for k in ('original_title', 'english_title')} - {''}
    if norm(subject.get('name')) not in originals or not chinese(subject.get('name_cn')):
        return None
    display = subject['name_cn']
    old = record.get('display_name_zh', '')
    if chinese(old) and norm(old) != norm(display):
        return None
    return {'display_name_zh': display, 'aliases_zh': [display], 'name_status': 'verified_title_sources',
            'name_checked_at': checked_at, 'sources': [source('bangumi_title', f"https://bangumi.tv/subject/{int(subject['id'])}", checked_at)]}


class Lookup:
    def __init__(self, key='', session=None, keys=None):
        supplied = keys if keys is not None else ([key] if key else [os.getenv(f'TAVILY_API_KEY_{slot}', '') for slot in range(1, 5)])
        self.keys = list(dict.fromkeys(str(value).strip() for value in supplied if str(value).strip()))
        if not self.keys and os.getenv('TAVILY_API_KEY', '').strip():
            self.keys = [os.environ['TAVILY_API_KEY'].strip()]
        self.key = self.keys[0] if self.keys else ''
        self.active_key = 0
        self.failovers = 0
        self.session = session or requests.Session()
        self.requests = 0
        self.network_errors = 0
        self.diagnostics = []
        self.stopped = False
        self.last_source_request = {}
        self.blocked_sources = set()

    def source_get(self, url, **kwargs):
        host=urlsplit(url).hostname
        if host in self.blocked_sources:
            return None
        wait=max(0,3.0-(time.monotonic()-self.last_source_request.get(host,0)))
        if wait:time.sleep(wait)
        self.last_source_request[host]=time.monotonic()
        response=self.session.get(url,headers={'User-Agent':'PrimeVideoCnCatalog/4.0 (public metadata verification)'},
            timeout=(8,20),allow_redirects=False,**kwargs)
        if response.status_code in (401,403,429):self.blocked_sources.add(host)
        return response

    def get_json(self, url, **kwargs):
        try:
            response = self.source_get(url, **kwargs)
            if response is None:return {}
            if response.status_code == 200:
                return response.json()
            self.diagnostics.append({'host': urlsplit(url).hostname, 'http_status': response.status_code})
        except (requests.RequestException, ValueError):
            pass
        self.network_errors += 1
        return {}

    def wiki_page(self, record, title, checked_at):
        payload = self.get_json('https://zh.wikipedia.org/w/api.php', params={
            'action': 'query', 'prop': 'extracts|info', 'exintro': 1, 'explaintext': 1,
            'inprop': 'url', 'redirects': 1, 'titles': title, 'format': 'json', 'variant': 'zh-hans'})
        patches = [p for page in payload.get('query', {}).get('pages', {}).values() if (p := wiki_patch(record, page, checked_at))]
        return patches[0] if len(patches) == 1 else None

    def direct(self, record, checked_at):
        title = record.get('english_title') or record.get('original_title')
        hint = {'movie': '电影', 'tv': '电视剧', 'anime': '电视动画'}.get(record.get('kind'), '')
        if record.get('kind') == 'anime':
            original = record.get('original_title') or title
            payload = self.get_json('https://api.bgm.tv/search/subject/' + requests.utils.quote(original, safe=''), params={'type': 2, 'responseGroup': 'small', 'max_results': 5})
            patches = []
            for candidate in payload.get('list', [])[:5]:
                if not str(candidate.get('id', '')).isdigit():
                    continue
                subject = self.get_json(f"https://api.bgm.tv/v0/subjects/{int(candidate['id'])}")
                patch = bangumi_patch(record, subject, checked_at)
                if patch:
                    patches.append(patch)
            if len(patches) == 1:
                return patches[0]
        payload = self.get_json('https://zh.wikipedia.org/w/api.php', params={'action': 'query', 'list': 'search', 'srsearch': f'{title} {hint}', 'srlimit': 2, 'format': 'json'})
        patches = [p for hit in payload.get('query', {}).get('search', []) if (p := self.wiki_page(record, hit['title'], checked_at))]
        return patches[0] if len(patches) == 1 else None

    def search_with_budget(self, record, checked_at, reserve):
        return self.search(record, checked_at, reserve)

    def resolve(self, record, checked_at, reserve=None, source_probe=None):
        from metadata_resolution import resolve
        return resolve(self, record, checked_at, reserve, source_probe)

    def search(self, record, checked_at, reserve=None, stage='tavily'):
        from metadata_resolution import query as metadata_query, bracket_names, grounded_answer_names, HOSTS
        title = record.get('english_title') or record.get('original_title')
        hint = {'movie': '电影', 'tv': '电视剧', 'anime': '电视动画'}.get(record.get('kind'), '')
        query = metadata_query(record, stage)
        # At most one attempt per configured key. Reserve every HTTP request first.
        # Rate limits, timeouts and server errors stop the run without key cycling.
        response = None
        while self.active_key < len(self.keys):
            slot = self.active_key + 1
            if reserve and not reserve(slot):
                self.stopped = True
                return None, {'query': query, 'error': 'local_budget_exhausted', 'results': []}
            self.requests += 1
            try:
                response = self.session.post('https://api.tavily.com/search', headers={'Authorization': 'Bearer ' + self.keys[self.active_key]},
                json={'query': query, 'search_depth': 'basic', 'auto_parameters': False, 'max_results': 6,
                      'include_answer': True, 'include_domains': sorted(HOSTS.get(stage, [])),
                      'include_raw_content': 'text', 'include_images': False, 'include_usage': True},
                timeout=(8, 45), allow_redirects=False)
            except requests.RequestException:
                self.stopped = True
                return None, {'query': query, 'error': 'search_unavailable', 'key_slot': slot, 'results': []}
            if response.status_code == 200:
                break
            if response.status_code in (401, 403, 432, 433) and self.active_key + 1 < len(self.keys):
                self.active_key += 1
                self.failovers += 1
                continue
            self.stopped = True
            return None, {'query': query, 'error': 'HTTP ' + str(response.status_code), 'key_slot': slot, 'results': []}
        if response is None:
            self.stopped = True
            return None, {'query': query, 'error': 'no_search_key', 'results': []}
        try:
            payload = response.json()
        except ValueError:
            self.stopped = True
            return None, {'query': query, 'error': 'search_unavailable', 'results': []}
        results = []
        for item in payload.get('results', [])[:6]:
            url = item.get('url', '')
            if urlsplit(url).scheme != 'https' or urlsplit(url).username or urlsplit(url).password:
                continue
            if stage in HOSTS and urlsplit(url).hostname not in HOSTS[stage]:
                continue
            results.append({'title': str(item.get('title', ''))[:200], 'url': url, 'score': item.get('score')})
            # Retain retrieved page text separately from the AI answer for verification.
            if item.get('raw_content'):
                results[-1]['page_text'] = str(item['raw_content'])[:16000]
            if item.get('content'):
                results[-1]['snippet'] = str(item['content'])[:1500]
            if urlsplit(url).hostname == 'zh.wikipedia.org' and item.get('raw_content'):
                # Only the first defining paragraph, not snippets or a later mention.
                paragraphs = [p.strip() for p in str(item['raw_content']).split('\n') if p.strip().startswith('《')]
                if paragraphs:
                    results[-1]['definition'] = paragraphs[0][:1000]
                    results[-1]['page_sha256'] = hashlib.sha256(item['raw_content'].encode()).hexdigest()
        patch = self.verify_results(record, results, checked_at)
        return patch, {'stage': stage, 'query': query, 'usage': payload.get('usage'), 'results': results,
                       'answer_text': str(payload.get('answer') or '')[:6000],
                       'candidate_names': list(dict.fromkeys(bracket_names(payload.get('answer'))+grounded_answer_names(payload.get('answer'),results))),
                       'status': 'verified' if patch else 'pending_review'}

    def verify_results(self, record, results, checked_at):
        from metadata_resolution import safe_source, page_patch, retrieved_page_patch
        patches = []
        for item in results:
            url = item.get('url', '')
            parts = urlsplit(url)
            retrieved=retrieved_page_patch(record,item,checked_at)
            if retrieved:
                if retrieved not in patches:patches.append(retrieved)
                continue
            # Search only locates a page. The page API must independently pass identity gates.
            if parts.hostname == 'zh.wikipedia.org':
                candidate = unquote(parts.path.rsplit('/', 1)[-1])
                patch = wiki_patch(record, {'extract': item.get('definition', ''), 'fullurl': url}, checked_at) or self.wiki_page(record, candidate, checked_at)
                if patch and patch not in patches:
                    patches.append(patch)
            elif parts.hostname in ('bgm.tv', 'bangumi.tv') and re.fullmatch(r'/subject/\d+', parts.path):
                subject = self.get_json('https://api.bgm.tv/v0/subjects/' + parts.path.rsplit('/', 1)[-1])
                patch = bangumi_patch(record, subject, checked_at)
                if patch and patch not in patches:
                    patches.append(patch)
            elif safe_source(url):
                try:
                    response = self.source_get(url)
                    if response is None:continue
                    if response.status_code == 200:
                        patch = page_patch(record, response.text, url, checked_at)
                        if patch and patch not in patches:
                            patches.append(patch)
                    else:
                        self.diagnostics.append({'host':parts.hostname,'http_status':response.status_code})
                except requests.RequestException:
                    self.network_errors += 1
        unique = {norm(p['display_name_zh']) for p in patches}
        return patches[0] if len(unique) == 1 else None


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def saved_results(evidence):
    result=list(evidence.get('results',[]))
    for stage in evidence.get('stages',[]):
        result.extend(stage.get('results',[]))
    return result


def check_keys():
    """Authenticate each key via /usage without searching or storing account data."""
    client = Lookup()
    slots = []
    for index, secret in enumerate(client.keys):
        status = None
        try:
            response = client.session.get('https://api.tavily.com/usage', headers={'Authorization': 'Bearer ' + secret}, timeout=(8, 20), allow_redirects=False)
            status = response.status_code
        except requests.RequestException:
            pass
        slots.append({'slot': index + 1, 'http_status': status, 'authenticated': status == 200})
    print(json.dumps({'configured_unique_keys': len(client.keys), 'slots': slots, 'searches': 0}))
    return 0 if any(s['authenticated'] for s in slots) else 1


def run(root=ROOT, *, force=False, limit=12, monthly_limit=100, clock=None, client=None, review_saved=False):
    clock = clock or datetime.now(timezone.utc)
    checked_at = clock.isoformat(timespec='seconds')
    path = root / 'data/title-cache.json'
    cache = load(path, {'schema': 1, 'records': {}, 'budgets': {}})
    if cache.get('schema') != 1:
        raise ValueError('unsupported title cache schema')
    last = cache.get('last_run_at')
    if not force and last:
        age = (clock - datetime.fromisoformat(last)).total_seconds()
        if 0 <= age < 604800:
            print('Title lookup not due; no requests or file changes.')
            return None
    current = load(root/'docs/catalog.json', {})
    version = load(root/'docs/version.json', {})
    # Overrides are limited to names; preserve all other fields byte-for-byte in items.
    rules = load(root/'data/title-overrides.json', {'rules': []})['rules']
    items = copy.deepcopy(current['items'])
    for item in items:
        for rule in rules:
            if rule['mediaType'] == item['kind'] and norm(rule['originalTitle']) in {norm(item.get('original_title')), norm(item.get('english_title'))}:
                item.update(display_name_zh=rule['preferredTitleZh'], name_status='user_app_preference')
                item['aliases_zh'] = list(dict.fromkeys(item.get('aliases_zh', []) + rule.get('aliasesZh', [])))
                if rule.get('sources'):
                    item.update(sources=merge(item, {'sources': rule['sources']})['sources'])
    client = client or Lookup()
    limit, monthly_limit = max(0, min(limit, 20)), max(0, min(monthly_limit, 200))
    month = clock.strftime('%Y-%m')
    used = int(cache.setdefault('budgets', {}).get(month, 0))
    report = {'checked_at': checked_at, 'searched': 0, 'direct_verified': 0, 'cache_hits': 0,
              'skipped_verified': 0, 'pending': 0, 'attempted': 0, 'monthly_reserved': used, 'monthly_limit': monthly_limit}
    def reserve(slot=None):
        nonlocal used
        if used >= monthly_limit:
            return False
        used += 1
        report['searched'] += 1
        cache['budgets'][month] = used
        usage = cache.setdefault('key_usage', {}).setdefault(month, {})
        usage[str(slot or 'legacy')] = usage.get(str(slot or 'legacy'), 0) + 1
        write_json(path, cache)
        return True
    # Rotate unresolved entries, prioritizing entirely missing Chinese names.
    items.sort(key=lambda r: (chinese(r.get('display_name_zh')), cache.get('records', {}).get(cache_key(r), {}).get('checked_at', ''), r['id']))
    updated = []
    for record in items:
        out = apply_cache(record, cache)
        if out != record:
            report['cache_hits'] += 1
        key = cache_key(out)
        old = cache['records'].get(key, {})
        if name_verified(out):
            report['skipped_verified'] += 1
            updated.append(out)
            continue
        previous_results=saved_results(old.get('evidence',{}))
        if review_saved:
            patch = client.verify_results(out, previous_results, checked_at) if old.get('identity') == identity(out) and previous_results else None
            if patch:
                patch = sanitize(patch, getattr(client, 'keys', [client.key]))
                cache['records'][key] = old | {'status': 'verified', 'patch': patch, 'checked_at': checked_at}
                out = merge(out, patch)
                report['cache_hits'] += 1
            updated.append(out)
            continue
        if old.get('next_retry_at') and checked_at < old['next_retry_at']:
            updated.append(out)
            continue
        if not (out.get('english_title') or out.get('original_title')) or not year(out):
            report['pending'] += 1
            updated.append(out)
            continue
        if report['attempted'] >= limit:
            updated.append(out)
            continue
        report['attempted'] += 1
        patch = None
        if old.get('identity') == identity(out) and previous_results:
            patch = client.verify_results(out, previous_results, checked_at)
            if patch:
                report['cache_hits'] += 1
        evidence = {'status': 'direct_source'}
        if not patch and hasattr(client, 'resolve'):
            patch, evidence = client.resolve(out, checked_at, reserve)
        elif not patch:
            patch = client.direct(out, checked_at)
        if patch:
            report['direct_verified'] += 1
        elif not hasattr(client, 'resolve') and client.key and not client.stopped and used < monthly_limit:
            if hasattr(client, 'search_with_budget'):
                patch, evidence = client.search_with_budget(out, checked_at, reserve)
            elif reserve():
                patch, evidence = client.search(out, checked_at)
        entry = {'identity': identity(out), 'checked_at': checked_at, 'status': 'verified' if patch else 'pending_review',
                 'evidence': evidence, 'next_retry_at': (clock + (timedelta(hours=6) if client.stopped else timedelta(days=7))).isoformat(timespec='seconds')}
        if evidence.get('candidate_names'):
            entry['candidate_names'] = evidence['candidate_names']
        if patch:
            if hasattr(client,'resolve'):
                from metadata_resolution import remember_summary
                remember_summary(out,patch,cache,checked_at)
            entry['patch'] = sanitize({k:v for k,v in patch.items() if k in FIELDS}, getattr(client, 'keys', [client.key]))
            out = merge(out, entry['patch'])
        else:
            report['pending'] += 1
        cache['records'][key] = sanitize(entry, getattr(client, 'keys', [client.key]))
        updated.append(out)
    # Chinese descriptions use a separate entity/season-bound cache, not the name cache.
    if hasattr(client, 'resolve'):
        from metadata_resolution import enrich_summaries
        updated, summary_report = enrich_summaries(updated, cache, client, checked_at, reserve,
            0 if review_saved else max(0, limit-report['attempted']), path)
        report['summaries'] = summary_report
    # Preserve catalogue order, IDs, playback binding and artwork.
    order = {r['id']: n for n,r in enumerate(current['items'])}
    updated.sort(key=lambda r: order[r['id']])
    if not client.stopped:
        cache['last_run_at'] = checked_at
    report['monthly_reserved'] = used
    report['keys_configured'] = len(getattr(client, 'keys', [client.key] if client.key else []))
    report['key_failovers'] = getattr(client, 'failovers', 0)
    report['remaining_unverified'] = sum(not name_verified(r) for r in updated)
    report['direct_network_errors'] = getattr(client, 'network_errors', 0)
    report['source_diagnostics'] = getattr(client, 'diagnostics', [])
    report['verified_changes'] = [{'id': a['id'], 'before': a['display_name_zh'], 'after': b['display_name_zh'], 'name_status': b.get('name_status')}
                                  for a,b in zip(current['items'], updated) if a != b]
    report['published_name_changes'] = sum(a != b for a,b in zip(current['items'], updated))
    report['lookup_order'] = ['tavily','douban','wikipedia','baidu']
    pending = report['remaining_unverified'] or report.get('summaries',{}).get('pending',0)
    report['status'] = 'degraded' if client.stopped else ('completed_with_pending' if pending else 'completed')
    if updated != current['items']:
        report['version'] = publish(root/'docs', updated, version, reason='verified_title_lookup')
    # Never store a credential or raw transport errors in public data/logs.
    raw = json.dumps(cache, ensure_ascii=False)
    if any(secret and secret in raw for secret in getattr(client, 'keys', [client.key])):
        raise ValueError('credential detected in title cache')
    write_json(path, cache)
    write_json(root/'logs/title-lookup.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true', help='Bypass the weekly timer, not verified cache/budget/cooldown')
    parser.add_argument('--review-saved', action='store_true', help='Recheck saved source pages without repeating paid searches')
    parser.add_argument('--check-keys', action='store_true', help='Check configured keys via /usage without searching')
    args = parser.parse_args()
    try:
        if args.check_keys:
            raise SystemExit(check_keys())
        run(force=args.force, review_saved=args.review_saved, limit=int(os.getenv('TITLE_LOOKUP_LIMIT','12')), monthly_limit=int(os.getenv('TITLE_LOOKUP_MONTHLY_LIMIT','100')))
    except Exception as exc:
        print('Title lookup aborted:', type(exc).__name__, '(transport and credentials suppressed)')
        raise SystemExit(1)
