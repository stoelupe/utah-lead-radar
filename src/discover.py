"""Stage 1: find water softener/treatment companies in Utah County via
Google Places API (New) Text Search.

Uses a field mask so each request only bills the fields we actually need
(id, name, address, phone, website, rating, review count) instead of the
full Place resource -- these are Enterprise-tier fields with a 1,000
free-calls/month cap per SKU, so a field mask + the query cap in config.py
keep a single run well under that.

Pipeline: search every (city, term) pair -> filter out non-leads -> dedupe
by phone or street address -> apply the MAX_COMPANIES cap.

Raw search results are cached in data/places_cache.json along with the
queries that produced them, so a run only spends quota on queries it hasn't
run before and filter changes can be re-applied offline.

Map-pack businesses from enrich's SerpApi cache that Places never returned
are imported too (see _import_serp_map_pack).
"""

import json
import re
import time
from typing import Optional

import requests

import config
from src import enrich

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
CACHE_PATH = config.DATA_DIR / "places_cache.json"

FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
        "places.primaryType",
    ]
)

# Exclusion rules, checked in order; the first match is the recorded reason.
# Patterns are matched case-insensitively against the company name.
GOVERNMENT_NAME = re.compile(
    r"\bcity of\b|\bcity\b.*\b(plant|wrf)\b|\bcounty\b|\bdistrict\b|wwtp|\bwrf\b|"
    r"treatment plant|reclamation|waste ?water|sewer|water resources|"
    r"water department|public works"
)
# City utility pages often live on city domains (springville.org,
# eaglemountaincity.com) rather than .gov.
GOVERNMENT_WEBSITE = re.compile(r"\.gov\b|city\.(com|org)|/public-(works|utilities)")
GOVERNMENT_TYPES = {"government_office", "local_government_office", "city_hall"}
# Water businesses that don't sell or service home treatment systems.
NON_RESIDENTIAL_NAME = re.compile(r"alkaline|hydrogen|testing|laborator|echo water")
NON_RESIDENTIAL_TYPES = {"wellness_center", "consultant"}
# "Water" in a water-heater company's name isn't treatment -- unless it also
# mentions softeners/filtration/treatment.
WATER_HEATER_NAME = re.compile(r"water heater")
TREATMENT_WORDS = re.compile(r"soft|filtr|treat|purif")
REFILL_KIOSK_NAME = re.compile(r"refill|kiosk|arctic mountain|water vending")
RESTORATION_NAME = re.compile(r"restoration|water damage|disaster|cleanup|mold remediation")
PLUMBER_NAME = re.compile(r"plumb|rooter|drain")
SUPPLY_CHAIN_DOMAINS = (
    "ferguson.com",
    "homedepot.com",
    "lowes.com",
    "grainger.com",
    "hdsupply.com",
    "coreandmain.com",
    "winsupplyinc.com",
)
# A kept company's name must look like residential water treatment.
WATER_TREATMENT_NAME = re.compile(
    r"water|soft|filtr|h2o|aqua|culligan|kinetico|rainsoft|ecowater|hague"
)

# "UT 84043", or just "UT" for service-area businesses with a city-only address.
STATE_ZIP = re.compile(r"^[A-Z]{2}( \d{5})?$")

ADDRESS_WORDS = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "street": "st",
    "road": "rd",
    "drive": "dr",
    "avenue": "ave",
    "boulevard": "blvd",
    "parkway": "pkwy",
    "suite": "ste",
}


def _search(query: str) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    # Pure service-area businesses (no storefront) are excluded by default.
    body = {"textQuery": query, "includePureServiceAreaBusinesses": True}
    resp = requests.post(PLACES_SEARCH_URL, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json().get("places", [])


def _address_city(address: Optional[str]) -> Optional[str]:
    """'281 S Vineyard Rd #107, Orem, UT 84059, USA' -> 'Orem'."""
    if not address:
        return None
    parts = [p.strip() for p in address.split(",")]
    for i, part in enumerate(parts):
        if i > 0 and STATE_ZIP.match(part):
            return parts[i - 1]
    return None


def _street_key(address: Optional[str]) -> Optional[str]:
    """Normalized street line (suite included) for duplicate detection."""
    if not address:
        return None
    street = address.split(",")[0].lower()
    street = re.sub(r"[.#]", " ", street)
    words = [ADDRESS_WORDS.get(w, w) for w in street.split()]
    return " ".join(words) or None


def _phone_key(phone: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else None


def _rejection_reason(company: dict) -> Optional[str]:
    name = (company["name"] or "").lower()
    website = (company["website"] or "").lower()

    excluded = config.EXCLUDED_COMPANIES.get(name)
    if excluded:
        return f"excluded: {excluded}"
    if company["business_status"] not in (None, "OPERATIONAL"):
        return "not operational"
    if (
        GOVERNMENT_NAME.search(name)
        or GOVERNMENT_WEBSITE.search(website)
        or company["primary_type"] in GOVERNMENT_TYPES
    ):
        return "government facility"
    if REFILL_KIOSK_NAME.search(name):
        return "refill kiosk"
    if any(domain in website for domain in SUPPLY_CHAIN_DOMAINS):
        return "supply chain"
    if RESTORATION_NAME.search(name):
        return "restoration company"
    if PLUMBER_NAME.search(name):
        return "plumber"
    if NON_RESIDENTIAL_NAME.search(name) or company["primary_type"] in NON_RESIDENTIAL_TYPES:
        return "not residential water treatment"
    if WATER_HEATER_NAME.search(name) and not TREATMENT_WORDS.search(name):
        return "water heater business"
    if not WATER_TREATMENT_NAME.search(name):
        return "not a water treatment business"
    # Service-area businesses publish no address; judge them by the city
    # whose search surfaced them instead.
    city = company["search_city"] if company.get("service_area") else company["city"]
    if city not in config.ALLOWED_CITIES:
        return f"outside city list ({city})"
    return None


def _dedupe(companies: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop companies sharing a phone number OR street address with one
    already kept. Most-reviewed listing wins, so sort before calling."""
    kept, dupes = [], []
    seen_phones: dict[str, str] = {}
    seen_streets: dict[str, str] = {}
    for company in companies:
        phone, street = _phone_key(company["phone"]), _street_key(company["address"])
        match = seen_phones.get(phone) if phone else None
        match = match or (seen_streets.get(street) if street else None)
        if match:
            dupes.append({**company, "rejected_reason": f"duplicate of {match}"})
            continue
        kept.append(company)
        if phone:
            seen_phones[phone] = company["name"]
        if street:
            seen_streets[street] = company["name"]
    return kept, dupes


def _place_record(place: dict, search_city: Optional[str]) -> dict:
    address = place.get("formattedAddress")
    return {
        "place_id": place.get("id"),
        "name": place.get("displayName", {}).get("text"),
        "address": address,
        "city": _address_city(address),
        "phone": place.get("nationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "business_status": place.get("businessStatus"),
        "primary_type": place.get("primaryType"),
        "search_city": search_city,
        "service_area": not address,
    }


def _import_serp_map_pack(cache: dict, budget: int) -> int:
    """Add map-pack businesses from enrich's SerpApi cache that Places Text
    Search never returned (typically service-area businesses with no
    storefront). Phone/rating/website come from the SERP entry; one Places
    lookup per business fills the rest (mainly the address, which the city
    filter needs). Lookups are cached like searches. Returns lookups run."""
    if not enrich.SERP_CACHE_PATH.exists():
        return 0
    serp_cache = json.loads(enrich.SERP_CACHE_PATH.read_text())
    places = cache["places"]

    def known(phone: Optional[str], domain: Optional[str], serp_id: str) -> bool:
        for p in places.values():
            if p.get("serp_place_id") == serp_id:
                return True
            if phone and _phone_key(p["phone"]) == phone:
                return True
            if domain and enrich._domain(p["website"]) == domain:
                return True
        return False

    lookups = 0
    for key, response in serp_cache.items():
        if " @ " not in key:
            continue  # pre-location searches, not used
        city = key.split(" @ ")[1].split(",")[0]
        for entry in (response.get("local_results") or {}).get("places", []):
            website = (entry.get("links") or {}).get("website")
            phone = entry.get("phone")
            serp_id = str(entry.get("place_id"))
            if known(_phone_key(phone), enrich._domain(website), serp_id):
                continue

            record = {
                "place_id": f"serp:{serp_id}",
                "name": entry.get("title"),
                "address": None,
                "city": None,
                "phone": phone,
                "website": website,
                "rating": entry.get("rating"),
                "review_count": entry.get("reviews"),
                "business_status": None,
                "primary_type": None,
                "search_city": city,
                "service_area": True,  # map-pack entries carry no address
                "source": "serpapi_map_pack",
                "serp_place_id": serp_id,
            }
            phone_key, domain = _phone_key(phone), enrich._domain(website)
            query = f"{record['name']}, Utah"
            if query not in cache["queries"] and lookups < budget:
                print(f"Places lookup: {query}")
                lookups += 1
                try:
                    results = _search(query)
                except requests.HTTPError as e:
                    print(f"  request failed: {e}")
                    results = []
                cache["queries"].append(query)
                match = next(
                    (
                        p for p in results
                        if (phone_key and _phone_key(p.get("nationalPhoneNumber")) == phone_key)
                        or (domain and enrich._domain(p.get("websiteUri")) == domain)
                    ),
                    None,
                )
                if match:
                    looked_up = _place_record(match, city)
                    # SERP values win; the lookup only fills gaps.
                    record.update({k: v for k, v in looked_up.items() if record.get(k) is None})
                    record["place_id"] = looked_up["place_id"]
                    record["service_area"] = looked_up["service_area"]
                else:
                    print("  no matching place (kept SERP data only)")
                time.sleep(0.2)
            places[record["place_id"]] = record
            _save_cache(cache)
    return lookups


def skips_email(company: dict) -> bool:
    """True if any of the company's flags is in config.EMAIL_SKIP_FLAGS."""
    flags = [f.strip() for f in (company.get("flag") or "").split(";")]
    return any(f.startswith(config.EMAIL_SKIP_FLAGS) for f in flags if f)


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {"queries": [], "places": {}}


def _save_cache(cache: dict) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def find_companies(
    limit: Optional[int] = None, offline: bool = False, refresh: bool = False
) -> dict:
    """Run uncached (city, term) searches, then filter, dedupe and cap
    everything in the cache.

    `limit` caps how many (city, term) query pairs are considered -- use a
    small value for cheap test runs. `offline` skips searching entirely and
    just re-filters the cache. `refresh` re-runs queries even if cached.

    Returns {"companies": [...kept], "rejected": [...with rejected_reason],
    "raw_count": int, "queries_run": int}.
    """
    cache = _load_cache()
    done = set() if refresh else set(cache["queries"])

    query_pairs = [
        (city, term) for city in config.SEARCH_CITIES for term in config.SEARCH_TERMS
    ]
    if limit is not None:
        query_pairs = query_pairs[:limit]
    query_pairs = [(c, t) for c, t in query_pairs if f"{t} in {c}, Utah" not in done]
    if offline:
        query_pairs = []
    query_pairs = query_pairs[: config.MAX_PLACES_QUERIES]

    if query_pairs and not config.GOOGLE_PLACES_API_KEY:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not set in .env")

    seen: dict[str, dict] = cache["places"]
    queries_run = 0
    for city, term in query_pairs:
        query = f"{term} in {city}, Utah"
        print(f"Searching: {query}")
        queries_run += 1
        try:
            places = _search(query)
        except requests.HTTPError as e:
            print(f"  request failed: {e}")
            continue

        for place in places:
            place_id = place.get("id")
            if not place_id or place_id in seen:
                continue
            seen[place_id] = _place_record(place, city)
        if query not in cache["queries"]:
            cache["queries"].append(query)
        _save_cache(cache)  # after each query, so a crash doesn't waste quota

        time.sleep(0.2)  # light pacing between requests

    budget = config.MAX_PLACES_QUERIES - queries_run
    queries_run += _import_serp_map_pack(cache, budget=0 if offline else budget)

    candidates, rejected = [], []
    for place in seen.values():
        company = {
            **place,
            "flag": config.FLAGGED_COMPANIES.get((place["name"] or "").lower()),
        }
        reason = _rejection_reason(company)
        if reason:
            rejected.append({**company, "rejected_reason": reason})
        else:
            candidates.append(company)

    candidates.sort(key=lambda c: c["review_count"] or 0, reverse=True)
    kept, dupes = _dedupe(candidates)
    rejected.extend(dupes)

    if len(kept) > config.MAX_COMPANIES:
        for company in kept[config.MAX_COMPANIES :]:
            rejected.append({**company, "rejected_reason": "over MAX_COMPANIES cap"})
        kept = kept[: config.MAX_COMPANIES]

    return {
        "companies": kept,
        "rejected": rejected,
        "raw_count": len(seen),
        "queries_run": queries_run,
    }
