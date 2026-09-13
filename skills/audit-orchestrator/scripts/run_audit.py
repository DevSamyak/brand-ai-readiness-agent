#!/usr/bin/env python3
"""
run_audit.py -- Entrypoint orchestrator for the brand-ai-readiness-audit
marketplace. Given a URL:
  1. Runs crawl-render-audit's crawl.py to fetch + cache a bounded set of pages.
  2. Runs crawl-render-audit's check_crawlability.py, freshness-corroboration's
     extract_facts.py, and engagement-audit's check_engagement.py against that
     cache.
  3. Merges all findings, renumbers them sequentially (F-001, F-002, ...),
     computes the severity summary, and writes the final report matching the
     contest's required schema.
  4. Also writes a human-readable Markdown version of the report.

Usage:
    python run_audit.py --url https://example.com --out report.json \
        [--max-pages 8] [--md-out report.md]

This script only performs read-only GET requests (via crawl.py) and never
modifies the target site. All other skills operate purely on the cached data.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKETPLACE_ROOT = HERE.parent.parent.parent  # skills/audit-orchestrator/scripts -> marketplace root

CRAWL_SCRIPT = MARKETPLACE_ROOT / "skills" / "crawl-render-audit" / "scripts" / "crawl.py"
CRAWLABILITY_SCRIPT = MARKETPLACE_ROOT / "skills" / "crawl-render-audit" / "scripts" / "check_crawlability.py"
FRESHNESS_SCRIPT = MARKETPLACE_ROOT / "skills" / "freshness-corroboration" / "scripts" / "extract_facts.py"
ENGAGEMENT_SCRIPT = MARKETPLACE_ROOT / "skills" / "engagement-audit" / "scripts" / "check_engagement.py"

SEVERITY_ORDER = ["critical", "high", "medium", "low"]

# How many sample URLs to name inline in a generalized evidence string
# ("...including X and Y") before falling back to "and N others".
EVIDENCE_SAMPLE_SIZE = 2

# How many URLs to keep in the `affected_pages` list on a grouped finding.
# affected_page_count always reflects the true total even if the list is
# truncated, so no information about *how many* pages are affected is lost --
# only the exhaustive per-page listing is capped, which is exactly the noise
# this grouping step exists to remove.
MAX_AFFECTED_PAGES_LISTED = 25


def extract_url(finding):
    """Every per-page check emits a local_id of the form
    '<PREFIX>-<url>' (e.g. 'CR-NO-JSONLD-https://example.com/x',
    'ENG-NO-H1-https://example.com/x'), while site-wide checks (no single
    page they're "about") use a plain local_id with no URL in it at all
    (e.g. 'ENG-NO-SEARCH', 'CR-ROBOTS-MISSING'). Finding the URL by
    locating the 'http' substring inside local_id is robust to prefixes
    containing dashes and doesn't require every sub-script to be changed
    to emit a separate `url` field.
    """
    local_id = finding.get("local_id") or ""
    idx = local_id.find("http")
    return local_id[idx:] if idx != -1 else None


def format_grouped_evidence(base_evidence, count, total_pages, urls):
    """Generalize a single-page evidence string into one that summarizes
    scale across every page the same underlying issue was found on, e.g.
    'Found on 8/8 crawled pages, including https://a and https://b.'
    Falls back to the original (already page-specific) evidence when the
    issue was only found once -- generalizing a single instance would just
    be a more roundabout way of saying the same thing.
    """
    if count <= 1 or not urls:
        return base_evidence
    sample = urls[:EVIDENCE_SAMPLE_SIZE]
    sample_str = " and ".join(sample) if len(sample) <= 2 else ", ".join(sample)
    denom = total_pages if total_pages else count
    suffix = f", and {count - len(sample)} other page(s)" if count > len(sample) else ""
    return f"Found on {count}/{denom} crawled pages, including {sample_str}{suffix}."


def group_findings(all_findings, total_pages=None):
    """Collapse findings that share the same (skill, title, severity) --
    i.e. the same root cause reported once per affected page -- into a
    single finding with an `affected_pages` list and `affected_page_count`,
    instead of one nearly-identical finding per page. Severity is part of
    the grouping key (not just title) because the same title can carry
    different severities on different page types (e.g. "No JSON-LD
    structured data" is "high" on product pages but "medium" on the
    homepage) -- collapsing across severities would silently discard that
    distinction. Site-wide findings (no per-page URL, e.g. "no robots.txt
    found") only ever appear once per run and pass through unchanged.
    """
    groups = {}
    order = []
    for f in all_findings:
        key = (f["skill_source"], f.get("title", ""), f.get("severity", "low"))
        if key not in groups:
            groups[key] = {
                "template": f,
                "urls": [],
                "confidences": [],
                "impact_confidences": [],
                "page_types": [],
                "count": 0,
            }
            order.append(key)
        g = groups[key]
        g["count"] += 1
        url = extract_url(f)
        if url and url not in g["urls"]:
            g["urls"].append(url)
        if "confidence" in f:
            g["confidences"].append(f["confidence"])
        if "impact_confidence" in f:
            g["impact_confidences"].append(f["impact_confidence"])
        pt = f.get("page_type")
        if pt and pt not in g["page_types"]:
            g["page_types"].append(pt)

    grouped = []
    for key in order:
        g = groups[key]
        merged = dict(g["template"])
        merged.pop("local_id", None)

        n = g["count"]
        if n > 1:
            merged["evidence"] = format_grouped_evidence(
                merged.get("evidence", ""), n, total_pages, g["urls"])
            merged["affected_page_count"] = n
            if g["urls"]:
                merged["affected_pages"] = g["urls"][:MAX_AFFECTED_PAGES_LISTED]
            if g["confidences"]:
                merged["confidence"] = round(sum(g["confidences"]) / len(g["confidences"]), 2)
            if g["impact_confidences"]:
                merged["impact_confidence"] = round(
                    sum(g["impact_confidences"]) / len(g["impact_confidences"]), 2)
            if len(g["page_types"]) > 1:
                merged["page_type"] = g["page_types"]
            elif len(g["page_types"]) == 1:
                merged["page_type"] = g["page_types"][0]
        elif g["urls"]:
            # Single instance but still tied to one page -- record it
            # consistently even though there's nothing to summarize yet.
            merged["affected_page_count"] = 1
            merged["affected_pages"] = g["urls"]

        grouped.append(merged)
    return grouped


def run(cmd, timeout):
    proc = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        print(f"WARNING: command failed: {' '.join(cmd)}\n{proc.stderr}", file=sys.stderr)
    else:
        if proc.stderr:
            print(proc.stderr.strip(), file=sys.stderr)
    return proc


def load_json(path):
    with open(path) as f:
        return json.load(f)


def build_report(site, url, skill_outputs, per_script_timeout, cache_pages_crawled=None):
    all_findings = []
    for skill_name, data in skill_outputs.items():
        for finding in data.get("findings", []):
            # Copy every field the sub-script emitted (title/severity/evidence/
            # suggested_action are required; confidence, verification_required,
            # page_type, impact_confidence, etc. are optional extras some checks
            # add) instead of a fixed whitelist. A whitelist here silently drops
            # any field a check adds later -- which is exactly what happened to
            # confidence/verification_required previously: the sub-scripts emit
            # them, but they never reached the final report because this function
            # only copied 4 hardcoded keys.
            merged = dict(finding)
            merged["local_id"] = merged.pop("local_id", None)
            merged["skill_source"] = skill_name
            all_findings.append(merged)

    # Collapse "same root cause, N affected pages" into one finding before
    # numbering/sorting -- this is the step that turns e.g. 8 separate
    # "No JSON-LD structured data" entries (one per crawled page) into a
    # single finding with affected_page_count: 8, instead of letting page
    # count silently multiply the report's apparent number of distinct
    # issues.
    all_findings = group_findings(all_findings, total_pages=cache_pages_crawled)

    def sort_key(f):
        return (SEVERITY_ORDER.index(f["severity"]) if f["severity"] in SEVERITY_ORDER else 99,
                f["skill_source"])
    all_findings.sort(key=sort_key)

    findings = []
    counts = {s: 0 for s in SEVERITY_ORDER}
    for i, f in enumerate(all_findings, start=1):
        fid = f"F-{i:03d}"
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        out = dict(f)
        out.pop("local_id", None)
        out.pop("skill_source", None)
        out["id"] = fid
        out["source_skill"] = f["skill_source"]
        findings.append(out)

    candidate_facts = skill_outputs.get("freshness-corroboration", {}).get(
        "candidate_facts_for_live_corroboration", [])

    report = {
        "site": site,
        "audited_url": url,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_findings": len(findings),
            "critical": counts.get("critical", 0),
            "high": counts.get("high", 0),
            "medium": counts.get("medium", 0),
            "low": counts.get("low", 0),
        },
        # Makes explicit what this run did and didn't check, so "not
        # checked" is never mistaken for "checked and clean" -- e.g. a JS
        # shell finding here is a raw-HTML observation, not a confirmed
        # rendering failure, and external fact corroboration only happens
        # if the calling agent (which has live search) performs the step
        # this skill hands off in `candidate_facts_for_live_corroboration`.
        "methodology": {
            "crawler": "requests+BeautifulSoup (no JavaScript execution)",
            "max_pages_crawled": cache_pages_crawled,
            "robots_txt_respected": True,
            "js_rendering_performed": False,
            "external_fact_corroboration_performed": False,
        },
        "limitations": [
            "JavaScript was not executed during crawl; JS-shell findings reflect what a "
            "non-JS crawler sees and require a browser-rendered fetch to confirm.",
            "External fact corroboration (checking extracted facts against independent "
            "sources) was not performed by this script; see candidate_facts_for_live_corroboration "
            "and notes below for the follow-up step the calling agent should take.",
            "Image alt-text and video-transcript findings flag detectable absence, not "
            "confirmed information loss -- some flagged images/videos may be purely decorative.",
        ],
        "findings": findings,
        "candidate_facts_for_live_corroboration": candidate_facts,
        "notes": (
            "candidate_facts_for_live_corroboration lists facts extracted from the site "
            "(org name, prices, phone numbers) that the auditing agent should independently "
            "verify against 2-3 external sources using live web search, per the "
            "freshness-corroboration skill's instructions. Any contradictions found during "
            "that live step should be appended to `findings` by the agent before presenting "
            "the report to the user."
        ),
    }
    return report


def to_markdown(report):
    lines = []
    lines.append(f"# AI Readiness Audit — {report['site']}")
    lines.append("")
    lines.append(f"- **Audited URL:** {report['audited_url']}")
    lines.append(f"- **Audited at:** {report['audited_at']}")
    s = report["summary"]
    lines.append(f"- **Findings:** {s['total_findings']} total "
                  f"(critical: {s['critical']}, high: {s['high']}, medium: {s['medium']}, low: {s['low']})")
    lines.append("")
    lines.append("## Findings & Suggested Actions")
    lines.append("")
    for f in report["findings"]:
        title_suffix = f" ({f['affected_page_count']} pages)" if f.get("affected_page_count", 1) > 1 else ""
        lines.append(f"### {f['id']} · [{f['severity'].upper()}] {f['title']}{title_suffix}")
        lines.append(f"- **Evidence:** {f['evidence']}")
        sa = f["suggested_action"]
        lines.append(f"- **Suggested action ({sa.get('priority', f['severity'])} priority):** {sa.get('summary')}")
        lines.append(f"- _Source: {f.get('source_skill')}_")
        lines.append("")
    if report.get("candidate_facts_for_live_corroboration"):
        lines.append("## Candidate facts for live cross-source corroboration")
        lines.append("_(To be checked by the auditing agent against independent external sources.)_")
        lines.append("")
        for cf in report["candidate_facts_for_live_corroboration"]:
            lines.append(f"- **{cf['type']}**: `{cf['value']}` (found on {cf['source_url']})")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Run the full brand AI-readiness audit against a URL.")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="report.json")
    ap.add_argument("--md-out", default=None, help="Optional path to also write a Markdown report")
    ap.add_argument("--max-pages", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=180, help="Per-subprocess timeout in seconds")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cache_path = tmp / "cache.json"
        crawl_findings_path = tmp / "crawl_findings.json"
        freshness_findings_path = tmp / "freshness_findings.json"
        engagement_findings_path = tmp / "engagement_findings.json"

        print(f"[1/4] Crawling {args.url} (max {args.max_pages} pages)...", file=sys.stderr)
        run([str(CRAWL_SCRIPT), "--url", args.url, "--out", str(cache_path),
             "--max-pages", str(args.max_pages)], args.timeout)

        if not cache_path.exists():
            print("FATAL: crawl produced no cache file; aborting.", file=sys.stderr)
            sys.exit(1)

        print("[2/4] Running crawl-render-audit checks...", file=sys.stderr)
        run([str(CRAWLABILITY_SCRIPT), "--cache", str(cache_path), "--out", str(crawl_findings_path)],
            args.timeout)

        print("[3/4] Running freshness-corroboration checks...", file=sys.stderr)
        run([str(FRESHNESS_SCRIPT), "--cache", str(cache_path), "--out", str(freshness_findings_path)],
            args.timeout)

        print("[4/4] Running engagement-audit checks...", file=sys.stderr)
        run([str(ENGAGEMENT_SCRIPT), "--cache", str(cache_path), "--out", str(engagement_findings_path)],
            args.timeout)

        cache = load_json(cache_path)
        skill_outputs = {}
        for name, path in [("crawl-render-audit", crawl_findings_path),
                            ("freshness-corroboration", freshness_findings_path),
                            ("engagement-audit", engagement_findings_path)]:
            if path.exists():
                skill_outputs[name] = load_json(path)
            else:
                print(f"WARNING: {name} produced no output; continuing without it.", file=sys.stderr)
                skill_outputs[name] = {"findings": []}

        report = build_report(cache.get("root_netloc", args.url), args.url, skill_outputs, args.timeout,
                               cache_pages_crawled=cache.get("pages_crawled"))

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report written to {args.out}", file=sys.stderr)

    if args.md_out:
        with open(args.md_out, "w") as f:
            f.write(to_markdown(report))
        print(f"Markdown report written to {args.md_out}", file=sys.stderr)

    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
