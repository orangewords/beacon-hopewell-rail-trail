#!/usr/bin/env python3
"""
Municipal Meeting Minutes Monitor

Checks a list of municipal websites for new content (page changes,
new document links) and sends an email report when updates are found.
Downloads new PDF documents and searches them for configurable keywords,
reporting matches with page numbers, sentences, and surrounding context.
Designed to run as a daily GitHub Actions cron job.
"""

import io
import json
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import resend
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

SNAPSHOTS_DIR = "snapshots"
SITES_FILE = "sites.json"
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 2        # seconds between requests (be polite)
MAX_PDF_SIZE = 50_000_000  # 50 MB — skip anything larger

DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xlsx", ".xls", ".csv", ".pptx"}
MEETING_KEYWORDS = {"agenda", "minute", "meeting", "resolution", "hearing", "session", "board"}

# Phrases to search for in documents.  Case-insensitive.
# Override with the SEARCH_PHRASES env var (comma-separated).
DEFAULT_SEARCH_PHRASES = ["Rail Trail"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def get_search_phrases() -> list[str]:
    env = os.environ.get("SEARCH_PHRASES", "")
    if env.strip():
        return [p.strip() for p in env.split(",") if p.strip()]
    return DEFAULT_SEARCH_PHRASES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_site_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def load_sites() -> list[dict]:
    with open(SITES_FILE) as f:
        return json.load(f)


def load_snapshot(site_id: str) -> dict | None:
    path = os.path.join(SNAPSHOTS_DIR, f"{site_id}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_snapshot(site_id: str, data: dict) -> None:
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOTS_DIR, f"{site_id}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Fetching & extraction
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException:
            if attempt == 0:
                time.sleep(3)
            else:
                raise


def extract_content(html: str, base_url: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "iframe"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    links: list[dict] = []
    seen_urls: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href.startswith(("javascript:", "mailto:", "#")):
            continue

        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        link_text = a_tag.get_text(strip=True)
        parsed = urlparse(full_url)
        ext = os.path.splitext(parsed.path)[1].lower()

        is_document = ext in DOCUMENT_EXTENSIONS
        is_meeting_related = any(kw in full_url.lower() for kw in MEETING_KEYWORDS) or \
                             any(kw in link_text.lower() for kw in MEETING_KEYWORDS)

        if is_document or is_meeting_related:
            links.append({
                "url": full_url,
                "text": link_text or "(no title)",
                "is_document": is_document,
            })

    return text, links


# ---------------------------------------------------------------------------
# PDF downloading & keyword search
# ---------------------------------------------------------------------------

def download_pdf(url: str) -> bytes | None:
    """Download a PDF and return its bytes, or None on failure."""
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=60, stream=True)
        resp.raise_for_status()

        # Check size from header before downloading the whole thing
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_PDF_SIZE:
            print(f"      Skipping (too large: {int(content_length) // 1_000_000} MB)")
            return None

        data = resp.content
        if len(data) > MAX_PDF_SIZE:
            return None
        return data

    except Exception as exc:
        print(f"      Download failed: {exc}")
        return None


def extract_pdf_text_by_page(pdf_bytes: bytes) -> list[tuple[int, str]]:
    """Extract text from a PDF, returning [(page_number, text), ...]."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append((i, text))
        return pages
    except Exception as exc:
        print(f"      PDF parse error: {exc}")
        return []


def find_phrase_matches(pages: list[tuple[int, str]], phrase: str) -> list[dict]:
    """
    Search extracted PDF pages for a phrase (case-insensitive).
    Returns a list of match dicts with page, sentence, and paragraph context.
    """
    matches = []
    phrase_lower = phrase.lower()

    for page_num, page_text in pages:
        # Split into paragraphs (blocks of text separated by blank lines
        # or multiple newlines)
        paragraphs = re.split(r"\n\s*\n", page_text)

        for para in paragraphs:
            if phrase_lower not in para.lower():
                continue

            para_clean = re.sub(r"\s+", " ", para).strip()
            if not para_clean:
                continue

            # Extract individual sentences that contain the phrase
            # Split on sentence-ending punctuation followed by a space
            sentences = re.split(r"(?<=[.!?])\s+", para_clean)
            matching_sentences = [
                s.strip() for s in sentences
                if phrase_lower in s.lower() and s.strip()
            ]

            # If sentence splitting didn't isolate anything useful,
            # fall back to the whole paragraph
            if not matching_sentences:
                matching_sentences = [para_clean]

            matches.append({
                "page": page_num,
                "sentences": matching_sentences,
                "paragraph": para_clean,
            })

    return matches


def search_page_text(text: str, phrase: str) -> list[dict]:
    """Search the HTML page's extracted text for a phrase."""
    phrase_lower = phrase.lower()
    if phrase_lower not in text.lower():
        return []

    matches = []
    paragraphs = re.split(r"\n\s*\n", text)

    for para in paragraphs:
        if phrase_lower not in para.lower():
            continue
        para_clean = re.sub(r"\s+", " ", para).strip()
        if not para_clean:
            continue

        sentences = re.split(r"(?<=[.!?])\s+", para_clean)
        matching_sentences = [
            s.strip() for s in sentences
            if phrase_lower in s.lower() and s.strip()
        ]
        if not matching_sentences:
            matching_sentences = [para_clean]

        matches.append({
            "page": None,
            "sentences": matching_sentences,
            "paragraph": para_clean,
        })

    return matches


# ---------------------------------------------------------------------------
# Per-site check
# ---------------------------------------------------------------------------

def check_site(site: dict, phrases: list[str]) -> dict:
    name = site["name"]
    url = site["url"]
    site_id = make_site_id(url)

    result = {
        "name": name,
        "url": url,
        "changed": False,
        "is_new": False,
        "content_changed": False,
        "new_links": [],
        "keyword_matches": [],   # list of per-document/page match results
        "error": None,
    }

    try:
        html = fetch_page(url)
        text, links = extract_content(html, url)
        content_hash = hashlib.sha256(text.encode()).hexdigest()

        prev = load_snapshot(site_id)

        if prev is None:
            result["is_new"] = True
            result["changed"] = True
            result["new_links"] = links
        else:
            if content_hash != prev.get("content_hash"):
                result["content_changed"] = True
                result["changed"] = True

            prev_urls = {link["url"] for link in prev.get("links", [])}
            new_links = [link for link in links if link["url"] not in prev_urls]
            if new_links:
                result["new_links"] = new_links
                result["changed"] = True

        # ----- Keyword search -----

        # 1. Search page content (only on first run or content change)
        if result["is_new"] or result["content_changed"]:
            for phrase in phrases:
                page_matches = search_page_text(text, phrase)
                if page_matches:
                    result["keyword_matches"].append({
                        "source": "page",
                        "source_name": name,
                        "source_url": url,
                        "phrase": phrase,
                        "matches": page_matches,
                    })

        # 2. Search new PDF documents
        prev_searched = set()
        if prev:
            prev_searched = set(prev.get("searched_docs", []))

        searched_docs = list(prev_searched)

        for link in result["new_links"]:
            link_url = link["url"]
            if not link.get("is_document"):
                continue
            if not link_url.lower().endswith(".pdf"):
                continue
            if link_url in prev_searched:
                continue

            print(f"    Downloading: {link['text']}... ", end="", flush=True)
            pdf_bytes = download_pdf(link_url)
            searched_docs.append(link_url)

            if pdf_bytes is None:
                print("skipped")
                time.sleep(REQUEST_DELAY)
                continue

            pages = extract_pdf_text_by_page(pdf_bytes)
            print(f"{len(pages)} pages", end="")

            for phrase in phrases:
                doc_matches = find_phrase_matches(pages, phrase)
                if doc_matches:
                    print(f"  ** '{phrase}' found! **", end="")
                    result["keyword_matches"].append({
                        "source": "document",
                        "source_name": link["text"],
                        "source_url": link_url,
                        "phrase": phrase,
                        "matches": doc_matches,
                    })

            print()  # newline after status
            time.sleep(REQUEST_DELAY)

        # Persist snapshot (including which docs we've searched)
        save_snapshot(site_id, {
            "content_hash": content_hash,
            "links": links,
            "searched_docs": searched_docs,
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "url": url,
        })

        # If we found keyword matches, ensure the result is marked as changed
        if result["keyword_matches"]:
            result["changed"] = True

    except Exception as exc:
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Report building (HTML email)
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def highlight_phrase(text: str, phrase: str) -> str:
    """Escape text for HTML and wrap phrase matches in a highlight span."""
    escaped = esc(text)
    pattern = re.compile(re.escape(esc(phrase)), re.IGNORECASE)
    return pattern.sub(
        lambda m: f'<span style="background:#fff176;font-weight:bold;padding:1px 3px;">{m.group()}</span>',
        escaped,
    )


def build_report(results: list[dict], phrases: list[str]) -> tuple[str | None, str | None]:
    changes = [r for r in results if r["changed"]]
    errors  = [r for r in results if r.get("error")]

    # Collect all keyword matches across sites
    all_kw_matches = []
    for r in results:
        all_kw_matches.extend(r.get("keyword_matches", []))

    if not changes and not errors and not all_kw_matches:
        return None, None

    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    date_short = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Adjust subject line if keyword matches were found
    if all_kw_matches:
        phrase_list = ", ".join(f'"{p}"' for p in phrases)
        subject = f'🔍 {phrase_list} mentioned — Meeting Minutes Update {date_short}'
    else:
        subject = f"Meeting Minutes Update — {date_short}"

    # ---- Build HTML email ----
    html = []
    html.append("""
<div style="font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
            max-width: 700px; margin: 0 auto; color: #1a1a1a;">
""")

    html.append(f"""
<h2 style="border-bottom: 2px solid #2563eb; padding-bottom: 8px;">
  Meeting Minutes Monitor — Daily Report
</h2>
<p style="color:#555;">Checked {len(results)} sites on {esc(now_str)}</p>
""")

    # ---- KEYWORD MATCHES (most important — shown first) ----
    if all_kw_matches:
        html.append(f"""
<div style="background:#fffde7; border-left:4px solid #f9a825; padding:16px;
            margin:20px 0; border-radius:4px;">
<h3 style="margin-top:0; color:#e65100;">
  🔍 Keyword Matches Found
</h3>
""")
        for km in all_kw_matches:
            source_type = km["source"]
            source_name = km["source_name"]
            source_url  = km["source_url"]
            phrase      = km["phrase"]

            if source_type == "document":
                html.append(f"""
<div style="margin-bottom:20px;">
<p style="margin:0 0 4px;">
  <strong>Document:</strong>
  <a href="{esc(source_url)}" style="color:#2563eb;">{esc(source_name)}</a>
</p>
<p style="margin:0 0 8px; color:#555;">
  Search term: <em>{esc(phrase)}</em>
</p>
""")
            else:
                html.append(f"""
<div style="margin-bottom:20px;">
<p style="margin:0 0 4px;">
  <strong>Web page:</strong>
  <a href="{esc(source_url)}" style="color:#2563eb;">{esc(source_name)}</a>
</p>
<p style="margin:0 0 8px; color:#555;">
  Search term: <em>{esc(phrase)}</em>
</p>
""")

            for match in km["matches"]:
                page = match["page"]
                page_str = f" (Page {page})" if page else ""

                # Highlighted sentences
                for sentence in match["sentences"]:
                    html.append(f"""
<div style="margin:8px 0; padding:8px 12px; background:#fff;
            border-left:3px solid #f9a825; border-radius:2px;">
  <p style="margin:0 0 2px; font-size:12px; color:#888;">
    Sentence{esc(page_str)}:
  </p>
  <p style="margin:0; line-height:1.5;">
    {highlight_phrase(sentence, phrase)}
  </p>
</div>
""")

                # Paragraph context (collapsed feel)
                para_preview = match["paragraph"]
                if len(para_preview) > 600:
                    para_preview = para_preview[:600] + "…"

                html.append(f"""
<details style="margin:4px 0 16px 12px;">
  <summary style="cursor:pointer; color:#666; font-size:13px;">
    Show surrounding context{esc(page_str)}
  </summary>
  <div style="margin-top:6px; padding:10px; background:#fafafa;
              border-radius:4px; font-size:14px; line-height:1.6; color:#333;">
    {highlight_phrase(para_preview, phrase)}
  </div>
</details>
""")

            html.append("</div>")  # close per-source div

        html.append("</div>")  # close yellow box

    # ---- SITE UPDATES ----
    if changes:
        html.append(f"""
<h3 style="margin-top:24px;">{len(changes)} Site(s) With Updates</h3>
""")
        for c in changes:
            html.append(f"""
<div style="margin-bottom:16px; padding:12px; background:#f5f5f5;
            border-radius:4px;">
  <p style="margin:0 0 4px;">
    <strong>{esc(c['name'])}</strong>
  </p>
  <p style="margin:0 0 8px;">
    <a href="{esc(c['url'])}" style="color:#2563eb; font-size:14px;">{esc(c['url'])}</a>
  </p>
""")
            if c.get("is_new"):
                html.append('<p style="margin:4px 0; color:#666;">First scan — baseline saved</p>')
            elif c.get("content_changed"):
                html.append('<p style="margin:4px 0; color:#666;">Page content changed since last check</p>')

            if c.get("new_links"):
                html.append(f'<p style="margin:8px 0 4px;"><strong>{len(c["new_links"])} new link(s):</strong></p>')
                html.append('<ul style="margin:4px 0; padding-left:20px;">')
                for link in c["new_links"]:
                    doc_badge = ' <span style="background:#e3f2fd;color:#1565c0;font-size:11px;padding:1px 6px;border-radius:3px;">PDF</span>' if link.get("is_document") else ""
                    html.append(f'<li style="margin:4px 0;"><a href="{esc(link["url"])}" style="color:#2563eb;">{esc(link["text"])}</a>{doc_badge}</li>')
                html.append("</ul>")

            html.append("</div>")

    # ---- ERRORS ----
    if errors:
        html.append(f'<h3 style="margin-top:24px; color:#c62828;">{len(errors)} Site(s) Had Errors</h3>')
        for e in errors:
            html.append(f'<p style="color:#c62828;">{esc(e["name"])}: {esc(e["error"])}</p>')

    # ---- UNCHANGED ----
    unchanged = [r for r in results if not r["changed"] and not r.get("error")]
    if unchanged:
        names = ", ".join(r["name"] for r in unchanged)
        html.append(f'<p style="margin-top:16px; color:#888;">Unchanged: {esc(names)}</p>')

    html.append("</div>")  # close wrapper

    return subject, "\n".join(html)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str) -> None:
    api_key    = os.environ.get("RESEND_API_KEY", "")
    email_to   = os.environ.get("EMAIL_TO", "")
    email_from = os.environ.get("EMAIL_FROM", "Meeting Monitor <onboarding@resend.dev>")

    if not all([api_key, email_to]):
        print("\n[!] Resend credentials not configured — printing report:\n")
        print(f"Subject: {subject}\n")
        # Strip tags for console output
        console_text = re.sub(r"<[^>]+>", "", html_body)
        console_text = re.sub(r"\n{3,}", "\n\n", console_text)
        print(console_text)
        return

    resend.api_key = api_key

    params: resend.Emails.SendParams = {
        "from": email_from,
        "to": [email_to],
        "subject": subject,
        "html": html_body,
    }

    resend.Emails.send(params)
    print(f"Email sent to {email_to}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sites = load_sites()
    phrases = get_search_phrases()

    print(f"Checking {len(sites)} site(s)...")
    print(f"Searching for: {', '.join(phrases)}\n")

    results: list[dict] = []
    for i, site in enumerate(sites):
        print(f"  [{i+1}/{len(sites)}] {site['name']}...", end=" ", flush=True)
        result = check_site(site, phrases)
        results.append(result)

        if result.get("error"):
            print(f"ERROR: {result['error']}")
        elif result["changed"]:
            kw_count = len(result.get("keyword_matches", []))
            extra = f" ({kw_count} keyword match(es))" if kw_count else ""
            print(f"changes detected{extra}")
        else:
            print("no changes")

        if i < len(sites) - 1:
            time.sleep(REQUEST_DELAY)

    subject, body = build_report(results, phrases)

    if subject:
        send_email(subject, body)
    else:
        print("\nNo changes detected. No notification sent.")


if __name__ == "__main__":
    main()
