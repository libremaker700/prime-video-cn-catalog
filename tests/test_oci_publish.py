import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from catalog import publish
from publish_oci import Mirror, PublishError, check_urls, checked_files

PUBLIC = "https://objectstorage.us-phoenix-1.oraclecloud.com/n/testnamespace/b/testbucket/o/catalog/"
UPLOAD = "https://objectstorage.us-phoenix-1.oraclecloud.com/p/test-only-token/n/testnamespace/b/testbucket/o/catalog/"


class Response:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content
    def json(self):
        return json.loads(self.content)


class Objects:
    def __init__(self, fail=None):
        self.data, self.puts, self.fail = {}, [], fail
    def get(self, url, **kwargs):
        relative = url[len(PUBLIC):]
        return Response(200, self.data[relative]) if relative in self.data else Response(404)
    def put(self, url, data, headers, **kwargs):
        relative = url[len(UPLOAD):]
        self.puts.append(relative)
        if relative == self.fail:
            return Response(503)
        self.data[relative] = data
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

    def test_only_validated_public_assets_and_version_last(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects()
            Mirror(docs, UPLOAD, PUBLIC, store).publish()
            self.assertEqual(store.puts[-1], "version.json")
            self.assertEqual(len(store.puts), 4)
            self.assertFalse(any(x.endswith((".keystore", ".apk")) or "private" in x for x in store.puts))

    def test_failed_asset_upload_preserves_existing_remote_version(self):
        with tempfile.TemporaryDirectory() as tmp, patch("publish_oci.time.sleep"):
            docs = Path(tmp)
            self.fixture(docs)
            store = Objects(fail="images/test.jpg")
            store.data["version.json"] = b'{"version":"previous"}'
            with self.assertRaises(PublishError):
                Mirror(docs, UPLOAD, PUBLIC, store).publish(force=True)
            self.assertNotIn("version.json", store.puts)
            self.assertEqual(store.data["version.json"], b'{"version":"previous"}')

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
