"""One-title Gemini/Google Search probe. Never writes the published catalogue."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

MODEL = "gemini-2.5-flash"
BASE = "https://generativelanguage.googleapis.com/v1beta"
PROMPT = """请实际使用 Google Search 核验 Vinland Saga 的中文名称。
目标是 2019 年首播的日本电视动画第一季，不是原作漫画、歌曲或其他季度。
核查中国大陆播出平台名称与其他地区译名，不要根据英文自行创造中文片名。
只输出一个 JSON 对象，字段为 original_name, mainland_name, aliases (数组),
kind (固定为 anime_tv), first_air_year (数字), season_number (数字),
synopsis_zh (不超过120字的简体中文简介), source_urls (公开来源URL数组)。
没有可靠资料的字段留空。请搜索并提供可核验来源，不仅凭记忆回答。
"""


def scrub(value, key):
    """Defense in depth for generated text, errors and report fields."""
    text = json.dumps(value, ensure_ascii=False)
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", text)
    return json.loads(text)


def error_summary(response):
    # Raw error messages/URLs can contain credentials; only expose selected fields.
    result = {"http_status": response.status_code}
    try:
        error = response.json().get("error", {})
        result["api_status"] = error.get("status")
        # The whole report is scrubbed before writing; no request URL or headers.
        result["message"] = str(error.get("message", ""))[:1200]
        result["quota_limit_zero"] = False
        reasons = []
        for detail in error.get("details", []):
            reason = detail.get("reason")
            if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]+", reason):
                reasons.append(reason)
            for violation in detail.get("violations", []):
                if str(violation.get("quotaValue")) == "0":
                    result["quota_limit_zero"] = True
        result["reasons"] = reasons
    except (ValueError, AttributeError, TypeError):
        result["api_status"] = "unparseable_error"
    return result


def public_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def run(key):
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "target": "Vinland Saga / 2019 / anime / season 1",
        "catalogue_modified": False,
        "generation_requests": 0,
        "status": "failed",
    }
    if not key:
        return report | {"failure": "missing_secret"}
    session = requests.Session()
    session.headers.update({"x-goog-api-key": key})
    try:
        models = session.get(BASE + "/models", timeout=(15, 45))
        report["authentication_http_status"] = models.status_code
        if models.status_code != 200:
            return report | {"failure": "model_list_failed", "error": error_summary(models)}
        available = {m.get("name") for m in models.json().get("models", [])}
        report["requested_model_available"] = "models/" + MODEL in available
        report["model_methods"] = next((m.get("supportedGenerationMethods", []) for m in models.json().get("models", []) if m.get("name") == "models/" + MODEL), [])
        if not report["requested_model_available"]:
            return report | {"failure": "requested_model_unavailable"}
        report["generation_requests"] = 1
        response = session.post(
            BASE + "/models/" + MODEL + ":generateContent",
            json={
                "contents": [{"parts": [{"text": PROMPT}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 2048,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=(15, 120),
        )
        report["generation_http_status"] = response.status_code
        if response.status_code != 200:
            return report | {"failure": "generation_failed", "error": error_summary(response)}
        payload = response.json()
        candidate = next(iter(payload.get("candidates", [])), {})
        report["finish_reason"] = candidate.get("finishReason")
        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []) if not p.get("thought"))
        grounding = candidate.get("groundingMetadata", {})
        report["search_queries"] = grounding.get("webSearchQueries", [])
        report["grounding_sources"] = [
            {"title": chunk["web"].get("title"), "url": public_url(chunk["web"].get("uri"))}
            for chunk in grounding.get("groundingChunks", [])
            if chunk.get("web") and public_url(chunk["web"].get("uri"))
        ]
        report["grounding_support_count"] = len(grounding.get("groundingSupports", []))
        report["usage"] = {k: v for k, v in payload.get("usageMetadata", {}).items() if k.endswith("TokenCount")}
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            parsed = json.loads(clean)
        except ValueError:
            return report | {"failure": "response_not_json"}
        fields = ("original_name", "mainland_name", "aliases", "kind", "first_air_year", "season_number", "synopsis_zh", "source_urls")
        report["result"] = {k: parsed.get(k) for k in fields}
        report["checks"] = {
            "actual_search_reported": bool(report["search_queries"]),
            "grounding_sources_present": bool(report["grounding_sources"]),
            "mainland_name_matches_independent_lookup": parsed.get("mainland_name") == "冰海战记",
            "identity_matches": parsed.get("kind") == "anime_tv" and parsed.get("first_air_year") == 2019 and parsed.get("season_number") == 1,
            "chinese_synopsis_present": bool(re.search(r"[\u4e00-\u9fff]", str(parsed.get("synopsis_zh", "")))),
        }
        if all(report["checks"].values()):
            report["status"] = "passed"
        else:
            report["failure"] = "evidence_or_identity_check_failed"
        return report
    except requests.RequestException as exc:
        return report | {"failure": "network_error", "error_type": type(exc).__name__}
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return report | {"failure": "unexpected_response", "error_type": type(exc).__name__}
    finally:
        session.close()


def main():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    report = scrub(run(key), key)
    destination = Path(os.environ.get("GEMINI_PROBE_REPORT", "gemini-probe-report.json"))
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
