import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from source_matching import (release_years, with_release_evidence, identity_match, select,
                             resolve_sources, binding_key)
from metadata_resolution import page_patch


def film():
    return {'prime_id': 'B0CVNJ632Z', 'kind': 'movie', 'season': 0, 'year': 2024,
            'english_title': 'In the Land of Saints and Sinners', 'display_name_zh': '圣人营救'}


def hit(**extra):
    return {'provider_id': '59463', 'title': '圣人营救', 'kind': 'movie', 'year': 2023,
            'episode_count': 1, 'has_routes': True, **extra}


class SourceMatchingTests(unittest.TestCase):
    def test_saints_exception_is_identity_bound_and_preserves_display(self):
        record = with_release_evidence(film())
        self.assertEqual(release_years(record), {2023, 2024})
        self.assertEqual(record['year'], 2024)
        self.assertEqual(identity_match(record, hit()), 'matched')
        self.assertEqual(identity_match(film(), hit()), 'needs_release_evidence')

    def test_registry_does_not_apply_by_title_alone(self):
        record = film() | {'prime_id': 'B000000000'}
        self.assertNotIn('releaseYearEvidence', with_release_evidence(record))

    def test_wrong_proof_identity_type_original_year_and_season_fail_closed(self):
        original = with_release_evidence(film())
        for field in ('officialId', 'originalTitle', 'kind', 'officialYear', 'season', 'imdbId'):
            record = copy.deepcopy(original); record['releaseYearEvidence'][field] = 'wrong'
            self.assertEqual(release_years(record), {2024}, field)

    def test_sources_must_be_independent_https_and_credential_free(self):
        original = with_release_evidence(film())
        for url in ('http://example.com/a', 'https://example.com/a?api_key=secret',
                    'https://user:password@example.com/a', 'https://www.labiennale.org/other'):
            record = copy.deepcopy(original); record['releaseYearEvidence']['releases'][1]['url'] = url
            self.assertEqual(release_years(record), {2024}, url)

    def test_no_generic_adjacent_year_tolerance(self):
        self.assertEqual(identity_match(with_release_evidence(film()), hit(year=2022)), 'needs_release_evidence')

    def test_conflicting_external_id_cast_or_director_is_rejected(self):
        record = with_release_evidence(film()) | {'directors': ['Robert Lorenz'], 'actors': ['Liam Neeson']}
        for patch in ({'imdb_id': 'tt0000000'}, {'directors': ['Another director']}, {'actors': ['Someone else']}):
            self.assertEqual(identity_match(record, hit(**patch)), 'rejected')

    def test_season_conflict_and_missing_season_are_never_guessed(self):
        record = {'prime_id': 'B012345678', 'english_title': 'Supernatural', 'display_name_zh': '邪恶力量', 'year': 2008, 'season': 4, 'kind': 'tv'}
        base = hit(title='邪恶力量第四季', year=2008, kind='tv')
        self.assertEqual(identity_match(record, base), 'matched')
        self.assertEqual(identity_match(record, base | {'season': 1}), 'rejected')
        self.assertEqual(identity_match(record, base | {'title': '邪恶力量'}), 'unverified')

    def test_frequency_and_more_episodes_cannot_resolve_identity_ambiguity(self):
        record = with_release_evidence(film())
        state, chosen = select(record, [hit(), hit(provider_id='999', episode_count=100)])
        self.assertEqual(state, 'ambiguous'); self.assertIsNone(chosen)

    def test_ai_candidate_name_without_verified_alias_is_not_accepted(self):
        record = with_release_evidence(film())
        self.assertEqual(identity_match(record, hit(title='AI guessed title')), 'rejected')

    def test_episode_count_difference_does_not_reject_the_same_work(self):
        record = with_release_evidence(film()); cache = {}
        result = resolve_sources(record, ['a', 'b'], lambda p, q: [hit(episode_count=1 if p == 'a' else 2)], Mock(), cache)
        self.assertEqual(result['status'], 'matched'); self.assertEqual(result['episode_count'], 2)

    def test_saved_provider_id_is_revalidated_before_reusing_without_search(self):
        record = with_release_evidence(film()); cache = {binding_key(record, '360'): {'provider_id': '59463'}}
        search = Mock(side_effect=AssertionError('must reuse detail')); detail = Mock(return_value=hit())
        result = resolve_sources(record, ['360'], search, detail, cache)
        search.assert_not_called(); detail.assert_called_once_with('360', '59463')
        self.assertTrue(result['sources'][0]['reused']); self.assertEqual(result['episode_count'], 1)

    def test_try_all_strict_provider_results_before_spending_a_name_lookup(self):
        lookup = Mock(side_effect=AssertionError('existing source needs no AI lookup'))
        result = resolve_sources(film(), ['empty', 'found'], lambda p, q: [] if p == 'empty' else [hit()], Mock(), {}, lookup)
        lookup.assert_not_called(); self.assertEqual(result['status'], 'matched')

    def test_transport_error_preserves_saved_binding_and_is_not_no_match(self):
        record = with_release_evidence(film()); cache = {binding_key(record, '360'): {'provider_id': '59463'}}
        before = copy.deepcopy(cache)
        result = resolve_sources(record, ['360'], Mock(), Mock(side_effect=TimeoutError()), cache)
        self.assertEqual(result['status'], 'unavailable'); self.assertEqual(cache, before)

    def test_failed_fallback_lookup_does_not_claim_no_sources(self):
        result = resolve_sources(film(), ['360'], lambda p, q: [], Mock(), {}, Mock(side_effect=TimeoutError()))
        self.assertEqual(result['status'], 'unavailable')

    def test_wrong_saved_identity_is_invalidated_and_searched_again(self):
        record = with_release_evidence(film()); cache = {binding_key(record, '360'): {'provider_id': 'old'}}
        result = resolve_sources(record, ['360'], lambda p, q: [hit()], lambda p, i: hit(title='Another film'), cache)
        self.assertEqual(result['sources'][0]['provider_id'], '59463')

    def test_bindings_and_output_do_not_contain_media_urls(self):
        cache = {}; result = resolve_sources(film(), ['360'], lambda p, q: [hit(vod_play_url='https://example.com/private.m3u8')], Mock(), cache)
        self.assertNotIn('m3u8', json.dumps([cache, result]))

    def test_verified_fallback_alias_is_searched_and_reused(self):
        record = film() | {'display_name_zh': 'Original guess'}; cache = {}
        lookup = Mock(return_value={'name_status': 'verified_title_sources', 'aliases_zh': ['圣人营救']})
        search = lambda p, q: [hit()] if q == '圣人营救' else []
        result = resolve_sources(record, ['360'], search, Mock(), cache, lookup)
        self.assertEqual(result['status'], 'matched'); lookup.assert_called_once()
        result = resolve_sources(record, ['360'], Mock(), lambda p, i: hit(), cache)
        self.assertTrue(result['sources'][0]['reused'])

    def test_page_lookup_uses_the_same_verified_release_exception(self):
        node = {'@type': 'Movie', 'name': '圣人营救', 'alternateName': film()['english_title'], 'datePublished': '2023-09-06'}
        page = '<script type="application/ld+json">' + json.dumps(node) + '</script>'
        self.assertIsNotNone(page_patch(film(), page, 'https://movie.douban.com/subject/35722688/', 'now'))
        self.assertIsNone(page_patch(film() | {'prime_id': 'B000000000'}, page, 'https://movie.douban.com/subject/35722688/', 'now'))

    def test_review_keeps_blocked_sources_distinct_and_never_publishes(self):
        from review_sources import review, check_policy
        result = review({'reference': film(), 'providers': [{'name': 'A', 'status': 'blocked'}, {'name': 'B', 'candidates': [hit()]}]})
        self.assertEqual(result['sources'][0]['status'], 'unavailable')
        self.assertEqual(result['sources'][1]['status'], 'matched')
        self.assertFalse(result['publication']); self.assertEqual(check_policy()['network_requests'], 0)


if __name__ == '__main__':
    unittest.main()
