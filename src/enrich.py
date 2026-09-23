"""Stage 2: enrich discovered companies with marketing signals.

No Places calls -- phone, website and ratings come from discover.

1. SerpApi: one Google search per SEARCH_CITY ("water softener {city} utah"),
   searched from that city's location.
   Records which companies show up in paid ads, Local Services Ads, the
   local/map pack and the top 10 organic results. Every raw response is
   cached in data/serpapi_cache.json so reruns never re-spend searches.
2. Website scan (free): fetch each homepage and look for a Google Ads tag,
   Google Analytics / Tag Manager, a Meta Pixel, and financing or
   free-water-test offers. Tags injected by Google Tag Manager aren't in
   the HTML, so each GTM container's gtm.js is fetched (and cached) and
   checked for Google Ads / Meta Pixel tags too. Sites whose domain no
   longer resolves get flag "site down".

SERP results are matched to companies by domain first, then by name.
"""

import json
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests

import config

SERPAPI_URL = "https://serpapi.com/search.json"
SERP_CACHE_PATH = config.DATA_DIR / "serpapi_cache.json"
SCAN_CACHE_PATH = config.DATA_DIR / "website_scan_cache.json"
COMPANIES_PATH = config.DATA_DIR / "raw_companies.json"
OUT_PATH = config.DATA_DIR / "enriched_companies.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; UtahLeadRadar/0.1; one-time homepage check for "
    "utahwaterguide.com)"
)

WEBSITE_SIGNALS = {
    "google_ads_tag": re.compile(r"\bAW-\d{6,}"),
    "google_analytics": re.compile(
        r"\bG-[A-Z0-9]{6,}\b|\bUA-\d{4,}-\d+|google-analytics\.com|gtag\("
    ),
    "google_tag_manager": re.compile(r"\bGTM-[A-Z0-9]{4,}|googletagmanager\.com/gtm\.js"),
    "meta_pixel": re.compile(r"connect\.facebook\.net/[^\"']*fbevents\.js|\bfbq\("),
    "financing_offer": re.compile(
        r"\bfinancing\b|\b0% (apr|interest)\b|\bmonthly payments?\b|\bper month\b|/mo\b",
        re.I,
    ),
    "free_water_test_offer": re.compile(
        r"free[\s-]+(in[\s-]home\s+)?water\s+(test|testing|analysis)", re.I
    ),
}

GTM_ID = re.compile(r"\bGTM-[A-Z0-9]{4,}\b")
GTM_JS_URL = "https://www.googletagmanager.com/gtm.js"
GTM_CACHE_PATH = config.DATA_DIR / "gtm_cache.json"
# Inside a GTM container, Google Ads conversion/remarketing tags appear as
# __awct / __sp templates with a bare numeric conversion id, or as AW- ids.
GTM_GOOGLE_ADS = re.compile(r"AW-\d{6,}|__awct|\"__sp\"|vtp_conversionId")
GTM_META_PIXEL = re.compile(r"fbevents\.js|fbq\(|connect\.facebook\.net")

# Words dropped before comparing business names.
NAME_STOPWORDS = {"inc", "llc", "co", "company", "the", "of", "utah", "valley", "ut"}


# ---------------------------------------------------------------- matching


def _domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    host = urlparse(url).netloc.lower().split(":")[0]
    return host.removeprefix("www.") or None


def _name_key(name: Optional[str]) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in NAME_STOPWORDS)


def _match(companies: list[dict], url: Optional[str], name: Optional[str]) -> Optional[dict]:
    """Domain match first; fall back to normalized-name match."""
    domain = _domain(url)
    if domain:
        for company in companies:
            if company["_domain"] and company["_domain"] == domain:
                return company
    key = _name_key(name)
    if len(key) >= 6:
        for company in companies:
            ckey = company["_name_key"]
            if ckey and (ckey == key or (len(ckey) >= 8 and (ckey in key or key in ckey))):
                return company
    return None


# ----------------------------------------------------------------- serpapi


def _load_serp_cache() -> dict:
    if SERP_CACHE_PATH.exists():
        return json.loads(SERP_CACHE_PATH.read_text())
    return {}


def _save_serp_cache(cache: dict) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    SERP_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _serp_key(city: str) -> tuple[str, str, str]:
    """(cache key, query, location). Searches originate from the city itself
    because ads and Local Services Ads are geo-targeted -- without a location
    SerpApi searches from a default US location and Utah ads never show."""
    query = f"water softener {city} utah"
    location = f"{city},Utah,United States"
    return f"{query} @ {location}", query, location


def _serp_search(query: str, location: str) -> dict:
    params = {
        "engine": "google",
        "q": query,
        "location": location,
        "gl": "us",
        "hl": "en",
        "api_key": config.SERPAPI_API_KEY,
    }
    resp = requests.get(SERPAPI_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


def _serp_entries(response: dict) -> list[tuple[str, int, Optional[str], Optional[str]]]:
    """Flatten a SerpApi Google response into (section, position, url, name)."""
    entries = []
    for i, ad in enumerate(response.get("ads", []), 1):
        entries.append(("ads", ad.get("position", i), ad.get("link"), ad.get("title")))

    local_ads = response.get("local_ads") or {}
    for i, ad in enumerate(local_ads.get("ads", []), 1):
        entries.append(("lsa", i, ad.get("link") or ad.get("website"), ad.get("title")))

    local = response.get("local_results") or {}
    places = local.get("places", []) if isinstance(local, dict) else local
    for i, place in enumerate(places, 1):
        links = place.get("links") or {}
        entries.append(
            ("map_pack", place.get("position", i), links.get("website") or place.get("website"),
             place.get("title"))
        )

    for result in response.get("organic_results", []):
        position = result.get("position", 99)
        if position <= 10:
            entries.append(("organic", position, result.get("link"), result.get("title")))
    return entries


def _run_serp(companies: list[dict], limit: Optional[int]) -> int:
    cities = config.SEARCH_CITIES if limit is None else config.SEARCH_CITIES[:limit]
    cache = _load_serp_cache()

    to_run = [c for c in cities if _serp_key(c)[0] not in cache]
    to_run = to_run[: config.MAX_SERPAPI_QUERIES]
    if to_run and not config.SERPAPI_API_KEY:
        raise RuntimeError("SERPAPI_API_KEY is not set in .env")

    searches_run = 0
    for city in to_run:
        key, query, location = _serp_key(city)
        print(f"SerpApi: {query} (from {location})")
        try:
            cache[key] = _serp_search(query, location)
        except (requests.RequestException, RuntimeError) as e:
            print(f"  search failed: {e}")
            continue
        searches_run += 1
        _save_serp_cache(cache)  # after each search, so a crash doesn't waste quota

    for city in cities:
        response = cache.get(_serp_key(city)[0])
        if not response:
            continue
        for section, position, url, name in _serp_entries(response):
            company = _match(companies, url, name)
            if company:
                company["serp"][section].append({"city": city, "position": position})
    return searches_run


# ------------------------------------------------------------ website scan


def _homepage(url: str) -> str:
    parsed = urlparse(url if "://" in url else "http://" + url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _scan_website(url: Optional[str]) -> dict:
    if not url:
        return {"url": None, "error": "no website"}
    homepage = _homepage(url)
    try:
        resp = requests.get(homepage, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"url": homepage, "error": f"{type(e).__name__}: {e}"[:200]}
    html = resp.text
    result = {"url": resp.url, "error": None}
    for signal, pattern in WEBSITE_SIGNALS.items():
        result[signal] = bool(pattern.search(html))
    result["gtm_ids"] = sorted(set(GTM_ID.findall(html)))
    return result


def _scan_gtm_container(gtm_id: str) -> dict:
    try:
        resp = requests.get(
            GTM_JS_URL, params={"id": gtm_id}, headers={"User-Agent": USER_AGENT}, timeout=10
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    js = resp.text
    return {
        "error": None,
        "google_ads": bool(GTM_GOOGLE_ADS.search(js)),
        "meta_pixel": bool(GTM_META_PIXEL.search(js)),
        "aw_ids": sorted(set(re.findall(r"AW-\d{6,}", js))),
    }


def _apply_gtm(scan: dict, gtm_cache: dict) -> dict:
    """Return a copy of a page scan with ad/pixel tags found inside its GTM
    containers merged in; *_via says where each tag was seen."""
    scan = dict(scan)
    containers = []
    for gtm_id in scan.get("gtm_ids", []):
        if gtm_id not in gtm_cache:
            print(f"  GTM container: {gtm_id}")
            gtm_cache[gtm_id] = _scan_gtm_container(gtm_id)
            time.sleep(0.5)
        containers.append(gtm_cache[gtm_id])
    for signal, gtm_key in (("google_ads_tag", "google_ads"), ("meta_pixel", "meta_pixel")):
        if scan.get(signal):
            scan[f"{signal}_via"] = "page"
        elif any(c.get(gtm_key) for c in containers):
            scan[signal] = True
            scan[f"{signal}_via"] = "GTM"
        else:
            scan[f"{signal}_via"] = None
    return scan


# -------------------------------------------------------------------- main


def enrich_companies(limit: Optional[int] = None) -> dict:
    """`limit` caps how many SEARCH_CITIES get a SerpApi search."""
    companies = json.loads(COMPANIES_PATH.read_text())
    for company in companies:
        company["_domain"] = _domain(company.get("website"))
        company["_name_key"] = _name_key(company.get("name"))
        company["serp"] = {"ads": [], "lsa": [], "map_pack": [], "organic": []}

    searches_run = _run_serp(companies, limit)

    # Successful scans are cached so reruns don't re-hit sites; only
    # failures are retried.
    # Every scan is cached, failures included, so only new websites get
    # fetched; delete an entry from the cache file to force a rescan. Scans
    # cached before GTM ids were recorded are redone once if they saw GTM.
    scan_cache = json.loads(SCAN_CACHE_PATH.read_text()) if SCAN_CACHE_PATH.exists() else {}
    gtm_cache = json.loads(GTM_CACHE_PATH.read_text()) if GTM_CACHE_PATH.exists() else {}
    for company in companies:
        website = company.get("website")
        scan = scan_cache.get(website) if website else None
        stale = scan and scan.get("google_tag_manager") and "gtm_ids" not in scan
        if not scan or stale:
            print(f"Scanning: {company['name']}")
            scan = _scan_website(website)
            if website:
                scan_cache[website] = scan
                time.sleep(0.5)  # be polite
        company["website_scan"] = scan if scan["error"] else _apply_gtm(scan, gtm_cache)
        if scan["error"] and re.search(r"NameResolution|getaddrinfo", scan["error"]):
            company["flag"] = "; ".join(filter(None, [company.get("flag"), "site down"]))
    SCAN_CACHE_PATH.write_text(json.dumps(scan_cache, indent=2))
    GTM_CACHE_PATH.write_text(json.dumps(gtm_cache, indent=2))

    for company in companies:
        del company["_domain"], company["_name_key"]
    OUT_PATH.write_text(json.dumps(companies, indent=2))
    return {"companies": companies, "searches_run": searches_run, "out_path": OUT_PATH}
