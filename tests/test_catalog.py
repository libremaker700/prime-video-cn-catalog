import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from catalog import match, merge, publish, repair, validate, write_json
from sources import Clients, SourceError, parse_prime, pick_tmdb
from sync import due, run, record_due


def record(n=0):
    ident = f"B{n:09d}"
    return {"id": "prime:" + ident, "slug": "test-" + str(n), "prime_id": ident,
            "kind": "tv", "identity_status": "verified_official_mapping",
            "display_name_zh": "作品" + str(n), "name_status": "verified", "seasons": []}


class CatalogTests(unittest.TestCase):
    def test_only_bounded_https_same_site_redirects_are_followed(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sources.time.sleep'):
            client = Clients(Path(tmp), print)
            redirect = SimpleNamespace(status_code=302, headers={'Location': '/detail/canonical'})
            good = SimpleNamespace(status_code=200)
            with patch.object(client.session, 'request', side_effect=[redirect, good]) as request:
                self.assertIs(client.request('https://www.primevideo.com/detail/old'), good)
                self.assertEqual(request.call_args.args[1], 'https://www.primevideo.com/detail/canonical')
                self.assertIsNone(request.call_args.kwargs['params'])
            for location in ('https://evil.example/detail', 'http://www.primevideo.com/detail', 'https://user:password@www.primevideo.com/detail', '/detail/old'):
                with self.subTest(location=location), patch.object(client.session, 'request', return_value=SimpleNamespace(status_code=302, headers={'Location': location})) as request:
                    with self.assertRaises(SourceError):client.request('https://www.primevideo.com/detail/old')
                    self.assertEqual(request.call_count, 1)
            with patch.object(client.session, 'request', side_effect=lambda *a,**k: SimpleNamespace(status_code=302,headers={'Location': a[1]+'x'})) as request:
                with self.assertRaises(SourceError):client.request('https://www.primevideo.com/detail/loop')
                self.assertEqual(request.call_count, 5)
            self.assertFalse(client.blocked)

    def test_missing_individual_titles_do_not_open_the_site_circuit(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sources.time.sleep'):
            client = Clients(Path(tmp), print)
            with patch.object(client.session, 'request', return_value=SimpleNamespace(status_code=404,headers={})):
                for n in range(4):
                    with self.assertRaises(SourceError):client.request(f'https://www.primevideo.com/detail/missing{n}')
            self.assertFalse(client.blocked)
    def test_prime_preserves_png_source_encoding_before_local_jpeg_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=Clients(Path(tmp),print)
            for ext in ("png","jpg"):
                data={"title":"作品", "entityType":"TV Show", "seasonNumber":2,
                      "images":{"heroshot":"https://m.media-amazon.com/images/S/pv-target-images/"+"a"*64+"."+ext}}
                with patch.object(client,"image",return_value={"scope":"season"}) as image:
                    client.prime_patch("B012345678",data)
                    self.assertTrue(image.call_args.args[0].endswith(f"._SX1280_FM{ext}_.{ext}"))
                    self.assertEqual(image.call_args.kwargs["season"],2)

    def test_request_budget_limits_timeout_and_prevents_late_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Clients(Path(tmp), print)
            client.deadline = 105
            with patch('sources.time.monotonic', return_value=100), patch.object(client.session, 'request') as request:
                request.return_value.status_code = 200
                client.request('https://www.primevideo.com/example')
                self.assertEqual(request.call_args.kwargs['timeout'], (2.5, 2.5))
            with patch('sources.time.monotonic', return_value=105), patch.object(client.session, 'request') as request:
                with self.assertRaisesRegex(SourceError, 'time budget reached'):
                    client.request('https://www.primevideo.com/example')
                request.assert_not_called()

    def test_retry_backoff_stops_at_deadline_without_second_http_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Clients(Path(tmp), print)
            clock = [100.0]
            client.deadline = 101
            with patch('sources.time.monotonic', side_effect=lambda: clock[0]), patch('sources.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)) as sleep, patch.object(client.session, 'request') as request:
                request.return_value.status_code = 503
                request.return_value.headers = {'Retry-After': '30'}
                with self.assertRaisesRegex(SourceError, 'time budget reached'):
                    client.request('https://www.primevideo.com/example')
                self.assertEqual(request.call_count, 1)
                sleep.assert_called_once_with(1.0)

    def test_schedule_boundary_future_and_bad_dates(self):
        clock = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(due({"last_success_at": "2026-10-29T00:00:00+00:00"}, clock=clock))
        self.assertFalse(due({"last_success_at": "2026-10-29T00:00:01+00:00"}, clock=clock))
        for value in (None, "bad", "2026-11-06T00:00:00+00:00", "2026-10-29T00:00:00"):
            self.assertTrue(due({"last_success_at": value}, clock=clock))

    def test_identity_does_not_merge_remakes_different_media_or_ambiguous_names(self):
        seed = {"original_title": "The Gift", "kind": "movie", "year": 2015}
        other = {"original_title": "The Gift", "kind": "movie", "year": 2000}
        tv = {"original_title": "The Gift", "kind": "tv", "year": 2015}
        self.assertIsNone(match(seed, [other, tv]))
        self.assertIsNone(match(seed, [seed.copy(), seed.copy()]))
        self.assertIsNotNone(match({"original_title": "ヴィンランド・サガ", "kind": "anime"}, [{"original_title": "ヴィンランド・サガ", "kind": "tv"}]))

    def test_merge_retains_curated_name_ids_details_sources_and_season_art(self):
        old = record()
        old.update(display_name_zh="辐射", name_status="user_app_preference", sources=[{"type": "prime", "url": "old"}], details={"first_air_year": 2024})
        old["seasons"] = [{"season": 2, "id": "stable", "official_id": "B012345678", "synopsis_zh": "第二季简介", "synopsis_status": "ok", "year": 2025, "poster": {"path": "kept"}, "poster_scope": "season"}]
        fresh = {"display_name_zh": "异尘馀生", "name_status": "verified", "details": {}, "sources": [{"type": "wiki", "url": "new"}], "seasons": [{"season": 2, "id": "temporary", "synopsis_zh": "", "synopsis_status": "missing", "poster": None, "poster_status": "missing"}]}
        out = merge(old, fresh)
        self.assertEqual(out["display_name_zh"], "辐射")
        self.assertIn("异尘馀生", out["aliases_zh"])
        self.assertEqual(out["details"]["first_air_year"], 2024)
        self.assertEqual(len(out["sources"]), 2)
        for key in ("id", "official_id", "synopsis_zh", "synopsis_status", "year", "poster", "poster_scope"):
            self.assertEqual(out["seasons"][0][key], old["seasons"][0][key])

    def test_real_snapshot_restores_all_previously_lost_season_ids(self):
        root = Path(__file__).resolve().parents[1]
        current = json.loads((root / "docs/catalog.json").read_text(encoding="utf-8"))["items"]
        base = json.loads((root / "data/fallback_catalog.json").read_text(encoding="utf-8"))["items"]
        restored = repair(current, base, [])
        for original in base:
            found = match(original, restored)
            if not found:
                continue
            seasons = {s["season"]: s for s in found.get("seasons", [])}
            for s in original.get("seasons", []):
                if s.get("official_id"):
                    self.assertEqual(seasons[s["season"]]["official_id"], s["official_id"])
        self.assertEqual(len({x["slug"] for x in restored}), len(restored))

    def test_prime_rejects_page_or_season_mismatch(self):
        ident = "B012345678"
        data = {"title": "辐射", "entityType": "TV Show", "seasonNumber": 2}
        state = {"init": {"preparations": {"body": {"atf": {"state": {"pageTitleId": ident, "detail": {"headerDetail": {ident: data}}}}}}}}
        content = '<script id="dv-web-page-hydration-data">' + json.dumps(state) + '</script>'
        self.assertEqual(parse_prime(content, ident, 2)["seasonNumber"], 2)
        for wrong_id, wrong_season in (("B112345678", 2), (ident, 1)):
            with self.assertRaises(SourceError):
                parse_prime(content, wrong_id, wrong_season)

    def test_tmdb_ambiguous_and_wrong_year_are_rejected(self):
        seed = {"original_title": "Invincible", "kind": "anime", "year": 2021}
        good = {"original_name": "Invincible", "first_air_date": "2021-03-25", "id": 1}
        bad = dict(good, first_air_date="2001-01-01", id=2)
        self.assertEqual(pick_tmdb(seed, [bad, good])["id"], 1)
        self.assertIsNone(pick_tmdb(seed, [good, good.copy()]))

    def test_prime_asin_compact_id_and_gti_alias_binding(self):
        asin, compact, gti = "B012345678", "0KBDY8OBBE3Y0RV5FOQH96H3N6", "amzn1.dv.gti.example"
        data = {"title": "辐射 第二季", "entityType": "TV Show", "seasonNumber": 2}
        state = {"pageTitleId": gti, "self": {gti: {"asins": [asin], "compactGTI": compact}}, "detail": {"headerDetail": {gti: data}}}
        hydration = {"init": {"preparations": {"body": {"atf": {"state": state}}}}}
        content = '<script id="dv-web-page-hydration-data">' + json.dumps(hydration) + '</script>'
        for alias in (asin, compact):
            self.assertEqual(parse_prime(content, alias, 2)["_catalog_id"], gti)
        with self.assertRaises(SourceError):
            parse_prime(content, "B112345678", 2)

    def test_image_scope_hash_dimensions_and_path_are_enforced(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            (docs / "images").mkdir()
            path = docs / "images/test.jpg"
            Image.new("RGB", (960, 540)).save(path)
            poster = {"path": "images/test.jpg", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "width": 960, "height": 540, "season_number": 2}
            item = record()
            item["seasons"] = [{"season": 2, "poster": poster, "poster_scope": "season"}]
            cat = {"schema": "prime-video-cn-catalog/1", "item_count": 1, "items": [item]}
            self.assertEqual(validate(cat, docs), [])
            for key, wrong in (("path", "../outside.jpg"), ("sha256", "0" * 64), ("height", 960), ("season_number", 1)):
                changed = copy.deepcopy(cat)
                changed["items"][0]["seasons"][0]["poster"][key] = wrong
                self.assertTrue(validate(changed, docs))

    def test_translation_called_with_same_season_source_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Clients(Path(tmp), print)
            source = {"type": "tmdb", "url": "https://www.themoviedb.org/tv/1/season/2"}
            with patch.object(client, "translate", return_value=("第二季译文", {"provider": "test"})) as translator:
                out = client.localized_summary("Season two synopsis", source, {}, "作品")
                translator.assert_called_once()
                self.assertEqual(out["synopsis_original"], "Season two synopsis")
                self.assertEqual(out["synopsis_source_type"], "machine_translation")
                self.assertEqual(out["synopsis_source_url"], source["url"])
            with patch.object(client, "translate", return_value=None):
                self.assertEqual(client.localized_summary("English", source, {}, "作品")["synopsis_status"], "translation_pending")

    def test_hash_release_and_corruption_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            version = publish(docs, [record()], {"last_success_at": "2026-10-29T00:00:00+00:00"}, reason="repair")
            raw = (docs / "catalog.json").read_bytes()
            self.assertEqual(version["last_success_at"], "2026-10-29T00:00:00+00:00")
            self.assertEqual(validate(json.loads(raw), docs, version, raw), [])
            self.assertIn("catalogue hash mismatch", validate(json.loads(raw), docs, version, raw + b" "))
            (docs / version["catalog_path"]).write_bytes(b"changed")
            self.assertIn("immutable release mismatch", validate(json.loads(raw), docs, version, raw))


class BatchTests(unittest.TestCase):
    def setup_repo(self, root):
        root = Path(root)
        write_json(root / "docs/catalog.json", {"items": [record(n) for n in range(156)]})
        write_json(root / "docs/version.json", {"last_success_at": "2020-01-01T00:00:00+00:00"})
        (root / "config").mkdir()
        (root / "config/seeds.yaml").write_text("seeds: []\n", encoding="utf-8")
        return root

    def client(self, fail=None):
        class Fake:
            seen = []
            def __init__(self, docs, log):
                self.__class__.seen = []
            def prime(self, asin, season):
                self.seen.append(asin)
                if fail == "all" or asin == fail:
                    raise SourceError("test: HTTP 429")
                return {"seasonNumber": 1}
            def prime_patch(self, asin, data, previous):
                return {"details": {"checked": True}}
            def tmdb(self, record):
                return None
        return Fake

    def test_full_batch_processes_156_not_only_seven_and_publishes(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = self.setup_repo(tmp)
            fake = self.client()
            self.assertEqual(run(root, ["--force", "--no-discovery"], fake), 0)
            self.assertEqual(len(fake.seen), 156)
            report = json.loads((root / "logs/last_run.json").read_bytes())
            self.assertEqual((report["processed"], report["checked"], report["failures"]), (156, 156, 0))

    def test_total_or_partial_network_failure_preserves_published_bytes_and_retries(self):
        for fail in ("all",):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                root = self.setup_repo(tmp)
                old = {p.name: p.read_bytes() for p in (root / "docs").iterdir()}
                fake = self.client(fail)
                self.assertEqual(run(root, ["--force", "--no-discovery"], fake), 2)
                self.assertEqual({p.name: p.read_bytes() for p in (root / "docs").iterdir()}, old)
                self.assertEqual(len(fake.seen), 156)
                self.assertTrue(due(json.loads(old["version.json"])))
                self.assertEqual(json.loads((root / "logs/last_attempt.json").read_bytes())["status"], "failed")

    def test_partial_batch_retains_failed_record_and_does_not_advance_full_success(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root=self.setup_repo(tmp);before=json.loads((root/'docs/catalog.json').read_bytes())
            fake=self.client('B000000078')
            self.assertEqual(run(root,['--force','--no-discovery'],fake),0)
            version=json.loads((root/'docs/version.json').read_bytes());after=json.loads((root/'docs/catalog.json').read_bytes())
            self.assertEqual(version['refresh_status'],'partial')
            self.assertEqual(version['last_success_at'],'2020-01-01T00:00:00+00:00')
            self.assertIn('last_partial_at',version)
            self.assertEqual(len(after['items']),156)
            old=next(x for x in before['items'] if x['prime_id']=='B000000078')
            failed=next(x for x in after['items'] if x['prime_id']=='B000000078')
            for key,value in old.items():self.assertEqual(failed.get(key),value)
            report=json.loads((root/'logs/last_run.json').read_bytes())
            self.assertEqual((report['checked'],report['failures'],report['status']),(155,1,'partial'))

    def test_successful_records_wait_seven_days_failed_records_retry_after_six_hours(self):
        clock=datetime(2026,10,30,12,tzinfo=timezone.utc)
        good={'refresh_status':'checked','metadata_checked_at':'2026-10-29T12:00:00+00:00'}
        bad={'refresh_status':'failed','metadata_attempted_at':'2026-10-30T05:00:00+00:00'}
        self.assertFalse(record_due(good,604800,clock));self.assertTrue(record_due(bad,604800,clock))
        self.assertTrue(due({'refresh_status':'partial','last_attempt_at':bad['metadata_attempted_at'],'last_success_at':'2026-10-29T12:00:00+00:00'},clock=clock))

    def test_time_budget_publishes_checked_results_retains_deferred_and_resumes_oldest(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = self.setup_repo(tmp)
            original = json.loads((root/'docs/catalog.json').read_bytes())['items']
            # Already attempted records go after previously unvisited records.
            original[0]['metadata_attempted_at'] = '2026-10-30T00:00:00+00:00'
            write_json(root/'docs/catalog.json', {'items': original})
            clock = [0.0]
            base = self.client()
            class Bounded(base):
                def prime_patch(self, asin, data, previous):
                    clock[0] += 61
                    return super().prime_patch(asin, data, previous)
            with patch.dict('os.environ', {'SYNC_BUDGET_SECONDS': '60'}), patch('sync.time.monotonic', side_effect=lambda: clock[0]):
                self.assertEqual(run(root, ['--force', '--no-discovery'], Bounded), 0)
            self.assertEqual(Bounded.seen, ['B000000001'])
            after = json.loads((root/'docs/catalog.json').read_bytes())['items']
            self.assertEqual([item['id'] for item in after], [item['id'] for item in original])
            self.assertEqual(after[0], original[0])
            self.assertEqual(after[2:], original[2:])
            report = json.loads((root/'logs/last_run.json').read_bytes())
            self.assertEqual((report['checked'], report['deferred'], report['attempted'], report['status']), (1, 155, 1, 'partial'))
            version = json.loads((root/'docs/version.json').read_bytes())
            self.assertEqual(version['last_success_at'], '2020-01-01T00:00:00+00:00')
            self.assertNotIn('last_full_success_at', version)

    def test_not_due_has_no_file_writes_or_network(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = self.setup_repo(tmp)
            write_json(root / "docs/version.json", {"last_success_at": datetime.now(timezone.utc).isoformat()})
            old = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with patch("sync.Clients", side_effect=AssertionError("network forbidden")):
                self.assertEqual(run(root, ["--due"], lambda *a: self.fail("network called")), 0)
            self.assertEqual({p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}, old)


if __name__ == "__main__":
    unittest.main()
