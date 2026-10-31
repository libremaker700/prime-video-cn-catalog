#!/usr/bin/env python3
"""Validate publication hash, release, image bytes/scopes and stable identities."""
import json
import sys
from pathlib import Path

from catalog import validate
from publish_source_verification import validate_publication

DOCS = Path(__file__).resolve().parent.parent / "docs"


def main():
    raw = (DOCS / "catalog.json").read_bytes()
    catalog = json.loads(raw)
    version = json.loads((DOCS / "version.json").read_bytes())
    failures = validate(catalog, DOCS, version, raw)
    try:
        validate_publication(DOCS)
    except (ValueError, OSError) as error:
        failures.append('source verification publication: ' + str(error))
    for failure in failures:
        print(f"ERROR: {failure}")
    print(f'{len(catalog["items"])} titles; {version["image_count"]} unique landscape images; validation {"FAILED" if failures else "passed"}.')
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
