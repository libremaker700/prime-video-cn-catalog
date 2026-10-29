#!/usr/bin/env python3
"""发布校验：JSON/图片一致性、海报横屏与哈希、季度简介防串季、范围(scope)纪律。"""
import hashlib, json, sys
from pathlib import Path
from PIL import Image

DOCS = Path(__file__).resolve().parent.parent / "docs"
fails, notes = [], []
cat = json.loads((DOCS / "catalog.json").read_text(encoding="utf-8"))
ver = json.loads((DOCS / "version.json").read_text(encoding="utf-8"))
if ver["item_count"] != len(cat["items"]):
    fails.append("version.item_count 与 catalog 条目数不一致")

def check_poster(owner, poster):
    p = DOCS / poster["path"]
    if not p.exists(): fails.append(f"{owner}: 海报文件缺失 {poster['path']}"); return
    im = Image.open(p); im.load()
    r = im.size[0] / im.size[1]
    if abs(r - 16 / 9) > 0.03: fails.append(f"{owner}: 海报比例 {r:.3f} 超出16:9容差")
    if im.size[0] < 960: notes.append(f"{owner}: 海报宽 {im.size[0]} <960（早期种子图）")
    if hashlib.sha256(p.read_bytes()).hexdigest() != poster["sha256"]:
        fails.append(f"{owner}: 海报哈希与记录不一致")

seen_syn = {}
n_post = n_season_post = 0
for it in cat["items"]:
    if it.get("poster"):
        n_post += 1
        if it.get("poster_scope") == "season":
            fails.append(f'{it["slug"]}: 分季图被标成整剧海报')
        check_poster(it["slug"], it["poster"])
    for s in it.get("seasons", []):
        owner = f'{it["slug"]} S{s["season"]}'
        if s.get("poster"):
            n_season_post += 1
            if s.get("poster_scope") != "season":
                fails.append(f"{owner}: 分季海报 scope 未标 season")
            check_poster(owner, s["poster"])
        t = (s.get("synopsis_zh") or "").strip()
        if t:
            if t in seen_syn and seen_syn[t] != (it["slug"], s["season"]):
                fails.append(f'{owner}: 简介与 {seen_syn[t]} 重复，疑似串季')
            seen_syn[t] = (it["slug"], s["season"])
print(f'条目 {len(cat["items"])}｜整剧海报 {n_post}｜分季海报 {n_season_post}｜'
      f'校验 {"失败: " + "; ".join(fails) if fails else "全部通过"}')
for n in notes: print("note:", n)
sys.exit(1 if fails else 0)
