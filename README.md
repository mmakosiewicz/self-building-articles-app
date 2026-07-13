# Self-Building Articles

A living-draft writing tool. You set a working title and a rough thesis, then drop **scraps** as you find them — quotes, URLs, screenshots, stream-of-thought notes, counterpoints. The app continuously re-reads the whole pile and renders **the article that's trying to be born**: what's emerging, a living outline, tensions in your material, gaps worth researching, and — when the material outgrows your thesis — a proposed stronger one.

## How it works

1. **Start** an article with a working title + thesis (both editable later).
2. **Drop scraps** as you find them:
   - text / quotes / stream-of-thought
   - URLs — fetched and summarized automatically
   - screenshots & images — read by a vision model (numbers, chart trends, quotes extracted)
   - files
   - anything can be tagged as a **counterpoint** (material arguing *against* your thesis — weighted specially in the analysis)
3. **"What's emerging"** — after every N scraps (configurable, or every scrap), an LLM re-reads everything and renders:
   - **Story so far** — the thread taking shape, in prose
   - **Living outline** — the sections the article would have today, with scraps grouped under each
   - **Tensions** — scraps that contradict each other
   - **Gaps** — concrete missing pieces, each with a one-click **Research** button (web-grounded LLM research lands back as a scrap)
   - **Emerging thesis** — only when the material genuinely pulls toward a stronger angle; one click adopts it
4. **Draft section by section** — every outline bucket has a **Draft this section** button producing 200–500 words grounded in your scraps (with `[#id]` traceability and `[NEEDS-SOURCE]` flags instead of invented evidence). Drafts are edited in place; drafted buckets show ✓ written.
5. **Version history** — every regeneration is diffed against the previous one; "what moved" notes track how the story drifts as material accumulates.

## GitHub content mirror (optional, off until configured)

The app can continuously push your **work-in-progress content** (scraps, analysis, drafts — *not* this code) to a GitHub repo as a backup: `articles/<slug>/{meta.json, story.md, scraps.md, drafts.md}`, updated on every change.

- Configure in **Settings**: on/off toggle + target repo (`owner/name`) + a PAT with Contents read/write on that repo.
- Your raw notes end up there — **use a private repo**.
- Switched off (default when no repo is set): nothing leaves your server; everything lives in the local Postgres database.

## Architecture

Built for a Flask blueprint auto-discovery scaffold (one module per app in `applications/`, exposing `NAME` + `blueprint`), but easily adapted to a plain Flask app.

- `applications/self_building_articles.py` — routes, schema bootstrap, GitHub mirror
- `applications/_self_building_articles_worker.py` — background worker (detached subprocess): URL fetch + summarize, image vision, gap research, story regeneration, section drafting
- `templates/self_building_articles/` — UI (vanilla JS, no build step)

**Templates extend `base.html`** — they expect your scaffold to provide a `templates/base.html` with `{% block title %}` and `{% block content %}`. Each page carries its own `<style>` inside the content block, so no `{% block head %}` support is required and no external CSS files are needed. A minimal base works fine:

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>{% block title %}{% endblock %}</title></head>
<body>{% block content %}{% endblock %}</body></html>
```

**Storage**: PostgreSQL (tables `sba_articles`, `sba_scraps`, `sba_sections`, `sba_story_versions`, `sba_settings` — created automatically on first import). Connects over the local socket with peer auth as the current OS user; adjust `conn()` for your environment.

**LLM**: OpenAI-compatible endpoint. Model split in the worker:
- `GEN_MODEL` (default `anthropic/claude-opus-4.8`) — story regeneration, section drafts, gap research (appends `:online` for web-grounded research, with graceful fallback)
- `MODEL` (default `anthropic/claude-sonnet-4.6`) — URL summaries, image OCR, version-diff notes

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `SBA_DATA_DIR` | `~/sba-data` | Uploads, PAT file, mirror working dir. **Keep this OUTSIDE the app directory** — file-watching scaffolds restart on every write, and worker logs/uploads land here continuously |
| `SBA_LOG_DIR` | `~/sba-data/logs` | Worker logs (one per article) |
| `SBA_LLM_BASE_URL` | `http://127.0.0.1:18080/api/v1` | OpenAI-compatible endpoint |
| `SBA_LLM_API_KEY` | `unused` | API key for that endpoint |
| `SBA_GEN_MODEL` | `anthropic/claude-opus-4.8` | Reasoning-heavy generation |
| `SBA_MODEL` | `anthropic/claude-sonnet-4.6` | Cheap mechanical calls |
| `SBA_FETCHER` | *(none)* | Optional external URL-fetcher script (prints markdown for a URL); plain urllib fallback otherwise |

## Security notes (read before deploying anywhere public)

This app was built to run behind an authenticating reverse proxy (all routes assume a trusted user). If you deploy it on the open internet you MUST add:

- **Auth** on every route — there is none built in.
- **Upload size limits** (`app.config["MAX_CONTENT_LENGTH"]`) — the original deployment relied on a proxy-level cap.
- The URL fetcher has an **SSRF guard** (rejects non-http(s) schemes and private/loopback/link-local/reserved addresses), but review it against your network layout.
- **Sandboxed / egress-gated environments**: the fetcher distinguishes a workspace firewall block ("approve the domain, re-add the URL") from a site-side refusal ("try an archive") and surfaces the right fix on the scrap. If your platform uses a different block sentinel than an HTTP error mentioning "blocked by firewall/policy" or a refused connection, adapt `fetch_url`'s error handling.
- The PAT settings endpoint writes a token to disk (`chmod 600`) — ensure only trusted users can reach it.
- File uploads are name-sanitized and stored outside the web root, but are not content-scanned.

## License

MIT
