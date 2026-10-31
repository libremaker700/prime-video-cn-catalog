import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_source_verification import (MAX_BYTES, MAX_ITEMS, encoded, publish,
                                        validate_publication, validate_snapshot)

CLOCK = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)


def row(**extra):
    return {'officialId': 'B0CVNJ632Z', 'originalTitle': 'In the Land of Saints and Sinners',
            'kind': 'movie', 'season': 0, 'year': 2024, 'status': 'matched',
            'checkedAt': '2026-10-30T12:00:00Z', **extra}


def write_input(root, rows):
    path = root / 'data/source-verification.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schema': 1, 'items': rows}), encoding='utf-8')


class SourcePublicationTests(unittest.TestCase):
    def test_matched_and_unknown_have_distinct_honest_check_times(self):
        payload = {'schema': 1, 'items': [row(), row(officialId='B012345678', status='pending', checkedAt=None)]}
        self.assertEqual(validate_snapshot(payload, clock=CLOCK), payload)
        for state in ('matched', 'no_match', 'unavailable'):
            with self.assertRaises(ValueError):
                validate_snapshot({'schema': 1, 'items': [row(status=state, checkedAt=None)]}, clock=CLOCK)

    def test_public_fields_reject_provider_addresses_ids_sessions_and_credentials(self):
        for field in ('provider_id', 'endpoint', 'vod_play_url', 'api_key', 'provider', 'evidence'):
            candidate = row(); candidate[field] = 'PRIVATE'
            with self.assertRaises(ValueError):
                validate_snapshot({'schema': 1, 'items': [candidate]}, clock=CLOCK)
        with self.assertRaises(ValueError):
            validate_snapshot({'schema': 1, 'items': [row(originalTitle='https://private.example/path')]}, clock=CLOCK)

    def test_identity_type_year_and_season_are_required(self):
        for changes in ({'officialId': 'bad'}, {'kind': 'live'}, {'season': 1}, {'season': True},
                        {'year': '2024'}, {'year': 0}, {'originalTitle': ''}, {'status': 'verified'},
                        {'status': {}}, {'kind': 'tv', 'season': 0}):
            with self.assertRaises(ValueError, msg=str(changes)):
                validate_snapshot({'schema': 1, 'items': [row(**changes)]}, clock=CLOCK)
        self.assertTrue(validate_snapshot({'schema': 1, 'items': [row(kind='tv', season=4)]}, clock=CLOCK))

    def test_duplicate_same_work_and_season_is_not_ambiguous(self):
        with self.assertRaises(ValueError):
            validate_snapshot({'schema': 1, 'items': [row(), row(year=2023)]}, clock=CLOCK)

    def test_bogus_or_future_check_dates_are_not_published(self):
        for value in ('yesterday', '2026-10-31T10:00:00', '2030-01-01T00:00:00Z', 123):
            with self.assertRaises(ValueError):
                validate_snapshot({'schema': 1, 'items': [row(checkedAt=value)]}, clock=CLOCK)

    def test_limits_match_the_native_reader(self):
        self.assertEqual(MAX_BYTES, 512 * 1024)
        self.assertEqual(MAX_ITEMS, 2000)
        with self.assertRaises(ValueError):
            validate_snapshot({'schema': 1, 'items': [row()] * (MAX_ITEMS + 1)}, clock=CLOCK)
        with self.assertRaises(ValueError):
            encoded({'large': 'a' * MAX_BYTES})

    def test_missing_input_preserves_publication_and_never_claims_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = publish(root, clock=CLOCK)
            self.assertEqual(result['status'], 'skipped_no_preprocessing_result')
            self.assertEqual(result['fresh_checks'], 0)
            self.assertFalse((root / 'docs').exists())

    def test_immutable_data_is_written_before_its_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_input(root, [row()])
            import publish_source_verification as module
            original = module.atomic_write; order = []
            def write(path, raw):
                order.append(path.relative_to(root / 'docs').as_posix())
                if path.name == 'source-verification-version.json':
                    manifest = json.loads(raw)
                    self.assertTrue((root / 'docs' / manifest['path']).is_file())
                original(path, raw)
            with patch.object(module, 'atomic_write', side_effect=write):
                result = publish(root, clock=CLOCK)
            self.assertEqual(order[-1], 'source-verification-version.json')
            self.assertTrue(order[0].startswith('source-verification/'))
            manifest = validate_publication(root / 'docs', clock=CLOCK)
            self.assertEqual(manifest['sha256'], result['sha256'])
            self.assertEqual(manifest['checkedAt'], '2026-10-30T12:00:00+00:00')
            self.assertEqual(result['fresh_checks'], 0)

    def test_unchanged_preprocessing_does_not_redate_checks_or_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_input(root, [row()])
            publish(root, clock=CLOCK)
            before = (root / 'docs/source-verification-version.json').read_bytes()
            result = publish(root, clock=CLOCK + timedelta(days=1))
            self.assertEqual(result['status'], 'unchanged')
            self.assertEqual((root / 'docs/source-verification-version.json').read_bytes(), before)

    def test_current_and_previous_source_releases_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); releases = []
            for index in range(3):
                write_input(root, [row(status=('matched', 'no_match', 'pending')[index])])
                publish(root, clock=CLOCK + timedelta(hours=index))
                releases.append(validate_publication(root / 'docs', clock=CLOCK)['path'])
            self.assertFalse((root / 'docs' / releases[0]).exists())
            self.assertTrue((root / 'docs' / releases[1]).is_file())
            self.assertTrue((root / 'docs' / releases[2]).is_file())

    def test_invalid_or_empty_replacement_does_not_change_last_valid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_input(root, [row()]); publish(root, clock=CLOCK)
            before = (root / 'docs/source-verification-version.json').read_bytes()
            for rows in ([row(provider_id='private')], []):
                write_input(root, rows)
                with self.assertRaises(ValueError):
                    publish(root, clock=CLOCK)
                self.assertEqual((root / 'docs/source-verification-version.json').read_bytes(), before)

    def test_corrupt_content_hash_fails_before_replacing_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_input(root, [row()]); publish(root, clock=CLOCK)
            manifest = validate_publication(root / 'docs', clock=CLOCK)
            (root / 'docs' / manifest['path']).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                publish(root, clock=CLOCK)

    def test_failed_manifest_write_keeps_old_hash_available_and_can_repair_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_input(root, [row()]); publish(root, clock=CLOCK)
            old = validate_publication(root / 'docs', clock=CLOCK)
            write_input(root, [row(status='no_match')])
            import publish_source_verification as module
            original = module.atomic_write
            def write(path, raw):
                if path.name == 'source-verification-version.json':
                    raise OSError('simulated disk failure')
                original(path, raw)
            with patch.object(module, 'atomic_write', side_effect=write):
                with self.assertRaises(OSError):
                    publish(root, clock=CLOCK)
            self.assertEqual(json.loads((root / 'docs/source-verification-version.json').read_bytes())['sha256'], old['sha256'])
            self.assertTrue((root / 'docs' / old['path']).is_file())
            # The App's immutable old path remains valid; another run repairs the side copy.
            self.assertEqual(publish(root, clock=CLOCK)['status'], 'published_preprocessing_result')
            self.assertIsNotNone(validate_publication(root / 'docs', clock=CLOCK))

    def test_partial_export_retains_missing_published_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first = row(); second = row(officialId='B012345678')
            write_input(root, [first]); publish(root, clock=CLOCK)
            write_input(root, [second]); result = publish(root, clock=CLOCK)
            snapshot = json.loads((root / 'docs/source-verification.json').read_bytes())
            self.assertEqual(snapshot['items'], [first, second])
            self.assertEqual(result['count'], 2)

    def test_incomplete_updates_preserve_fresh_positive_without_redating(self):
        for status in ('pending', 'unavailable'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); first = row(); write_input(root, [first]); publish(root, clock=CLOCK)
                before = (root / 'docs/source-verification-version.json').read_bytes()
                write_input(root, [row(status=status, checkedAt='2026-10-31T09:00:00Z')])
                self.assertEqual(publish(root, clock=CLOCK)['status'], 'unchanged')
                self.assertEqual((root / 'docs/source-verification-version.json').read_bytes(), before)
                self.assertEqual(json.loads((root / 'docs/source-verification.json').read_bytes())['items'], [first])

    def test_explicit_clean_miss_can_replace_fresh_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_input(root, [row()]); publish(root, clock=CLOCK)
            miss = row(status='no_match', checkedAt='2026-10-31T09:00:00Z')
            write_input(root, [miss]); publish(root, clock=CLOCK)
            self.assertEqual(json.loads((root / 'docs/source-verification.json').read_bytes())['items'], [miss])

    def test_stale_positive_can_receive_incomplete_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_input(root, [row(checkedAt='2026-10-01T12:00:00Z')]); publish(root, clock=CLOCK)
            update = row(status='unavailable', checkedAt='2026-10-31T09:00:00Z')
            write_input(root, [update]); publish(root, clock=CLOCK)
            self.assertEqual(json.loads((root / 'docs/source-verification.json').read_bytes())['items'], [update])

    def test_changed_original_or_year_does_not_reuse_same_slot_positive(self):
        for changed in ({'year': 2023}, {'originalTitle': 'Different Original Film'}):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); write_input(root, [row()]); publish(root, clock=CLOCK)
                update = row(status='pending', checkedAt=None, **changed)
                write_input(root, [update]); publish(root, clock=CLOCK)
                self.assertEqual(json.loads((root / 'docs/source-verification.json').read_bytes())['items'], [update])

    def test_retained_union_cannot_exceed_native_reader_record_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_input(root, [row(officialId=f'B{index:09d}') for index in range(MAX_ITEMS)])
            publish(root, clock=CLOCK)
            before = (root / 'docs/source-verification-version.json').read_bytes()
            write_input(root, [row(officialId='B099999999')])
            with self.assertRaises(ValueError):
                publish(root, clock=CLOCK)
            self.assertEqual((root / 'docs/source-verification-version.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
