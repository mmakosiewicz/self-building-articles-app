#!/usr/bin/env python3
"""Worker for self-building-articles: URL fetch, image vision, story regen (+versions),
section drafting, and gap research."""

import argparse
import base64
import mimetypes
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pwd

import psycopg2
import psycopg2.extras
from openai import OpenAI

MODEL = os.environ.get("SBA_MODEL", "anthropic/claude-sonnet-4.6")          # cheap mechanical calls (URL summary, image OCR, diff note)
GEN_MODEL = os.environ.get("SBA_GEN_MODEL", "anthropic/claude-opus-4.8")        # reasoning-heavy generation (story, section drafts, research)


def conn():
    user = pwd.getpwuid(os.getuid()).pw_name
    return psycopg2.connect(host="/var/run/postgresql", user=user, database="console_db")


def llm():
    return OpenAI(
        base_url=os.environ.get("SBA_LLM_BASE_URL", "http://127.0.0.1:18080/api/v1"),
        api_key=os.environ.get("SBA_LLM_API_KEY", "unused"),
    )


def q_one(sql, params=()):
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def q_all(sql, params=()):
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def q_exec(sql, params=()):
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)


# ----------- url fetch -----------

def _is_safe_url(url: str) -> bool:
    """Reject non-http(s) schemes and private/loopback/link-local targets (SSRF guard)."""
    import ipaddress
    import socket
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def fetch_url(url: str) -> tuple[str, str | None]:
    """Returns (markdown_body, error_string_or_none)."""
    if not _is_safe_url(url):
        return "", "URL rejected (non-http scheme or private address)"
    # Optional external fetcher script (set SBA_FETCHER to a script that prints
    # markdown for a URL); falls back to plain urllib below.
    fetcher = os.environ.get("SBA_FETCHER")
    if fetcher and not os.path.exists(fetcher):
        fetcher = None
    if fetcher:
        try:
            out = subprocess.run(
                [sys.executable, fetcher, url],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout[:30000], None
        except Exception:
            pass
    try:
        import urllib.request, urllib.error, re as _re
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read(2_000_000).decode("utf-8", errors="ignore")
        text = _re.sub(r"<script.*?</script>", " ", html, flags=_re.S | _re.I)
        text = _re.sub(r"<style.*?</style>", " ", text, flags=_re.S | _re.I)
        text = _re.sub(r"<[^>]+>", " ", text)
        text = _re.sub(r"\s+", " ", text)
        return text[:20000], None
    except urllib.error.HTTPError as e:
        # Distinguish a sandbox/workspace egress block from the site refusing us —
        # they need different user actions.
        snippet = ""
        try:
            snippet = e.read(2000).decode("utf-8", errors="ignore").lower()
        except Exception:
            pass
        if "blocked by firewall" in snippet or "blocked by policy" in snippet:
            return "", ("Blocked by this workspace's network policy (not the site). "
                        "Fix: approve outbound access for this domain, then re-add the URL.")
        return "", (f"The site returned HTTP {e.code} (site-side refusal, not a policy block). "
                    "Fix: try an archived/cached copy, or paste the text as a text scrap.")
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "refused" in reason.lower():
            return "", ("Connection refused — in a sandboxed workspace this usually means the "
                        "domain is not on the outbound allowlist. Fix: approve the domain, then re-add the URL.")
        return "", f"Network error: {reason}"
    except Exception as e:
        return "", str(e)


def summarize_url(url: str, body: str) -> str:
    if not body.strip():
        return "(could not fetch content)"
    try:
        resp = llm().chat.completions.create(
            model=MODEL,
            max_tokens=400,
            messages=[
                {"role": "system", "content": (
                    "Summarize the following web page in 4-7 bullet points. "
                    "Capture the core argument, key data points, anything quotable. "
                    "Plain markdown bullets, no preamble.")},
                {"role": "user", "content": f"URL: {url}\n\n---\n\n{body[:15000]}"},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"(summary failed: {e})"


def handle_fetch(scrap_id: int):
    s = q_one("SELECT * FROM sba_scraps WHERE id=%s", (scrap_id,))
    if not s or not s["url"]:
        return
    print(f"[fetch] scrap {scrap_id} {s['url']}", flush=True)
    body, err = fetch_url(s["url"])
    if err and not body:
        q_exec("UPDATE sba_scraps SET fetch_status=%s, summary=%s WHERE id=%s",
               ("failed", f"fetch error: {err[:200]}", scrap_id))
        return
    summary = summarize_url(s["url"], body)
    q_exec("UPDATE sba_scraps SET content=%s, summary=%s, fetch_status='ok' WHERE id=%s",
           (body[:8000], summary, scrap_id))
    print(f"[fetch] scrap {scrap_id} done", flush=True)


# ----------- image vision -----------

def describe_image(scrap_id: int):
    s = q_one("SELECT * FROM sba_scraps WHERE id=%s", (scrap_id,))
    if not s or not s["file_path"]:
        return
    p = Path(s["file_path"])
    if not p.exists():
        q_exec("UPDATE sba_scraps SET fetch_status='failed', summary='(image file missing)' WHERE id=%s",
               (scrap_id,))
        return
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    data_url = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    print(f"[vision] scrap {scrap_id} {p.name}", flush=True)
    try:
        resp = llm().chat.completions.create(
            model=MODEL,
            max_tokens=400,
            messages=[
                {"role": "system", "content": (
                    "You extract the substance of screenshots/images dropped into a writer's "
                    "research pile. Describe what the image shows in 3-6 bullets: any numbers, "
                    "chart trends, table values, quotes, or UI states. Be specific with figures. "
                    "Plain markdown bullets, no preamble.")},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": s["content"] or "Describe this image."},
                ]},
            ],
        )
        summary = resp.choices[0].message.content.strip()
        q_exec("UPDATE sba_scraps SET summary=%s, fetch_status='ok' WHERE id=%s",
               (summary, scrap_id))
        print(f"[vision] scrap {scrap_id} done", flush=True)
    except Exception as e:
        q_exec("UPDATE sba_scraps SET fetch_status='failed', summary=%s WHERE id=%s",
               (f"(vision failed: {e})"[:300], scrap_id))


# ----------- gap research -----------

def research_scrap(scrap_id: int):
    s = q_one("SELECT * FROM sba_scraps WHERE id=%s", (scrap_id,))
    if not s:
        return
    art = q_one("SELECT title, thesis FROM sba_articles WHERE slug=%s", (s["slug"],))
    gap = s["content"] or ""
    print(f"[research] scrap {scrap_id}: {gap[:80]}", flush=True)
    prompt = (
        f"I'm writing an article titled \"{art['title']}\" "
        f"(thesis: {art['thesis'] or 'not set'}).\n\n"
        f"Research this gap and report back:\n\n{gap}\n\n"
        "Return 4-8 markdown bullets with concrete findings — data points, named sources, "
        "URLs where you found them. If you can't verify something, say so explicitly. No preamble."
    )
    summary = None
    try:
        resp = llm().chat.completions.create(
            model=GEN_MODEL + ":online",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[research] online model failed ({e}), falling back", flush=True)
    if not summary:
        try:
            resp = llm().chat.completions.create(
                model=GEN_MODEL,
                max_tokens=500,
                messages=[{"role": "user", "content": (
                    prompt + "\n\n(Web access unavailable — instead list what to search for, "
                    "likely best sources, and what you know from training data, clearly labeled as unverified.)")}],
            )
            summary = "_(live web research unavailable — model suggestions below)_\n\n" + \
                      resp.choices[0].message.content.strip()
        except Exception as e:
            q_exec("UPDATE sba_scraps SET fetch_status='failed', summary=%s WHERE id=%s",
                   (f"(research failed: {e})"[:300], scrap_id))
            return
    q_exec("UPDATE sba_scraps SET summary=%s, fetch_status='ok' WHERE id=%s", (summary, scrap_id))
    print(f"[research] scrap {scrap_id} done", flush=True)


# ----------- story regeneration -----------

STORY_SYSTEM = """You are watching a writer build an article from raw scraps. Based on the working title, thesis, and accumulated scraps, render the following sections IN THIS ORDER:

## Story so far

2-4 short paragraphs of plain prose. Tell what's emerging: what's the thread, where it's pulling, what tension is appearing. This is NOT the article — it's a midwife's view of the article that's trying to be born. Be honest: if the scraps contradict the thesis, say so.

## Living outline

A bullet tree of the sections this article would have if you wrote it today. Group scraps under sections (reference scrap IDs in brackets like [#12]). Top-level bullets are section titles in bold. Keep it loose — organizing buckets, not chapter headings.

- **Section title**
  - sub-point [#12]

## Tensions

Scraps that pull against each other. One bullet per pair: `- [#4] vs [#9] — one-line description of the contradiction and what resolving it would mean.` If nothing genuinely conflicts, write `- (none yet)`. Don't invent tension.

## Gaps

What this article still needs before it's credible. Each bullet is ONE concrete, researchable gap phrased as a task (e.g. `- Find the actual % of sites that recovered — current claim is anecdotal`). 2-5 bullets. If a scrap already covers it, don't list it.

## Emerging thesis

ONLY include this section if the material is genuinely pulling toward a stronger or different angle than the stated thesis. Give the proposed replacement thesis in 1-2 sentences, then one sentence on why. If the stated thesis is holding up, OMIT this section entirely.

Tone throughout: conversational, sharp, not corporate. Respect counterpoints — they often reveal where the article's real edge is.
"""


def build_scrap_corpus(slug: str):
    art = q_one("SELECT * FROM sba_articles WHERE slug=%s", (slug,))
    scraps = q_all("SELECT * FROM sba_scraps WHERE slug=%s ORDER BY created_at", (slug,))
    return art, scraps


def scraps_block(scraps, per_scrap_chars=1500):
    parts = []
    for s in scraps:
        tag = "[COUNTERPOINT] " if s["is_counterpoint"] else ""
        head = f"#{s['id']} {tag}({s['kind']})"
        if s["url"]:
            head += f" — {s['url']}"
        parts.append(head)
        if s["summary"]:
            parts.append(f"  Summary:\n  {s['summary']}")
        if s["content"] and s["kind"] not in ("url",):
            parts.append(f"  Content: {s['content'][:per_scrap_chars]}")
        parts.append("")
    return "\n".join(parts)


def change_note(old_story: str, new_story: str) -> str:
    if not old_story or old_story.startswith("_("):
        return "First story rendered."
    try:
        resp = llm().chat.completions.create(
            model=MODEL,
            max_tokens=250,
            messages=[
                {"role": "system", "content": (
                    "Compare two versions of a 'story so far' document about an article in progress. "
                    "In 1-3 short markdown bullets, say what MOVED: new threads, sections that appeared/"
                    "merged/died, thesis drift, new tensions. Only real changes — no fluff. "
                    "If essentially nothing changed, output exactly: - No structural change.")},
                {"role": "user", "content": f"OLD:\n{old_story[:6000]}\n\n---\n\nNEW:\n{new_story[:6000]}"},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"(change summary failed: {e})"


def regen_story(slug: str):
    art, scraps = build_scrap_corpus(slug)
    if not art:
        return
    if not scraps:
        q_exec("UPDATE sba_articles SET story_md=%s, scraps_since_story=0, updated_at=NOW() WHERE slug=%s",
               ("_(no scraps yet — drop a few and regenerate)_", slug))
        return

    user_msg = (
        f"WORKING TITLE: {art['title']}\n"
        f"THESIS: {art['thesis'] or '(not set yet)'}\n\n"
        f"SCRAPS:\n\n{scraps_block(scraps)}"
    )
    print(f"[story] regenerating for {slug} ({len(scraps)} scraps)", flush=True)
    try:
        resp = llm().chat.completions.create(
            model=GEN_MODEL,
            max_tokens=2000,
            messages=[
                {"role": "system", "content": STORY_SYSTEM},
                {"role": "user", "content": user_msg[:60000]},
            ],
        )
        story = resp.choices[0].message.content.strip()
    except Exception as e:
        story = f"_(story regeneration failed: {e})_"

    old_story = art["story_md"] or ""
    note = change_note(old_story, story) if not story.startswith("_(story regeneration failed") else "(failed regen)"

    q_exec("UPDATE sba_articles SET story_md=%s, scraps_since_story=0, updated_at=NOW() WHERE slug=%s",
           (story, slug))
    q_exec("INSERT INTO sba_story_versions (slug, story_md, change_note, scrap_count) VALUES (%s,%s,%s,%s)",
           (slug, story, note, len(scraps)))
    print(f"[story] done for {slug}", flush=True)
    _mirror(slug)


# ----------- section drafting -----------

def draft_section(section_id: int):
    sec = q_one("SELECT * FROM sba_sections WHERE id=%s", (section_id,))
    if not sec:
        return
    art, scraps = build_scrap_corpus(sec["slug"])
    print(f"[draft] section {section_id}: {sec['section_title']}", flush=True)
    sys_prompt = (
        "You are drafting ONE section of an article from the writer's raw scraps. "
        "Write 200-500 words of publishable prose in a conversational, sharp, non-corporate voice. "
        "Ground every claim in the scraps — do not invent evidence. Where a scrap backs a claim, "
        "keep the reference inline like [#12] so the writer can trace it. "
        "If the scraps are too thin for part of the section, write [NEEDS-SOURCE: what's missing] "
        "instead of bluffing. Markdown. Start with the section heading as ## heading. No preamble."
    )
    user_msg = (
        f"ARTICLE TITLE: {art['title']}\n"
        f"THESIS: {art['thesis'] or '(not set)'}\n\n"
        f"SECTION TO DRAFT: {sec['section_title']}\n"
        f"OUTLINE CONTEXT FOR THIS SECTION:\n{sec['outline_context'] or '(none)'}\n\n"
        f"ALL SCRAPS:\n\n{scraps_block(scraps)}"
    )
    try:
        resp = llm().chat.completions.create(
            model=GEN_MODEL,
            max_tokens=1200,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg[:60000]},
            ],
        )
        draft = resp.choices[0].message.content.strip()
        q_exec("UPDATE sba_sections SET draft_md=%s, status='done', updated_at=NOW() WHERE id=%s",
               (draft, section_id))
        print(f"[draft] section {section_id} done", flush=True)
    except Exception as e:
        q_exec("UPDATE sba_sections SET status='failed', draft_md=%s, updated_at=NOW() WHERE id=%s",
               (f"_(draft failed: {e})_", section_id))


# ----------- github mirror (best-effort) -----------

def _mirror(slug: str):
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import self_building_articles as app
        app._mirror_worker(slug)
    except Exception as e:
        print(f"[mirror] failed: {e}", flush=True)


# ----------- main -----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--fetch-scrap", type=int)
    ap.add_argument("--describe-scrap", type=int)
    ap.add_argument("--research-scrap", type=int)
    ap.add_argument("--draft-section", type=int)
    ap.add_argument("--regen-story", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.fetch_scrap:
        handle_fetch(args.fetch_scrap)
    if args.describe_scrap:
        describe_image(args.describe_scrap)
    if args.research_scrap:
        research_scrap(args.research_scrap)
    if args.draft_section:
        draft_section(args.draft_section)

    did_scrap_work = any([args.fetch_scrap, args.describe_scrap, args.research_scrap])

    if args.regen_story:
        regen_story(args.slug)
    elif did_scrap_work:
        a = q_one("SELECT scraps_since_story, story_threshold, auto_regen_mode FROM sba_articles WHERE slug=%s",
                  (args.slug,))
        if a and (a.get("auto_regen_mode") == "every"
                  or a["scraps_since_story"] >= (a["story_threshold"] or 4)):
            regen_story(args.slug)
        else:
            _mirror(args.slug)


if __name__ == "__main__":
    main()
