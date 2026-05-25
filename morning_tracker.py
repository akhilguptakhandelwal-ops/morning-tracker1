"""
Morning Intelligence Tracker
âââââââââââââââââââââââââââââ
Scrapes YouTube + websites â Gemini AI summary â Google Apps Script email delivery
No SMTP. No app passwords. Pure Google.
"""

import os
import re
import json
import sqlite3
import hashlib
import logging
import textwrap
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# âââââââââââââââââââââââââââââââââââââââââââââ
# Logging
# âââââââââââââââââââââââââââââââââââââââââââââ
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = os.environ.get("TRACKER_DB", "tracker.db")

# âââââââââââââââââââââââââââââââââââââââââââââ
# 1. DATABASE LAYER
# âââââââââââââââââââââââââââââââââââââââââââââ

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                type            TEXT    NOT NULL CHECK(type IN ('youtube','website')),
                name            TEXT    NOT NULL,
                identifier      TEXT    NOT NULL UNIQUE,
                last_scraped_id TEXT    DEFAULT NULL,
                active          INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS recipients (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT    NOT NULL UNIQUE
            );
        """)

        sources_seed = [
            ("youtube", "Fintex Pro",
             "https://www.youtube.com/feeds/videos.xml?channel_id=UC6sN_Mv_wG76_0ZfPym80mQ"),
            ("website", "Income Tax India",
             "https://incometaxindia.gov.in"),
        ]
        for src_type, name, identifier in sources_seed:
            conn.execute(
                "INSERT OR IGNORE INTO sources (type, name, identifier) VALUES (?,?,?)",
                (src_type, name, identifier),
            )

        for email in ("gupta_akhil@ymail.com"):
            conn.execute(
                "INSERT OR IGNORE INTO recipients (email) VALUES (?)", (email,)
            )

        conn.commit()
    log.info("Database initialised â %s", DB_PATH)


def get_sources(conn):
    return conn.execute("SELECT * FROM sources WHERE active=1").fetchall()


def get_recipients(conn) -> list[str]:
    return [r["email"] for r in conn.execute("SELECT email FROM recipients").fetchall()]


def update_last_scraped(conn, source_id: int, scraped_id: str) -> None:
    conn.execute("UPDATE sources SET last_scraped_id=? WHERE id=?", (scraped_id, source_id))
    conn.commit()


# âââââââââââââââââââââââââââââââââââââââââââââ
# 2. INGESTION LAYER
# âââââââââââââââââââââââââââââââââââââââââââââ

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _get(url: str, timeout: int = 20) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        log.error("HTTP error fetching %s: %s", url, exc)
        return None


def scrape_youtube(identifier: str) -> Optional[dict]:
    resp = _get(identifier)
    if not resp:
        return None

    soup = BeautifulSoup(resp.content, "xml")
    entry = soup.find("entry")
    if not entry:
        log.warning("No entries in YouTube feed: %s", identifier)
        return None

    video_id_tag = entry.find("videoId")
    if video_id_tag:
        video_id = video_id_tag.text.strip()
    else:
        id_tag = entry.find("id")
        raw = id_tag.text if id_tag else ""
        video_id = raw.split(":")[-1] if ":" in raw else raw

    title_tag = entry.find("title")
    title = title_tag.text.strip() if title_tag else "Untitled"
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    raw_text = ""
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        raw_text = " ".join(c["text"] for c in transcript_list)
        log.info("YouTube '%s' â transcript fetched (%d chars)", title, len(raw_text))
    except (TranscriptsDisabled, NoTranscriptFound):
        log.warning("No transcript for video %s", video_id)
        raw_text = f"[No transcript available for: {title}]"
    except Exception as exc:
        log.error("Transcript error for %s: %s", video_id, exc)
        raw_text = f"[Transcript extraction failed: {exc}]"

    return {"id": video_id, "title": title, "url": video_url, "raw_text": raw_text}


def scrape_website(identifier: str) -> Optional[dict]:
    resp = _get(identifier)
    if not resp:
        return None

    soup = BeautifulSoup(resp.content, "html.parser")
    for tag in soup(["script","style","noscript","nav","footer","header","aside","form","svg","iframe"]):
        tag.decompose()

    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag else identifier

    blocks = []
    for tag in soup.find_all(["h1","h2","h3","h4","p","li","td","th","article","section"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) > 40:
            blocks.append(text)

    raw_text = re.sub(r"\s{3,}", "\n", "\n".join(blocks[:120]))
    content_id = hashlib.md5(raw_text[:500].encode()).hexdigest()[:12]

    log.info("Website '%s' â %d chars scraped", title, len(raw_text))
    return {"id": content_id, "title": title, "url": identifier, "raw_text": raw_text}


# âââââââââââââââââââââââââââââââââââââââââââââ
# 3. AI PROCESSING LAYER (Gemini)
# âââââââââââââââââââââââââââââââââââââââââââââ

SYSTEM_INSTRUCTION = textwrap.dedent("""\
    You are an expert intelligence analyst preparing a concise daily brief for a busy finance professional.

    Given raw scraped content from a YouTube video transcript or a website, you must:
    1. Identify the core message, announcement, or update being communicated.
    2. Extract the 3â5 most important points or takeaways.
    3. Flag any actionable items, deadlines, or regulatory changes.
    4. Output a structured summary using this exact HTML format (no markdown fences):

    <div class="ai-summary">
      <h3>ð Core Message</h3>
      <p>[one sentence describing the main point]</p>
      <h3>ð Key Takeaways</h3>
      <ul>
        <li>[takeaway 1]</li>
        <li>[takeaway 2]</li>
        <li>[takeaway 3]</li>
      </ul>
      <h3>â¡ Action Items / Alerts</h3>
      <p>[deadlines, filings, or must-do items â or "None identified" if absent]</p>
    </div>

    Keep the language professional, concise, and relevant to Indian finance/taxation context where applicable.
    Never invent information not present in the source content.
""")


def summarise_with_gemini(source_name: str, raw_text: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY not set â skipping AI summary")
        return '<div class="ai-summary"><p><em>â ï¸ AI summary unavailable â GEMINI_API_KEY not configured.</em></p></div>'

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = f"Source: {source_name}\n\nContent:\n{raw_text[:15000]}"
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"system_instruction": SYSTEM_INSTRUCTION},
        )
        summary_html = response.text.strip()
        log.info("Gemini summary done for '%s' (%d chars)", source_name, len(summary_html))
        return summary_html
    except Exception as exc:
        log.error("Gemini error for '%s': %s", source_name, exc)
        return f'<div class="ai-summary"><p><em>â ï¸ Gemini failed: {exc}</em></p></div>'


# âââââââââââââââââââââââââââââââââââââââââââââ
# 4. EMAIL BUILDER
# âââââââââââââââââââââââââââââââââââââââââââââ

EMAIL_CSS = """
<style>
  body{font-family:'Segoe UI',Arial,sans-serif;background:#f0f4f8;margin:0;padding:0}
  .wrapper{max-width:680px;margin:30px auto;background:#fff;border-radius:12px;
           overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10)}
  .header{background:linear-gradient(135deg,#1a237e,#283593);padding:32px 36px;color:#fff}
  .header h1{margin:0 0 4px;font-size:22px;letter-spacing:.5px}
  .header p{margin:0;opacity:.8;font-size:13px}
  .card{border:1px solid #e3e8f0;border-radius:10px;margin:24px 28px;padding:24px;background:#fafbff}
  .card-header{display:flex;align-items:center;gap:12px;margin-bottom:16px;
               border-bottom:2px solid #e3e8f0;padding-bottom:12px}
  .badge{background:#1a237e;color:#fff;border-radius:6px;padding:3px 10px;
         font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px}
  .badge.website{background:#00695c}
  .card-title{font-size:16px;font-weight:700;color:#1a237e;margin:0}
  .source-url{font-size:12px;color:#888;margin:0}
  .ai-summary h3{color:#283593;font-size:14px;margin:16px 0 6px}
  .ai-summary p,.ai-summary li{color:#333;font-size:14px;line-height:1.7}
  .ai-summary ul{padding-left:20px;margin:6px 0}
  .footer{text-align:center;padding:20px;font-size:12px;color:#aaa;border-top:1px solid #f0f0f0}
  a{color:#1a237e}
</style>
"""


def build_html_email(reports: list[dict], run_date: str) -> str:
    cards = ""
    for r in reports:
        badge_cls = "badge website" if r["source_type"] == "website" else "badge"
        badge_lbl = "YouTube" if r["source_type"] == "youtube" else "Website"
        cards += f"""
        <div class="card">
          <div class="card-header">
            <span class="{badge_cls}">{badge_lbl}</span>
            <div>
              <p class="card-title">{r['source_name']}</p>
              <p class="source-url"><a href="{r['url']}">{r['url']}</a></p>
            </div>
          </div>
          <p style="color:#555;font-size:13px;margin:0 0 12px">
            <strong>ð Latest:</strong> {r['content_title']}
          </p>
          {r['summary_html']}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">{EMAIL_CSS}</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>ð Morning Intelligence Digest</h1>
    <p>{run_date} Â· Auto-generated by Morning Intelligence Tracker</p>
  </div>
  {cards}
  <div class="footer">
    You are receiving this because you are subscribed to the Morning Intelligence Tracker.<br>
    Built with â¥ by Akhil Gupta Â· Plant Finance Head Â· Safari Manufacturing Ltd.
  </div>
</div>
</body>
</html>"""


# âââââââââââââââââââââââââââââââââââââââââââââ
# 5. DELIVERY LAYER â Google Apps Script
# âââââââââââââââââââââââââââââââââââââââââââââ

def send_via_apps_script(recipients: list[str], html_body: str, run_date: str) -> bool:
    """
    POST the email payload to a Google Apps Script Web App.
    The Apps Script handles actual Gmail sending â no SMTP, no passwords.
    Set GAS_WEBHOOK_URL as a GitHub Secret / environment variable.
    """
    webhook_url = os.environ.get("GAS_WEBHOOK_URL")
    if not webhook_url:
        log.error("GAS_WEBHOOK_URL not set â cannot send email.")
        return False

    payload = {
        "subject": f"ð Morning Intelligence Digest â {run_date}",
        "htmlBody": html_body,
        "recipients": recipients,
        "token": os.environ.get("GAS_SECRET_TOKEN", ""),
    }

    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code == 200:
            log.info("â Email dispatched via Apps Script to: %s", ", ".join(recipients))
            return True
        else:
            log.error("Apps Script returned %s: %s", resp.status_code, resp.text[:300])
            return False
    except Exception as exc:
        log.error("Apps Script POST failed: %s", exc)
        return False


# âââââââââââââââââââââââââââââââââââââââââââââ
# 6. ORCHESTRATOR
# âââââââââââââââââââââââââââââââââââââââââââââ

def run() -> None:
    log.info("ââââââââââââââââââââââââââââââââââââââ")
    log.info("  Morning Intelligence Tracker â START")
    log.info("ââââââââââââââââââââââââââââââââââââââ")

    init_db()

    run_date = datetime.now().strftime("%A, %d %B %Y")
    reports: list[dict] = []

    SCRAPERS = {"youtube": scrape_youtube, "website": scrape_website}

    with get_connection() as conn:
        sources    = get_sources(conn)
        recipients = get_recipients(conn)

        if not recipients:
            log.warning("No recipients configured â aborting.")
            return

        for source in sources:
            src_id, src_type, src_name = source["id"], source["type"], source["name"]
            src_url, last_id = source["identifier"], source["last_scraped_id"]

            log.info("ââ Processing [%s] %s", src_type.upper(), src_name)

            scraper = SCRAPERS.get(src_type)
            if not scraper:
                log.warning("Unknown type '%s' â skipping.", src_type)
                continue

            result = scraper(src_url)
            if not result:
                log.warning("Scraping failed for '%s' â skipping.", src_name)
                continue

            content_id = result["id"]

            if content_id == last_id:
                log.info("No new content for '%s' â skipping.", src_name)
                continue

            summary_html = summarise_with_gemini(src_name, result["raw_text"])

            reports.append({
                "source_type":   src_type,
                "source_name":   src_name,
                "url":           result["url"],
                "content_title": result["title"],
                "summary_html":  summary_html,
            })

            update_last_scraped(conn, src_id, content_id)
            log.info("State updated for '%s' â %s", src_name, content_id)

    if not reports:
        log.info("No new content across all sources â no email sent.")
        return

    log.info("Composing digest with %d report(s)â¦", len(reports))
    html_body = build_html_email(reports, run_date)
    send_via_apps_script(recipients, html_body, run_date)

    log.info("ââââââââââââââââââââââââââââââââââââââ")
    log.info("  Morning Intelligence Tracker â DONE ")
    log.info("ââââââââââââââââââââââââââââââââââââââ")


if __name__ == "__main__":
    run()
