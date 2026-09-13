#!/usr/bin/env python3
"""
check_crawlability.py -- Reads the JSON cache produced by crawl.py and emits
findings covering the "off-site discoverability" half of the audit:
  1. Can the crawler get in at all? (robots.txt, status codes, noindex)
  2. Can it read what's on the page? (JS-render gaps, text-to-HTML ratio)
  3. Can it pick out a specific, quotable fact? (structured data, facts
     locked in non-text like images/video, missing summary metadata)

Each finding maps to the shared report schema:
  { id, title, severity, evidence, suggested_action }

Usage:
    python check_crawlability.py --cache /tmp/audit_cache.json --out /tmp/crawl_findings.json
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

JS_SHELL_TEXT_RATIO_THRESHOLD = 0.02   # visible-text / html-size below this + SPA marker = strong signal
LOW_TEXT_RATIO_THRESHOLD = 0.06        # softer signal even without an SPA marker
THIN_CONTENT_CHARS = 150               # pages with less visible text than this are effectively empty to a reader
CRITICAL_THIN_CHARS = 300              # below this, near-zero-text is itself strong evidence (not just a heuristic)

# Locale-root paths like /in, /in/, /en-gb, /uk -- a 2-5 char locale segment
# with nothing after it is still "the homepage", just a localized one.
LOCALE_ROOT_RE = re.compile(r"^/[a-z]{2}(-[a-z]{2,3})?/?$", re.I)

PAGE_TYPE_PATTERNS = [
    ("account", re.compile(r"/(account|login|signin|sign-in|cart|checkout|profile|my-)", re.I)),
    ("search_or_api", re.compile(r"/(api/|search\?|/search$)", re.I)),
    ("legal", re.compile(r"/(privacy|terms|legal|cookie-policy|accessibility)", re.I)),
    ("product", re.compile(r"/(product|shop|store|buy|pricing|price|plans?)/?", re.I)),
    ("about", re.compile(r"/(about|company|who-we-are)", re.I)),
]


JSONLD_TYPE_TO_PAGE_TYPE = {
    # schema.org @type -> our page_type label. This is a structural, generic
    # signal (any site emitting these types means the same thing regardless
    # of industry/framework), unlike matching on a URL keyword or product
    # name, which doesn't generalize across sites.
    "product": "product", "offer": "product", "aggregateoffer": "product",
    "article": "article", "blogposting": "article", "newsarticle": "article",
    "faqpage": "article",
    "aboutpage": "about",
}


def classify_page_type_from_jsonld(page):
    """Return a page_type inferred from schema.org @type in the page's
    JSON-LD, or None if nothing usable is present. Checked as a secondary
    signal alongside the URL pattern (see classify_page_type) rather than as
    a replacement for it -- a URL match is cheap and rarely wrong, but many
    real product/article pages don't put the word "product" or "blog" in
    their URL at all (e.g. https://example.com/mac), so URL-only matching
    silently buckets them into generic "content" and skews downstream
    JSON-LD/freshness severity for exactly the pages that matter most.
    """
    for block in page.get("json_ld_blocks", []) if page else []:
        parsed = block.get("parsed")
        objs = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            candidates = [obj.get("@type")]
            graph = obj.get("@graph")
            if isinstance(graph, list):
                candidates.extend(g.get("@type") for g in graph if isinstance(g, dict))
            for t in candidates:
                types = t if isinstance(t, list) else [t]
                for ty in types:
                    if isinstance(ty, str) and ty.lower() in JSONLD_TYPE_TO_PAGE_TYPE:
                        return JSONLD_TYPE_TO_PAGE_TYPE[ty.lower()]
    return None


def classify_page_type(url, page=None, is_start_page=False):
    """Classify a page's semantic type from its URL path first, then from
    structural JSON-LD evidence on the page itself.

    Deliberately checks the actual URL path *before* falling back to
    `is_start_page`. `is_start_page` only means "this was the audit's crawl
    entrypoint" -- e.g. auditing https://example.com/in/store starts the
    crawl at /in/store, but /in/store is a store/product listing, not the
    site's homepage. Treating "first page crawled" as "the homepage" would
    silently mislabel entry pages like that and skew page-type-dependent
    severity (JSON-LD, freshness) for them.

    `page` (the crawl-cache record, optional) lets a page with no URL-pattern
    match still be classified from its own declared schema.org type (e.g. a
    Product-schema page at a URL with no "product"/"shop"/"pricing" keyword
    in it) instead of falling into the generic "content" bucket. This is a
    structural signal, not a per-site keyword list, so it generalizes to
    sites whose URLs don't happen to match the (necessarily incomplete)
    keyword patterns below.
    """
    path = urlparse(url).path or "/"
    if path in ("", "/") or LOCALE_ROOT_RE.match(path):
        return "homepage"
    for label, pattern in PAGE_TYPE_PATTERNS:
        if pattern.search(path):
            return label
    from_jsonld = classify_page_type_from_jsonld(page)
    if from_jsonld:
        return from_jsonld
    if is_start_page:
        # Entrypoint wasn't the root and didn't match a known pattern --
        # treat as generic content, not "homepage".
        return "content"
    return "content"


def sev_rank(s):
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s, 4)


def check(cache):
    findings = []
    # crawl.py already excludes sitemaps/feeds/PDFs/etc. from cache["pages"]
    # (see its `is_html` handling); the `is_html` filter here is defense in
    # depth in case an older cache or a future crawler change lets a
    # non-HTML record slip through -- structural checks (JSON-LD, alt text,
    # JS-shell heuristics) only make sense against genuine HTML pages.
    pages = [p for p in cache.get("pages", []) if p.get("is_html", True)]
    site = cache.get("root_netloc", cache.get("start_url", "unknown"))

    # --- Gate 1: crawlability -------------------------------------------------
    if not cache.get("robots_txt_found"):
        findings.append({
            "local_id": "CR-ROBOTS-MISSING",
            "title": "No robots.txt found",
            "severity": "low",
            "evidence": f"No robots.txt was retrievable at {cache.get('robots_url')}.",
            "suggested_action": {
                "summary": "Publish a robots.txt that explicitly allows crawlers to the pages you want "
                            "AI assistants to cite, and points to your sitemap.xml.",
                "priority": "low",
            },
        })

    if cache.get("skipped_by_robots"):
        n = len(cache["skipped_by_robots"])
        sample = cache["skipped_by_robots"][:5]
        findings.append({
            "local_id": "CR-ROBOTS-BLOCKED",
            "title": "robots.txt blocks pages that were discoverable via internal links/sitemap",
            "severity": "high",
            "evidence": f"{n} linked/sitemap URL(s) are disallowed for this crawler, e.g.: {sample}.",
            "suggested_action": {
                "summary": "Audit robots.txt Disallow rules; anything a human visitor can navigate to and "
                            "that represents real brand content should not be blocked from crawlers.",
                "priority": "high",
            },
        })

    if not cache.get("sitemap_url"):
        findings.append({
            "local_id": "CR-NO-SITEMAP",
            "title": "No sitemap.xml discoverable",
            "severity": "medium",
            "evidence": "Neither robots.txt nor /sitemap.xml exposed a sitemap during the crawl.",
            "suggested_action": {
                "summary": "Publish an XML sitemap listing key pages (product/pricing/docs/about) and "
                            "reference it from robots.txt, so crawlers can find pages efficiently instead "
                            "of relying on link discovery alone.",
                "priority": "medium",
            },
        })

    if cache.get("fetch_errors"):
        for err in cache["fetch_errors"][:5]:
            findings.append({
                "local_id": f"CR-FETCH-ERROR-{err['url']}",
                "title": "Page unreachable during crawl",
                "severity": "medium",
                "evidence": f"{err['url']} failed to load: {err['error']}",
                "suggested_action": {
                    "summary": "Investigate connectivity/SSL/DNS for this URL; an unreachable page is "
                                "invisible to both crawlers and AI assistants that browse live.",
                    "priority": "medium",
                },
            })

    # Broken non-HTML resources (linked PDFs, feeds, API endpoints, sitemaps
    # that returned an error) are still a real discoverability problem --
    # a dead link is a dead link regardless of content type -- but they must
    # never be run through the HTML-structure checks below (they're kept out
    # of `pages` for exactly that reason).
    for res in cache.get("non_html_resources", []):
        url, status = res.get("url"), res.get("status_code")
        if status is not None and status >= 400:
            findings.append({
                "local_id": f"CR-STATUS-{url}",
                "title": f"Linked resource returns HTTP {status}",
                "severity": "high" if status >= 500 else "medium",
                "evidence": f"{url} (a linked non-HTML resource) returned status {status}.",
                "suggested_action": {
                    "summary": "Fix the broken link or remove the reference; a dead linked resource is a "
                                "dead end for both visitors and crawlers.",
                    "priority": "medium",
                },
            })

    for i, p in enumerate(pages):
        url = p.get("url", "<unknown>")
        page_type = classify_page_type(url, page=p, is_start_page=(i == 0))
        try:
            status = p.get("status_code")

            if status is not None and status >= 400:
                findings.append({
                    "local_id": f"CR-STATUS-{url}",
                    "title": f"Page returns HTTP {status}",
                    # `i == 0` (the crawl's start page) is a deliberate escalation, not
                    # `p is pages[0]` -- `pages` here is a freshly filtered list each
                    # call, so identity/index-0 comparisons must use the loop index.
                    "severity": "high" if status >= 500 or i == 0 else "medium",
                    "evidence": f"{url} returned status {status}.",
                    "suggested_action": {
                        "summary": "Fix the broken route or redirect it to the correct live page; a "
                                    "non-200 response means crawlers and cited links both fail here.",
                        "priority": "high",
                    },
                })
                continue  # remaining checks assume readable content

            if p.get("robots_meta") and re.search(r"\bnoindex\b", p["robots_meta"]):
                findings.append({
                    "local_id": f"CR-NOINDEX-META-{url}",
                    "title": "Page is marked noindex",
                    "severity": "critical",
                    "evidence": f"{url} has <meta name=\"robots\" content=\"{p['robots_meta']}\">.",
                    "suggested_action": {
                        "summary": "Remove the noindex directive if this page should be discoverable; "
                                    "noindex pages are deliberately excluded from search and AI-citation surfaces.",
                        "priority": "critical",
                    },
                })

            if p.get("x_robots_tag") and re.search(r"\bnoindex\b", p["x_robots_tag"], re.I):
                findings.append({
                    "local_id": f"CR-NOINDEX-HEADER-{url}",
                    "title": "X-Robots-Tag header blocks indexing",
                    "severity": "critical",
                    "evidence": f"{url} sent header X-Robots-Tag: {p['x_robots_tag']}.",
                    "suggested_action": {
                        "summary": "Remove the noindex value from the X-Robots-Tag response header for pages "
                                    "that should be citable.",
                        "priority": "critical",
                    },
                })

            # --- Gate 2: can the crawler read the content? ---------------------
            ratio = p.get("text_to_html_ratio", 1)
            text_len = p.get("text_len", 0)
            thin = text_len < THIN_CONTENT_CHARS
            requires_js_phrase = bool(p.get("requires_js_phrase"))
            if p.get("spa_mount_present") and (ratio < JS_SHELL_TEXT_RATIO_THRESHOLD or thin):
                # This is a raw-HTML (non-JS-executing) crawl, so an empty-looking mount
                # point is evidence of what a non-JS client sees, not a proven rendering
                # failure -- a genuine app shell can still hydrate into full content.
                # Only escalate to "critical, high confidence" when there's independent
                # corroborating evidence (an explicit "enable JavaScript" message) rather
                # than raw thinness alone, since thin raw HTML alone can't distinguish a
                # broken SPA from one that just hydrates client-side as designed.
                if requires_js_phrase:
                    severity, confidence = "critical", 0.9
                elif text_len < CRITICAL_THIN_CHARS:
                    severity, confidence = "high", 0.7
                else:
                    severity, confidence = "high", 0.5
                findings.append({
                    "local_id": f"CR-JS-SHELL-{url}",
                    "title": "Page content is likely assembled client-side (JS shell)",
                    "severity": severity,
                    "evidence": (f"{url}: a client-side mount point (root/app/__next-style element) was found, "
                                  f"but the raw HTML contains only {text_len} characters of visible "
                                  f"text (text-to-HTML ratio {ratio}). A crawler or assistant that fetches HTML "
                                  f"without executing JavaScript would see almost no content here. This crawler "
                                  f"only fetches raw HTML and does not execute JavaScript, so this finding "
                                  f"reflects what a non-JS crawler sees, not a confirmed rendering failure; "
                                  f"verify with a browser-rendered fetch of the same URL before treating this "
                                  f"as confirmed."),
                    "suggested_action": {
                        "summary": "Server-side render (SSR) or statically pre-render (SSG) the key content -- "
                                    "at minimum the primary facts (name, offering, pricing, contact) -- so it's "
                                    "present in the initial HTML response, not only assembled after JS runs. "
                                    "If unsure whether this is a real problem, fetch the same URL with a "
                                    "headless browser and compare rendered text length to the raw-HTML figure above.",
                        "priority": severity,
                    },
                    "confidence": confidence,
                    "verification_required": True,
                })
            elif ratio < LOW_TEXT_RATIO_THRESHOLD and thin:
                findings.append({
                    "local_id": f"CR-THIN-CONTENT-{url}",
                    "title": "Very little readable text in the raw HTML response",
                    "severity": "medium",
                    "evidence": f"{url} has only {text_len} characters of visible text "
                                 f"(ratio {ratio}) in the raw HTML.",
                    "suggested_action": {
                        "summary": "Check whether this page's real content depends on JavaScript, lazy-loading, "
                                    "or an iframe; ensure the core facts are present as plain text on load.",
                        "priority": "medium",
                    },
                    "confidence": 0.6,
                    "verification_required": True,
                })

            if requires_js_phrase:
                findings.append({
                    "local_id": f"CR-JS-PHRASE-{url}",
                    "title": "Page explicitly tells visitors to enable JavaScript",
                    "severity": "high",
                    "evidence": f"{url} contains a 'please enable JavaScript' style message in its rendered "
                                 f"text, meaning non-JS clients see a placeholder instead of content.",
                    "suggested_action": {
                        "summary": "Provide a no-JS fallback with the essential content, or SSR the page so "
                                    "the substance isn't gated behind script execution.",
                        "priority": "high",
                    },
                    "confidence": 0.9,
                })

            # --- Gate 3: can the crawler pick out a specific fact? --------------
            ld_blocks = p.get("json_ld_blocks", [])
            broken_ld = [b for b in ld_blocks if not b.get("parsed_ok")]

            if not ld_blocks:
                # Absence of JSON-LD is near-certain (detection_confidence); whether it's
                # a *meaningful* discoverability problem depends heavily on page type --
                # a missing Organization schema on the homepage or a missing Product/Offer
                # schema matters far more than the same absence on a legal or account page.
                ld_severity_by_type = {
                    "homepage": "medium", "about": "medium",
                    "product": "high",
                    "legal": "low", "account": "low", "search_or_api": "low",
                    "content": "medium",
                }
                impact_confidence_by_type = {
                    "homepage": 0.7, "about": 0.7,
                    "product": 0.8,
                    "legal": 0.9, "account": 0.9, "search_or_api": 0.9,
                    "content": 0.6,
                }
                severity = ld_severity_by_type.get(page_type, "medium")
                findings.append({
                    "local_id": f"CR-NO-JSONLD-{url}",
                    "title": "No JSON-LD structured data",
                    "severity": severity,
                    "evidence": f"{url} (page type: {page_type}) contains zero "
                                 f"<script type=\"application/ld+json\"> blocks. Detection is certain; "
                                 f"severity reflects how much this page type typically relies on structured "
                                 f"data for AI discoverability -- a site can still be machine-readable via "
                                 f"other signals (microdata, well-marked-up HTML, meta tags) even without JSON-LD.",
                    "suggested_action": {
                        "summary": "Add schema.org JSON-LD appropriate to this page (Organization on "
                                    "homepage/about, Product+Offer on product pages, Article on blog posts, "
                                    "FAQPage where applicable) so assistants can extract precise, structured "
                                    "facts rather than guessing from prose.",
                        "priority": severity,
                    },
                    "confidence": 0.95,
                    "impact_confidence": impact_confidence_by_type.get(page_type, 0.6),
                    "page_type": page_type,
                })
            elif broken_ld:
                findings.append({
                    "local_id": f"CR-INVALID-JSONLD-{url}",
                    "title": "JSON-LD block present but fails to parse",
                    "severity": "medium",
                    "evidence": f"{url} has {len(broken_ld)} malformed JSON-LD block(s), "
                                 f"e.g. error: {broken_ld[0].get('error')}",
                    "suggested_action": {
                        "summary": "Fix the JSON syntax error(s); invalid JSON-LD is silently ignored by "
                                    "parsers, which is functionally identical to having none.",
                        "priority": "medium",
                    },
                    "confidence": 0.95,
                    "page_type": page_type,
                })

            if not p.get("meta_description") and not p.get("og_tags", {}).get("og:description"):
                findings.append({
                    "local_id": f"CR-NO-SUMMARY-{url}",
                    "title": "No meta description or og:description",
                    "severity": "low",
                    "evidence": f"{url} has neither <meta name=\"description\"> nor an og:description tag.",
                    "suggested_action": {
                        "summary": "Add a concise, factual 1-2 sentence meta description; assistants often "
                                    "lean on this as a ready-made, quotable summary of the page.",
                        "priority": "low",
                    },
                })

            images_missing_alt = p.get("images_missing_alt", 0)
            image_count = p.get("image_count", 0)
            if image_count >= 3 and images_missing_alt / max(image_count, 1) > 0.6:
                findings.append({
                    "local_id": f"CR-IMG-ALT-{url}",
                    "title": "Most images lack alt text",
                    "severity": "medium",
                    "evidence": f"{url}: {images_missing_alt}/{image_count} images have no alt attribute. "
                                 f"Detection of missing alt attributes is exact; not every one of them is "
                                 f"necessarily a meaningful AI-readability defect, since some images are "
                                 f"purely decorative rather than information-bearing.",
                    "suggested_action": {
                        "summary": "Add descriptive alt text to images that carry real information (specs, "
                                    "pricing tables rendered as images, infographics). Any fact that only "
                                    "exists inside a picture is invisible to a text-based reader.",
                        "priority": "medium",
                    },
                    "confidence": 0.95,       # detection: the attribute is genuinely absent
                    "impact_confidence": 0.65,  # not all of those images necessarily carry real information
                })

            if p.get("video_embed_count", 0) > 0:
                # Heuristic: if there's a video but the surrounding text is thin, the facts in
                # the video are probably not duplicated anywhere readable.
                if p.get("text_len", 0) < 400:
                    findings.append({
                        "local_id": f"CR-VIDEO-NO-TRANSCRIPT-{url}",
                        "title": "Video content with little to no surrounding text",
                        "severity": "medium",
                        "evidence": f"{url} embeds {p['video_embed_count']} video(s) but has only "
                                     f"{p.get('text_len', 0)} characters of surrounding visible text.",
                        "suggested_action": {
                            "summary": "Add a text transcript or a written summary of the video's key claims "
                                        "near the embed; facts locked inside audio/video are not extractable "
                                        "by text-based readers.",
                            "priority": "medium",
                        },
                    })
        except Exception as e:
            # One malformed record should never take down the whole skill --
            # log it and keep going so the rest of the report still gets
            # produced, rather than silently emitting zero findings (which
            # looks identical to "no problems found").
            print(f"WARNING: skipping crawlability checks for {url} due to unexpected error: {e}",
                  file=sys.stderr)
            continue

    for f in findings:
        f.setdefault("confidence", 0.95)  # deterministic checks (status codes, robots, meta tags) default high
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
            "skill": "crawl-render-audit",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "findings": findings,
        }, f, indent=2)

    print(f"crawl-render-audit: {len(findings)} finding(s) -> {args.out}")


if __name__ == "__main__":
    main()
