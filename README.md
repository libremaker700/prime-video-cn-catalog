# Prime Video Chinese Metadata Catalog

2026-10-31: Add evidence-bound source identity checks, strict-first lookup with verified-name fallback, private provider-ID reuse, and a documented 2023/2024 release exception for *In the Land of Saints and Sinners*. Regression checks run before catalog publication.

Public film and TV metadata for a personal Android TV app: verified title mappings, Chinese and original-language names, season descriptions, release dates, genres, and landscape artwork. This repository does not publish playback URLs, provider sessions, personal watchlists, playback progress, signing keys, or upload credentials.

## Publication and app updates

Public catalog: [GitHub Pages](https://libremaker700.github.io/prime-video-cn-catalog/).
An optional Oracle Object Storage mirror is configured through the repository's `OCI_CATALOG_PUBLIC_URL` variable. Its full address does not need to be repeated here. The app downloads updates from GitHub; Oracle remains available for an explicit connectivity check.

The app reads `version.json`, downloads `releases/<SHA256>/catalog.json`, verifies the original bytes and every referenced image, and switches its local catalog atomically. A failed update leaves the last valid catalog available. Images use content hashes, are decoded before acceptance, and must be landscape artwork at least 960 pixels wide with a 16:9 aspect ratio. App and catalog versions are independent. Newly discovered metadata does not add playable titles without separate playback-source verification.

## Identity mappings and compatible fields

The initial snapshot contained 156 records in `data/fallback_catalog.json`. A retained snapshot is not a fresh crawl; source provenance and timestamps remain explicit.

`data/app-metadata.json` contains public official identifiers, original names, and saved official season links. It excludes provider playback IDs and media URLs. The app project's `tools/prime-official/export-cloud-metadata.py` exports this whitelist. `scripts/import_app_metadata.py` matches official identity and media type, fills missing names and season IDs, and normalizes legacy movie date/genre fields without advancing crawl-success timestamps.

Season refresh supports official ASINs and compact IDs, verifies the exact season number, and can use a saved same-season compact link when an older ASIN is unavailable. A saved link alone does not mean its content was refreshed. App 1.3.36 and later can receive these mapped names, compatible movie fields, and verified season metadata. Playback routes and personal state remain under app control.

Canonical redirects are limited to four HTTPS redirects within the same site. Prime's `www` and non-`www` hosts are treated as one site. Cross-site, insecure, or external login redirects are refused. A normal redirect or an individual missing title does not open a site-wide failure circuit. Final page identity is still verified.

Ordinary app season numbers are limited to 0–200. Prime sometimes encodes specials as 202 or larger values. Those entries are retained separately in `official_unmapped_seasons`, outside the ordinary season and artwork manifests, so they cannot make the catalog incompatible with existing app validation.

## Refresh schedule and failure retention

The catalog workflow checks daily at approximately 03:17 UTC (11:17 Asia/Shanghai). The default full refresh interval is 604800 seconds.

- Reuse successful records for seven days. Failed or partial records become eligible for retry after six hours and are retried by the daily workflow. Records without a usable source or identity rotate on a seven-day interval.
- Publish verified changes while retaining old metadata for failed records. Keep every existing title and mark incomplete refreshes as `refresh_status=partial`.
- Partial refreshes update `last_partial_at` and `last_attempt_at`, but do not advance `last_success_at` or claim a full refresh. The app displays full and partial refresh times separately.
- Do not publish when every source fails, validation fails, or the existing title set would be lost. Failed-attempt diagnostics are retained as an Actions artifact.
- Default to a 20-minute collection budget, leaving time for validation and publication. Retain deferred records and resume unvisited or oldest attempts first while preserving catalog order. `SYNC_BUDGET_SECONDS` accepts 60–1800 seconds; request timeouts and retry delays respect the remaining budget.

```bash
python -m unittest discover -s tests -v
python scripts/import_app_metadata.py
python scripts/sync.py --due
python scripts/sync.py --force --no-discovery
python scripts/validate.py
python scripts/prune_publication.py
```

Manual `mode=sync` forces collection. `mode=publish` aligns and validates the existing catalog without collecting new content. Optional TMDB and translation settings are described in [Translation configuration](config/translation.md).

## Chinese title lookup and numbered Tavily credentials

The title workflow checks daily at approximately 03:47 UTC (11:47 Asia/Shanghai), with an independent seven-day title-cache schedule. It reuses verified names, saved preferences, and cached evidence. The ordered metadata resolver checks Tavily candidates, Douban pages, Chinese Wikipedia and Baidu Baike. Quoted/bracketed AI names are search terms, never verified identities. The original title, year, media type and season scope must agree before a name or description is published. A series-wide synopsis cannot overwrite a particular season's synopsis.

Configure `TAVILY_API_KEY_1` through `TAVILY_API_KEY_4` in Actions Secrets. The first configured credential is primary; remaining unique credentials are backups. They authenticate to the same Tavily API. Duplicate values are deduplicated; legacy environments can still use `TAVILY_API_KEY` when numbered credentials are absent.

- Authentication/account-quota responses 401, 403, 432, or 433 can switch to the backup. Each credential is attempted at most once per query, and a working backup is reused afterward.
- Rate limiting (429), timeouts, and server failures stop paid requests for the run. The workflow does not cycle keys to bypass rate limits or enable a more expensive search tier.
- Reserve budget before every paid search request, including failed primary and backup attempts. All slots share one monthly budget and have separate usage counters.
- Defaults: at most 12 titles per run (hard limit 20) and 100 reserved requests per month (hard limit 200). Configure `TITLE_LOOKUP_LIMIT` and `TITLE_LOOKUP_MONTHLY_LIMIT` as repository variables. Usage by other programs is outside this local ledger.
- Reuse verified identity-based results; unresolved entries normally wait seven days, with shorter retries after search-service failures. `review_saved=true` with `limit=0` reviews saved evidence without new searches.
- `python scripts/title_lookup.py --check-keys` calls the official `/usage` endpoint and reports only slot/status information. It does not print credentials or account responses and does not perform searches.

## Source identity and verified fallback

See [Source matching policy](config/source-matching.md) for the reference/candidate format and private review command. The policy requires exact verified titles and media type; TV seasons cannot be substituted. Movie release years can differ only when public release evidence is bound to the same official ID, English original, medium, season and IMDb ID. The official display year stays unchanged.

`scripts/source_matching.py` is a reusable policy with injected search/detail callbacks; it does not make provider requests itself. It checks strict provider results before requesting a verified name fallback, revalidates a saved provider ID before searching again, and never stores media URLs. Interface failures remain unavailable rather than becoming negative matches. Provider IDs and local binding caches remain private; they are not catalog fields or public artifacts.

`data/release-year-evidence.json` currently contains the independently checked Saints and Sinners exception. The title-page verifier uses the same evidence. Adding a new exception requires explicit public dates; this is not an automatic one-year tolerance. Existing App 1.3.60 matching still runs on the device. Updating this repository alone does not replace installed App code or upload its source bindings.

## Retention

Two production workflows are maintained: catalog collection/publication and verified title lookup/publication. Obsolete Gemini, Google Overview, and one-off Tavily probes and bootstrap import copies have been removed.

Keep the active and previous immutable catalog releases and every image referenced by either. `scripts/prune_publication.py` removes older hash-named releases and unreferenced hash-named images. Git history remains available for recovery. Retain title evidence, the request ledger, preference rules, the fallback snapshot, app identity mappings, and mirror publication tools.

## Optional Oracle mirror and security boundary

Enable the mirror with `OCI_MIRROR_ENABLED=true` and configure `OCI_CATALOG_PUBLIC_URL` as its public read endpoint. Store the scoped, expiring upload PAR URL only in the `OCI_CATALOG_UPLOAD_URL` Actions Secret. The upload URL is a credential and must never be committed, displayed in documentation, or copied into an APK. Renew it before its configured expiry.

`scripts/publish_oci.py` selects only validated catalog JSON and referenced artwork. It uploads assets first, verifies the immutable catalog, and changes `version.json` last. It does not publish keystores, APKs, provider configuration, logs, or arbitrary local files, and does not use a virtual machine or SSH credentials.

Public read addresses intentionally expose catalog metadata and artwork; knowing such an address does not grant a cloud account login or upload permission. Anonymous traffic can still consume requests or bandwidth. Keep private data outside public buckets, protect the repository's write access and Actions Secrets, and review traffic and permissions in the cloud console. Removing a README link does not erase copies in Git history or make a public endpoint secret.

This project is not affiliated with Amazon or TMDB. Source attribution and applicable license requirements remain attached to supplemental metadata.
