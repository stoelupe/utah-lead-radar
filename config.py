"""Central config: secrets (from .env only) and pipeline constants."""

from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

# dotenv_values reads the file directly and never falls back to os.environ,
# so a stray system-level ANTHROPIC_API_KEY etc. can't leak into the run.
_env = dotenv_values(ROOT / ".env")

GOOGLE_PLACES_API_KEY = _env.get("GOOGLE_PLACES_API_KEY", "")
SERPAPI_API_KEY = _env.get("SERPAPI_API_KEY", "")
ANTHROPIC_API_KEY = _env.get("ANTHROPIC_API_KEY", "")

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Cities we run Places searches for.
SEARCH_CITIES = [
    "Provo",
    "Orem",
    "Lehi",
    "American Fork",
    "Pleasant Grove",
    "Spanish Fork",
    "Payson",
    "Springville",
    "Saratoga Springs",
    "Eagle Mountain",
    "Lindon",
    "Mapleton",
    "Alpine",
    "Salem",
]

# Every incorporated Utah County city/town. A company is kept only if the
# city in its address is on this list (searches return plenty of SLC-area hits).
ALLOWED_CITIES = [
    "Alpine",
    "American Fork",
    "Cedar Fort",
    "Cedar Hills",
    "Eagle Mountain",
    "Elk Ridge",
    "Fairfield",
    "Genola",
    "Goshen",
    "Highland",
    "Lehi",
    "Lindon",
    "Mapleton",
    "Orem",
    "Payson",
    "Pleasant Grove",
    "Provo",
    "Salem",
    "Santaquin",
    "Saratoga Springs",
    "Spanish Fork",
    "Springville",
    "Vineyard",
    "Woodland Hills",
]

# Manual flags, keyed by lowercase name. Flags are informational (enrich also
# adds "site down"); only flags starting with an EMAIL_SKIP_FLAGS entry are
# skipped for outreach emails.
FLAGGED_COMPANIES = {
    "wellness water filtration systems": "competitor (likely lead-gen site)",
}
EMAIL_SKIP_FLAGS = ("competitor",)

# Manual exclusions, keyed by lowercase name: dropped in discover with this
# reason (for listings the rule-based filters can't catch).
EXCLUDED_COMPANIES = {
    "water protection one": "listing for Alta Water (Draper, outside city list)",
}

SEARCH_TERMS = [
    "water softener company",
    "water treatment company",
]

# Hard caps so a bug can't quietly burn a free-tier quota.
MAX_COMPANIES = 45  # applied after filtering + dedupe
MAX_PLACES_QUERIES = 40  # per run; 14 cities x 2 terms = 28, and cached queries don't count
MAX_SERPAPI_QUERIES = 20  # per run; one per SEARCH_CITY (14), cached, well under the 250/month free cap

TOP_N_FOR_EMAILS = 10

SENDER_NAME = "Shennan"
SENDER_TITLE = "founder of Utah Water Guide"
EMAIL_SIGNATURE = "Shennan, Utah Water Guide"
RESOURCE_DESCRIPTION = (
    "utahwaterguide.com is an independent, unbiased water-hardness data "
    "resource covering Utah County and Salt Lake County -- not a contractor "
    "site, doesn't sell treatment."
)
