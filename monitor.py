#!/usr/bin/env python3
"""
Municipal Meeting Minutes Monitor

Checks a list of municipal websites for new content (page changes,
new document links) and sends an email report when updates are found.
Designed to run as a daily GitHub Actions cron job.
"""

import json
import hashlib
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SNAPSHOTS_DIR = "snapshots"
SITES_FILE = "sites.json"
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 2  # seconds between requests (be polite)

DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xlsx", ".xls", ".csv", ".pptx"}
MEETING_KEYWORDS = {"agenda", "minute", "meeting", "resolution", "hearing", "session", "board"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_site_id(url: str) -> str:
    """Create a stable short ID from a URL for use as a filename."""
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
    """Fetch a URL and return its HTML. Retries once on failure."""
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
    """
    Extract meaningful text and document/meeting links from HTML.
    Returns (text_content, list_of_link_dicts).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove elements that add noise
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "iframe"]):
        tag.decompose()

    # --- Text content ---
    text = soup.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # --- Links ---
    links: list[dict] = []
    seen_urls: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href.startswith(("javascript:", "mailto:", "#")):
            continue

        full_url = urljoin(base_url, href)

        # De-duplicate
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        link_text = a_tag.get_text(strip=True)
        parsed = urlparse(full_url)
        ext = os.path.splitext(parsed.path)[1].lower()

        # Keep document links and meeting-related links
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
# Per-site check
# ---------------------------------------------------------------------------

def check_site(site: dict) -> dict:
    """
    Fetch a site, compare to its snapshot, and return a change report.
    """
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
        "error": None,
    }

    try:
        html = fetch_page(url)
        text, links = extract_content(html, url)
        content_hash = hashlib.sha256(text.encode()).hexdigest()

        prev = load_snapshot(site_id)

        if prev is None:
            # First run — save baseline
            result["is_new"] = True
            result["changed"] = True
            result["new_links"] = links
        else:
            # Compare content hash
            if content_hash != prev.get("content_hash"):
                result["content_changed"] = True
                result["changed"] = True

            # Find newly appeared links
            prev_urls = {link["url"] for link in prev.get("links", [])}
            new_links = [link for link in links if link["url"] not in prev_urls]
            if new_links:
                result["new_links"] = new_links
                result["changed"] = True

        # Persist snapshot
        save_snapshot(site_id, {
            "content_hash": content_hash,
            "links": links,
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "url": url,
        })

    except Exception as exc:
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

def build_report(results: list[dict]) -> tuple[str | None, str | None]:
    """
    Build email subject + plain-text body.  Returns (None, None) when there
    is nothing to report.
    """
    changes = [r for r in results if r["changed"]]
    errors  = [r for r in results if r.get("error")]

    if not changes and not errors:
        return None, None

    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    date_short = datetime.now(timezone.utc).strftime("%B %d, %Y")

    subject = f"Meeting Minutes Update — {date_short}"
    lines: list[str] = []

    lines.append("MEETING MINUTES MONITOR — DAILY REPORT")
    lines.append(f"Checked {len(results)} sites on {now_str}")
    lines.append("=" * 55)

    # ---- Sites with updates ----
    if changes:
        lines.append(f"\n{len(changes)} SITE(S) WITH UPDATES\n")
        for c in changes:
            lines.append(f"  {c['name']}")
            lines.append(f"  {c['url']}")

            if c["is_new"]:
                lines.append("  Status: First scan — baseline saved")
                if c["new_links"]:
                    lines.append(f"  Found {len(c['new_links'])} document/meeting link(s):")
            else:
                if c["content_changed"]:
                    lines.append("  Status: Page content changed since last check")
                if c["new_links"]:
                    lines.append(f"  {len(c['new_links'])} NEW link(s) detected:")

            for link in c.get("new_links", []):
                doc_flag = " [document]" if link.get("is_document") else ""
                lines.append(f"    - {link['text']}{doc_flag}")
                lines.append(f"      {link['url']}")

            lines.append("")  # blank spacer

    # ---- Errors ----
    if errors:
        lines.append(f"\n{len(errors)} SITE(S) HAD ERRORS\n")
        for e in errors:
            lines.append(f"  {e['name']}: {e['error']}")
        lines.append("")

    # ---- Unchanged ----
    unchanged = [r for r in results if not r["changed"] and not r.get("error")]
    if unchanged:
        names = ", ".join(r["name"] for r in unchanged)
        lines.append(f"Unchanged: {names}")

    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str) -> None:
    smtp_server  = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port    = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user    = os.environ.get("SMTP_USER", "")
    smtp_pass    = os.environ.get("SMTP_PASSWORD", "")
    email_to     = os.environ.get("EMAIL_TO", "")
    email_from   = os.environ.get("EMAIL_FROM", smtp_user)

    if not all([smtp_user, smtp_pass, email_to]):
        print("\n[!] Email credentials not configured — printing report:\n")
        print(f"Subject: {subject}\n")
        print(body)
        return

    msg = MIMEMultipart()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

    print(f"Email sent to {email_to}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sites = load_sites()
    print(f"Checking {len(sites)} site(s)...\n")

    results: list[dict] = []
    for i, site in enumerate(sites):
        print(f"  [{i+1}/{len(sites)}] {site['name']}...", end=" ", flush=True)
        result = check_site(site)
        results.append(result)

        if result.get("error"):
            print(f"ERROR: {result['error']}")
        elif result["changed"]:
            print("changes detected")
        else:
            print("no changes")

        # Polite delay between requests
        if i < len(sites) - 1:
            time.sleep(REQUEST_DELAY)

    subject, body = build_report(results)

    if subject:
        send_email(subject, body)
    else:
        print("\nNo changes detected. No notification sent.")


if __name__ == "__main__":
    main()
