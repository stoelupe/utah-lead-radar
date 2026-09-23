"""Render README visuals and the public sample CSV from cached data only --
no API calls, no network.

  docs/pipeline_run.svg     stage-by-stage summary of the latest full run
  docs/top_leads.svg        top 10 ranked companies with their signals
  docs/filter_summary.svg   what the filters dropped, and why
  sample_output/sample_leads.csv   top 5 rows

Everything here is published, so company names are anonymized ("Company A,
B, C..." by rank), review counts and ratings are bucketed (also inside
reasons), and phones, websites and contact emails are left out.
"""

import csv
import json
import re
from collections import Counter

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from src import contacts, discover, emails, enrich, export, score

DOCS_DIR = config.ROOT / "docs"
SAMPLE_PATH = config.ROOT / "sample_output" / "sample_leads.csv"
WIDTH = 110

# Public outputs are anonymized: companies become "Company A, B, C..." by
# rank, names are scrubbed from reasons, and no phones, websites, contact
# emails or drafts are published. The real data stays in data/ (gitignored).
SAMPLE_COLUMNS = [
    "rank", "score", "score_reason", "name", "location",
    "rating", "review_count", "map_pack_cities", "organic_top10_cities",
    "google_ads_tag", "meta_pixel",
]
# Words too generic to identify a company, so they're left in reasons.
GENERIC_NAME_WORDS = {
    "water", "waters", "soft", "softwater", "softener", "softeners", "system", "systems",
    "service", "services", "solutions", "treatment", "filtration", "filters", "pure",
    "utah", "valley", "company", "the", "and", "of", "inc", "llc", "co", "home", "best",
    "clear", "group", "technologies", "works", "waterworks", "doctor", "lab", "pros",
}
# City names appear in reasons as locations ("map pack in Spanish Fork"), so a
# company named after a city mustn't turn them into its alias.
GENERIC_NAME_WORDS |= {w.lower() for city in config.ALLOWED_CITIES for w in city.split()}
GENERIC_NAME_WORDS |= {"wasatch", "mountain", "salt", "lake"}


def _load(path):
    return json.loads(path.read_text())


def _alias(rank: int) -> str:
    """1 -> 'Company A', 26 -> 'Company Z', 27 -> 'Company AA'."""
    letters = ""
    while rank:
        rank, rem = divmod(rank - 1, 26)
        letters = chr(65 + rem) + letters
    return f"Company {letters}"


def _aliases(scored: list[dict]) -> dict[str, str]:
    """place_id -> alias, in rank order (the order of scored_companies.json)."""
    return {c["place_id"]: _alias(rank) for rank, c in enumerate(scored, 1)}


def _review_bucket(count) -> str:
    if count in (None, ""):
        return ""
    count = int(count)
    return "under 25" if count < 25 else "25-99" if count < 100 else "100-499" if count < 500 else "500+"


def _rating_bucket(rating) -> str:
    if rating in (None, ""):
        return ""
    rating = float(rating)
    return "5.0" if rating >= 5 else "4.5+" if rating >= 4.5 else "4.0+" if rating >= 4 else "under 4.0"


# Exact review counts / ratings inside Claude's reasons, e.g. "698 reviews",
# "reviews (33)", "4.9★", "4.8 stars", "rating 4.6".
REVIEWS_BEFORE = re.compile(r"\b(\d[\d,]*)(\s+(?:Google\s+)?reviews?)", re.I)
REVIEWS_AFTER = re.compile(r"(reviews?\s*\()(\d[\d,]*)", re.I)
RATING = re.compile(r"\b([1-5]\.\d)(\s*(?:★|stars?))|(rating\s+)([1-5]\.\d)\b", re.I)


def _generalize(text: str) -> str:
    """Swap exact review counts and ratings in free text for their buckets."""
    text = REVIEWS_BEFORE.sub(lambda m: _review_bucket(m[1].replace(",", "")) + m[2], text)
    text = REVIEWS_AFTER.sub(lambda m: m[1] + _review_bucket(m[2].replace(",", "")), text)
    return RATING.sub(
        lambda m: (_rating_bucket(m[1]) + m[2]) if m[1] else (m[3] + _rating_bucket(m[4])), text
    )


def _scrub(text: str, scored: list[dict], aliases: dict[str, str]) -> str:
    """Replace any company's name -- or a distinctive word from it, like
    'Culligan' -- with that company's alias, and bucket exact review counts
    and ratings."""
    text = _generalize(text)
    for c in sorted(scored, key=lambda c: len(c["name"]), reverse=True):
        alias = aliases[c["place_id"]]
        base = re.sub(r",?\s+(inc\.?|llc|co\.?)$", "", c["name"], flags=re.I)
        for variant in (c["name"], base):
            text = re.sub(re.escape(variant), alias, text, flags=re.I)
        for word in re.findall(r"[A-Za-z+']{4,}", base):
            if word.lower() not in GENERIC_NAME_WORDS:
                text = re.sub(rf"\b{re.escape(word)}\b", alias, text, flags=re.I)
    return text


def _console() -> Console:
    return Console(record=True, width=WIDTH, force_terminal=True, color_system="truecolor")


def _save(console: Console, name: str, title: str) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    # Fixed unique_id keeps the SVG byte-identical across reruns (clean diffs).
    console.save_svg(str(DOCS_DIR / name), title=title, unique_id=name.split(".")[0])


def _check(value) -> str:
    return "[green]yes[/]" if value else "[dim]-[/]"


def render_pipeline_run() -> None:
    places = _load(discover.CACHE_PATH)
    serp = _load(enrich.SERP_CACHE_PATH)
    kept = _load(config.DATA_DIR / "raw_companies.json")
    rejected = _load(config.DATA_DIR / "rejected_companies.json")
    enriched = _load(enrich.OUT_PATH)
    scored = _load(score.OUT_PATH)
    drafts = _load(emails.OUT_PATH)
    contact_cache = contacts.load_cache()

    serp_searches = sum(1 for k in serp if " @ " in k)
    lookups = sum(1 for p in places["places"].values() if p.get("source") == "serpapi_map_pack")
    in_map_pack = sum(1 for c in enriched if c["serp"]["map_pack"])
    in_organic = sum(1 for c in enriched if c["serp"]["organic"])
    ad_tag = sum(1 for c in enriched if not c["website_scan"]["error"] and c["website_scan"].get("google_ads_tag"))
    via_gtm = sum(
        1 for c in enriched
        if c["website_scan"].get("google_ads_tag_via") == "GTM"
        or c["website_scan"].get("meta_pixel_via") == "GTM"
    )
    pixel = sum(1 for c in enriched if not c["website_scan"]["error"] and c["website_scan"].get("meta_pixel"))
    with_email = sum(1 for c in scored if contacts.contact_email(c.get("website"), contact_cache))
    buckets = Counter(
        "8-10" if c["score"] >= 8 else "5-7" if c["score"] >= 5 else "1-4" if c["score"] >= 1 else "0"
        for c in scored if c.get("score") is not None
    )

    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold", expand=True)
    table.add_column("Stage", style="bold cyan", no_wrap=True)
    table.add_column("What happened")
    table.add_column("Result", justify="right", style="bold")
    table.add_row(
        "1  discover",
        f"Google Places Text Search: {len(config.SEARCH_CITIES)} cities x "
        f"{len(config.SEARCH_TERMS)} terms, incl. service-area businesses; "
        f"{lookups} map-pack businesses imported from SerpApi",
        f"{len(places['places'])} places",
    )
    table.add_row(
        "", "Filters + dedupe (government, kiosks, plumbers, out-of-area, ...)",
        f"[red]-{len(rejected)}[/] -> [green]{len(kept)}[/] kept",
    )
    table.add_row(
        "2  enrich",
        f"{serp_searches} SerpApi searches (one per city, geo-located); "
        "homepage + GTM container scan",
        f"{in_map_pack} in map pack\n{in_organic} in organic top 10",
    )
    table.add_row(
        "", "Ad signals found on company websites",
        f"{ad_tag} Google Ads tags\n{pixel} Meta Pixels\n({via_gtm} only via GTM)",
    )
    table.add_row(
        "3  score",
        "Claude Haiku rates lead-buying likelihood 1-10 with a one-line reason",
        f"{buckets['8-10']} scored 8-10\n{buckets['5-7']} scored 5-7\n{buckets['1-4']} scored 1-4",
    )
    table.add_row(
        "4  contacts", "Free homepage + contact-page scan for public emails",
        f"{with_email} emails found",
    )
    table.add_row(
        "5  emails", f"Drafts for top {config.TOP_N_FOR_EMAILS} non-competitors (never sent)",
        f"{len(drafts)} drafts",
    )
    table.add_row("6  export", "Ranked CSV for Excel / Sheets", f"{len(scored)} rows")

    console = _console()
    console.print(Panel(
        table,
        title="[bold]Utah Lead Radar[/] -- full pipeline run",
        subtitle="[dim]rendered from cached data[/]",
        border_style="cyan",
    ))
    _save(console, "pipeline_run.svg", "python main.py <stage>")


def render_top_leads() -> None:
    scored = _load(score.OUT_PATH)
    aliases = _aliases(scored)
    top = scored[:10]

    table = Table(box=box.ROUNDED, header_style="bold", expand=True, show_lines=True)
    table.add_column("#", justify="right", style="dim", width=2)
    table.add_column("Company", style="bold", ratio=3)
    table.add_column("Score", justify="center", width=5)
    table.add_column("Map pack", justify="center", width=8)
    table.add_column("Organic", justify="center", width=7)
    table.add_column("Ads tag", justify="center", width=7)
    table.add_column("Pixel", justify="center", width=5)
    table.add_column("Reason (Claude Haiku)", ratio=6)
    for rank, c in enumerate(top, 1):
        serp, scan = c["serp"], c["website_scan"]
        color = "green" if c["score"] >= 8 else "yellow" if c["score"] >= 5 else "red"
        map_cities = len({h["city"] for h in serp["map_pack"]})
        organic = len({h["city"] for h in serp["organic"]})
        table.add_row(
            str(rank),
            aliases[c["place_id"]],
            f"[bold {color}]{c['score']}[/]",
            str(map_cities) if map_cities else "[dim]-[/]",
            str(organic) if organic else "[dim]-[/]",
            _check(not scan["error"] and scan.get("google_ads_tag")),
            _check(not scan["error"] and scan.get("meta_pixel")),
            Text(_scrub(c["score_reason"], scored, aliases), style="dim"),
        )

    console = _console()
    console.print(Panel(
        table,
        title="[bold]Top 10 leads[/] -- ranked by likelihood to buy homeowner leads",
        subtitle="[dim]map pack / organic = number of Utah County city searches[/]",
        border_style="green",
    ))
    _save(console, "top_leads.svg", "python main.py score")


def render_filter_summary() -> None:
    rejected = _load(config.DATA_DIR / "rejected_companies.json")
    kept = _load(config.DATA_DIR / "raw_companies.json")

    groups: Counter = Counter()
    outside: Counter = Counter()
    for r in rejected:
        reason = r["rejected_reason"]
        if reason.startswith("outside city list"):
            groups["outside Utah County city list"] += 1
            outside[reason.split("(")[-1].rstrip(")")] += 1
        elif reason.startswith("duplicate of"):
            groups["duplicate (same phone or address)"] += 1
        elif reason.startswith("excluded:"):
            groups["manually excluded"] += 1
        else:
            groups[reason] += 1

    total = len(rejected) + len(kept)
    peak = max(*groups.values(), len(kept))

    def bar(count: int) -> str:
        return "█" * max(1, round(count / peak * 40))  # full-block character

    table = Table(box=box.SIMPLE, header_style="bold", expand=True)
    table.add_column("Reason", style="bold", ratio=3)
    table.add_column("Count", justify="right", width=5)
    table.add_column("", ratio=4)
    for reason, count in groups.most_common():
        table.add_row(reason, str(count), f"[red]{bar(count)}[/]")
    table.add_row("[green]kept[/]", f"[green]{len(kept)}[/]", f"[green]{bar(len(kept))}[/]")

    note = Text(
        "Outside the city list: " + ", ".join(f"{city} {n}" for city, n in outside.most_common()),
        style="dim",
    )
    console = _console()
    console.print(Panel(
        Group(table, note),
        title=f"[bold]Filtered out and why[/] -- {len(rejected)} of {total} places",
        border_style="red",
    ))
    _save(console, "filter_summary.svg", "python main.py discover")


def write_sample_csv(n: int = 5) -> None:
    export.export_csv()  # refresh data/leads.csv from cached data
    with export.OUT_PATH.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))[:n]
    scored = _load(score.OUT_PATH)
    aliases = _aliases(scored)
    for row in rows:
        # leads.csv rank == position in scored_companies.json, same as the SVG
        row["name"] = _alias(int(row["rank"]))
        row["score_reason"] = _scrub(row["score_reason"], scored, aliases)
        row["rating"] = _rating_bucket(row["rating"])
        row["review_count"] = _review_bucket(row["review_count"])
    SAMPLE_PATH.parent.mkdir(exist_ok=True)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def render_all() -> list:
    render_pipeline_run()
    render_top_leads()
    render_filter_summary()
    write_sample_csv()
    return [DOCS_DIR / "pipeline_run.svg", DOCS_DIR / "top_leads.svg",
            DOCS_DIR / "filter_summary.svg", SAMPLE_PATH]
