"""Stage 3: score each enriched company 1-10 on how likely it is to buy
homeowner leads, with a one-line reason, using Claude Haiku.

Competitors (flag starting with "competitor") score 0 without an API call.
The model sees a compact summary of each company's signals, not raw HTML
or SERP data. Output is validated JSON via structured outputs.

(No prompt caching: the system prompt is far below Haiku 4.5's 4096-token
cache minimum, so a cache_control marker would silently do nothing.)
"""

import json
from typing import Optional

import anthropic
from pydantic import BaseModel

import config

IN_PATH = config.DATA_DIR / "enriched_companies.json"
OUT_PATH = config.DATA_DIR / "scored_companies.json"
# Scores keyed by place_id, reused while the company's signals are unchanged,
# so reruns don't re-call Claude or reshuffle scores.
CACHE_PATH = config.DATA_DIR / "score_cache.json"

SYSTEM_PROMPT = f"""\
You score local water softener / water treatment companies in Utah County on \
how likely they are to BUY homeowner leads (homeowners asking for a water \
softener quote). The seller is {config.RESOURCE_DESCRIPTION}

Score 1-10, where 10 = almost certainly buys leads and 1 = very unlikely.

Weigh the signals like this:
- Google Ads tag or Meta Pixel on their site: the strongest signal. They \
already pay to acquire customers, so buying leads is a small step. "via GTM" \
means the tag is loaded through Google Tag Manager -- it counts the same.
- Map pack and top-10 organic presence across the 14 Utah County city \
searches: shows they invest in marketing and want local customers.
- Review count: a proxy for size and install volume -- more capacity to \
take on leads.
- A real, working website: without one (or with the site down) they're \
unlikely to have a marketing budget.

Financing or free-water-test offers suggest a sales-driven, lead-hungry \
business. A national brand's local dealer is plausible but may get leads \
from corporate.

Calibration:
- 8-10: Google Ads tag and/or Meta Pixel, plus real reviews or search presence.
- 5-7: no ad tags, but strong map pack/organic presence or many reviews.
- 3-4: working website but little search presence and few reviews.
- 1-2: no website, site down, or almost no reviews and no presence.
A company without an ad tag or pixel should not score above 7.

Give a one-line reason (under 25 words) naming the signals that drove the \
score."""


class LeadScore(BaseModel):
    score: int
    reason: str


def _signals(c: dict) -> dict:
    """Compact per-company summary for the prompt."""
    serp, scan = c["serp"], c["website_scan"]
    tags = {}
    if not scan["error"]:
        for signal in ("google_ads_tag", "meta_pixel"):
            tags[signal] = (
                f"yes (via {scan.get(f'{signal}_via')})" if scan.get(signal) else "no"
            )
        for signal in ("google_analytics", "google_tag_manager",
                       "financing_offer", "free_water_test_offer"):
            tags[signal] = bool(scan.get(signal))
    return {
        "name": c["name"],
        "location": (
            f"service-area business (ranks in {c['search_city']})"
            if c.get("service_area") else c.get("city")
        ),
        "rating": c.get("rating"),
        "review_count": c.get("review_count") or 0,
        "website": c.get("website") or "none",
        "website_status": scan["error"] or "ok",
        "flag": c.get("flag"),
        "paid_ads_cities": len(serp["ads"]),
        "local_services_ads_cities": len(serp["lsa"]),
        "map_pack_cities": sorted({h["city"] for h in serp["map_pack"]}),
        "organic_top10_cities": len({h["city"] for h in serp["organic"]}),
        "best_organic_position": min((h["position"] for h in serp["organic"]), default=None),
        "site_tags": tags,
    }


def _is_competitor(c: dict) -> bool:
    flags = [f.strip() for f in (c.get("flag") or "").split(";")]
    return any(f.startswith("competitor") for f in flags)


def _score_one(client: anthropic.Anthropic, company: dict) -> LeadScore:
    response = client.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": "Score this company:\n" + json.dumps(_signals(company), indent=2),
        }],
        output_format=LeadScore,
    )
    result = response.parsed_output
    result.score = max(1, min(10, result.score))
    return result


def score_companies(limit: Optional[int] = None, names: Optional[list[str]] = None) -> dict:
    """Score enriched companies. `limit` scores only the first N; `names`
    scores only companies whose name starts with one of the given strings."""
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    companies = json.loads(IN_PATH.read_text())
    if names:
        companies = [c for c in companies if c["name"].startswith(tuple(names))]
    if limit is not None:
        companies = companies[:limit]

    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    for company in companies:
        if _is_competitor(company):
            company["score"], company["score_reason"] = 0, "Competitor -- not a lead buyer."
            continue
        signals = _signals(company)
        cached = cache.get(company["place_id"])
        if cached and cached["signals"] == signals:
            company["score"], company["score_reason"] = cached["score"], cached["reason"]
            continue
        print(f"Scoring: {company['name']}")
        try:
            result = _score_one(client, company)
        except anthropic.APIStatusError as e:
            print(f"  API error {e.status_code}: {e.message}")
            company["score"], company["score_reason"] = None, f"API error {e.status_code}"
            continue
        except anthropic.APIConnectionError:
            print("  network error")
            company["score"], company["score_reason"] = None, "network error"
            continue
        company["score"], company["score_reason"] = result.score, result.reason
        cache[company["place_id"]] = {
            "signals": signals, "score": result.score, "reason": result.reason,
        }
        CACHE_PATH.write_text(json.dumps(cache, indent=2))

    companies.sort(key=lambda c: c["score"] if c["score"] is not None else -1, reverse=True)
    return {"companies": companies, "out_path": OUT_PATH}
