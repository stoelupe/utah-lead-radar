"""Find a public contact email for each company, for free.

Fetches the homepage and one contact page (the homepage's own "contact" link
if it has one, else /contact) and collects mailto: links and plain-text
addresses, including Cloudflare-obfuscated ones. Results are cached per
website in data/contact_cache.json, so each site is fetched at most once;
delete an entry to re-check it.
"""

import json
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

import config
from src import enrich

CACHE_PATH = config.DATA_DIR / "contact_cache.json"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO = re.compile(r"mailto:([^\"'?>\s]+)", re.I)
CF_EMAIL = re.compile(r'data-cfemail="([0-9a-f]+)"', re.I)
CONTACT_LINK = re.compile(r'href="([^"#]*contact[^"#]*)"', re.I)

# Placeholders and asset names that look like emails but aren't.
JUNK_DOMAINS = ("example.com", "domain.com", "email.com", "yourdomain", "sentry",
                "wixpress.com", "godaddy.com", "squarespace.com")
JUNK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")


def _decode_cfemail(hex_str: str) -> str:
    key = int(hex_str[:2], 16)
    return "".join(chr(int(hex_str[i:i + 2], 16) ^ key) for i in range(2, len(hex_str), 2))


def _emails_in(html: str) -> list[str]:
    found = [m for m in MAILTO.findall(html)]
    found += [_decode_cfemail(h) for h in CF_EMAIL.findall(html)]
    found += EMAIL.findall(html)
    clean = []
    for email in found:
        email = requests.utils.unquote(email).strip().strip(".").lower()
        if not EMAIL.fullmatch(email):
            continue
        if email.endswith(JUNK_SUFFIXES) or any(j in email for j in JUNK_DOMAINS):
            continue
        if email not in clean:
            clean.append(email)
    return clean


def _fetch(url: str) -> tuple[Optional[str], Optional[str]]:
    try:
        resp = requests.get(url, headers={"User-Agent": enrich.USER_AGENT}, timeout=10)
        resp.raise_for_status()
        return resp.text, None
    except requests.RequestException as e:
        return None, f"{type(e).__name__}"


def _scan(website: str) -> dict:
    homepage = enrich._homepage(website)
    pages, emails = {}, []
    html, error = _fetch(homepage)
    pages[homepage] = error or "ok"
    contact_url = homepage.rstrip("/") + "/contact"
    if html:
        emails += _emails_in(html)
        link = CONTACT_LINK.search(html)
        if link:
            candidate = urljoin(homepage, link.group(1))
            if urlparse(candidate).netloc == urlparse(homepage).netloc:
                contact_url = candidate
    if not html or not emails:
        time.sleep(0.5)  # be polite
        html, error = _fetch(contact_url)
        pages[contact_url] = error or "ok"
        if html:
            emails += [e for e in _emails_in(html) if e not in emails]
    return {"emails": emails, "pages": pages}


def best_email(website: Optional[str], emails: list[str]) -> Optional[str]:
    """Prefer an address on the company's own domain, then the first found."""
    domain = enrich._domain(website)
    for email in emails:
        if domain and email.split("@")[1].removeprefix("www.") == domain:
            return email
    return emails[0] if emails else None


def load_cache() -> dict:
    return json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}


def contact_email(website: Optional[str], cache: dict) -> Optional[str]:
    entry = cache.get(website) if website else None
    return best_email(website, entry["emails"]) if entry else None


def find_contacts(companies: list[dict]) -> dict:
    cache = load_cache()
    for company in companies:
        website = company.get("website")
        if not website or website in cache:
            continue
        print(f"Contact scan: {company['name']}")
        cache[website] = _scan(website)
        CACHE_PATH.write_text(json.dumps(cache, indent=2))
        time.sleep(0.5)  # be polite
    return cache
