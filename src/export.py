"""Export scored companies (plus email drafts, if any) to a CSV that opens
cleanly in Excel / Google Sheets: data/leads.csv."""

import csv
import json

import config
from src import contacts

SCORED_PATH = config.DATA_DIR / "scored_companies.json"
DRAFTS_PATH = config.DATA_DIR / "email_drafts.json"
OUT_PATH = config.DATA_DIR / "leads.csv"

COLUMNS = [
    "rank", "score", "score_reason", "name", "location", "contact_email", "phone", "website",
    "rating", "review_count", "map_pack_cities", "organic_top10_cities",
    "google_ads_tag", "meta_pixel", "financing_offer", "free_water_test_offer",
    "flag", "email_subject", "email_body",
]


def _tag(scan: dict, signal: str) -> str:
    if scan["error"]:
        return "unknown (site error)"
    if not scan.get(signal):
        return "no"
    return "yes (via GTM)" if scan.get(f"{signal}_via") == "GTM" else "yes"


def export_csv() -> dict:
    companies = json.loads(SCORED_PATH.read_text())
    drafts = json.loads(DRAFTS_PATH.read_text()) if DRAFTS_PATH.exists() else []
    drafts_by_id = {d["place_id"]: d for d in drafts}
    contact_cache = contacts.load_cache()  # filled by the contacts stage; blank if unscanned

    rows = []
    for rank, c in enumerate(companies, 1):
        serp, scan = c["serp"], c["website_scan"]
        draft = drafts_by_id.get(c["place_id"], {})
        rows.append({
            "rank": rank,
            "score": c.get("score"),
            "score_reason": c.get("score_reason"),
            "name": c["name"],
            "location": (
                f"service area ({c['search_city']})" if c.get("service_area") else c.get("city")
            ),
            "contact_email": contacts.contact_email(c.get("website"), contact_cache) or "",
            "phone": c.get("phone"),
            "website": c.get("website"),
            "rating": c.get("rating"),
            "review_count": c.get("review_count"),
            "map_pack_cities": ", ".join(sorted({h["city"] for h in serp["map_pack"]})),
            "organic_top10_cities": len({h["city"] for h in serp["organic"]}),
            "google_ads_tag": _tag(scan, "google_ads_tag"),
            "meta_pixel": _tag(scan, "meta_pixel"),
            "financing_offer": "" if scan["error"] else ("yes" if scan.get("financing_offer") else "no"),
            "free_water_test_offer": (
                "" if scan["error"] else ("yes" if scan.get("free_water_test_offer") else "no")
            ),
            "flag": c.get("flag") or "",
            "email_subject": draft.get("subject", ""),
            "email_body": draft.get("body", ""),
        })

    # utf-8-sig so Excel detects UTF-8 (stars, dashes) correctly.
    with OUT_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return {"rows": len(rows), "drafts": len(drafts_by_id), "out_path": OUT_PATH}
