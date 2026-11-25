#!/usr/bin/env python3
"""Publish only validated public catalogue assets to a scoped OCI upload URL."""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from catalog import poster_refs, validate
from catalog_exclusions import read_registry, require_filtered

DOCS = Path(__file__).resolve().parents[1] / "docs"


class PublishError(RuntimeError):
    pass


def checked_files(docs):
    raw = (docs / "catalog.json").read_bytes()
    catalog = json.loads(raw)
    version = json.loads((docs / "version.json").read_bytes())
    errors = validate(catalog, docs, version, raw)
    if errors:
        raise PublishError("local publication validation failed")
    registry = read_registry()
    require_filtered(catalog, registry)
    images = {p["path"] for _, _, p, _ in poster_refs(catalog["items"])}
    releases = [safe_release(version["catalog_path"])]
    if rollback := version.get("previous_catalog_path"):
        previous = verified_release(rollback, (docs / safe_release(rollback)).read_bytes())
        require_filtered(previous, registry)
        if validate(previous, docs):
            raise PublishError("Local rollback validation failed")
        images.update(p["path"] for _, _, p, _ in poster_refs(previous["items"]))
        releases.append(rollback)
    # No APK, keystore, provider configuration, logs or arbitrary repository files.
    return version, sorted(images) + releases


def safe_release(path):
    if not isinstance(path, str) or not re.fullmatch(r"releases/[a-f0-9]{64}/catalog\.json", path):
        raise PublishError("Unsafe catalog release path")
    return path


def verified_release(path, raw):
    if hashlib.sha256(raw).hexdigest() != safe_release(path).split("/")[1]:
        raise PublishError("Catalog release checksum mismatch")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("schema") != "prime-video-cn-catalog/1" or not isinstance(doc.get("items"), list):
        raise PublishError("Invalid catalog release")
    return doc


def check_urls(upload, public):
    u, p = urlsplit(upload), urlsplit(public)
    if any(x.scheme != "https" or x.username or x.password or x.query or x.fragment for x in (u, p)):
        raise PublishError("OCI URLs must be plain HTTPS endpoints")
    if u.hostname != p.hostname or not re.fullmatch(r"objectstorage\.[a-z0-9-]+\.oraclecloud\.com", u.hostname or ""):
        raise PublishError("upload and public endpoints must belong to the same OCI region")
    match = re.fullmatch(r"/p/[^/]+(/n/[a-zA-Z0-9]+/b/[a-zA-Z0-9_.-]+/o/catalog/)", u.path)
    if not match or p.path != match.group(1):
        raise PublishError("upload credential must be restricted to this catalogue prefix")


class Mirror:
    def __init__(self, docs, upload, public, session=None):
        check_urls(upload, public)
        self.docs, self.upload, self.public = docs, upload, public
        # Independent requests use requests' connection handling; no credential-bearing URL is logged.
        self.http = session or requests

    def get(self, relative):
        try:
            response = self.http.get(self.public + quote(relative, safe="/"), timeout=(8, 25), headers={"Cache-Control": "no-cache"}, allow_redirects=False)
        except requests.RequestException:
            raise PublishError("public OCI endpoint unavailable") from None
        return response

    def put(self, relative, *, before=None, require_absent=False):
        data = (self.docs / relative).read_bytes()
        immutable = relative.startswith(("images/", "releases/"))
        headers = {"Content-Type": "image/jpeg" if relative.endswith(".jpg") else "application/json; charset=utf-8",
                   "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache",
                   "Content-MD5": base64.b64encode(hashlib.md5(data).digest()).decode("ascii")}
        if immutable or require_absent:
            headers["If-None-Match"] = "*"
        elif before:
            headers["If-Match"] = before
        for attempt in range(3):
            try:
                response = self.http.put(self.upload + quote(relative, safe="/"), data=data, headers=headers, timeout=(8, 40), allow_redirects=False)
                if 200 <= response.status_code < 300:
                    return
                if response.status_code == 412:
                    check = self.get(relative)
                    if check.status_code == 200 and check.content == data:
                        return
                    raise PublishError("OCI object changed during publication")
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise PublishError(f"OCI upload rejected (HTTP {response.status_code})")
            except requests.RequestException:
                pass
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise PublishError("OCI upload failed after bounded retries; version pointer was not advanced")

    def replace(self, relative, current):
        if current.status_code == 404:
            self.put(relative, require_absent=True)
        elif current.status_code == 200 and (etag := current.headers.get("ETag")):
            self.put(relative, before=etag)
        else:
            raise PublishError("Current OCI object ETag is unavailable")

    def publish(self, force=False):
        version, paths = checked_files(self.docs)
        current = self.get("version.json")
        if current.status_code not in (200, 404) or current.status_code == 200 and not current.headers.get("ETag"):
            raise PublishError("Could not read the current OCI version and ETag")
        rollback_ready = True
        if rollback := version.get("previous_catalog_path"):
            old = self.get(rollback)
            if old.status_code == 404:
                rollback_ready = False
            elif old.status_code == 200:
                require_filtered(verified_release(rollback, old.content), read_registry())
            else:
                raise PublishError("Rollback object is unavailable")
        legacy = self.get("catalog.json")
        if legacy.status_code not in (200, 404) or legacy.status_code == 200 and not legacy.headers.get("ETag"):
            raise PublishError("Could not read the compatibility catalog and ETag")
        expected_version = (self.docs / "version.json").read_bytes()
        expected_catalog = (self.docs / "catalog.json").read_bytes()
        if (not force and rollback_ready and current.status_code == legacy.status_code == 200
                and current.content == expected_version and legacy.content == expected_catalog):
            existing = self.get(version["catalog_path"])
            if existing.status_code == 200 and existing.content == expected_catalog:
                print("OCI mirror already current; no uploads needed.")
                return
        # Upload and verify immutable assets before changing either public pointer.
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(self.put, paths))
        remote = self.get(version["catalog_path"])
        if remote.status_code != 200 or remote.content != expected_catalog:
            raise PublishError("remote catalogue hash mismatch; version pointer was not advanced")
        if rollback:
            old = self.get(rollback)
            if old.status_code != 200:
                raise PublishError("Rollback object is unavailable at readback")
            verified_release(rollback, old.content)
        observed = self.get("version.json")
        if observed.status_code != current.status_code or observed.headers.get("ETag") != current.headers.get("ETag"):
            raise PublishError("OCI version changed before compatibility publication")
        if legacy.content != expected_catalog:
            self.replace("catalog.json", legacy)
        if self.get("catalog.json").content != expected_catalog:
            raise PublishError("Compatibility catalog readback failed")
        self.replace("version.json", current)
        remote_version = self.get("version.json")
        if (remote_version.status_code != 200 or remote_version.content != expected_version
                or self.get("catalog.json").content != expected_catalog):
            raise PublishError("OCI version pointer verification failed")
        print(f'OCI mirror verified: {version["item_count"]} titles, {version["image_count"]} images, version={version["version"]}.')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()
    if args.validate_only:
        version, files = checked_files(DOCS)
        print(f'Public upload plan validated: {len(files) + 2} files, version={version["version"]}.')
        return 0
    if os.getenv("OCI_MIRROR_ENABLED", "false").lower() != "true":
        print("OCI mirror disabled; only the existing GitHub Pages publication is configured.")
        return 0
    upload = os.getenv("OCI_CATALOG_UPLOAD_URL", "")
    public = os.getenv("OCI_CATALOG_PUBLIC_URL", "")
    if not upload or not public:
        raise PublishError("OCI mirror enabled but its upload credential/public URL is missing")
    Mirror(DOCS, upload, public).publish(args.force)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PublishError, OSError, ValueError, KeyError, requests.RequestException) as exc:
        # Request exceptions include full URLs, which could reveal the upload credential.
        print("OCI mirror publication failed. Check configured scope, expiry, network and validated assets; upload credentials were not logged.")
        sys.exit(2)
