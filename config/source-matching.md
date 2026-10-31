# Source matching policy

The official US catalog display date and a source provider's first-release year
can differ. Keep the display date and verify the work instead of accepting every
adjacent year. *In the Land of Saints and Sinners* is the initial checked example:
2023 Venice presentation and 2024 US theatrical release, IMDb `tt15782690`.
The two public references are retained in `data/release-year-evidence.json`.

## Ordered decisions

1. Revalidate the existing provider ID against the same official work and season.
   A timeout preserves that private binding and reports unavailable. An explicit
   identity conflict invalidates it and allows a new search.
2. Search the calibrated Chinese title, verified aliases and English original.
   A provider is bounded to three strict queries; up to 18 providers are accepted.
   The calling client must impose network timeouts, rate limits and cancellation.
3. If no strict provider match exists, request verified domestic-name metadata
   once. Search those verified aliases in a second bounded pass. A raw AI name
   cannot enter the verified alias set merely because many sites repeat it.
4. A conflicting title/type/season or explicit IMDb/Douban/director/cast conflict
   rejects the candidate. Missing season identity remains unverified. Unexplained
   release-year differences remain pending review rather than becoming matches.
5. Distinct matching provider IDs remain ambiguous; episode count cannot choose
   between different works. For accepted sources, display the maximum actual
   episode count, allowing different update progress on different providers.
6. Blocked, timed-out and failed interfaces are unavailable. Only clean completed
   searches can report no match. This is not a permanent claim of no source.

## Private review command

The collector can normalize a result into the following snapshot. A TV reference
must include an explicit `season`; a candidate can supply it or a season suffix.
`has_routes` must reflect a real playlist response, not a model's prediction.

```json
{
  "reference": {
    "prime_id": "B0CVNJ632Z", "kind": "movie", "season": 0, "year": 2024,
    "english_title": "In the Land of Saints and Sinners",
    "display_name_zh": "圣人营救"
  },
  "providers": [{
    "name": "LOCAL_PROVIDER_NAME", "status": "ok",
    "candidates": [{
      "provider_id": "LOCAL_RESULT_ID", "title": "圣人营救",
      "kind": "movie", "year": 2023, "has_routes": true, "episode_count": 1
    }]
  }]
}
```

```bash
python scripts/review_sources.py --check-policy
python scripts/review_sources.py --input .local/source-snapshot.json
```

Output stays in `.local/source-review.json`, which is ignored by Git. The command
does not query source sites, update production catalogs, change titles or start
playback. `resolve_sources` also exposes callbacks for a local collector's live
search/detail operations; its binding cache must be persisted privately.

Public GitHub Actions run regression tests and validate the evidence before
publishing ordinary metadata. They do not receive private provider endpoints,
source sessions, watch history or source-ID caches. The new policy must be called
by a local collector or device implementation to govern its source searches;
repository publication is not an App binary update.
