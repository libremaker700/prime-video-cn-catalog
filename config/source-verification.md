# Preprocessed source-verification metadata

The app should not repeat an entire catalog's name and source discovery when it
opens. Title/identity resolution and a bounded source check run before delivery.
The app downloads the small result snapshot and displays only results bound to
the same official ID, English original, medium, season and official year.

This publication is a **verification-status snapshot**, not a source directory.
It excludes provider IDs, provider endpoints, source configuration, sessions,
playback/media URLs, API keys, favorites and watch history. A `matched` result
means the preprocessing client confirmed a same-identity record with a playlist;
it does not mean every media URL was played or will remain valid indefinitely.

## Public record format

```json
{
  "schema": 1,
  "items": [{
    "officialId": "OFFICIAL_ASIN",
    "originalTitle": "Exact English original title",
    "kind": "movie",
    "season": 0,
    "year": 2024,
    "status": "pending",
    "checkedAt": null
  }]
}
```

The example ID is a placeholder. Real records require a 10–26 character uppercase
official ID, a nonempty original title, a known year, and an explicit media type
and season. Movies use season 0; TV seasons use 1–200. The publisher rejects all
extra fields instead of attempting to guess which private fields to strip.

| Status | Meaning |
| --- | --- |
| `matched` | Identity and an actual returned playlist were verified. |
| `no_match` | Completed usable checks did not find a unique matching record. |
| `unavailable` | A check was blocked, timed out or failed; this is not no source. |
| `pending` | The work remains unverified or needs review. |

`checkedAt` is the actual source-check timestamp in timezone-qualified ISO 8601.
Unknown `pending` records can use `null`. Re-exporting or republishing a result
must not advance its timestamp. A failed retry must not overwrite a previous
positive result with a newly invented negative or a freshly dated positive.

Partial nonempty exports retain published identities absent from the new input.
For the same original title and year, a `pending` or `unavailable` update retains
a positive checked within seven days without changing its timestamp. An explicit
completed `no_match` may replace it. A changed original title or year replaces
the slot instead of inheriting another identity's result. The merged snapshot
must still fit the native reader's byte and record limits.

## Preprocessing and publication order

1. Export calibrated official identities into `data/source-verification-targets.json`.
2. Feed real local audit results into `data/source-verification.json`, or run the
   bounded checker using `SOURCE_PROVIDERS_JSON` from Actions Secrets.
3. Run `scripts/publish_source_verification.py`. It validates the exact allowlist,
   creates the immutable data file, updates the convenience copy and changes the
   manifest last. It never performs source requests itself.
4. Run full publication validation, commit the changed result/data files, and
   deploy Pages. Both regular workflows run the checker and publisher before
   validating and delivering metadata.

```bash
python scripts/check_source_verification.py --limit 12 --budget-seconds 120
python scripts/publish_source_verification.py
python scripts/publish_source_verification.py --check
python scripts/validate.py
```

`SOURCE_CHECK_LIMIT` and `SOURCE_CHECK_BUDGET_SECONDS` repository variables
configure the workflow defaults. The checker owns actual retry scheduling and
request counts. A skipped or unavailable checker must retain previous valid
results and remain explicit in its diagnostics. The publisher reports
`fresh_checks=0` even when it publishes new input: publication is not collection.

## App download contract

The app first reads `source-verification-version.json` (maximum 8 KiB), then reads
the manifest's immutable `path` and verifies the exact bytes with `sha256`.
Snapshots are limited to **512 KiB and 2,000 records**, matching the native reader.

```json
{
  "schema": 1,
  "path": "source-verification/<SHA256>.json",
  "sha256": "<SHA256>",
  "count": 1,
  "checkedAt": null,
  "publishedAt": "2026-10-31T00:00:00+00:00"
}
```

`publishedAt` describes publication only; `checkedAt` summarizes the newest real
record check. The App should make its own freshness decision using each record's
`checkedAt`, and retain its last valid snapshot when downloading or validation
fails. The convenience `source-verification.json` is for inspection; the App
should use the manifest and immutable file for atomic updates. Current and prior
immutable snapshots are retained locally and in Git history.

This sidecar is deployed through GitHub Pages. The existing OCI publisher's
allowlist still covers its catalog and referenced images; it does not silently
upload this new sidecar or arbitrary files. Private source binding and live
playlist refresh remain under the device/source-client's control.
