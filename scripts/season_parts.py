"""Resolve explicit Prime season/part labels; never guess from a numeric code alone."""
import re

def season_fields(number, title):
    raw = int(number or 0)
    match = re.search(r'\bSeason\s+(\d+)\s*[,;:\-–—]?\s*Part\s+(\d+)\b', str(title or ''), re.I)
    if not match:
        return {'season': raw}
    season, part = map(int, match.groups())
    if not (1 <= season <= 30 and 1 <= part <= 99) or raw not in (0, season, season * 100 + part):
        raise ValueError('Conflicting official season/part identity')
    return {'season': season, 'officialSeasonNumber': raw, 'seasonPart': part}
