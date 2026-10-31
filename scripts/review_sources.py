"""Review an exported source-result snapshot privately, or validate public rules.

This command consumes results already queried by the app/local collector. It does
not contact provider endpoints or publish provider IDs and playback data.
"""
import argparse
import json
from pathlib import Path
from source_matching import evidence_registry, with_release_evidence, release_years, select

ROOT = Path(__file__).resolve().parents[1]


def check_policy():
    seen = set()
    for proof in evidence_registry():
        ident = proof.get('officialId')
        if ident in seen:
            raise ValueError('duplicate official release identity')
        seen.add(ident)
        reference = {'prime_id': ident, 'english_title': proof.get('originalTitle'),
                     'year': proof.get('officialYear'), 'kind': proof.get('kind'),
                     'season': proof.get('season'), 'releaseYearEvidence': proof}
        if len(release_years(reference)) < 2:
            raise ValueError('invalid or unbound release evidence')
        allowed = {'officialId', 'originalTitle', 'officialYear', 'kind', 'season', 'imdbId', 'verifiedAt', 'releases'}
        if set(proof) - allowed:
            raise ValueError('unexpected public proof field')
        if any(set(r) != {'year', 'region', 'url'} for r in proof['releases']):
            raise ValueError('unexpected public release field')
    return {'verified_release_exceptions': len(seen), 'network_requests': 0}


def review(snapshot):
    reference = with_release_evidence(snapshot['reference'])
    output = []
    for provider in snapshot['providers'][:18]:
        if provider.get('status') in ('blocked', 'unavailable', 'timeout'):
            output.append({'provider': provider['name'], 'status': 'unavailable'}); continue
        state, candidate = select(reference, provider.get('candidates', []))
        row = {'provider': provider['name'], 'status': state}
        if candidate:
            row.update(provider_id=str(candidate['provider_id']), episode_count=candidate.get('episode_count', 0))
        output.append(row)
    return {'sources': output, 'publication': False, 'network_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-policy', action='store_true')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / '.local/source-review.json')
    args = parser.parse_args()
    if args.check_policy:
        print(json.dumps(check_policy())); return
    if not args.input:
        parser.error('--input is required for a private review')
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / '.local').resolve()):
        parser.error('source review output must remain in this checkout .local/ directory')
    result = review(json.loads(args.input.read_text(encoding='utf-8')))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'providers': len(result['sources']), 'publication': False, 'output': str(output)}))


if __name__ == '__main__':
    main()
