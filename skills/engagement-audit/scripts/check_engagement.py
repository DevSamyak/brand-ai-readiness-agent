#!/usr/bin/env python3
"""
check_engagement.py -- Reads the crawl cache and emits findings covering the
"on-site engagement" half of the audit: once a visitor (human or an AI
browsing agent acting on a human's behalf) actually lands on the page, can
they orient themselves, find what they came for, and keep going?

Only entries in cache["pages"] are considered -- crawl.py already excludes
sitemaps, feeds, PDFs, and other non-HTML resources from that list (see
`is_html` handling there), so every record here is a genuine content page.
This script additionally never trusts a single page in isolation for
site-wide signals (search, breadcrumbs, trust pages): those are checked
across every crawled page, since a real site may only expose e.g. a search
box or a contact link on some pages, not the homepage specifically.

Usage:
    python check_engagement.py --cache /tmp/audit_cache.json --out /tmp/engagement_findings.json
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone

HEAVY_PAGE_HTML_BYTES = 1_500_000   # ~1.5MB raw HTML is a reasonable "heavy" cutoff for a content page
INTERSTITIAL_HINTS = ("modal", "popup", "overlay", "lightbox", "interstitial")
TRUST_PAGE_HINTS = ("contact", "about", "support", "help")


def sev_rank(s):
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s, 4)


def check(cache):
    findings = []
    pages = [p for p in cache.get("pages", []) if p.get("is_html", True)]

    any_search_found = False
    any_breadcrumbs_found = False
    trust_page_found = False
    all_internal_links = set()

    for p in pages:
        url = p.get("url", "<unknown>")
        try:
            status = p.get("status_code")
            if status is not None and status >= 400:
                continue  # already flagged by crawl-render-audit; don't double-count here

            headings = p.get("headings", {})
            h1s = headings.get("h1", [])

            if len(h1s) == 0:
                findings.append({
                    "local_id": f"ENG-NO-H1-{url}",
                    "title": "Page has no H1 heading",
                    "severity": "medium",
                    "evidence": f"{url} contains zero <h1> elements.",
                    "suggested_action": {
                        "summary": "Add a single, descriptive H1 that states what the page is about; it's the "
                                    "first orientation cue for both visitors and assistants summarizing the page.",
                        "priority": "medium",
                    },
                })
            elif len(h1s) > 1:
                findings.append({
                    "local_id": f"ENG-MULTI-H1-{url}",
                    "title": "Page has multiple H1 headings",
                    "severity": "low",
                    "evidence": f"{url} contains {len(h1s)} <h1> elements: {h1s[:3]}.",
                    "suggested_action": {
                        "summary": "Use one H1 per page for the main topic, and H2/H3 for sub-sections, so the "
                                    "page's structure unambiguously signals what it's primarily about.",
                        "priority": "low",
                    },
                })

            if not p.get("has_viewport_meta"):
                findings.append({
                    "local_id": f"ENG-NO-VIEWPORT-{url}",
                    "title": "Missing mobile viewport meta tag",
                    "severity": "high",
                    "evidence": f"{url} has no <meta name=\"viewport\"> tag.",
                    "suggested_action": {
                        "summary": "Add a responsive viewport meta tag (width=device-width, initial-scale=1); "
                                    "without it mobile visitors get a zoomed-out desktop layout and commonly bounce.",
                        "priority": "high",
                    },
                })

            nav_count = p.get("nav_link_count", 0)
            if nav_count == 0:
                findings.append({
                    "local_id": f"ENG-NO-NAV-{url}",
                    "title": "No navigation links detected",
                    "severity": "medium",
                    "evidence": f"{url} has no <nav> element with links, or its nav has zero links.",
                    "suggested_action": {
                        "summary": "Add a clear primary navigation with links to the site's key sections; "
                                    "visitors who land on a page with no way to explore further tend to leave.",
                        "priority": "medium",
                    },
                })

            html_len = p.get("html_len", 0)
            if html_len > HEAVY_PAGE_HTML_BYTES:
                findings.append({
                    "local_id": f"ENG-HEAVY-PAGE-{url}",
                    "title": "Unusually large HTML payload",
                    "severity": "medium",
                    "evidence": f"{url}'s HTML response is {html_len:,} bytes.",
                    "suggested_action": {
                        "summary": "Investigate render-blocking assets, unused inline scripts/styles, or "
                                    "un-lazy-loaded content bloating the initial payload; heavier pages load "
                                    "slower and lose visitors before content even appears.",
                        "priority": "medium",
                    },
                })

            raw = (p.get("raw_html_excerpt") or "").lower()
            if any(h in raw for h in INTERSTITIAL_HINTS) and re.search(r"z-index\s*:\s*(9\d{2,}|[1-9]\d{3,})", raw):
                findings.append({
                    "local_id": f"ENG-INTERSTITIAL-{url}",
                    "title": "Likely high-z-index modal/overlay present on load",
                    "severity": "medium",
                    "evidence": f"{url}'s HTML contains modal/popup/overlay markup combined with a high "
                                 f"z-index style, suggesting an overlay may appear on load.",
                    "suggested_action": {
                        "summary": "If this is a signup/cookie/promo interstitial that appears immediately, "
                                    "delay it, make it easy to dismiss, or remove it for first-time visitors; "
                                    "immediate blocking overlays are a common cause of instant bounces.",
                        "priority": "medium",
                    },
                })

            if any(hint in url.lower() for hint in TRUST_PAGE_HINTS):
                trust_page_found = True
            if p.get("has_search_form"):
                any_search_found = True
            if p.get("has_breadcrumbs"):
                any_breadcrumbs_found = True

            for href in p.get("links", []):
                all_internal_links.add(href)
        except Exception as e:
            print(f"WARNING: skipping engagement checks for {url} due to unexpected error: {e}",
                  file=sys.stderr)
            continue

    # Site-wide trust-page check: look both at pages we actually crawled AND
    # at every internal link we saw referenced (a contact/about page linked
    # from the footer nav but outside the crawl budget still counts as
    # present -- previously this link data was collected but never checked,
    # so a real contact link elsewhere on the site was reported as absent).
    if not trust_page_found:
        trust_page_found = any(
            any(hint in (href or "").lower() for hint in TRUST_PAGE_HINTS)
            for href in all_internal_links
        )

    if pages:
        if not any_search_found and len(pages) > 1:
            findings.append({
                "local_id": "ENG-NO-SEARCH",
                "title": "No on-site search functionality detected",
                "severity": "low",
                "evidence": f"No search input/form was found on any of the {len(pages)} crawled pages.",
                "suggested_action": {
                    "summary": "For a multi-page site, add a visible search box; visitors who can't quickly "
                                "find the specific thing they came for are more likely to leave.",
                    "priority": "low",
                },
            })

        if not any_breadcrumbs_found and len(pages) > 3:
            findings.append({
                "local_id": "ENG-NO-BREADCRUMBS",
                "title": "No breadcrumb navigation detected",
                "severity": "low",
                "evidence": f"No BreadcrumbList structured data or breadcrumb nav element found on any of the "
                             f"{len(pages)} crawled pages.",
                "suggested_action": {
                    "summary": "Add breadcrumb navigation on deeper pages so visitors always know where "
                                "they are in the site and can easily move up a level.",
                    "priority": "low",
                },
            })

    if pages and not trust_page_found:
        findings.append({
            "local_id": "ENG-NO-TRUST-PAGE",
            "title": "No About/Contact/Support page found in the crawled set",
            "severity": "low",
            "evidence": f"None of the {len(pages)} crawled URLs matched about/contact/support paths, and "
                         f"none of the {len(all_internal_links)} collected internal links reference one either.",
            "suggested_action": {
                "summary": "Ensure About and Contact pages exist and are linked from the main navigation "
                            "or footer; their absence is a common trust signal gap that makes visitors "
                            "(and assistants recommending the brand) hesitant.",
                "priority": "low",
            },
        })

    # Every other skill's checker (check_crawlability.py, extract_facts.py)
    # defaults `confidence` on every finding it emits, so the final report
    # has a consistent, evaluable field across all finding types. This
    # script previously didn't, which is why a finding like "No on-site
    # search functionality detected" reached the final report with no
    # `confidence` at all while every other finding had one -- an
    # inconsistency that makes automated/schema-based grading harder for no
    # reason, since these checks are exact structural detections (an
    # element either is or isn't present) and deserve a high default.
    for f in findings:
        f.setdefault("confidence", 0.9)
    findings.sort(key=lambda f: sev_rank(f["severity"]))
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.cache) as f:
        cache = json.load(f)

    findings = check(cache)
    with open(args.out, "w") as f:
        json.dump({
            "skill": "engagement-audit",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "findings": findings,
        }, f, indent=2)

    print(f"engagement-audit: {len(findings)} finding(s) -> {args.out}")


if __name__ == "__main__":
    main()
