#!/usr/bin/env python3
"""
将 App 现有资料快照（bootstrap-catalog.json）导入本仓库目录结构。

纪律（与 README 一致）：
- 154 条全部导入，与既有 catalog 按 官方ID > 原名+类型 身份去重，绝不以少覆多。
- 33 条 identityStatus=pending_official_identity_review 的条目保持待核验，不编造 ID。
- seedYear 只是 App 当前收录季度年份，单独存 details.seed_year 并标注 scope；
  firstAirYear 有值才用，无值留空，不用 seedYear 冒充首播年份。
- officialMetadata 只对应其 seasonNumber 那一季；scope=season 的图片只挂到该季，
  scope=movie_or_series 的图片才作整剧/电影海报。分季简介只进对应季度。
- 图片逐张 Pillow 解码验真：宽>=960 且比例在 16:9 像素舍入容差内才入库，哈希与文件名核对。
- title-overrides.json 的用户偏好名优先作 display_name_zh，其余地区译名进 aliases_zh。
- TMDB 等外部来源的图片/文本一律按其真实来源标注；快照来源标 app_snapshot，
  provenance 原样保留，不冒充本次官网重新抓取。
"""
import hashlib, json, re, shutil, sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/bootstrap_audit")
DOCS, IMAGES = ROOT / "docs", ROOT / "docs" / "images"
NOW = "2026-10-29T00:00:00+08:00"  # 快照导出日（Asia/Shanghai），非本次抓取时间

def norm(s): return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
def clean_title(t):
    return (t or "").strip().strip("《》").strip()

def verify_image(src_path):
    data = src_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    im = Image.open(src_path); im.load()
    w, h = im.size; ratio = w / h if h else 0
    ok = (sha == src_path.stem and w >= 960 and abs(ratio - 16 / 9) <= 0.03)
    return ok, {"path": f"images/{src_path.name}", "width": w, "height": h, "sha256": sha}

def artwork_record(art, src_dir):
    if not art: return None, "missing"
    p = src_dir / art["path"]
    if not p.exists(): return None, "missing_file"
    ok, info = verify_image(p)
    if not ok: return None, "rejected_invalid"
    IMAGES.mkdir(parents=True, exist_ok=True)
    dst = IMAGES / p.name
    if not dst.exists(): shutil.copyfile(p, dst)
    info.update({"scope": art.get("scope"), "season_number": art.get("seasonNumber"),
                 "source_url": art.get("sourceUrl"), "provenance": art.get("provenance"),
                 "source_type": "app_snapshot_official_mapping"})
    return info, "ok"

def main():
    boot = json.loads((ROOT / "data/bootstrap-catalog.json").read_text(encoding="utf-8"))
    overrides = {norm(r["originalTitle"]): r for r in
                 json.loads((ROOT / "data/title-overrides.json").read_text(encoding="utf-8"))["rules"]}
    prev = {}
    cat_path = DOCS / "catalog.json"
    if cat_path.exists():
        for it in json.loads(cat_path.read_text(encoding="utf-8")).get("items", []):
            prev[norm(it.get("original_title"))] = it
    items, stats = [], {"total": 0, "official_mapped": 0, "pending_identity": 0,
                        "seasons": 0, "season_overviews": 0, "images_ok": 0, "images_rejected": 0,
                        "series_posters": 0, "season_posters": 0, "wiki_merged": 0, "wiki_only": 0}
    seen_ids = set()
    for b in boot["items"]:
        stats["total"] += 1
        om = b.get("officialMetadata") or {}
        genres = om.get("genres") or ""
        kind = "anime" if ("动画" in genres or "動畫" in genres) else b["mediaType"]
        official_id = om.get("officialId")
        identity = "verified_official_mapping" if b["identityStatus"] == "existing_official_mapping" else "pending_review"
        if identity == "verified_official_mapping": stats["official_mapped"] += 1
        else: stats["pending_identity"] += 1
        rec_id = f"prime:{official_id}" if official_id else f"seed:{b['seedId']}"
        if rec_id in seen_ids:  # 同一官方ID重复种子只保留一条，记日志不静默
            print(f"dup official id skipped: {rec_id} {b['originalTitle']}"); continue
        seen_ids.add(rec_id)
        orig = b.get("originalTitle") or None  # 部分条目快照只有中文名，原名留空不编造
        ov = overrides.get(norm(orig)) if orig else None
        display = (ov or {}).get("preferredTitleZh") or b.get("preferredTitleZh") or orig or clean_title(om.get("officialTitleZh"))
        aliases = list((ov or {}).get("aliasesZh", []))
        slug = re.sub(r"[^a-z0-9]+", "-", orig.lower()).strip("-") if orig else f'title-{b["seedId"].lower()}'
        rec = {"id": rec_id, "slug": slug,
               "kind": kind, "identity_status": identity,
               "prime_id": official_id, "seed_id": b["seedId"],
               "original_title": orig, "english_title": orig,
               "display_name_zh": display,
               "name_status": "user_app_preference" if ov else "existing_app_curated_not_fresh_web_verified",
               "prime_official_name_zh": clean_title(om.get("officialTitleZh")) or None,
               "aliases_zh": aliases, "synopsis_zh": "", "synopsis_source_type": None,
               "details": {"first_air_year": b.get("firstAirYear"),
                           "seed_year": b.get("seedYear"), "seed_year_scope": b.get("seedYearScope"),
                           "release_date": om.get("releaseDate"), "genres": genres or None,
                           "official_type": om.get("officialType")},
               "poster": None, "poster_scope": None, "poster_status": "missing",
               "seasons": [], "sources": [], "updated_at": NOW,
               "snapshot_status": "existing_app_snapshot_not_redownloaded"}
        if om.get("officialUrl"):
            rec["sources"].append({"type": "app_snapshot", "name": "Prime Video 官方页（App既有映射快照）",
                                   "url": om["officialUrl"], "retrieved_at": NOW})
        # 整剧/电影级图片与简介：仅 scope=movie_or_series 的 officialMetadata 属于整部作品
        if om.get("artwork") and om["artwork"].get("scope") == "movie_or_series":
            info, st = artwork_record(om["artwork"], SRC)
            if info: rec.update(poster=info, poster_scope="series", poster_status="ok"); stats["images_ok"] += 1; stats["series_posters"] += 1
            elif st.startswith("rejected"): stats["images_rejected"] += 1
            if om.get("overviewZh"):
                rec["synopsis_zh"] = om["overviewZh"]; rec["synopsis_source_type"] = "existing_snapshot_app"
        # 分季条目
        season_map = {}
        for s in b.get("seasons", []):
            m = s.get("metadata") or {}
            srec = {"season": s["number"], "id": f'{rec_id}:s{s["number"]}',
                    "official_id": m.get("officialId"), "official_title_zh": clean_title(m.get("officialTitleZh")) or None,
                    "synopsis_zh": m.get("overviewZh") or "",
                    "synopsis_source_type": "existing_snapshot_app" if m.get("overviewZh") else None,
                    "synopsis_status": "ok" if m.get("overviewZh") else "missing",
                    "poster": None, "poster_scope": None, "poster_status": "missing",
                    "year": m.get("year"), "release_date": m.get("releaseDate")}
            if srec["synopsis_zh"]: stats["season_overviews"] += 1
            if m.get("artwork"):
                info, st = artwork_record(m["artwork"], SRC)
                if info:
                    info["scope"] = "season"; srec.update(poster=info, poster_scope="season", poster_status="ok")
                    stats["images_ok"] += 1; stats["season_posters"] += 1
                elif st.startswith("rejected"): stats["images_rejected"] += 1
            season_map[s["number"]] = srec; stats["seasons"] += 1
        # officialMetadata 若是某一季且 seasons[] 没有该季，补建该季条目（不并入其他季）
        sn = om.get("seasonNumber")
        if sn and om.get("artwork") and om["artwork"].get("scope") == "season" and sn not in season_map:
            srec = {"season": sn, "id": f"{rec_id}:s{sn}", "official_id": official_id,
                    "official_title_zh": clean_title(om.get("officialTitleZh")) or None,
                    "synopsis_zh": om.get("overviewZh") or "",
                    "synopsis_source_type": "existing_snapshot_app" if om.get("overviewZh") else None,
                    "synopsis_status": "ok" if om.get("overviewZh") else "missing",
                    "poster": None, "poster_scope": None, "poster_status": "missing",
                    "year": om.get("year"), "release_date": om.get("releaseDate")}
            if srec["synopsis_zh"]: stats["season_overviews"] += 1
            info, st = artwork_record(om["artwork"], SRC)
            if info:
                info["scope"] = "season"; srec.update(poster=info, poster_scope="season", poster_status="ok")
                stats["images_ok"] += 1; stats["season_posters"] += 1
            elif st.startswith("rejected"): stats["images_rejected"] += 1
            season_map[sn] = srec; stats["seasons"] += 1
        rec["seasons"] = [season_map[k] for k in sorted(season_map)]
        # 与既有维基核验条目合并（同原名）：补全剧简介与别名，不覆盖 App 身份与偏好名
        w = prev.pop(norm(orig), None) if orig else None
        if w and w.get("name_status") == "verified":
            stats["wiki_merged"] += 1
            if not rec["synopsis_zh"] and w.get("synopsis_zh"):
                rec["synopsis_zh"] = w["synopsis_zh"]; rec["synopsis_source_type"] = "existing_chinese_wikipedia"
                rec["sources"].extend(w.get("sources", []))
            wn = w.get("display_name_zh")
            if wn and wn != rec["display_name_zh"] and wn not in rec["aliases_zh"]:
                rec["aliases_zh"].append(wn)
        items.append(rec)
    # 既有目录中未被快照覆盖的条目（如 Vinland Saga）保留，不丢失
    for w in prev.values():
        stats["wiki_only"] += 1; items.append(w)
    catalog = {"schema": "prime-video-cn-catalog/1", "generated_at": NOW,
               "source_note": "首版 = App既有资料快照导入（非本次官网重抓）+ 中文维基核验补充",
               "item_count": len(items), "items": items}
    blob = json.dumps(catalog, ensure_ascii=False, indent=2)
    (DOCS / "catalog.json").write_text(blob, encoding="utf-8")
    version = {"version": hashlib.sha256(blob.encode()).hexdigest()[:16],
               "catalog_sha256": hashlib.sha256(blob.encode()).hexdigest(),
               "generated_at": NOW, "last_success_at": NOW,
               "item_count": len(items), "image_count": stats["images_ok"], "stats": stats}
    (DOCS / "version.json").write_text(json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "data/fallback_catalog.json").write_text(blob, encoding="utf-8")
    # 供后续“已有种子更新”使用的种子清单（与新作品发现分开统计）
    seed_list = [{"original_title": i.get("original_title"), "kind": i.get("kind"),
                  "prime_id": i.get("prime_id"), "identity_status": i.get("identity_status", "legacy_wiki_seed"),
                  "first_air_year": (i.get("details") or {}).get("first_air_year")} for i in items]
    (ROOT / "data/seeds_from_bootstrap.json").write_text(
        json.dumps(seed_list, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
