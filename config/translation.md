# Translation configuration

Translation runs only when a title or season has an original-language description but no Chinese description for that same entity. Missing originals remain `missing`; an available original without a configured translator remains `translation_pending`. A series-wide description must not replace a season description.

Configure one of the following options in **Settings → Secrets and variables → Actions**. Local execution reads environment variables; if a local `.env` file is used, load it into the environment first. `.env` is ignored and must never be committed.

## Option A: DeepL

- `TRANSLATE_PROVIDER=deepl`
- `DEEPL_API_KEY`: your DeepL authentication key. Keys ending in `:fx` select the free API endpoint automatically.

## Option B: OpenAI-compatible endpoint

- `TRANSLATE_PROVIDER=openai`
- `TRANSLATE_API_KEY`: the endpoint's API credential.
- `TRANSLATE_MODEL`: the model supported by that endpoint; the current code defaults to `gpt-4o-mini`.
- `TRANSLATE_BASE_URL`: defaults to `https://api.openai.com/v1`; another compatible HTTPS endpoint may be configured.

## Optional metadata source

`TMDB_API_KEY` enables supplemental Chinese descriptions and series-wide landscape backdrops. TMDB's season poster images are portrait artwork and are not used as landscape images. A series backdrop remains `scope=series` and is not relabeled as a specific season's artwork. TMDB source records remain distinct from Prime-verified records.

Machine-translated results use `synopsis_source_type: machine_translation` and preserve the original source URL, provider, and model for review. Translation credentials are used only by the collection workflow and are not published in catalog data or the app.
