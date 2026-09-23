"""Stage 4: draft outreach emails for the top-scored companies.

DRAFTS ONLY -- this module never sends anything; it has no mail code at all.

Picks the top TOP_N_FOR_EMAILS scored companies (skipping competitor flags).
The email copy matches the utahwaterguide.com/partners page and is fixed
text assembled in code: subject, greeting ("Hi Spencer," when the contact
address is a first name), intro, partnership and free-start paragraphs,
closing, signature and footer. Claude Haiku writes exactly one sentence --
the personalization -- from one verified fact chosen in code (only the top
AD_FACT_TOP_N may use the ad signal, which always gets the fixed wording
AD_PERSONALIZATION instead). Code checks that sentence for flattery,
usage/traffic claims and length (one retry).

Each draft stores its personalization sentence, so `emails --rebuild`
re-applies changed copy to saved drafts with no API calls. Drafts get a
send_wave (by score) and a city_conflict flag, since the partnership is one
local company per area. Contact emails come from contacts.py.
"""

import json
import re
from typing import Optional

import anthropic
from pydantic import BaseModel

import config
from src import contacts, discover

IN_PATH = config.DATA_DIR / "scored_companies.json"
OUT_PATH = config.DATA_DIR / "email_drafts.json"

# --- Fixed copy (matches the partners page) ---------------------------------
SUBJECT = "A free partnership idea for {company}"
INTRO = (
    "I'm building utahwaterguide.com, an independent resource that helps Utah "
    "County and Salt Lake County homeowners look up how hard their water is."
)
PARTNERSHIP = (
    "When homeowners on the site want help with their water, I'd like to send "
    "them to one trusted local company, and I think you'd be a great fit."
)
FREE_START = (
    "It's free to start. Your first 5 homeowner inquiries are free, with no "
    "contract, so you can see the quality for yourself before deciding anything."
)
CLOSING = (
    f"If you're interested, you can see the details and apply at "
    f"{config.PARTNERS_URL}, or just reply 'yes' and I'll send them over."
)
SIGNATURE = config.EMAIL_SIGNATURE
FOOTER = f"{config.MAILING_ADDRESS}\nNot interested? Just reply and I won't reach out again."

# --- Send waves: top 3 by score, next 3, then the rest -----------------------
WAVE_SIZES = (3, 3)

# --- The model-written personalization sentence ------------------------------
MAX_PERSONAL_WORDS = 30
MIN_REVIEWS_FOR_FACT = 10  # "2 Google reviews" isn't a detail worth leading with
# Only this many top targets lead with the ad signal; the rest use their
# next-best fact so the batch doesn't all open the same way.
AD_FACT_TOP_N = 2
AD_FACT_PREFIX = "Already "
# Ad-signal facts get this fixed wording rather than naming the ad platforms.
AD_PERSONALIZATION = "I noticed you're already investing in online marketing."
# Backstops for the prompt's rules. They apply to the model's sentence only;
# the fixed copy above is written by hand.
FLATTERY = re.compile(
    r"\b(trust(ed)?|excellent|strong|impressive|clearly|tells me|great|solid|"
    r"reputation)\b",
    re.I,
)
USAGE_CLAIM = re.compile(
    r"homeowners (already |often |regularly )?(use|visit|come|land|find|rely|search)"
    r"|on the site|(our|my|the site's) (visitors|users|traffic|readers|audience)"
    r"|\b(visitors|traffic)\b",
    re.I,
)

# Local parts that are a role, not a person, so they don't become "Hi Info,".
ROLE_MAILBOXES = {
    "info", "contact", "contactus", "service", "services", "sales", "office",
    "admin", "support", "hello", "team", "help", "mail", "customerservice",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "msn.com",
}

SYSTEM_PROMPT = f"""\
You write one sentence for a cold outreach email from {config.SENDER_NAME}, \
{config.SENDER_TITLE}, to a local water softener / treatment company. The \
rest of the email is already written; your sentence follows an intro about \
utahwaterguide.com.

Write exactly ONE short sentence, in first person singular ("I"), stating \
the provided fact about the company plainly as something you noticed, e.g. \
"I noticed you show up in Google's map pack for water softener searches in \
Alpine."
- Don't interpret the fact or compliment them -- no "which tells me", \
"trusted", "strong", "excellent", "clearly", "impressive", "great".
- Never invent anything beyond the fact. Make no claims about the website's \
traffic, visitors or users.
- No greeting, offer, question or sign-off.
- Under {MAX_PERSONAL_WORDS} words."""


class Personalization(BaseModel):
    sentence: str


def _facts(c: dict) -> list[str]:
    """Human-readable facts from our data, strongest personalization first."""
    serp, scan = c["serp"], c["website_scan"]
    facts = []
    if not scan["error"]:
        if scan.get("google_ads_tag") and scan.get("meta_pixel"):
            facts.append("Already advertises online (Google Ads and Meta/Facebook ads)")
        elif scan.get("google_ads_tag"):
            facts.append("Already runs Google Ads")
        elif scan.get("meta_pixel"):
            facts.append("Already runs Meta/Facebook ads")
    map_cities = sorted({h["city"] for h in serp["map_pack"]})
    if map_cities:
        facts.append(
            "Shows up in Google's map pack for 'water softener' searches in "
            + ", ".join(map_cities)
        )
    organic_cities = {h["city"] for h in serp["organic"]}
    if len(organic_cities) >= 2:
        facts.append(
            f"Ranks in Google's top 10 for 'water softener' searches in "
            f"{len(organic_cities)} Utah County cities"
        )
    if (c.get("review_count") or 0) >= MIN_REVIEWS_FOR_FACT:
        facts.append(f"Has {c['review_count']} Google reviews averaging {c['rating']} stars")
    if not scan["error"]:
        if scan.get("free_water_test_offer"):
            facts.append("Offers free water testing on its website")
        if scan.get("financing_offer"):
            facts.append("Offers financing on its website")
    if c.get("service_area"):
        facts.append(f"Serves homeowners around {c['search_city']}")
    elif c.get("city"):
        facts.append(f"Based in {c['city']}")
    return facts


def _word_count(text: str) -> int:
    return len(text.split())


def _short_name(company: dict) -> str:
    return re.sub(r",?\s+(inc\.?|llc|co\.?)$", "", company["name"], flags=re.I)


def _greeting(company: dict, email: Optional[str]) -> str:
    """'Hi Spencer,' when the contact address is a person's first name,
    else 'Hi <Company> team,'."""
    local, _, domain = (email or "").partition("@")
    looks_like_name = (
        local.isalpha()
        and 2 < len(local) < 12
        and local not in ROLE_MAILBOXES
        and domain not in FREE_MAIL_DOMAINS  # there it's usually a username
        and not re.search(r"[^aeiouy]{4}", local)  # "nickmdj" isn't a name
    )
    if looks_like_name:
        return f"Hi {local.capitalize()},"
    return f"Hi {_short_name(company)} team,"


def _problems(sentence: str) -> list[str]:
    """Checks on the model-written sentence only."""
    problems = []
    flattery = FLATTERY.search(sentence)
    if flattery:
        problems.append(f'remove "{flattery.group(0)}" -- state the fact without compliments')
    claim = USAGE_CLAIM.search(sentence)
    if claim:
        problems.append(f'remove "{claim.group(0)}" -- make no claims about the site\'s users')
    words = _word_count(sentence)
    if words >= MAX_PERSONAL_WORDS:
        problems.append(f"it was {words} words; keep it under {MAX_PERSONAL_WORDS}")
    return problems


def _city(company: dict) -> str:
    """The company's area: the address city, or for a service-area business
    the city whose search surfaced it."""
    return company["search_city"] if company.get("service_area") else company.get("city")


def _assemble(company: dict, email: Optional[str], personalization: str) -> dict:
    """Build the full email around the model-written sentence."""
    personalization = personalization.strip()
    body = "\n\n".join([
        _greeting(company, email),
        f"{INTRO} {personalization}",
        PARTNERSHIP,
        FREE_START,
        CLOSING,
        SIGNATURE,
        FOOTER,
    ])
    return {
        "subject": SUBJECT.format(company=_short_name(company)),
        "city": _city(company),
        "personalization": personalization,
        "body": body,
        # Greeting through signature; the footer isn't counted.
        "word_count": _word_count(body.rsplit("\n\n", 1)[0]),
    }


def _mark_waves_and_conflicts(drafts: list[dict]) -> None:
    """send_wave by score order (drafts are already ranked), and a flag for
    drafts sharing a city, since the partnership is one company per area."""
    for rank, d in enumerate(drafts):
        wave, start = 1, 0
        for size in WAVE_SIZES:
            if rank < start + size:
                break
            start += size
            wave += 1
        d["send_wave"] = wave
    by_city: dict[str, list[str]] = {}
    for d in drafts:
        by_city.setdefault(d["city"], []).append(d["name"])
    for d in drafts:
        others = [n for n in by_city[d["city"]] if n != d["name"]]
        d["city_conflict"] = (
            f"same city ({d['city']}) as {', '.join(others)}" if others else None
        )


def _draft_one(
    client: anthropic.Anthropic, company: dict, allow_ad_fact: bool, email: Optional[str]
) -> dict:
    # Code picks the single strongest fact, so the model can't pick a weak
    # one or combine several.
    facts = _facts(company)
    if not allow_ad_fact:
        facts = [f for f in facts if not f.startswith(AD_FACT_PREFIX)]
    fact = facts[0] if facts else None
    if fact and fact.startswith(AD_FACT_PREFIX):
        return {**_assemble(company, email, AD_PERSONALIZATION), "fact_used": fact, "problems": []}
    prompt = f"Company: {company['name']}\nFact: {fact or 'none -- mention their local area'}"
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        response = client.messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_format=Personalization,
        )
        sentence = response.parsed_output.sentence
        problems = _problems(sentence)
        if not problems:
            break
        messages = [{
            "role": "user",
            "content": prompt + "\n\nRewrite your last sentence: " + "; ".join(problems) + ".",
        }]
    return {**_assemble(company, email, sentence), "fact_used": fact, "problems": problems}


def _targets() -> list[dict]:
    companies = json.loads(IN_PATH.read_text())
    eligible = [
        c for c in companies if c.get("score") is not None and not discover.skips_email(c)
    ]
    eligible.sort(key=lambda c: (c["score"], c.get("review_count") or 0), reverse=True)
    return eligible[: config.TOP_N_FOR_EMAILS]


def draft_emails(limit: Optional[int] = None) -> dict:
    """Draft emails for the top TOP_N_FOR_EMAILS eligible companies.
    `limit` drafts only the first N of those (for test runs)."""
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    targets = _targets()
    if limit is not None:
        targets = targets[:limit]
    contact_cache = contacts.find_contacts(targets)

    drafts = []
    for rank, company in enumerate(targets):
        print(f"Drafting: {company['name']}")
        email = contacts.contact_email(company.get("website"), contact_cache)
        try:
            draft = _draft_one(client, company, allow_ad_fact=rank < AD_FACT_TOP_N, email=email)
        except anthropic.APIStatusError as e:
            print(f"  API error {e.status_code}: {e.message}")
            continue
        except anthropic.APIConnectionError:
            print("  network error")
            continue
        drafts.append({
            "place_id": company["place_id"],
            "name": company["name"],
            "score": company["score"],
            "contact_email": email,
            "phone": company.get("phone"),
            "website": company.get("website"),
            **draft,
        })
    _mark_waves_and_conflicts(drafts)
    return {"drafts": drafts, "out_path": OUT_PATH}


def _legacy_personalization(draft: dict) -> str:
    """For drafts saved before only the personalization was stored: take the
    model-written paragraph and drop its own "I'm building..." sentence,
    which the fixed INTRO now replaces."""
    middle = draft.get("middle") or draft["body"].split("\n\n")[1]
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", middle.strip())
    if len(sentences) > 1 and sentences[0].startswith("I'm building"):
        sentences = sentences[1:]
    return " ".join(sentences)


def rebuild_drafts() -> dict:
    """Re-apply the fixed copy to the saved drafts, keeping each draft's
    personalization sentence. No API calls."""
    drafts = json.loads(OUT_PATH.read_text())
    companies = {c["place_id"]: c for c in json.loads(IN_PATH.read_text())}
    order = {c["place_id"]: i for i, c in enumerate(_targets())}
    drafts.sort(key=lambda d: order.get(d["place_id"], len(order)))
    for d in drafts:
        if (d.get("fact_used") or "").startswith(AD_FACT_PREFIX):
            personalization = AD_PERSONALIZATION
        else:
            personalization = d.get("personalization") or _legacy_personalization(d)
        d.pop("middle", None)
        d.update(_assemble(companies[d["place_id"]], d["contact_email"], personalization))
        d["problems"] = _problems(personalization)
    _mark_waves_and_conflicts(drafts)
    return {"drafts": drafts, "out_path": OUT_PATH}
