import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from catalog import publish
from publish_oci import Mirror, PublishError, check_urls, checked_files

PUBLIC = "https://objectstorage.us-phoenix-1.oraclecloud.com/n/testnamespace/b/testbucket/o/catalog/"
UPLOAD = "https://objectstorage.us-phoenix-1.oraclecloud.com/p/test-only-token/n/testnamespace/b/testbucket/o/catalog/"


class Response:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content
        self.headers = {"ETag": hashlib.sha256(content).hexdigest()} if status == 200 else {}
    def json(self):
        return json.loads(self.content)


class Objects:
    def __init__(self, fail=None):
        self.data, self.puts, self.fail = {}, [], fail
        self.concurrent_manifest = None
        self.lost_manifest_response = False
    def get(self, url, **kwargs):
        relative = url[len(PUBLIC):]
        return Response(200, self.data[relative]) if relative in self.data else Response(404)
    def put(self, url, data, headers, **kwargs):
        relative = url[len(UPLOAD):]
        self.puts.append(relative)
        if relative == self.fail:
            return Response(503)
        if self.concurrent_manifest and relative == "version.json":
            self.data[relative] = self.concurrent_manifest
            self.concurrent_manifest = None
        existing = self.data.get(relative)
        if ((existing is not None and headers.get("If-None-Match") == "*")
                or ("If-Match" in headers and (existing is None
                    or hashlib.sha256(existing).hexdigest() != headers["If-Match"]))):
            return Response(412)
        self.data[relative] = data
        if relative == "version.json" and self.lost_manifest_response:
            self.lost_manifest_response = False
            raise requests.Timeout()
        return Response(200)


class PublisherTests(unittest.TestCase):
    def fixture(self, docs):
        (docs / "images").mkdir()
        image = docs / "images/test.jpg"
        Image.new("RGB", (960, 540)).save(image)
        record = {"id": "test", "slug": "test", "kind": "movie", "identity_status": "pending_review", "display_name_zh": "测试",
                  "poster_scope": "series", "poster": {"path": "images/test.jpg", "sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "width": 960, "height": 540}}
        publish(docs, [record], {})
        # These must never be selected for upload, even if present beside the public JSON.
        for name in ("preview.keystore", "provider-private.json", "app.apk"):
            (docs / name).write_text("must stay local")

    def revised_catalog(self, docs, name="修正"):
        previous = json.loads((docs / "version.json").read_bytes())
        items = json.loads((docs / "catalog.json").read_bytes())["items"]
        return publish(docs, [{**items[0], "display_name_zh": name}], previous)

    def test_only_validated_public_assets_before_manifest_then_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            Mirror(docs, UPLOAD, PUBLIC, store).publish()
            self.assertEqual(store.puts[-2:], ["version.json", "catalog.json"])
            self.assertEqual(len(store.puts), 4)
            self.assertFalse(any(x.endswith((".keystore", ".apk")) or "private" in x for x in store.puts))

    def test_failed_asset_upload_preserves_existing_remote_version(self):
        with tempfile.TemporaryDirectory() as tmp, patch("publish_oci.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            mirror = Mirror(docs, UPLOAD, PUBLIC, store)
            mirror.publish()
            previous = store.data["version.json"]
            self.revised_catalog(docs)
            store.puts.clear()
            store.fail = "images/test.jpg"
            with self.assertRaises(PublishError):
                mirror.publish(force=True)
            self.assertNotIn("version.json", store.puts)
            self.assertEqual(store.data["version.json"], previous)

    def test_repeated_publication_skips_uploads_after_hash_verification(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            publisher = Mirror(docs, UPLOAD, PUBLIC, store)
            publisher.publish()
            store.puts.clear()
            publisher.publish()
            self.assertEqual(store.puts, [])

    def test_corrupt_rollback_prevents_publication_and_new_clean_rollback_is_uploaded(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            previous = json.loads((docs / "version.json").read_bytes())
            items = json.loads((docs / "catalog.json").read_bytes())["items"]
            publish(docs, [{**items[0], "display_name_zh": "修正"}], previous)
            store = Objects()
            store.data[previous["catalog_path"]] = b"damaged"
            with self.assertRaises(PublishError):
                Mirror(docs, UPLOAD, PUBLIC, store).publish()
            self.assertEqual(store.puts, [])
            store = Objects()
            Mirror(docs, UPLOAD, PUBLIC, store).publish()
            self.assertEqual(store.data[previous["catalog_path"]], (docs / previous["catalog_path"]).read_bytes())
            self.assertEqual(store.puts[-2:], ["version.json", "catalog.json"])

    def test_conditional_write_preserves_a_concurrent_version_and_repairs_its_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            mirror = Mirror(docs, UPLOAD, PUBLIC, store)
            mirror.publish()
            winner = self.revised_catalog(docs, "另一任务发布")
            winner_manifest = (docs / "version.json").read_bytes()
            winner_catalog = (docs / "catalog.json").read_bytes()
            store.data[winner["catalog_path"]] = winner_catalog
            self.revised_catalog(docs)
            store.concurrent_manifest = winner_manifest
            with self.assertRaises(PublishError):
                mirror.publish()
            self.assertEqual(store.data["version.json"], winner_manifest)
            self.assertEqual(store.data["catalog.json"], winner_catalog)

    def test_manifest_failure_does_not_publish_uncommitted_compatibility_catalog(self):
        with tempfile.TemporaryDirectory() as tmp, patch("publish_oci.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            mirror = Mirror(docs, UPLOAD, PUBLIC, store)
            mirror.publish()
            before = {name: store.data[name] for name in ("version.json", "catalog.json")}
            self.revised_catalog(docs)
            store.fail = "version.json"
            store.puts.clear()
            with self.assertRaises(PublishError):
                mirror.publish()
            self.assertNotIn("catalog.json", store.puts)
            self.assertEqual({name: store.data[name] for name in before}, before)

    def test_retry_repairs_compatibility_from_a_previous_successful_manifest_write(self):
        with tempfile.TemporaryDirectory() as tmp, patch("publish_oci.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            mirror = Mirror(docs, UPLOAD, PUBLIC, store)
            mirror.publish()
            old_legacy = store.data["catalog.json"]
            self.revised_catalog(docs)
            store.fail = "catalog.json"
            with self.assertRaisesRegex(PublishError, "compatibility recovery did not finish"):
                mirror.publish()
            self.assertEqual(store.data["version.json"], (docs / "version.json").read_bytes())
            self.assertEqual(store.data["catalog.json"], old_legacy)
            store.fail = None
            store.puts.clear()
            mirror.publish()
            self.assertEqual(store.puts, ["catalog.json"])
            self.assertEqual(store.data["catalog.json"], (docs / "catalog.json").read_bytes())

    def test_recovers_legacy_drift_even_when_next_manifest_attempt_fails(self):
        with tempfile.TemporaryDirectory() as tmp, patch("publish_oci.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            mirror = Mirror(docs, UPLOAD, PUBLIC, store)
            mirror.publish()
            old_legacy = store.data["catalog.json"]
            self.revised_catalog(docs)
            store.data["catalog.json"] = (docs / "catalog.json").read_bytes()
            store.fail = "version.json"
            with self.assertRaises(PublishError):
                mirror.publish()
            self.assertEqual(store.data["catalog.json"], old_legacy)

    def test_lost_manifest_response_does_not_restore_the_old_catalog(self):
        with tempfile.TemporaryDirectory() as tmp, patch("publish_oci.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            mirror = Mirror(docs, UPLOAD, PUBLIC, store)
            mirror.publish()
            self.revised_catalog(docs)
            store.lost_manifest_response = True
            mirror.publish()
            self.assertEqual(store.data["version.json"], (docs / "version.json").read_bytes())
            self.assertEqual(store.data["catalog.json"], (docs / "catalog.json").read_bytes())

    def test_scope_and_transport_are_restricted(self):
        check_urls(UPLOAD, PUBLIC)
        for bad in (UPLOAD.replace("https:", "http:"), UPLOAD.replace("oraclecloud.com", "example.com"), UPLOAD.replace("/o/catalog/", "/o/"), UPLOAD + "?secret=not-allowed"):
            with self.subTest(url=bad), self.assertRaises(PublishError):
                check_urls(bad, PUBLIC)

    def test_invalid_local_hash_aborts_before_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            self.fixture(docs)
            (docs / "catalog.json").write_text("{}")
            with self.assertRaises(PublishError):
                checked_files(docs)


if __name__ == "__main__":
    unittest.main()
