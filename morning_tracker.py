"""
Morning Intelligence Tracker

Scrapes YouTube and websites, summarises with Gemini, and sends the
result via a Google Apps Script webhook.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import textwrap
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = os.environ.get("TRACKER_DB", "tracker.db")
LOG_PATH = os.environ.get("SENT_LOG", "sent_log.json")
EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('youtube','website')),
                name TEXT NOT NULL,
                identifier TEXT NOT NULL UNIQUE,
                last_scraped_id TEXT DEFAULT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE
            );
            """
        )

        sources_seed = [
            (
                "youtube",
                "Aishwarya Srinivasan - AI with Aish",
                "https://www.youtube.com/feeds/videos.xml?channel_id=UCzd4ZN716evEjtbJERBMTfg",
            ),
            ("website", "Taxguru", "https://taxguru.in"),
        ]
        for src_type, name, identifier in sources_seed:
            conn.execute(
                "INSERT OR IGNORE INTO sources (type, name, identifier) VALUES (?,?,?)",
                (src_type, name, identifier),
            )

        for email in ("gupta_akhil@ymail.com",):
            conn.execute(
                "INSERT OR IGNORE INTO recipients (email) VALUES (?)",
                (email,),
            )

        # Remove bad rows created by the earlier string-iteration bug.
        conn.execute("DELETE FROM recipients WHERE instr(email, '@') = 0")
        conn.commit()

    log.info("Database initialised -> %s", DB_PATH)


def get_sources(conn):
    return conn.execute("SELECT * FROM sources WHERE active=1").fetchall()


def get_recipients(conn):
    return [r["email"] for r in conn.execute("SELECT email FROM recipients").fetchall()]


def update_last_scraped(conn, source_id, scraped_id):
    conn.execute(
        "UPDATE sources SET last_scraped_id=? WHERE id=?",
        (scraped_id, source_id),
    )
    conn.commit()


def load_sent_log():
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_sent_log(entry):
    data = load_sent_log()
    data.insert(0, entry)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data[:30], f, indent=2)
    log.info("Email log saved -> %s", LOG_PATH)


def normalise_recipients(recipients):
    cleaned = []
    invalid = []
    for recipient in recipients:
        email = (recipient or "").strip()
        if not email:
            continue
        if EMAIL_RE.fullmatch(email):
            if email not in cleaned:
                cleaned.append(email)
        else:
            invalid.append(email)
    if invalid:
        log.warning("Ignoring invalid recipients: %s", ", ".join(invalid))
    return cleaned


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _get(url, timeout=20):
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        log.error("HTTP error fetching %s: %s", url, exc)
        return None


def scrape_youtube(identifier):
    response = _get(identifier)
    if not response:
        return None

    soup = BeautifulSoup(response.content, "xml")
    entry = soup.find("entry")
    if not entry:
        return None

    vid_tag = entry.find("videoId")
    video_id = (
        vid_tag.text.strip()
        if vid_tag
        else (entry.find("id").text.split(":")[-1] if entry.find("id") else "")
    )
    title = entry.find("title").text.strip() if entry.find("title") else "Untitled"
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        raw_text = " ".join(
            chunk["text"] for chunk in YouTubeTranscriptApi.get_transcript(video_id)
        )
        log.info("YouTube '%s' transcript (%d chars)", title, len(raw_text))
    except (TranscriptsDisabled, NoTranscriptFound):
        raw_text = f"[No transcript available for: {title}]"
    except Exception as exc:
        raw_text = f"[Transcript failed: {exc}]"

    return {"id": video_id, "title": title, "url": url, "raw_text": raw_text}


def scrape_website(identifier):
    response = _get(identifier)
    if not response:
        return None

    soup = BeautifulSoup(response.content, "html.parser")
    for tag in soup(
        ["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"]
    ):
        tag.decompose()

    title = soup.find("title").text.strip() if soup.find("title") else identifier
    blocks = [
        tag.get_text(" ", strip=True)
        for tag in soup.find_all(
            ["h1", "h2", "h3", "h4", "p", "li", "td", "th", "article", "section"]
        )
        if len(tag.get_text(" ", strip=True)) > 40
    ]
    raw_text = re.sub(r"\s{3,}", "\n", "\n".join(blocks[:120]))
    content_id = hashlib.md5(raw_text[:500].encode()).hexdigest()[:12]
    log.info("Website '%s' scraped (%d chars)", title, len(raw_text))
    return {"id": content_id, "title": title, "url": identifier, "raw_text": raw_text}


SYSTEM_INSTRUCTION = textwrap.dedent(
    """\
    You are an expert intelligence analyst preparing a concise daily brief for a busy finance professional.
    Given raw scraped content from a YouTube video transcript or a website, output a structured summary in this exact HTML format (no markdown fences):
    <div class="ai-summary">
      <h3>Core Message</h3><p>[one sentence]</p>
      <h3>Key Takeaways</h3><ul><li>[point 1]</li><li>[point 2]</li><li>[point 3]</li></ul>
      <h3>Action Items / Alerts</h3><p>[deadlines or "None identified"]</p>
    </div>
    Be concise, professional, relevant to Indian finance/taxation. Never invent information.
    """
)


def summarise_with_gemini(source_name, raw_text):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured.")

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Source: {source_name}\n\nContent:\n{raw_text[:15000]}",
            config={"system_instruction": SYSTEM_INSTRUCTION},
        )
        log.info("Gemini completed for '%s'", source_name)
        return (response.text or "").strip()
    except Exception as exc:
        log.error("Gemini error for '%s': %s", source_name, exc)
        return f'<div class="ai-summary"><p><em>Gemini failed: {exc}</em></p></div>'


EMAIL_CSS = """<style>
  body{font-family:'Segoe UI',Arial,sans-serif;background:#f0f4f8;margin:0;padding:0}
  .wrapper{max-width:680px;margin:30px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10)}
  .header{background:linear-gradient(135deg,#1a237e,#283593);padding:32px 36px;color:#fff}
  .header h1{margin:0 0 4px;font-size:22px;letter-spacing:.5px}
  .header p{margin:0;opacity:.8;font-size:13px}
  .card{border:1px solid #e3e8f0;border-radius:10px;margin:24px 28px;padding:24px;background:#fafbff}
  .card-header{display:flex;align-items:center;gap:12px;margin-bottom:16px;border-bottom:2px solid #e3e8f0;padding-bottom:12px}
  .badge{background:#1a237e;color:#fff;border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px}
  .badge.website{background:#00695c}
  .ai-summary h3{color:#283593;font-size:14px;margin:16px 0 6px}
  .ai-summary p,.ai-summary li{color:#333;font-size:14px;line-height:1.7}
  .ai-summary ul{padding-left:20px;margin:6px 0}
  .no-update{background:#f5f7fa;border:1px dashed #ccc;border-radius:10px;margin:16px 28px;padding:18px 24px;color:#888;font-size:14px;text-align:center}
  .footer{text-align:center;padding:20px;font-size:12px;color:#aaa;border-top:1px solid #f0f0f0}
  a{color:#1a237e}
</style>"""


def build_html_email(reports, skipped, run_date):
    cards = ""

    for report in reports:
        badge = "badge website" if report["source_type"] == "website" else "badge"
        label = "Website" if report["source_type"] == "website" else "YouTube"
        cards += f"""<div class="card">
          <div class="card-header">
            <span class="{badge}">{label}</span>
            <div><p style="font-size:16px;font-weight:700;color:#1a237e;margin:0">{report['source_name']}</p>
            <p style="font-size:12px;color:#888;margin:0"><a href="{report['url']}">{report['url']}</a></p></div>
          </div>
          <p style="color:#555;font-size:13px;margin:0 0 12px"><strong>Latest:</strong> {report['content_title']}</p>
          {report['summary_html']}</div>"""

    for item in skipped:
        cards += f"""<div class="no-update"><strong>{item['name']}</strong> - No new updates since last digest.
          <br><small><a href="{item['url']}">{item['url']}</a></small></div>"""

    if not reports and not skipped:
        cards = '<div class="no-update">All sources encountered errors today. No summaries generated.</div>'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">{EMAIL_CSS}</head><body>
<div class="wrapper">
  <div class="header"><h1>Morning Intelligence Digest</h1><p>{run_date} | Morning Intelligence Tracker</p></div>
  {cards}
  <div class="footer">Built by Morning Intelligence Tracker</div>
</div></body></html>"""


def send_via_apps_script(recipients, html_body, subject):
    webhook_url = os.environ.get("GAS_WEBHOOK_URL")
    if not webhook_url:
        log.error("GAS_WEBHOOK_URL not set.")
        return False

    payload = {
        "subject": subject,
        "htmlBody": html_body,
        "recipients": recipients,
        "token": os.environ.get("GAS_SECRET_TOKEN", ""),
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=30)
        body_preview = (response.text or "")[:300]

        if response.status_code != 200:
            log.error("Apps Script %s: %s", response.status_code, body_preview)
            return False

        try:
            response_payload = response.json()
        except ValueError:
            response_payload = None

        if isinstance(response_payload, dict) and response_payload.get("ok") is False:
            log.error(
                "Apps Script rejected request: %s",
                response_payload.get("error") or body_preview,
            )
            return False

        log.info("Apps Script accepted request for: %s", ", ".join(recipients))
        return True
    except Exception as exc:
        log.error("Apps Script failed: %s", exc)
        return False


def run():
    log.info("========================================")
    log.info("  Morning Intelligence Tracker - START")
    log.info("========================================")

    init_db()
    now = datetime.now()
    run_date = now.strftime("%A, %d %B %Y")
    reports = []
    skipped = []
    errors = []

    scrapers = {"youtube": scrape_youtube, "website": scrape_website}

    with get_connection() as conn:
        sources = get_sources(conn)
        recipients = normalise_recipients(get_recipients(conn))
        if not recipients:
            raise RuntimeError("No valid recipients configured.")

        for source in sources:
            src_id = source["id"]
            src_type = source["type"]
            src_name = source["name"]
            src_url = source["identifier"]
            last_id = source["last_scraped_id"]
            log.info("Processing [%s] %s", src_type.upper(), src_name)

            scraper = scrapers.get(src_type)
            if not scraper:
                continue

            result = scraper(src_url)
            if not result:
                errors.append(src_name)
                continue

            content_id = result["id"]
            if content_id == last_id:
                log.info("No new content for '%s' - adding no-update card.", src_name)
                skipped.append({"name": src_name, "url": result["url"]})
                continue

            summary_html = summarise_with_gemini(src_name, result["raw_text"])
            reports.append(
                {
                    "source_type": src_type,
                    "source_name": src_name,
                    "url": result["url"],
                    "content_title": result["title"],
                    "summary_html": summary_html,
                }
            )
            update_last_scraped(conn, src_id, content_id)

    log.info(
        "Sending digest - %d new, %d no-update, %d errors",
        len(reports),
        len(skipped),
        len(errors),
    )
    subject = f"Morning Intelligence Digest - {run_date}"
    html_body = build_html_email(reports, skipped, run_date)
    success = send_via_apps_script(recipients, html_body, subject)

    save_sent_log(
        {
            "timestamp": now.isoformat(),
            "date": run_date,
            "subject": subject,
            "recipients": recipients,
            "new_sources": [r["source_name"] for r in reports],
            "no_update": [s["name"] for s in skipped],
            "errors": errors,
            "sent": success,
            "report_count": len(reports),
            "html_body": html_body,
        }
    )

    if not success:
        raise RuntimeError(
            "Digest delivery failed. Check GAS_WEBHOOK_URL, GAS_SECRET_TOKEN, and the Apps Script deployment."
        )

    log.info("========================================")
    log.info("  Morning Intelligence Tracker - DONE")
    log.info("========================================")


if __name__ == "__main__":
    run()
