#!/usr/bin/env python3
"""Align public App identities and fields; never claim a fresh web crawl."""
import json
from pathlib import Path
from app_metadata import apply_app_metadata
from catalog import publish

ROOT = Path(__file__).resolve().parents[1]


def run(root=ROOT):
    docs = root / 'docs'
    current = json.loads((docs / 'catalog.json').read_text(encoding='utf-8'))
    previous = json.loads((docs / 'version.json').read_text(encoding='utf-8'))
    asset = json.loads((root / 'data/app-metadata.json').read_text(encoding='utf-8'))
    items = apply_app_metadata(current['items'], asset)
    if items == current['items']:
        print('App mappings already aligned; no files changed.')
        return
    version = publish(docs, items, previous, reason='app_metadata_alignment')
    print(f"Aligned {len(items)} records; version={version['version']}; crawl success time unchanged.")


if __name__ == '__main__':
    run()
