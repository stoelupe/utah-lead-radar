"""Stage 4: draft outreach emails for the top-scored companies.

DRAFTS ONLY -- this module never sends anything; it has no mail code at all.

Picks the top TOP_N_FOR_EMAILS scored companies (skipping competitor flags)
and gives Claude Haiku one verified fact about each -- chosen in code, with
only the top AD_FACT_TOP_N allowed to lead with "already runs ads" -- to
work into a short, plain email. The greeting ("Hi Spencer," when the contact
address is a first name), [OFFER] placeholder line, closing, signature and
footer are added in code so they're always exact. Code checks each draft for
the site URL, the word limit, flattery and usage/traffic claims (one retry).
Contact emails come from contacts.py (free homepage + contact-page scan).
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

MAX_WORDS = 120  # greeting through signature; the footer isn't counted
OFFER = "[OFFER]"  # placeholder line for the concrete offer, filled in by hand
CLOSING = "Open to a quick chat?"
MIN_REVIEWS_FOR_FACT = 10  # "2 Google reviews" isn't a detail worth leading with
# Local parts that are a role, not a person, so they don't become "Hi Info,".
ROLE_MAILBOXES = {
    "info", "contact", "contactus", "service", "services", "sales", "office",
    "admin", "support", "hello", "team", "help", "mail", "customerservice",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "msn.com",
}
# Backstop for the no-flattery rule.
FLATTERY = re.compile(
    r"\b(trust(ed)?|excellent|strong|impressive|clearly|tells me|great|solid|"
    r"reputation)\b",
    re.I,
)
FOOTER = "[MAILING ADDRESS]\nNot interested? Just reply and I won't reach out again."
# Only this many top targets lead with the ad signal; the rest use their
# next-best fact so the batch doesn't all open the same way.
AD_FACT_TOP_N = 2
AD_FACT_PREFIX = "Already "
# Backstop for the no-usage-claims rule (the prompt is the main control).
USAGE_CLAIM = re.compile(
    r"homeowners (already |often |regularly )?(use|visit|come|land|find|rely|search)"
    r"|on the site|(our|my|the site's) (visitors|users|traffic|readers|audience)"
    r"|\b(visitors|traffic)\b",
    re.I,
)

SYSTEM_PROMPT = f"""\
You write short cold outreach emails from {config.SENDER_NAME}, \
{config.SENDER_TITLE}, to local water softener / treatment companies.

About the sender: {config.RESOURCE_DESCRIPTION} {config.SENDER_NAME} is \
BUILDING it -- a resource for homeowners to look up how hard their water is. \
The goal is to start a conversation about eventually connecting the company \
with homeowners in its area who are researching water softeners.

Write exactly two short sentences, in first person singular as \
{config.SENDER_NAME} ("I", not "we"):
1. What you're building (see below).
2. The one provided fact about the company, stated plainly as something you \
noticed. Don't interpret it or compliment them -- no "which tells me", \
"trusted", "strong", "excellent", "clearly", "impressive".
- Friendly, plain, local. No hype, no exclamation marks, no buzzwords.
- Include the exact text "utahwaterguide.com" and describe it as an \
independent water-hardness resource covering Utah County and Salt Lake County \
that you're building ("I'm building...").
- Never invent anything. Make NO claims about the site's traffic, searches, \
visitors, users, trust, or results, and don't imply anyone uses it today \
(no "homeowners use it", "land on the site", "researching on the site"). \
Talk about what it's for, not who uses it. The only facts are the one about \
the company and the description above.
- No greeting -- it's added separately.
- Don't describe an offer, a partnership, or connecting/exploring/working \
together, and don't ask to chat -- an offer line, closing question and \
signature are added separately.
- The body must be under {MAX_WORDS - 15} words.

Also write a short, plain subject line (under 8 words)."""


class EmailDraft(BaseModel):
    subject: str
    body: str


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
    name = re.sub(r",?\s+(inc\.?|llc|co\.?)$", "", company["name"], flags=re.I)
    return f"Hi {name} team,"


def _problems(full: str) -> list[str]:
    problems = []
    flattery = FLATTERY.search(full)
    if flattery:
        problems.append(f'remove "{flattery.group(0)}" -- state the fact without compliments')
    claim = USAGE_CLAIM.search(full)
    if claim:
        problems.append(
            f'remove "{claim.group(0)}" -- make no claims about who uses the site '
            "or its traffic; say you're building it"
        )
    if "utahwaterguide.com" not in full.lower():
        problems.append('it must include the exact text "utahwaterguide.com"')
    words = _word_count(full)
    if words >= MAX_WORDS:
        problems.append(f"it was {words} words with the closing added; make the body shorter")
    return problems


def _draft_one(
    client: anthropic.Anthropic, company: dict, allow_ad_fact: bool, email: Optional[str]
) -> dict:
    # Code picks the single strongest fact, so the model can't pick a weak
    # one or combine several.
    facts = _facts(company)
    if not allow_ad_fact:
        facts = [f for f in facts if not f.startswith(AD_FACT_PREFIX)]
    fact = facts[0] if facts else None
    prompt = f"Company: {company['name']}\nFact to use: {fact or 'none -- keep it general'}"
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        response = client.messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_format=EmailDraft,
        )
        draft = response.parsed_output
        full = (
            f"{_greeting(company, email)}\n\n{draft.body.strip()}\n\n{OFFER}\n\n"
            f"{CLOSING}\n\n{config.EMAIL_SIGNATURE}"
        )
        problems = _problems(full)
        if not problems:
            break
        messages = [{
            "role": "user",
            "content": prompt + "\n\nRewrite your last draft: " + "; ".join(problems) + ".",
        }]
    return {
        "subject": draft.subject.strip(),
        "body": f"{full}\n\n{FOOTER}",
        "word_count": _word_count(full),
        "fact_used": fact,
        "problems": problems,
    }


def draft_emails(limit: Optional[int] = None) -> dict:
    """Draft emails for the top TOP_N_FOR_EMAILS eligible companies.
    `limit` drafts only the first N of those (for test runs)."""
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    companies = json.loads(IN_PATH.read_text())
    eligible = [
        c for c in companies if c.get("score") is not None and not discover.skips_email(c)
    ]
    eligible.sort(key=lambda c: (c["score"], c.get("review_count") or 0), reverse=True)
    targets = eligible[: config.TOP_N_FOR_EMAILS]
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
    return {"drafts": drafts, "out_path": OUT_PATH}
