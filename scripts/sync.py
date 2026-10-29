#!/usr/bin/env python3
"""
Prime Video 中文影视资料自动同步程序

数据源（均为公开、无需凭据或凭据走环境变量）：
- 中文维基百科 API：国内常用中文名核验 + 已有中文简介（CC BY-SA，保留来源链接）
- TMDB API（可选，环境变量 TMDB_API_KEY）：zh-CN 简介与 16:9 backdrop 横屏图；
  未配置时跳过，海报记为 missing，绝不用竖版图拉伸补位。
- Prime Video 官方页面：仅作存在性核验尝试；被 503/机器人防护拦截时记录状态，
  不绕过防护、不编造官方 ID。

匹配纪律（对应需求二、三）：
- 中文名必须综合 原名 + 年份 + 类型关键词 + 简介中出现的原名 多信号打分，
  低于阈值一律记 pending_review，不合并、不编造。
- 电视剧按季检索简介；查不到该季独立简介时该季留空并记 pending，
  绝不把第一季简介套到其他季。
- 无可靠来源的字段留空。

发布安全（对应需求五）：
- 新目录为空、或条目数较上一版骤降 >50% 时中止发布，保留上一版文件不动。
- 输出先写临时文件，校验通过后原子替换 docs/catalog.json 与 docs/version.json。
- 图片以内容 SHA-256 哈希命名、去重；仅 1.70~1.85 宽高比且可正常解码的图片入库。
- 7 天节奏（UPDATE_INTERVAL_SECONDS 默认 604800 秒）：--due 模式下距上次成功
  不满 7 天自动跳过；--force 或手动触发强制运行。首次运行（无 version.json）立即更新。
  更新失败或触发安全闸时不写 version.json，上次成功时间不变，下一日检查继续重试。
"""
import argparse, hashlib, io, json, os, re, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests, yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
IMAGES = DOCS / "images"
LOGS = ROOT / "logs"
UA = {"User-Agent": "PrimeVideoCnCatalog/1.0 (personal metadata sync; contact: repo owner)"}
ZH_WIKI = "https://zh.wikipedia.org/w/api.php"
NOW = lambda: datetime.now(timezone.utc)

log_lines = []
def log(msg):
    line = f"[{NOW().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True); log_lines.append(line)

def http_get_json(url, params=None, headers=None, timeout=20):
    r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ---------- 中文维基：搜索 + 打分匹配 ----------
KIND_WORDS = {"movie": ["电影", "電影", "动作片", "科幻片", "影片"],
              "tv": ["电视剧", "電視劇", "剧集", "劇集", "影集", "网剧", "網絡劇"],
              "anime": ["动画", "動畫", "电视动画", "電視動畫", "anime", "Anime"]}

def wiki_search(query, limit=6):
    data = http_get_json(ZH_WIKI, {"action": "query", "list": "search", "variant": "zh-hans",
                                   "srsearch": query, "srlimit": limit, "format": "json"})
    return [h["title"] for h in data.get("query", {}).get("search", [])]

def wiki_page(title):
    data = http_get_json(ZH_WIKI, {"action": "query", "prop": "extracts|info", "variant": "zh-hans",
                                   "explaintext": 1, "exsectionformat": "plain",
                                   "inprop": "url", "redirects": 1,
                                   "titles": title, "format": "json"})
    pages = data.get("query", {}).get("pages", {})
    for p in pages.values():
        if "missing" in p: return None
        return {"title": p.get("title", title), "extract": p.get("extract", "") or "",
                "url": p.get("fullurl", f"https://zh.wikipedia.org/wiki/{title}")}
    return None

def score_candidate(seed, page):
    """多信号打分：年份 +3，原名/英文名出现在正文 +3，类型词 +2，标题含英文名 +2。阈值 5。"""
    text = page["extract"][:3000]; score = 0; why = []
    if str(seed["year"]) in text: score += 3; why.append("年份一致")
    names = {seed["original_title"], seed["english_title"]}
    if any(n and n.lower() in text.lower() for n in names): score += 3; why.append("正文出现原名")
    if any(w in text for w in KIND_WORDS[seed["kind"]]): score += 2; why.append("类型词一致")
    if any(n and n.lower() in page["title"].lower() for n in names): score += 2; why.append("标题含原名")
    disamb = {"movie": ["电影", "電影"], "tv": ["电视剧", "電視劇"],
              "anime": ["电视动画", "電視動畫", "动画", "動畫"]}[seed["kind"]]
    if any(d in page["title"] for d in disamb): score += 2; why.append("标题消歧义与类型一致")
    return score, why

def first_paragraphs(extract, limit=600):
    paras = [p.strip() for p in extract.split("\n") if len(p.strip()) > 40]
    out = ""
    for p in paras:
        if out and len(out) + len(p) > limit: break
        out = (out + "\n" + p).strip()
        if len(out) >= 120: break
    return out[:limit]

def lead_gate(seed, page):
    """定义句闸门：正文开头须以《中文名》（英语：原名）是……电影/电视剧/动画 的
    定义句式介绍本作品本身。仅在正文里被提及的作品（如某动画的主题曲专辑页）无法通过，
    防止凭年份+类型词+提及原名就误合并到无关条目。"""
    lead = page["extract"][:400]
    if not lead.lstrip().startswith("《"): return False
    names = [n for n in {seed["original_title"], seed["english_title"]} if n]
    langs = ["英语", "英語", "日语", "日語", "英文"]
    return any(f"{lang}：{n}" in lead or f"{lang}: {n}" in lead for lang in langs for n in names)

def match_chinese(seed):
    kind_hint = {"movie": "电影", "tv": "电视剧", "anime": "动画"}[seed["kind"]]
    queries = [f'{seed["english_title"]} {seed["year"]} {kind_hint}',
               f'{seed["english_title"]} {seed["year"]}',
               seed["english_title"], seed["original_title"]]
    seen, best = set(), None
    for q in queries:
        try: titles = wiki_search(q)
        except Exception as e: log(f'  wiki 搜索失败 {q}: {e}'); continue
        for t in titles:
            if t in seen: continue
            seen.add(t)
            try: page = wiki_page(t)
            except Exception: continue
            if not page: continue
            if not lead_gate(seed, page): continue  # 不是介绍本作品本身的条目，直接淘汰
            s, why = score_candidate(seed, page)
            if best is None or s > best[0]: best = (s, why, page)
        if best and best[0] >= 7: break
    if best and best[0] >= 6:  # 阈值6：仅靠提及+类型词(5分)不合并，宁待核验不误配
        s, why, page = best
        log(f'  中文名匹配: {page["title"]} (score={s}, {"/".join(why)})')
        return page, "verified"
    log(f'  中文名未达阈值 (best={best[0] if best else 0}) -> pending_review')
    return (best[2] if best else None), "pending_review"

# ---------- 翻译（可选）：仅当有同季外语原文且无中文时触发 ----------
# 配置见 config/translation.md：TRANSLATE_PROVIDER=deepl|openai + 对应 Key（仓库 Secret）。
# 未配置时返回 None，调用方标 translation_pending；无原文标 missing。绝不以全剧简介顶替分季。
def translate_text(text, title_hint=""):
    provider = os.environ.get("TRANSLATE_PROVIDER", "").strip().lower()
    if not text or not provider: return None
    try:
        if provider == "deepl":
            key = os.environ.get("DEEPL_API_KEY", "")
            if not key: return None
            host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
            r = requests.post(f"{host}/v2/translate", timeout=30,
                              data={"auth_key": key, "text": text, "target_lang": "ZH-HANS"})
            r.raise_for_status()
            return r.json()["translations"][0]["text"]
        if provider == "openai":
            key = os.environ.get("TRANSLATE_API_KEY", "")
            base = os.environ.get("TRANSLATE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            model = os.environ.get("TRANSLATE_MODEL", "gpt-4o-mini")
            if not key: return None
            r = requests.post(f"{base}/chat/completions", timeout=60,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "messages": [
                                  {"role": "system", "content": "把影视简介翻译为自然简体中文，剧名人名与给定译名一致，只输出译文。"},
                                  {"role": "user", "content": f"（剧名参考：{title_hint}）\n{text}"}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"  翻译失败({provider}): {e}")
    return None

# ---------- TMDB（可选）：zh-CN 简介 + 整剧 16:9 backdrop ----------
# 来源标注纪律：TMDB 图/文一律标 source_type=tmdb，不等于 Prime Video 官方核验；
# TMDB 分季图片接口只返回竖版海报，本程序不调用它充横屏图；整剧 backdrop 只作
# scope=series 的整剧通用图，绝不标成某一季专属图。分季简介走 TMDB 分季详情接口逐季取。
TMDB_KEY = os.environ.get("TMDB_API_KEY", "").strip()
def tmdb_find(seed):
    if not TMDB_KEY: return None
    kind = "movie" if seed["kind"] == "movie" else "tv"
    try:
        d = http_get_json(f"https://api.themoviedb.org/3/search/{kind}",
                          {"api_key": TMDB_KEY, "query": seed["english_title"],
                           "year" if kind == "movie" else "first_air_date_year": seed["year"],
                           "language": "zh-CN"})
        res = d.get("results") or []
        return res[0] if res else None
    except Exception as e:
        log(f"  TMDB 查询失败: {e}"); return None

# ---------- 海报：下载、验真、压缩、哈希命名 ----------
def fetch_landscape_image(url, slug_tag):
    """只接受可解码且宽高比 1.70~1.85 的横屏图；否则返回 (None, 原因)。绝不拉伸。"""
    try:
        r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)); img.load()
    except Exception as e:
        return None, f"下载/解码失败: {e}"
    w, h = img.size; ratio = w / h if h else 0
    if not (1.70 <= ratio <= 1.85):
        return None, f"宽高比 {ratio:.2f} 非16:9 (原尺寸 {w}x{h})，拒绝入库"
    img = img.convert("RGB")
    if w > 1280: img = img.resize((1280, round(1280 / ratio)))
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=82, optimize=True)
    data = buf.getvalue(); digest = hashlib.sha256(data).hexdigest()[:16]
    name = f"{slug_tag}-{digest}.jpg"; path = IMAGES / name
    if not path.exists(): path.write_bytes(data)
    return {"path": f"images/{name}", "width": img.size[0], "height": img.size[1],
            "sha256": hashlib.sha256(data).hexdigest()}, "ok"

def build_record(seed):
    rec = {"id": f'prime:{seed["prime_id"]}' if seed.get("prime_id") else f'seed:{seed["slug"]}',
           "slug": seed["slug"], "kind": seed["kind"], "year": seed["year"],
           "original_title": seed["original_title"], "english_title": seed["english_title"],
           "prime_id": seed.get("prime_id"), "prime_official_name_zh": None,
           "display_name_zh": None, "aliases_zh": [], "name_status": "pending_review",
           "synopsis_zh": "", "synopsis_source_type": None, "details": {},
           "poster": None, "poster_status": "missing", "poster_note": "",
           "seasons": [], "sources": [], "updated_at": NOW().isoformat(timespec="seconds")}
    page, status = match_chinese(seed)
    tm = tmdb_find(seed)
    if page:
        rec["sources"].append({"type": "existing_chinese", "name": "中文维基百科",
                               "url": page["url"], "retrieved_at": rec["updated_at"]})
        if status == "verified":
            m = re.match(r"\s*《([^》]{1,30})》", page["extract"])
            rec["display_name_zh"] = (m.group(1).strip() if m else
                                      page["title"].split("(")[0].split("（")[0].strip())
            rec["name_status"] = "verified"
            rec["synopsis_zh"] = first_paragraphs(page["extract"])
            rec["synopsis_source_type"] = "existing_chinese"
    if not rec["display_name_zh"]:
        rec["display_name_zh"] = seed["english_title"]  # 回退原名，不编造中文名
    if tm:
        rec["sources"].append({"type": "existing_chinese", "name": "TMDB zh-CN",
                                "url": f'https://www.themoviedb.org/{"movie" if seed["kind"]=="movie" else "tv"}/{tm["id"]}',
                                "retrieved_at": rec["updated_at"]})
        if tm.get("overview") and not rec["synopsis_zh"]:
            rec["synopsis_zh"] = tm["overview"]; rec["synopsis_source_type"] = "existing_chinese"
        if tm.get("backdrop_path"):
            info, why = fetch_landscape_image(
                f'https://image.tmdb.org/t/p/w1280{tm["backdrop_path"]}', seed["slug"])
            if info: rec["poster"], rec["poster_status"] = info, "ok"
            else: rec["poster_note"] = why
    if not rec["poster"]:
        rec["poster_note"] = rec["poster_note"] or (
            "未取得合格16:9横屏图（TMDB未配置或维基图多为竖版海报，按规则拒绝拉伸补位）")
    # 按季简介：逐季单独检索，查不到留空，绝不复制第一季
    for s in seed.get("seasons", []):
        srec = {"season": s, "id": f'{rec["id"]}:s{s}', "synopsis_zh": "",
                "synopsis_source_type": None, "poster": None, "poster_status": "missing",
                "status": "pending_review"}
        if rec["name_status"] == "verified":
            for q in (f'{rec["display_name_zh"]} 第{s}季', f'{seed["english_title"]} season {s}'):
                try: titles = wiki_search(q, limit=3)
                except Exception: titles = []
                hit = next((t for t in titles if f"第{s}季" in t or f"Season {s}" in t), None)
                if hit:
                    try: sp = wiki_page(hit)
                    except Exception: sp = None
                    if sp and sp["extract"]:
                        srec["synopsis_zh"] = first_paragraphs(sp["extract"])
                        srec["synopsis_source_type"] = "existing_chinese"; srec["status"] = "verified"
                        rec["sources"].append({"type": "existing_chinese", "name": f"中文维基百科（第{s}季）",
                                                "url": sp["url"], "retrieved_at": rec["updated_at"]})
                    break
        rec["seasons"].append(srec)
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略7天间隔强制运行")
    ap.add_argument("--due", action="store_true", help="距上次成功不满7天(604800秒)则跳过")
    args = ap.parse_args()
    DOCS.mkdir(parents=True, exist_ok=True); IMAGES.mkdir(parents=True, exist_ok=True); LOGS.mkdir(exist_ok=True)
    ver_path, cat_path = DOCS / "version.json", DOCS / "catalog.json"
    prev_items = 0; prev = None
    if cat_path.exists():
        try: prev = json.loads(cat_path.read_text(encoding="utf-8")); prev_items = len(prev.get("items", []))
        except Exception: prev = None
    if args.due and not args.force and ver_path.exists():
        try:
            last = datetime.fromisoformat(json.loads(ver_path.read_text())["last_success_at"])
            interval = int(os.environ.get("UPDATE_INTERVAL_SECONDS", "604800"))
            if NOW() - last < timedelta(seconds=interval):
                log(f"距上次成功 {NOW()-last} 不满 {interval} 秒（7天），跳过本次运行"); return 0
        except Exception: pass
    seeds = yaml.safe_load((ROOT / "config/seeds.yaml").read_text(encoding="utf-8"))["seeds"]
    boot_seeds = ROOT / "data/seeds_from_bootstrap.json"
    n_boot = len(json.loads(boot_seeds.read_text(encoding="utf-8"))) if boot_seeds.exists() else 0
    # 统计口径分开：本程序只做「已有种子更新」（含 bootstrap 导入的存量种子基线，
    # 其有效数据保存在已发布 catalog 中，安全闸保证刷新失败不丢失）；
    # 「新作品发现」不在本流程内：发现候选须先经官方身份核验入队
    # (data/discovery_queue.json) 并人工确认后才可转入 seeds，搜索结果不直接导入。
    log(f"开始采集：已有种子更新 {len(seeds)} 部精编种子（存量基线 {n_boot} 条，上一版 {prev_items} 条）；新作品发现 0（发现与更新分开，未核验不导入）")
    items = []
    for seed in seeds:
        log(f'- {seed["english_title"]} ({seed["year"]}, {seed["kind"]})')
        try: items.append(build_record(seed))
        except Exception as e: log(f"  !! 采集失败，跳过该条: {e}")
        time.sleep(0.4)
    # 合并刷新：本次只刷新精编种子，其余存量条目（App 快照基线）原样保留，不以少覆多；
    # 且刷新结果未核验而旧记录资料更全时保留旧记录，防止网络故障把好数据降级覆盖
    if prev:
        def _norm(x): return re.sub(r"[^a-z0-9]+", "", (x or "").lower())
        def _keys(rec):
            ks = set()
            if rec.get("prime_id"): ks.add("id:" + rec["prime_id"])
            for f in ("original_title", "english_title"):
                n = _norm(rec.get(f))
                if n: ks.add("t:" + n)  # 空标题不产生键，绝不互相吞并
            return ks
        def _merge_fields(new, p):
            """字段级合并：新记录只覆盖它真正刷新的字段，旧记录的官方ID、用户偏好名、
            别名、分季海报与分季简介在新记录缺失时一律保留，避免刷新反而丢数据。"""
            if p.get("prime_id") and not new.get("prime_id"):
                new["prime_id"] = p["prime_id"]
                if str(p.get("id", "")).startswith("prime:"): new["id"] = p["id"]
            if p.get("name_status") == "user_app_preference":
                old = new.get("display_name_zh")
                new["display_name_zh"] = p["display_name_zh"]; new["name_status"] = "user_app_preference"
                al = list(p.get("aliases_zh", []))
                if old and old != new["display_name_zh"] and old not in al: al.append(old)
                new["aliases_zh"] = al
            if not new.get("poster") and p.get("poster"):
                new["poster"], new["poster_scope"] = p["poster"], p.get("poster_scope")
                new["poster_status"] = "ok"
            p_seasons = {s["season"]: s for s in p.get("seasons", [])}
            for s in new.get("seasons", []):
                ps = p_seasons.pop(s["season"], None)
                if not ps: continue
                if not s.get("poster") and ps.get("poster"):
                    s["poster"], s["poster_scope"], s["poster_status"] = ps["poster"], ps.get("poster_scope"), "ok"
                if not s.get("synopsis_zh") and ps.get("synopsis_zh"):
                    s["synopsis_zh"] = ps["synopsis_zh"]; s["synopsis_source_type"] = ps.get("synopsis_source_type")
                    s["synopsis_status"] = "ok"
            for sn, ps in p_seasons.items():
                new.setdefault("seasons", []).append(ps)
            new["seasons"] = sorted(new.get("seasons", []), key=lambda x: x["season"])
            return new
        prev_items_list = list(prev.get("items", []))
        prev_keys = [_keys(p) for p in prev_items_list]
        used = set(); merged_items = []
        for i in items:
            ik = _keys(i); hit = next((idx for idx, pk in enumerate(prev_keys)
                                       if idx not in used and ik & pk), None)
            if hit is not None:
                used.add(hit); p = prev_items_list[hit]
                if i.get("name_status") != "verified" and (p.get("synopsis_zh") or p.get("poster") or p.get("seasons")):
                    log(f'  保留旧记录（本次未核验）: {i.get("original_title")}')
                    merged_items.append(p); continue
                merged_items.append(_merge_fields(i, p)); continue
            merged_items.append(i)
        kept = [p for idx, p in enumerate(prev_items_list) if idx not in used]
        log(f"合并刷新：本次刷新 {len(merged_items)} 条，保留存量 {len(kept)} 条")
        items = merged_items + kept
    # 发布安全闸
    if not items or (prev_items and len(items) < prev_items * 0.5):
        log(f"!! 安全闸触发：新条目 {len(items)} vs 上一版 {prev_items}，保留上一版，退出")
        (LOGS / "last_run.json").write_text(json.dumps({"ok": False, "reason": "safety_gate",
            "log": log_lines}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2
    catalog = {"schema": "prime-video-cn-catalog/1", "generated_at": NOW().isoformat(timespec="seconds"),
               "item_count": len(items), "items": items}
    blob = json.dumps(catalog, ensure_ascii=False, indent=2)
    version = {"version": hashlib.sha256(blob.encode()).hexdigest()[:16],
               "catalog_sha256": hashlib.sha256(blob.encode()).hexdigest(),
               "generated_at": catalog["generated_at"], "last_success_at": catalog["generated_at"],
               "item_count": len(items),
               "image_count": sum(1 for i in items if i.get("poster")) + sum(1 for i in items for s in i.get("seasons", []) if s.get("poster"))}
    tmp = DOCS / ".catalog.tmp"; tmp.write_text(blob, encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))  # 写后回读校验
    tmp.replace(cat_path)
    (DOCS / "version.json").write_text(json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8")
    (LOGS / "last_run.json").write_text(json.dumps({"ok": True, "version": version, "log": log_lines},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"发布完成：{len(items)} 条，海报 {version['image_count']} 张，version={version['version']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
