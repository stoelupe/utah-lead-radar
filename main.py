"""Utah Lead Radar pipeline orchestrator.

Usage:
    python main.py discover --limit 5   # quick test run
    python main.py discover             # full run (skips cached queries)
    python main.py discover --offline   # re-filter cached results, no API calls
    python main.py discover --refresh   # re-run every query, ignoring the cache
    python main.py enrich --limit 2     # SerpApi for the first 2 cities only
    python main.py enrich               # SerpApi for all cities (cached) + website scan
    python main.py score --names "A+" Guardian   # test-score a few (not saved)
    python main.py score                # score all, save data/scored_companies.json
    python main.py contacts             # find public contact emails (cached, free)
    python main.py emails --limit 2     # test-draft 2 emails (not saved; never sent)
    python main.py emails               # draft top 10, save data/email_drafts.json
    python main.py emails --rebuild     # re-apply subject/offer/signature, no API calls
    python main.py export               # write data/leads.csv
    python main.py docs                 # README SVGs + sample CSV, from cache only
"""

import argparse
import json
from collections import Counter

import config
from src import contacts, discover, emails, enrich, export, score


def run_discover(limit, offline, refresh):
    result = discover.find_companies(limit=limit, offline=offline, refresh=refresh)
    companies, rejected = result["companies"], result["rejected"]

    config.DATA_DIR.mkdir(exist_ok=True)
    out_path = config.DATA_DIR / "raw_companies.json"
    out_path.write_text(json.dumps(companies, indent=2))
    rejected_path = config.DATA_DIR / "rejected_companies.json"
    rejected_path.write_text(json.dumps(rejected, indent=2))

    print(f"\n{result['queries_run']} Places queries, {result['raw_count']} unique places in cache")
    print(f"Filtered out {len(rejected)}:")
    for reason, count in Counter(r["rejected_reason"] for r in rejected).most_common():
        print(f"  {count:3d}  {reason}")
    flagged = [c for c in companies if c["flag"]]
    if flagged:
        print(f"Flagged {len(flagged)} (kept):")
        for c in flagged:
            skip = " -- skipped for emails" if discover.skips_email(c) else ""
            print(f"  {c['name']}: {c['flag']}{skip}")
    print(f"Saved {len(companies)} companies to {out_path}")
    print(f"Saved {len(rejected)} rejected to {rejected_path}")


def _yes(hits):
    return f"yes ({len(hits)})" if hits else "-"


def _tag(scan, signal):
    if not scan[signal]:
        return "-"
    return "yes (GTM)" if scan.get(f"{signal}_via") == "GTM" else "yes"


def run_enrich(limit):
    result = enrich.enrich_companies(limit=limit)
    companies = result["companies"]

    print(f"\n{result['searches_run']} SerpApi searches run (rest from cache)")
    header = (
        f"{'Company':40} {'Ads':8} {'LSA':8} {'Map pack':9} {'Organic':8} "
        f"{'Ad tag':10} {'FB pixel':10} Flag"
    )
    print(header)
    print("-" * len(header))
    for c in companies:
        serp, scan = c["serp"], c["website_scan"]
        if scan["error"]:
            ad_tag = fb = "err"
        else:
            ad_tag = _tag(scan, "google_ads_tag")
            fb = _tag(scan, "meta_pixel")
        print(
            f"{c['name'][:40]:40} {_yes(serp['ads']):8} {_yes(serp['lsa']):8} "
            f"{_yes(serp['map_pack']):9} {_yes(serp['organic']):8} {ad_tag:10} {fb:10} "
            f"{c.get('flag') or ''}"
        )
    print(f"\nSaved {len(companies)} companies to {result['out_path']}")


def run_score(limit, names):
    result = score.score_companies(limit=limit, names=names)
    companies = result["companies"]
    print()
    for c in companies:
        s = "-" if c["score"] is None else c["score"]
        print(f"{s:>3}  {c['name'][:40]:40} {c['score_reason']}")
    # A partial (test) run doesn't overwrite a full scored file.
    if limit is None and not names:
        result["out_path"].write_text(json.dumps(companies, indent=2))
        print(f"\nSaved {len(companies)} scored companies to {result['out_path']}")


def run_emails(limit, rebuild):
    result = emails.rebuild_drafts() if rebuild else emails.draft_emails(limit=limit)
    for d in result["drafts"]:
        issues = f"  ** {'; '.join(d['problems'])} **" if d["problems"] else ""
        print(f"\n=== {d['name']} (score {d['score']}, {d['word_count']} words){issues}")
        print(f"To: {d['contact_email'] or '(no email found)'}")
        print(f"Fact: {d['fact_used']}")
        print(f"Subject: {d['subject']}\n")
        print(d["body"])
    conflicts = [d for d in result["drafts"] if d["city_conflict"]]
    if conflicts:
        print("\nCity conflicts (one exclusive spot per city):")
        for d in conflicts:
            print(f"  {d['name']}: {d['city_conflict']}")
    # A partial (test) run doesn't overwrite a full drafts file.
    if limit is None:
        result["out_path"].write_text(json.dumps(result["drafts"], indent=2))
        print(f"\nSaved {len(result['drafts'])} drafts to {result['out_path']} (nothing sent)")


def run_contacts():
    companies = json.loads(score.OUT_PATH.read_text())
    cache = contacts.find_contacts(companies)
    found = [c for c in companies if contacts.contact_email(c.get("website"), cache)]
    with_site = sum(1 for c in companies if c.get("website"))
    print(f"\nFound an email for {len(found)} of {with_site} companies with websites "
          f"({len(companies)} total)")


def run_docs():
    from src import render_docs  # needs rich; keep other stages importable without it

    for path in render_docs.render_all():
        print(f"Wrote {path}")


def run_export():
    result = export.export_csv()
    print(f"Wrote {result['rows']} rows ({result['drafts']} with email drafts) to {result['out_path']}")


def main():
    parser = argparse.ArgumentParser(description="Utah Lead Radar pipeline")
    parser.add_argument(
        "stage",
        choices=["discover", "enrich", "score", "contacts", "emails", "export", "docs"],
        help="pipeline stage to run",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="discover: limit city/search-term query pairs; enrich: limit SerpApi cities; "
        "score/emails: limit companies (for cheap test runs)",
    )
    parser.add_argument(
        "--names", nargs="+", help="score: only companies whose name starts with these"
    )
    parser.add_argument(
        "--offline", action="store_true", help="re-filter cached results without searching"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-run queries even if already cached"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="emails: re-apply subject/offer/signature to saved drafts, no API calls",
    )
    args = parser.parse_args()

    if args.stage == "discover":
        run_discover(limit=args.limit, offline=args.offline, refresh=args.refresh)
    elif args.stage == "enrich":
        run_enrich(limit=args.limit)
    elif args.stage == "score":
        run_score(limit=args.limit, names=args.names)
    elif args.stage == "contacts":
        run_contacts()
    elif args.stage == "emails":
        run_emails(limit=args.limit, rebuild=args.rebuild)
    elif args.stage == "export":
        run_export()
    elif args.stage == "docs":
        run_docs()


if __name__ == "__main__":
    main()
