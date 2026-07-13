"""Self-Building Articles — capture working title + thesis, drop scraps, watch the story unfold."""

NAME = "Self-Building Articles"

import base64
import json
import os
import pwd
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

blueprint = Blueprint(
    "self_building_articles",
    __name__,
    template_folder="../templates/self_building_articles",
)

ROOT = Path(os.environ.get("SBA_DATA_DIR", os.path.expanduser("~/sba-data")))
ARTICLES_DIR = ROOT / "articles"
PAT_PATH = ROOT / ".git" / "github_pat"
UPLOADS_DIR = ROOT / "uploads"
LOG_DIR = Path(os.environ.get("SBA_LOG_DIR", os.path.expanduser("~/sba-data/logs")))
WORKER = Path(__file__).parent / "_self_building_articles_worker.py"
DEFAULT_REPO = ""  # set your mirror repo (owner/name) in Settings

_IS_CONSOLE = pwd.getpwuid(os.getuid()).pw_name == "console"
if _IS_CONSOLE:
    ROOT.mkdir(parents=True, exist_ok=True)
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / ".git").mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


# ----------------- DB -----------------

def conn():
    user = pwd.getpwuid(os.getuid()).pw_name
    return psycopg2.connect(host="/var/run/postgresql", user=user, database="console_db")


def init_schema():
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sba_articles (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                thesis TEXT DEFAULT '',
                status TEXT DEFAULT 'drafting',
                story_md TEXT DEFAULT '',
                scraps_since_story INT DEFAULT 0,
                story_threshold INT DEFAULT 4,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE sba_articles ADD COLUMN IF NOT EXISTS auto_regen_mode TEXT DEFAULT 'threshold';
            CREATE TABLE IF NOT EXISTS sba_scraps (
                id SERIAL PRIMARY KEY,
                slug TEXT REFERENCES sba_articles(slug) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                content TEXT DEFAULT '',
                url TEXT,
                summary TEXT,
                file_path TEXT,
                is_counterpoint BOOLEAN DEFAULT FALSE,
                fetch_status TEXT DEFAULT 'ok',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS sba_scraps_slug_idx ON sba_scraps(slug, created_at);
            CREATE TABLE IF NOT EXISTS sba_story_versions (
                id SERIAL PRIMARY KEY,
                slug TEXT REFERENCES sba_articles(slug) ON DELETE CASCADE,
                story_md TEXT DEFAULT '',
                change_note TEXT DEFAULT '',
                scrap_count INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS sba_story_versions_slug_idx ON sba_story_versions(slug, created_at DESC);
            CREATE TABLE IF NOT EXISTS sba_sections (
                id SERIAL PRIMARY KEY,
                slug TEXT REFERENCES sba_articles(slug) ON DELETE CASCADE,
                section_title TEXT NOT NULL,
                outline_context TEXT DEFAULT '',
                draft_md TEXT DEFAULT '',
                status TEXT DEFAULT 'drafting',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS sba_sections_slug_idx ON sba_sections(slug, created_at);
            CREATE TABLE IF NOT EXISTS sba_settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );
        """)


init_schema()


def q_all(sql, params=()):
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def q_one(sql, params=()):
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def q_exec(sql, params=()):
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)


# ----------------- helpers -----------------

def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_]+", "-", s)
    return s[:80] or f"article-{uuid.uuid4().hex[:6]}"


def detect_kind(content: str) -> str:
    c = content.strip()
    if re.match(r"^https?://\S+$", c):
        return "url"
    if len(c) > 280:
        return "voice"
    return "text"


def get_pat():
    try:
        return PAT_PATH.read_text().strip()
    except Exception:
        return None


def get_setting(key, default=""):
    row = q_one("SELECT value FROM sba_settings WHERE key=%s", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    q_exec("INSERT INTO sba_settings (key, value) VALUES (%s, %s) "
           "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))


def mirror_enabled():
    return get_setting("mirror_enabled", "on") == "on" and bool(mirror_repo())


def mirror_repo():
    return get_setting("mirror_repo", DEFAULT_REPO)


def gh_put(path: str, content_bytes: bytes, message: str):
    pat = get_pat()
    if not pat:
        return False, "no_pat"
    url = f"https://api.github.com/repos/{mirror_repo()}/contents/{urllib.parse.quote(path)}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "self-building-articles",
    }
    sha = None
    try:
        r = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(r) as resp:
            sha = json.loads(resp.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False, f"GET {e.code}"
    body = {"message": message, "content": base64.b64encode(content_bytes).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="PUT")
    try:
        urllib.request.urlopen(req)
        return True, None
    except urllib.error.HTTPError as e:
        return False, f"PUT {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, str(e)


def gh_delete(path: str, message: str):
    pat = get_pat()
    if not pat:
        return False, "no_pat"
    url = f"https://api.github.com/repos/{mirror_repo()}/contents/{urllib.parse.quote(path)}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "self-building-articles",
    }
    try:
        r = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(r) as resp:
            sha = json.loads(resp.read()).get("sha")
    except Exception:
        return True, None
    body = {"message": message, "sha": sha}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="DELETE")
    try:
        urllib.request.urlopen(req)
        return True, None
    except Exception as e:
        return False, str(e)


def mirror_article(slug: str):
    if not mirror_enabled():
        return
    threading.Thread(target=_mirror_worker, args=(slug,), daemon=True).start()


def _mirror_worker(slug: str):
    if not mirror_enabled():
        return
    try:
        art = q_one("SELECT * FROM sba_articles WHERE slug=%s", (slug,))
        if not art:
            return
        scraps = q_all("SELECT * FROM sba_scraps WHERE slug=%s ORDER BY created_at", (slug,))
        meta = {
            "slug": art["slug"],
            "title": art["title"],
            "thesis": art["thesis"],
            "status": art["status"],
            "created_at": art["created_at"].isoformat() if art["created_at"] else None,
            "updated_at": art["updated_at"].isoformat() if art["updated_at"] else None,
            "scrap_count": len(scraps),
        }
        gh_put(f"articles/{slug}/meta.json", json.dumps(meta, indent=2).encode(),
               f"update meta: {art['title']}")

        story = art["story_md"] or "_(no story yet — add a few scraps and regenerate)_\n"
        story_doc = f"# {art['title']}\n\n## Working thesis\n\n{art['thesis'] or '_(no thesis yet)_'}\n\n---\n\n{story}"
        gh_put(f"articles/{slug}/story.md", story_doc.encode(), f"update story: {art['title']}")

        lines = [f"# Scraps — {art['title']}\n"]
        for s in scraps:
            ts = s["created_at"].strftime("%Y-%m-%d %H:%M") if s["created_at"] else ""
            tag = "**COUNTERPOINT** · " if s["is_counterpoint"] else ""
            lines.append(f"## {tag}#{s['id']} · {s['kind']} · {ts}\n")
            if s["url"]:
                lines.append(f"**URL:** {s['url']}\n")
            if s["summary"]:
                lines.append(f"**Summary:** {s['summary']}\n")
            if s["file_path"]:
                lines.append(f"**File:** `{Path(s['file_path']).name}`\n")
            if s["content"]:
                lines.append(s["content"] + "\n")
            lines.append("\n---\n")
        gh_put(f"articles/{slug}/scraps.md", "\n".join(lines).encode(),
               f"update scraps ({len(scraps)}): {art['title']}")

        # Drafted sections
        sections = q_all("SELECT * FROM sba_sections WHERE slug=%s AND status='done' ORDER BY created_at", (slug,))
        if sections:
            dlines = [f"# Drafted sections — {art['title']}\n"]
            for sec in sections:
                dlines.append(sec["draft_md"])
                dlines.append("\n---\n")
            gh_put(f"articles/{slug}/drafts.md", "\n".join(dlines).encode(),
                   f"update drafts ({len(sections)}): {art['title']}")
    except Exception as e:
        print(f"[sba mirror] {slug}: {e}", file=sys.stderr)


# ----------------- routes -----------------

@blueprint.route("/")
def index():
    articles = q_all(
        "SELECT slug, title, thesis, status, scraps_since_story, updated_at, "
        "(SELECT COUNT(*) FROM sba_scraps s WHERE s.slug=a.slug) AS scrap_count "
        "FROM sba_articles a ORDER BY updated_at DESC"
    )
    has_pat = bool(get_pat())
    return render_template("self_building_articles/index.html", articles=articles, has_pat=has_pat)


@blueprint.route("/articles", methods=["POST"])
def create_article():
    payload = request.get_json(silent=True) or request.form
    title = (payload.get("title") or "").strip()
    thesis = (payload.get("thesis") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    slug = slugify(title)
    base = slug
    i = 2
    while q_one("SELECT slug FROM sba_articles WHERE slug=%s", (slug,)):
        slug = f"{base}-{i}"
        i += 1
    q_exec("INSERT INTO sba_articles (slug, title, thesis) VALUES (%s, %s, %s)", (slug, title, thesis))
    mirror_article(slug)
    return jsonify({"slug": slug, "url": f"/applications/self_building_articles/{slug}"})


@blueprint.route("/<slug>")
def view(slug):
    art = q_one("SELECT * FROM sba_articles WHERE slug=%s", (slug,))
    if not art:
        abort(404)
    scraps = q_all("SELECT * FROM sba_scraps WHERE slug=%s ORDER BY created_at DESC", (slug,))
    sections = q_all("SELECT * FROM sba_sections WHERE slug=%s ORDER BY created_at", (slug,))
    versions = q_all(
        "SELECT id, change_note, scrap_count, created_at FROM sba_story_versions "
        "WHERE slug=%s ORDER BY created_at DESC LIMIT 30", (slug,))
    all_articles = q_all(
        "SELECT slug, title, status, "
        "(SELECT COUNT(*) FROM sba_scraps s WHERE s.slug=a.slug) AS scrap_count "
        "FROM sba_articles a ORDER BY updated_at DESC"
    )
    return render_template(
        "self_building_articles/article.html",
        article=art, scraps=scraps, sections=sections,
        versions=versions, all_articles=all_articles,
    )


@blueprint.route("/<slug>/data")
def article_data(slug):
    art = q_one("SELECT * FROM sba_articles WHERE slug=%s", (slug,))
    if not art:
        return jsonify({"error": "not found"}), 404
    scraps = q_all(
        "SELECT id, kind, content, url, summary, file_path, is_counterpoint, "
        "fetch_status, created_at FROM sba_scraps WHERE slug=%s ORDER BY created_at DESC", (slug,))
    sections = q_all(
        "SELECT id, section_title, status, draft_md, updated_at FROM sba_sections "
        "WHERE slug=%s ORDER BY created_at", (slug,))
    versions = q_all(
        "SELECT id, change_note, scrap_count, created_at FROM sba_story_versions "
        "WHERE slug=%s ORDER BY created_at DESC LIMIT 30", (slug,))
    return jsonify({
        "article": {
            "slug": art["slug"], "title": art["title"], "thesis": art["thesis"],
            "status": art["status"], "story_md": art["story_md"],
            "scraps_since_story": art["scraps_since_story"],
            "story_threshold": art["story_threshold"],
            "auto_regen_mode": art.get("auto_regen_mode") or "threshold",
            "updated_at": art["updated_at"].isoformat() if art["updated_at"] else None,
        },
        "scraps": [{
            "id": s["id"], "kind": s["kind"], "content": s["content"], "url": s["url"],
            "summary": s["summary"], "file_path": s["file_path"],
            "is_counterpoint": s["is_counterpoint"], "fetch_status": s["fetch_status"],
            "created_at": s["created_at"].isoformat() if s["created_at"] else None,
        } for s in scraps],
        "sections": [{
            "id": x["id"], "section_title": x["section_title"], "status": x["status"],
            "draft_md": x["draft_md"],
            "updated_at": x["updated_at"].isoformat() if x["updated_at"] else None,
        } for x in sections],
        "versions": [{
            "id": v["id"], "change_note": v["change_note"], "scrap_count": v["scrap_count"],
            "created_at": v["created_at"].isoformat() if v["created_at"] else None,
        } for v in versions],
    })


@blueprint.route("/<slug>/thesis", methods=["POST"])
def update_thesis(slug):
    payload = request.get_json(silent=True) or {}
    thesis = (payload.get("thesis") or "").strip()
    title = payload.get("title")
    if title is not None:
        title = title.strip()
    if not q_one("SELECT slug FROM sba_articles WHERE slug=%s", (slug,)):
        return jsonify({"error": "not found"}), 404
    if title:
        q_exec("UPDATE sba_articles SET title=%s, thesis=%s, updated_at=NOW() WHERE slug=%s",
               (title, thesis, slug))
    else:
        q_exec("UPDATE sba_articles SET thesis=%s, updated_at=NOW() WHERE slug=%s", (thesis, slug))
    mirror_article(slug)
    return jsonify({"ok": True})


@blueprint.route("/<slug>/adopt-thesis", methods=["POST"])
def adopt_thesis(slug):
    """Adopt an emerging thesis surfaced by the story regen; old thesis kept in version log."""
    art = q_one("SELECT thesis FROM sba_articles WHERE slug=%s", (slug,))
    if not art:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    new_thesis = (payload.get("thesis") or "").strip()
    if not new_thesis:
        return jsonify({"error": "empty"}), 400
    q_exec("UPDATE sba_articles SET thesis=%s, updated_at=NOW() WHERE slug=%s", (new_thesis, slug))
    note = f"**Thesis adopted.** New: {new_thesis[:300]}\n\nPrevious: {art['thesis'][:300] or '(none)'}"
    q_exec("INSERT INTO sba_story_versions (slug, story_md, change_note, scrap_count) "
           "VALUES (%s, '', %s, (SELECT COUNT(*) FROM sba_scraps WHERE slug=%s))",
           (slug, note, slug))
    mirror_article(slug)
    return jsonify({"ok": True})


@blueprint.route("/<slug>/settings", methods=["POST"])
def article_settings(slug):
    if not q_one("SELECT slug FROM sba_articles WHERE slug=%s", (slug,)):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    mode = payload.get("auto_regen_mode")
    threshold = payload.get("story_threshold")
    if mode in ("threshold", "every"):
        q_exec("UPDATE sba_articles SET auto_regen_mode=%s WHERE slug=%s", (mode, slug))
    if isinstance(threshold, int) and 1 <= threshold <= 50:
        q_exec("UPDATE sba_articles SET story_threshold=%s WHERE slug=%s", (threshold, slug))
    return jsonify({"ok": True})


@blueprint.route("/<slug>/scraps", methods=["POST"])
def add_scrap(slug):
    if not q_one("SELECT slug FROM sba_articles WHERE slug=%s", (slug,)):
        return jsonify({"error": "not found"}), 404

    if request.files:
        return _add_file_scrap(slug)

    payload = request.get_json(silent=True) or {}
    content = (payload.get("content") or "").strip()
    is_cp = bool(payload.get("counterpoint"))
    kind_hint = payload.get("kind")

    if not content:
        return jsonify({"error": "empty"}), 400

    kind = kind_hint or detect_kind(content)
    url = None
    fetch_status = "ok"

    if kind == "url":
        url = content
        fetch_status = "pending"
        content = ""

    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sba_scraps (slug, kind, content, url, is_counterpoint, fetch_status) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (slug, kind, content, url, is_cp, fetch_status),
        )
        scrap_id = cur.fetchone()[0]

    _post_scrap_hooks(slug, scrap_id, fetch_url=(kind == "url"))
    return jsonify({"id": scrap_id, "ok": True})


def _add_file_scrap(slug):
    f = next(iter(request.files.values()))
    is_cp = request.form.get("counterpoint") == "true"
    safe_name = re.sub(r"[^\w.\-]", "_", f.filename or "upload")
    dest = UPLOADS_DIR / f"{slug}_{int(time.time())}_{safe_name}"
    f.save(dest)
    is_image = (f.mimetype or "").startswith("image/")
    kind = "image" if is_image else "file"
    caption = (request.form.get("caption") or "").strip()
    fetch_status = "pending" if is_image else "ok"

    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sba_scraps (slug, kind, content, file_path, is_counterpoint, fetch_status) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (slug, kind, caption, str(dest), is_cp, fetch_status),
        )
        scrap_id = cur.fetchone()[0]
    _post_scrap_hooks(slug, scrap_id, describe_image=is_image)
    return jsonify({"id": scrap_id, "ok": True})


def _post_scrap_hooks(slug, scrap_id, fetch_url=False, describe_image=False, research=False):
    """After any scrap insert: bump counter, decide whether to fire worker."""
    art = q_one(
        "UPDATE sba_articles SET scraps_since_story = scraps_since_story + 1, "
        "updated_at=NOW() WHERE slug=%s RETURNING scraps_since_story, story_threshold, auto_regen_mode",
        (slug,),
    )
    auto_story = art and (
        (art.get("auto_regen_mode") or "threshold") == "every"
        or art["scraps_since_story"] >= (art["story_threshold"] or 4)
    )
    needs_scrap_work = fetch_url or describe_image or research
    if needs_scrap_work or auto_story:
        spawn_worker(
            slug,
            fetch_scrap=scrap_id if fetch_url else None,
            describe_scrap=scrap_id if describe_image else None,
            research_scrap=scrap_id if research else None,
            # If scrap work is pending, let the worker decide regen after it finishes;
            # if no scrap work, fire regen directly.
            regen_story=(auto_story and not needs_scrap_work),
        )
    mirror_article(slug)


@blueprint.route("/<slug>/research", methods=["POST"])
def research_gap(slug):
    """Kick off web research on a gap; result lands as a 'research' scrap."""
    if not q_one("SELECT slug FROM sba_articles WHERE slug=%s", (slug,)):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    gap = (payload.get("gap") or "").strip()
    if not gap:
        return jsonify({"error": "empty"}), 400
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sba_scraps (slug, kind, content, fetch_status) "
            "VALUES (%s, 'research', %s, 'pending') RETURNING id",
            (slug, gap),
        )
        scrap_id = cur.fetchone()[0]
    _post_scrap_hooks(slug, scrap_id, research=True)
    return jsonify({"id": scrap_id, "ok": True})


@blueprint.route("/<slug>/scraps/<int:scrap_id>", methods=["DELETE"])
def delete_scrap(slug, scrap_id):
    q_exec("DELETE FROM sba_scraps WHERE slug=%s AND id=%s", (slug, scrap_id))
    q_exec("UPDATE sba_articles SET updated_at=NOW() WHERE slug=%s", (slug,))
    mirror_article(slug)
    return jsonify({"ok": True})


@blueprint.route("/<slug>/regenerate-story", methods=["POST"])
def regenerate_story(slug):
    if not q_one("SELECT slug FROM sba_articles WHERE slug=%s", (slug,)):
        return jsonify({"error": "not found"}), 404
    spawn_worker(slug, regen_story=True)
    return jsonify({"ok": True, "queued": True})


# ----------------- sections (drafting) -----------------

@blueprint.route("/<slug>/sections", methods=["POST"])
def create_section(slug):
    if not q_one("SELECT slug FROM sba_articles WHERE slug=%s", (slug,)):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    title = (payload.get("section_title") or "").strip()
    ctx = (payload.get("outline_context") or "").strip()
    if not title:
        return jsonify({"error": "section_title required"}), 400
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sba_sections (slug, section_title, outline_context, status) "
            "VALUES (%s, %s, %s, 'drafting') RETURNING id",
            (slug, title, ctx),
        )
        section_id = cur.fetchone()[0]
    spawn_worker(slug, draft_section=section_id)
    return jsonify({"id": section_id, "ok": True})


@blueprint.route("/<slug>/sections/<int:section_id>", methods=["POST"])
def update_section(slug, section_id):
    payload = request.get_json(silent=True) or {}
    draft = payload.get("draft_md")
    if draft is None:
        return jsonify({"error": "draft_md required"}), 400
    q_exec("UPDATE sba_sections SET draft_md=%s, updated_at=NOW() WHERE slug=%s AND id=%s",
           (draft, slug, section_id))
    mirror_article(slug)
    return jsonify({"ok": True})


@blueprint.route("/<slug>/sections/<int:section_id>/redraft", methods=["POST"])
def redraft_section(slug, section_id):
    q_exec("UPDATE sba_sections SET status='drafting', updated_at=NOW() WHERE slug=%s AND id=%s",
           (slug, section_id))
    spawn_worker(slug, draft_section=section_id)
    return jsonify({"ok": True})


@blueprint.route("/<slug>/sections/<int:section_id>", methods=["DELETE"])
def delete_section(slug, section_id):
    q_exec("DELETE FROM sba_sections WHERE slug=%s AND id=%s", (slug, section_id))
    return jsonify({"ok": True})


# ----------------- versions -----------------

@blueprint.route("/<slug>/versions/<int:version_id>")
def get_version(slug, version_id):
    v = q_one("SELECT * FROM sba_story_versions WHERE slug=%s AND id=%s", (slug, version_id))
    if not v:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": v["id"], "story_md": v["story_md"], "change_note": v["change_note"],
        "scrap_count": v["scrap_count"],
        "created_at": v["created_at"].isoformat() if v["created_at"] else None,
    })


# ----------------- delete -----------------

@blueprint.route("/<slug>/delete", methods=["POST"])
def delete_article(slug):
    art = q_one("SELECT title FROM sba_articles WHERE slug=%s", (slug,))
    if not art:
        return jsonify({"error": "not found"}), 404
    q_exec("DELETE FROM sba_articles WHERE slug=%s", (slug,))
    threading.Thread(
        target=lambda: [
            gh_delete(f"articles/{slug}/meta.json", f"delete {slug}"),
            gh_delete(f"articles/{slug}/story.md", f"delete {slug}"),
            gh_delete(f"articles/{slug}/scraps.md", f"delete {slug}"),
            gh_delete(f"articles/{slug}/drafts.md", f"delete {slug}"),
        ],
        daemon=True,
    ).start()
    return jsonify({"ok": True})


# ----------------- settings (PAT) -----------------

@blueprint.route("/settings")
def settings():
    has_pat = bool(get_pat())
    return render_template(
        "self_building_articles/settings.html",
        has_pat=has_pat,
        mirror_on=mirror_enabled(),
        repo=mirror_repo(),
    )


@blueprint.route("/settings/mirror", methods=["POST"])
def save_mirror():
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    repo = (payload.get("repo") or "").strip()
    if enabled is not None:
        set_setting("mirror_enabled", "on" if enabled else "off")
    if repo:
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            return jsonify({"error": "repo must look like owner/name"}), 400
        set_setting("mirror_repo", repo)
    return jsonify({"ok": True, "mirror_enabled": mirror_enabled(), "repo": mirror_repo()})


@blueprint.route("/settings/pat", methods=["POST"])
def save_pat():
    payload = request.get_json(silent=True) or request.form
    pat = (payload.get("pat") or "").strip()
    if not pat:
        return jsonify({"error": "empty"}), 400
    PAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAT_PATH.write_text(pat)
    PAT_PATH.chmod(0o600)
    return jsonify({"ok": True})


# ----------------- worker spawn -----------------

def spawn_worker(slug, fetch_scrap=None, describe_scrap=None, research_scrap=None,
                 draft_section=None, regen_story=False):
    args = [sys.executable, str(WORKER), "--slug", slug]
    if fetch_scrap:
        args += ["--fetch-scrap", str(fetch_scrap)]
    if describe_scrap:
        args += ["--describe-scrap", str(describe_scrap)]
    if research_scrap:
        args += ["--research-scrap", str(research_scrap)]
    if draft_section:
        args += ["--draft-section", str(draft_section)]
    if regen_story:
        args += ["--regen-story"]
    try:
        log_fh = open(LOG_DIR / f"{slug}.log", "a")
    except Exception:
        log_fh = subprocess.DEVNULL
    subprocess.Popen(
        args, stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
