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
import time
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
                category TEXT NOT NULL DEFAULT 'General',
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
            ("youtube", "FinTaxPro",
             "https://www.youtube.com/@fintaxpro/videos", "Accounts and Taxation"),
            ("youtube", "Aishwarya Srinivasan - AI with Aish",
             "https://www.youtube.com/feeds/videos.xml?channel_id=UCzd4ZN716evEjtbJERBMTfg", "AI"),
            ("youtube", "GSTPLATFORM",
             "https://www.youtube.com/@gstplatform/videos", "Accounts and Taxation"),
        ]
        for source_seed in sources_seed:
            if len(source_seed) == 4:
                src_type, name, identifier, category = source_seed
            elif len(source_seed) == 3:
                src_type, name, identifier = source_seed
                category = "General"
            else:
                raise ValueError(f"Invalid source seed entry: {source_seed!r}")
            conn.execute(
                "INSERT OR IGNORE INTO sources (type, name, identifier, category) VALUES (?,?,?,?)",
                (src_type, name, identifier, category),
            )

        for email in ("gupta_akhil@ymail.com", "akhil.gupta@safari.in"):
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


def _get(url, timeout=20, retries=3, retry_delay=5):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response, None
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < retries:
                log.warning(
                    "HTTP error fetching %s (attempt %d/%d): %s",
                    url,
                    attempt,
                    retries,
                    exc,
                )
                time.sleep(retry_delay)
            else:
                log.error("HTTP error fetching %s: %s", url, exc)
    return None, last_error


def get_youtube_fallback_text(entry, url, title):
    description = ""

    for tag_name in ("media:description", "description", "summary"):
        tag = entry.find(tag_name)
        if tag and tag.text:
            description = tag.text.strip()
            if description:
                break

    if not description:
        response, _ = _get(url)
        if response:
            page = BeautifulSoup(response.text, "html.parser")
            meta = page.find("meta", attrs={"name": "description"}) or page.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta and meta.get("content"):
                description = meta["content"].strip()

    if description:
        return (
            f"Video title: {title}\n"
            f"Source: YouTube metadata fallback because transcript was unavailable.\n"
            f"Description:\n{description}"
        )

    return (
        f"Video title: {title}\n"
        "Source: YouTube metadata fallback because transcript was unavailable.\n"
        "Description unavailable."
    )


def fetch_youtube_transcript_text(video_id):
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(chunk["text"] for chunk in transcript)

    transcript_api = YouTubeTranscriptApi()
    fetched_transcript = transcript_api.fetch(video_id, languages=["en"])

    snippets = []
    for snippet in fetched_transcript:
        text = getattr(snippet, "text", None)
        if text:
            snippets.append(text)

    if snippets:
        return " ".join(snippets)

    raw_data = fetched_transcript.to_raw_data()
    return " ".join(chunk["text"] for chunk in raw_data if chunk.get("text"))


def normalise_youtube_channel_page_url(identifier):
    if "youtube.com/feeds/videos.xml?channel_id=" in identifier:
        channel_id = identifier.split("channel_id=", 1)[-1].strip()
        return f"https://www.youtube.com/channel/{channel_id}/videos"

    if "youtube.com/watch?v=" in identifier:
        return identifier

    if identifier.rstrip("/").endswith("/videos"):
        return identifier

    return f"{identifier.rstrip('/')}/videos"


def resolve_youtube_feed_url(identifier):
    if "youtube.com/feeds/videos.xml?channel_id=" in identifier:
        return identifier, None

    response, error = _get(identifier)
    if not response:
        return None, f"YouTube channel page fetch failed: {error or 'Unknown error'}"

    html = response.text
    patterns = [
        r'"channelId":"(UC[a-zA-Z0-9_-]{20,})"',
        r'"externalId":"(UC[a-zA-Z0-9_-]{20,})"',
        r'"browseId":"(UC[a-zA-Z0-9_-]{20,})"',
        r'itemprop="channelId"\s+content="(UC[a-zA-Z0-9_-]{20,})"',
        r'https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{20,})',
        r'https://www\.youtube\.com/feeds/videos\.xml\?channel_id=(UC[a-zA-Z0-9_-]{20,})',
    ]
    channel_id = None
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            channel_id = match.group(1)
            break

    if not channel_id:
        return None, "Could not resolve YouTube channel ID from channel page."

    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", None


def get_video_metadata_from_page(url, fallback_title):
    response, error = _get(url, retries=2, retry_delay=2)
    if not response:
        return fallback_title, "", error

    soup = BeautifulSoup(response.text, "html.parser")
    meta_title = soup.find("meta", attrs={"property": "og:title"})
    meta_description = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )

    title = (
        meta_title.get("content", "").strip()
        if meta_title and meta_title.get("content")
        else fallback_title
    )
    description = (
        meta_description.get("content", "").strip()
        if meta_description and meta_description.get("content")
        else ""
    )
    return title or fallback_title, description, None


def scrape_latest_video_from_channel_page(page_url, fallback_title="Latest YouTube video"):
    response, error = _get(page_url, retries=2, retry_delay=2)
    if not response:
        return {"error": f"YouTube channel page fetch failed: {error or 'Unknown error'}"}

    html = response.text
    video_ids = []
    for match in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
        if match not in video_ids:
            video_ids.append(match)

    if not video_ids:
        return {"error": "YouTube channel page returned no latest video IDs."}

    video_id = video_ids[0]
    url = f"https://www.youtube.com/watch?v={video_id}"
    title, description, metadata_error = get_video_metadata_from_page(url, fallback_title)
    if metadata_error:
        log.warning("YouTube metadata page fetch failed for %s: %s", url, metadata_error)

    raw_text = ""
    try:
        raw_text = fetch_youtube_transcript_text(video_id)
        log.info("YouTube '%s' transcript (%d chars)", title, len(raw_text))
    except (TranscriptsDisabled, NoTranscriptFound):
        raw_text = (
            f"Video title: {title}\n"
            "Source: YouTube channel page fallback because feed/transcript was unavailable.\n"
            f"Description:\n{description or 'Description unavailable.'}"
        )
        log.info("YouTube '%s' using channel-page metadata fallback", title)
    except Exception as exc:
        raw_text = (
            f"Video title: {title}\n"
            "Source: YouTube channel page fallback because transcript was unavailable.\n"
            f"Description:\n{description or 'Description unavailable.'}"
        )
        log.warning("YouTube '%s' transcript failed, using channel-page fallback: %s", title, exc)

    return {"id": video_id, "title": title, "url": url, "raw_text": raw_text}


def scrape_youtube(identifier):
    page_url = normalise_youtube_channel_page_url(identifier)
    feed_url, resolve_error = resolve_youtube_feed_url(identifier)
    response = None
    error = None
    if feed_url:
        response, error = _get(feed_url)
    else:
        log.warning("YouTube feed resolution failed for %s: %s", identifier, resolve_error)

    if not response:
        log.warning(
            "Falling back to YouTube channel page scrape for %s because feed fetch failed.",
            identifier,
        )
        return scrape_latest_video_from_channel_page(page_url)

    soup = BeautifulSoup(response.content, "xml")
    entry = soup.find("entry")
    if not entry:
        log.warning(
            "YouTube feed returned no latest entry for %s. Falling back to channel page.",
            identifier,
        )
        return scrape_latest_video_from_channel_page(page_url)

    vid_tag = entry.find("videoId")
    video_id = (
        vid_tag.text.strip()
        if vid_tag
        else (entry.find("id").text.split(":")[-1] if entry.find("id") else "")
    )
    title = entry.find("title").text.strip() if entry.find("title") else "Untitled"
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        raw_text = fetch_youtube_transcript_text(video_id)
        log.info("YouTube '%s' transcript (%d chars)", title, len(raw_text))
    except (TranscriptsDisabled, NoTranscriptFound):
        raw_text = get_youtube_fallback_text(entry, url, title)
        log.info("YouTube '%s' using metadata fallback", title)
    except Exception as exc:
        raw_text = get_youtube_fallback_text(entry, url, title)
        log.warning("YouTube '%s' transcript failed, using metadata fallback: %s", title, exc)

    return {"id": video_id, "title": title, "url": url, "raw_text": raw_text}


def scrape_website(identifier):
    response, error = _get(identifier)
    if not response:
        return {"error": f"Website fetch failed: {error or 'Unknown error'}"}

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
    if not raw_text.strip():
        return {"error": "Website content extraction returned no usable text."}
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

    last_error = None
    for attempt in range(1, 4):
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Source: {source_name}\n\nContent:\n{raw_text[:15000]}",
                config={"system_instruction": SYSTEM_INSTRUCTION},
            )
            log.info("Gemini completed for '%s'", source_name)
            return (response.text or "").strip(), None
        except Exception as exc:
            last_error = str(exc)
            if attempt < 3:
                log.warning(
                    "Gemini error for '%s' (attempt %d/3): %s",
                    source_name,
                    attempt,
                    exc,
                )
                time.sleep(5 * attempt)
            else:
                log.error("Gemini error for '%s': %s", source_name, exc)
    return (
        f'<div class="ai-summary"><p><em>Gemini failed: {last_error}</em></p></div>',
        f"Gemini failed: {last_error}",
    )


EMAIL_CSS = """<style>
  body{font-family:Segoe UI,Arial,sans-serif;background:#eef2f7;margin:0;padding:0;color:#152033}
  .wrapper{max-width:720px;margin:24px auto;background:#ffffff;border:1px solid #dbe3ef;border-radius:18px;overflow:hidden}
  .preheader{display:none!important;visibility:hidden;opacity:0;color:transparent;height:0;width:0;overflow:hidden;mso-hide:all}
  .header{background:#1f3c88;padding:28px 32px;color:#ffffff}
  .header h1{margin:0 0 6px;font-size:30px;line-height:1.2;font-weight:700}
  .header p{margin:0;font-size:15px;line-height:1.5;color:#d8e3ff}
  .intro{padding:18px 32px 0;font-size:15px;line-height:1.7;color:#41506a}
  .card{border:1px solid #dbe3ef;border-radius:16px;margin:20px 24px;padding:24px;background:#ffffff}
  .card-header{margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid #e6edf6}
  .badge{display:inline-block;background:#1f3c88;color:#ffffff;border-radius:999px;padding:5px 12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
  .badge.website{background:#0f766e}
  .source-name{font-size:24px;line-height:1.25;font-weight:700;color:#1a2850;margin:0 0 6px}
  .source-url{font-size:13px;line-height:1.5;color:#5f6f89;word-break:break-all}
  .latest-label{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6d7c95;margin:0 0 8px}
  .latest-link{font-size:19px;line-height:1.45;color:#163b85;text-decoration:none;font-weight:600}
  .latest-link:hover{text-decoration:underline}
  .open-link{display:inline-block;margin-top:12px;padding:10px 14px;border-radius:10px;background:#eff4ff;color:#163b85;text-decoration:none;font-size:13px;font-weight:700}
  .ai-summary h3{color:#1f3c88;font-size:15px;line-height:1.4;margin:20px 0 8px}
  .ai-summary p,.ai-summary li{color:#1f2937;font-size:16px;line-height:1.7}
  .ai-summary ul{padding-left:22px;margin:8px 0}
  .no-update{background:#f7f9fc;border:1px solid #dbe3ef;border-radius:14px;margin:16px 24px;padding:18px 20px;color:#53627b;font-size:15px;line-height:1.6}
  .error-box{background:#fff7f7;border:1px solid #f0c7c7;border-radius:14px;margin:16px 24px;padding:18px 20px}
  .error-title{font-size:14px;font-weight:700;color:#9f2f2f;margin:0 0 6px}
  .error-text{font-size:14px;line-height:1.6;color:#7a4242}
  .footer{text-align:center;padding:22px 24px 28px;font-size:12px;line-height:1.6;color:#7b8799;border-top:1px solid #edf2f7}
  a{color:#163b85}
</style>"""


def build_html_email(digest_name, reports, skipped, error_details, run_date):
    cards = ""

    for report in reports:
        badge = "badge website" if report["source_type"] == "website" else "badge"
        label = "Website" if report["source_type"] == "website" else "YouTube"
        cards += f"""<div class="card">
          <div class="card-header">
            <span class="{badge}">{label}</span>
            <div><p class="source-name">{report['source_name']}</p>
            <p class="source-url"><a href="{report['url']}">{report['url']}</a></p></div>
          </div>
          <p class="latest-label">Latest item</p>
          <a class="latest-link" href="{report['url']}">{report['content_title']}</a>
          <br><a class="open-link" href="{report['url']}">Open source</a>
          {report['summary_html']}</div>"""

    for item in skipped:
        cards += f"""<div class="no-update"><strong>{item['name']}</strong> - No new updates since last digest.
          <br><small><a href="{item['url']}">{item['url']}</a></small></div>"""

    for item in error_details:
        cards += f"""<div class="error-box"><div class="error-title">{item['name']}</div><div class="error-text">{item['message']}<br><small>{item['category']}</small></div></div>"""

    if not reports and not skipped and not error_details:
        cards = '<div class="no-update">No source activity was available for this digest.</div>'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">{EMAIL_CSS}</head><body>
<div class="preheader">{digest_name} updates for {run_date}</div>
<div class="wrapper">
  <div class="header"><h1>{digest_name} Digest</h1><p>{run_date} | Morning Intelligence Tracker</p></div>
  <div class="intro">A compact morning brief with the latest items, summaries, and any source issues that need attention.</div>
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
    digest_buckets = {}

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
            src_category = source["category"] or "General"
            last_id = source["last_scraped_id"]
            log.info("Processing [%s] %s", src_type.upper(), src_name)

            bucket = digest_buckets.setdefault(
                src_category,
                {"reports": [], "skipped": [], "errors": [], "error_details": []},
            )

            scraper = scrapers.get(src_type)
            if not scraper:
                continue

            result = scraper(src_url)
            if not result:
                bucket["errors"].append(src_name)
                bucket["error_details"].append(
                    {"name": src_name, "message": "Unknown scraper failure.", "category": src_category}
                )
                continue

            if result.get("error"):
                bucket["errors"].append(src_name)
                bucket["error_details"].append(
                    {"name": src_name, "message": result["error"], "category": src_category}
                )
                continue

            content_id = result["id"]
            if content_id == last_id:
                log.info("No new content for '%s' - adding no-update card.", src_name)
                bucket["skipped"].append({"name": src_name, "url": result["url"]})
                continue

            summary_html, summary_error = summarise_with_gemini(src_name, result["raw_text"])
            bucket["reports"].append(
                {
                    "source_type": src_type,
                    "source_name": src_name,
                    "url": result["url"],
                    "content_title": result["title"],
                    "summary_html": summary_html,
                }
            )
            if summary_error:
                bucket["error_details"].append(
                    {"name": src_name, "message": summary_error, "category": src_category}
                )
            update_last_scraped(conn, src_id, content_id)

    all_success = True
    digest_items = list(digest_buckets.items())
    for idx, (digest_name, bucket) in enumerate(digest_items):
        reports = bucket["reports"]
        skipped = bucket["skipped"]
        errors = bucket["errors"]
        error_details = bucket["error_details"]
        log.info(
            "Sending digest [%s] - %d new, %d no-update, %d errors",
            digest_name,
            len(reports),
            len(skipped),
            len(errors),
        )
        subject = f"{digest_name} Digest - {run_date}"
        html_body = build_html_email(digest_name, reports, skipped, error_details, run_date)
        success = send_via_apps_script(recipients, html_body, subject)
        all_success = all_success and success

        save_sent_log(
            {
                "timestamp": now.isoformat(),
                "date": run_date,
                "subject": subject,
                "digest_name": digest_name,
                "recipients": recipients,
                "new_sources": [r["source_name"] for r in reports],
                "no_update": [s["name"] for s in skipped],
                "errors": errors,
                "error_details": error_details,
                "sent": success,
                "report_count": len(reports),
                "html_body": html_body,
            }
        )

        if idx < len(digest_items) - 1:
            log.info("Waiting 5 minutes before sending next digest...")
            time.sleep(300)

    if not all_success:
        raise RuntimeError(
            "Digest delivery failed. Check GAS_WEBHOOK_URL, GAS_SECRET_TOKEN, and the Apps Script deployment."
        )

    log.info("========================================")
    log.info("  Morning Intelligence Tracker - DONE")
    log.info("========================================")


if __name__ == "__main__":
    run()
