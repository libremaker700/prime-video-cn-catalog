"""Import public App mappings without playback IDs, sessions or guessed identities."""
import copy
import re
from urllib.parse import urlsplit

from catalog import family, merge


def compact_id(url):
    value = str(url or '')
    if value.startswith('https://www.primevideo.com/'):
        value = urlsplit(value).path
    match = re.fullmatch(r'/-/zh/detail/([A-Z0-9]{10}|[A-Z0-9]{26})(?:\?[^\s<>"\']*)?', value)
    return match[1] if match else None


def normalize_fields(items):
    """Keep current top-level fields and support the original nested movie format."""
    result = copy.deepcopy(items)
    for item in result:
        # Prime encodes some specials as 202 etc. These are not ordinary App seasons.
        # Retain their source metadata separately without adding unsupported UI seasons
        # or image references outside the App's validated image manifest.
        supported, extras = [], {s['season']: s for s in item.get('official_unmapped_seasons', [])}
        for season in item.get('seasons', []):
            number = season.get('season')
            if type(number) is int and number > 200:
                extras[number] = {key: value for key, value in season.items() if key not in ('poster', 'poster_scope', 'poster_status')}
            else:
                supported.append(season)
        if 'seasons' in item:
            item['seasons'] = supported
        if extras:
            item['official_unmapped_seasons'] = [extras[n] for n in sorted(extras)]
        if item.get('identity_status') == 'verified_official_mapping' and (item.get('details') or {}).get('official_type') == 'Movie':
            item['kind'] = 'movie'
        if item.get('kind') != 'movie':
            continue
        details = item.get('details') or {}
        for key in ('release_date', 'genres'):
            if not item.get(key) and details.get(key):
                item[key] = copy.deepcopy(details[key])
    return result


def apply_app_metadata(items, asset):
    if asset.get('schema') != 'prime-video-app-metadata/1':
        raise ValueError('unsupported App mapping format')
    entries = {}
    for entry in asset.get('items', []):
        ident = entry.get('prime_id')
        if not re.fullmatch(r'[A-Z0-9]{10}', ident or '') or ident in entries:
            raise ValueError('invalid or duplicate App official identity')
        entries[ident] = entry
    result = normalize_fields(items)
    for item in result:
        entry = entries.get(item.get('prime_id'))
        if not entry or item.get('identity_status') != 'verified_official_mapping':
            continue
        if family(item.get('kind')) != family(entry.get('kind')):
            raise ValueError('App media type mismatch')
        english = str(entry.get('english_title') or '').strip()
        if english and not item.get('english_title'):
            item['english_title'] = english
        if english and not item.get('original_title'):
            item['original_title'] = english
        seasons = {s['season']: s for s in item.get('seasons', [])}
        seen = set()
        for source in entry.get('seasons', []):
            number = source.get('season')
            if type(number) is not int or not 1 <= number <= 200 or number in seen:
                raise ValueError('invalid or duplicate App season')
            seen.add(number)
            target = seasons.get(number)
            if target is None:
                target = {'season': number, 'id': f"{item['id']}:s{number}", 'synopsis_status': 'missing'}
                item.setdefault('seasons', []).append(target)
            ident = source.get('official_id')
            if ident:
                if not re.fullmatch(r'[A-Z0-9]{10}', ident):
                    raise ValueError('invalid App season ASIN')
                if target.get('official_id') and target['official_id'] != ident:
                    raise ValueError('conflicting App season ASIN')
                target.setdefault('official_id', ident)
                if not target.get('official_id'):
                    target['official_id'] = ident
            compact = compact_id(source.get('official_link'))
            if source.get('official_link') and not compact:
                raise ValueError('invalid App official season link')
            if compact and not target.get('official_compact_id'):
                target['official_compact_id'] = compact
                target['official_link'] = '/-/zh/detail/' + compact
                target['identity_source'] = 'app_official_selector_snapshot'
        item['seasons'] = sorted(item.get('seasons', []), key=lambda s: s['season'])
    return result
