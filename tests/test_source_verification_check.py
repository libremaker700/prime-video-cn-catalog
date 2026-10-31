import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_source_verification import Probe, candidate, configured, native_playlist, run
from source_matching import select

CLOCK = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)


def target(number=1, **extra):
    return {'officialId': f'B{number:09d}', 'originalTitle': 'Example Film', 'kind': 'movie',
            'season': 0, 'year': 2024, 'displayTitle': '示例电影', 'aliases': ['示例别名'], **extra}


def result(record=None, **extra):
    record = record or target()
    return {key: record[key] for key in ('officialId', 'originalTitle', 'kind', 'season', 'year')} | {
        'status': 'matched', 'checkedAt': '2026-10-30T10:00:00Z', **extra}


def source(**extra):
    return {'vod_id': '88123999', 'vod_name': '示例电影', 'type_name': '动作片', 'vod_year': '2024',
            'vod_play_url': '正片$https://media.example/film.m3u8?token=PRIVATE', **extra}


class Response:
    def __init__(self, rows=None, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps({'list': rows or []}, ensure_ascii=False).encode('utf-8')
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('private transport')

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


class SourceVerificationCheckTests(unittest.TestCase):
    def files(self, root, targets=None, previous=None):
        data = root / 'data'; data.mkdir(parents=True, exist_ok=True)
        (data / 'source-verification-targets.json').write_text(json.dumps({'schema': 1, 'items': targets or [target()]}), encoding='utf-8')
        if previous is not None:
            (data / 'source-verification.json').write_text(json.dumps({'schema': 1, 'items': previous}), encoding='utf-8')

    def quiet_run(self, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run(*args, **kwargs)

    def test_fresh_positive_is_reused_without_a_source_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.files(root, previous=[result()])
            path = root / 'data/source-verification.json'; before = path.read_bytes()
            probe = Mock(requests=0)
            actual = self.quiet_run(root, providers=[{'url': 'https://provider.example/api'}], probe=probe, clock=CLOCK)
            probe.check.assert_not_called()
            self.assertEqual(actual['items'], [result()]); self.assertEqual(path.read_bytes(), before)

    def test_failed_retry_preserves_old_positive_and_original_check_time(self):
        old = result(checkedAt='2026-10-01T10:00:00Z')
        for response in ('unavailable', 'no_match', 'pending'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); self.files(root, previous=[old])
                probe = Mock(requests=0); probe.check.return_value = response
                actual = self.quiet_run(root, providers=[{}], probe=probe, clock=CLOCK)
                probe.check.assert_called_once()
                self.assertEqual(actual['items'], [old], response)

    def test_missing_configuration_performs_zero_requests_and_preserves_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.files(root, previous=[result()]); probe = Mock(requests=0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                actual = run(root, providers=[], probe=probe, clock=CLOCK)
            probe.check.assert_not_called()
            self.assertEqual(actual['items'], [result()])
            self.assertIn('not_configured', output.getvalue())

    def test_empty_partial_checks_are_unavailable_instead_of_no_match(self):
        session = Mock()
        session.get.side_effect = [Response(), Response(), Response(), requests.Timeout('PRIVATE')]
        probe = Probe([{'url': 'https://working.example/api'}, {'url': 'https://blocked.example/api'}], 200, session)
        with patch('check_source_verification.time.monotonic', return_value=100):
            self.assertEqual(probe.check(target()), 'unavailable')
        self.assertEqual(probe.requests, 4)

    def test_only_clean_completed_misses_become_no_match(self):
        session = Mock(); session.get.side_effect = lambda *args, **kwargs: Response()
        probe = Probe([{'url': 'https://working.example/api'}], 200, session)
        with patch('check_source_verification.time.monotonic', return_value=100):
            self.assertEqual(probe.check(target()), 'no_match')
        self.assertEqual(probe.requests, 3)

    def test_native_playlist_requires_real_media_path_not_html_query_or_credentials(self):
        self.assertTrue(native_playlist(source()))
        for url in ('https://example.com/player?url=film.m3u8', 'javascript:film.mp4',
                    'https://user:password@example.com/film.m3u8', 'https://example.com/watch.html'):
            self.assertFalse(native_playlist(source(vod_play_url='正片$' + url)), url)

    def test_same_title_season_conflict_is_rejected_and_exact_season_matches(self):
        record = target(kind='tv', season=4, displayTitle='示例剧', originalTitle='Example Series')
        from check_source_verification import reference
        ref = reference(record)
        correct = candidate(source(vod_name='示例剧第四季', type_name='欧美剧'))
        wrong = candidate(source(vod_name='示例剧第一季', type_name='欧美剧'))
        self.assertEqual(select(ref, [correct])[0], 'matched')
        self.assertEqual(select(ref, [wrong])[0], 'no_match')

    def test_public_output_and_logs_contain_no_provider_identity_or_media_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.files(root)
            session = Mock(); session.get.return_value = Response([source()])
            probe = Probe([{'url': 'https://provider.example/private-api'}], 200, session)
            with patch('check_source_verification.time.monotonic', return_value=100), contextlib.redirect_stdout(io.StringIO()) as log:
                actual = run(root, providers=[{}], probe=probe, clock=CLOCK)
            self.assertEqual(actual['items'][0]['status'], 'matched')
            text = json.dumps(actual) + log.getvalue()
            for private in ('88123999', 'private-api', 'media.example', 'PRIVATE', 'provider_id', 'vod_play_url'):
                self.assertNotIn(private, text)
            self.assertEqual(set(actual['items'][0]), {'officialId', 'originalTitle', 'kind', 'season', 'year', 'status', 'checkedAt'})

    def test_per_run_item_limit_is_capped_and_unvisited_rows_remain_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.files(root, targets=[target(index) for index in range(35)])
            probe = Mock(requests=0); probe.check.return_value = 'matched'
            actual = self.quiet_run(root, providers=[{}], probe=probe, limit=999, clock=CLOCK)
            self.assertEqual(probe.check.call_count, 30)
            self.assertEqual(sum(row['status'] == 'matched' for row in actual['items']), 30)
            self.assertTrue(all(row['checkedAt'] is None for row in actual['items'] if row['status'] == 'pending'))

    def test_zero_item_limit_makes_no_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.files(root); probe = Mock(requests=0)
            actual = self.quiet_run(root, providers=[{}], probe=probe, limit=0, clock=CLOCK)
            probe.check.assert_not_called(); self.assertEqual(actual['items'][0]['status'], 'pending')

    def test_global_deadline_stops_before_starting_another_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.files(root, targets=[target(1), target(2)])
            probe = Mock(requests=0); probe.check.return_value = 'matched'
            with patch('check_source_verification.time.monotonic', side_effect=[100, 101, 200]):
                actual = self.quiet_run(root, providers=[{}], probe=probe, budget=15, clock=CLOCK)
            self.assertEqual(probe.check.call_count, 1)
            self.assertEqual(actual['items'][1]['status'], 'pending')

    def test_expired_request_deadline_makes_no_http_call(self):
        session = Mock(); probe = Probe([{'url': 'https://provider.example/api'}], 99, session)
        with patch('check_source_verification.time.monotonic', return_value=100):
            with self.assertRaises(RuntimeError):
                probe.get(probe.providers[0], {})
        session.get.assert_not_called(); self.assertEqual(probe.requests, 0)

    def test_source_and_query_counts_are_bounded(self):
        session = Mock(); session.get.side_effect = lambda *args, **kwargs: Response()
        probe = Probe([{'url': f'https://host{index}.example/api'} for index in range(30)], 200, session)
        with patch('check_source_verification.time.monotonic', return_value=100):
            self.assertEqual(probe.check(target(aliases=['甲', '乙', '丙', '丁', '戊'])), 'no_match')
        self.assertEqual(len(probe.providers), 18); self.assertEqual(probe.requests, 54)

    def test_blocked_host_stops_additional_requests_to_same_host(self):
        response = Response(status=429); session = Mock(); session.get.return_value = response
        providers = [{'url': 'https://limited.example/a'}, {'url': 'https://limited.example/b'}]
        probe = Probe(providers, 200, session)
        with patch('check_source_verification.time.monotonic', return_value=100):
            self.assertEqual(probe.check(target()), 'unavailable')
        self.assertEqual(session.get.call_count, 1); self.assertTrue(response.closed)

    def test_streaming_response_byte_limit_is_enforced_before_json_decode(self):
        response = Response(raw=b'a' * 2_000_001); session = Mock(); session.get.return_value = response
        probe = Probe([{'url': 'https://provider.example/api'}], 200, session)
        with patch('check_source_verification.time.monotonic', return_value=100):
            with self.assertRaises(RuntimeError):
                probe.get(probe.providers[0], {})
        self.assertTrue(response.closed); self.assertTrue(session.get.call_args.kwargs['stream'])

    def test_bad_configuration_is_rejected_and_unsupported_adapter_is_skipped(self):
        self.assertEqual(configured(json.dumps([{'url': 'https://example.com/api', 'adapter': 'juzi'}])), [])
        for value in ('{}', '["PRIVATE"]', '[{"url":"file:///private"}]',
                      '[{"url":"https://user:password@example.com/api"}]'):
            with self.assertRaises(ValueError):
                configured(value)


if __name__ == '__main__':
    unittest.main()
