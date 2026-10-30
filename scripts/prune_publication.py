#!/usr/bin/env python3
"""Keep the active/previous immutable releases and every image they reference."""
import json
import re
from pathlib import Path
from catalog import poster_refs,validate

ROOT=Path(__file__).resolve().parents[1]


def prune(docs):
    docs=docs.resolve()
    version=json.loads((docs/'version.json').read_text(encoding='utf-8'))
    raw=(docs/'catalog.json').read_bytes();current=json.loads(raw)
    if validate(current,docs,version,raw):raise ValueError('cannot prune an invalid publication')
    paths={version['catalog_path']}
    previous=version.get('previous_catalog_path')
    if previous:
        if not re.fullmatch(r'releases/[a-f0-9]{64}/catalog\.json',previous):raise ValueError('invalid previous release path')
        if (docs/previous).is_file():paths.add(previous)
    images=set()
    for rel in paths:
        catalog=json.loads((docs/rel).read_text(encoding='utf-8'))
        images.update(p['path'] for _,_,p,_ in poster_refs(catalog['items']))
    removed=[]
    for file in list((docs/'releases').glob('*/catalog.json'))+list((docs/'images').glob('*')):
        resolved=file.resolve()
        if not resolved.is_relative_to(docs) or file.is_symlink():raise ValueError('unexpected publication target')
        rel=file.relative_to(docs).as_posix()
        candidate=bool(re.fullmatch(r'releases/[a-f0-9]{64}/catalog\.json',rel) or re.fullmatch(r'images/[a-f0-9]{64}\.(jpg|png)',rel))
        if candidate and rel not in paths and rel not in images:
            file.unlink();removed.append(rel)
    for directory in (docs/'releases').iterdir():
        if directory.is_dir() and not directory.is_symlink() and directory.resolve().is_relative_to(docs) and not any(directory.iterdir()):directory.rmdir()
    return removed


if __name__=='__main__':
    print(json.dumps({'removed':prune(ROOT/'docs')},ensure_ascii=False))
