#!/usr/bin/env python3
"""
extract_facts.py -- Reads the crawl cache and handles the deterministic half
of the "off-site agreement" problem: freshness signals (can a machine tell
this fact is current?) and entity-disambiguation signals (can a machine tell
this brand apart from others sharing its name?).

It ALSO writes out a short list of "candidate facts" (org name, founding
year/claims, address, phone, pricing figures) under `candidate_facts_for_live_corroboration`.
Cross-checking those against independent sources requires live web access,
which this sandboxed script deliberately does NOT perform -- that step is
done by the calling agent (which has real search tools) per the instructions
in this skill's SKILL.md. This keeps the skill's deterministic parts fast,
offline-testable, and free of hidden network calls, while still directing
the agent to do the part that genuinely requires it.

Usage:
    python extract_facts.py --cache /tmp/audit_cache.json --out /tmp/freshness_findings.json
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

DATE_KEYS = ("datemodified", "datepublished", "dateupdated", "uploaddate")
STALE_DAYS_HARD = 730   # >2 years with zero freshness signal on a "living" page = high
STALE_DAYS_SOFT = 365   # >1 year = medium

# Kept in sync with crawl-render-audit/scripts/check_crawlability.py's
# classify_page_type. Duplicated (rather than imported) deliberately -- each
# skill's scripts stay self-contained and independently testable, per this
# marketplace's convention -- but the logic must match so a page gets the
# same type from either skill.
LOCALE_ROOT_RE = re.compile(r"^/[a-z]{2}(-[a-z]{2,3})?/?$", re.I)
PAGE_TYPE_PATTERNS = [
    ("account", re.compile(r"/(account|login|signin|sign-in|cart|checkout|profile|my-)", re.I)),
    ("search_or_api", re.compile(r"/(api/|search\?|/search$)", re.I)),
    ("legal", re.compile(r"/(privacy|terms|legal|cookie-policy|accessibility)", re.I)),
    ("product", re.compile(r"/(product|shop|store|buy|pricing|price|plans?)/?", re.I)),
    ("about", re.compile(r"/(about|company|who-we-are)", re.I)),
]


JSONLD_TYPE_TO_PAGE_TYPE = {
    "product": "product", "offer": "product", "aggregateoffer": "product",
    "article": "article", "blogposting": "article", "newsarticle": "article",
    "faqpage": "article",
    "aboutpage": "about",
}


def classify_page_type_from_jsonld(page):
    """See the identical function in check_crawlability.py for the full
    rationale -- kept in sync here per this file's existing duplication
    convention."""
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
    path = urlparse(url).path or "/"
    if path in ("", "/") or LOCALE_ROOT_RE.match(path):
        return "homepage"
    for label, pattern in PAGE_TYPE_PATTERNS:
        if pattern.search(path):
            return label
    from_jsonld = classify_page_type_from_jsonld(page)
    if from_jsonld:
        return from_jsonld
    return "content"


# Volatile-fact keywords: a page is only "time-sensitive" (i.e. staleness
# actually matters) when it shows evidence of one of these, not merely
# because it contains a 4-digit number that happens to look like a year.
# A bare year match alone flags "Founded in 1984", "iPhone 2026", "Copyright
# 2026", "ISO 9001:2015", and "Windows 2025 support" as equally time-sensitive
# as an actual live price or availability claim, which is far too broad.
VOLATILE_KEYWORDS_RE = re.compile(
    r"\b(price|pricing|discount|sale|on sale|availability|in stock|out of stock|"
    r"subscription|per month|/mo\b|shipping time|delivery time|release date|"
    r"event date|opening hours|business hours|interest rate|exchange rate|"
    r"plan limit|storage limit|current version|latest version|now available)\b",
    re.I,
)
CURRENCY_PRICE_RE = re.compile(
    r"(₹|Rs\.?|INR|USD|\$|EUR|€|GBP|£)\s?(\d[\d,]*(?:\.\d+)?)",
    re.I,
)
CURRENCY_SYMBOL_MAP = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}

# A monetary amount on the page isn't always the product's price -- a
# cashback offer, a minimum-transaction threshold for a bank promo, and a
# subscription fee are different facts with different corroboration needs.
# Classify by the language immediately around the match rather than
# defaulting every currency figure to a bare "price".
FACT_TYPE_KEYWORDS = [
    ("cashback", re.compile(r"cashback|cash back", re.I)),
    ("minimum_transaction_value", re.compile(r"minimum transaction|min\.?\s*transaction|min\.?\s*order", re.I)),
    ("subscription_fee", re.compile(r"/mo\b|per month|/month|subscri", re.I)),
    ("shipping_fee", re.compile(r"shipping|delivery fee", re.I)),
    ("discount", re.compile(r"discount|% off|save up to", re.I)),
]


def classify_fact_type(context_text):
    for label, pattern in FACT_TYPE_KEYWORDS:
        if pattern.search(context_text):
            return label
    return "price"


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def sentence_containing(text, pos):
    """Return just the sentence that contains character offset `pos`, not a
    fixed character window. A fixed +/-N window can bleed into an adjacent,
    unrelated sentence's keyword -- e.g. "...package starts at Rs45,000. Get
    instant cashback of Rs2,000..." would tag the 45,000 *price* as a
    "cashback" fact purely because the word "cashback" happens to fall
    within a generic +/-40 char window of it, even though it belongs to the
    next sentence and a different amount entirely."""
    start = text.rfind(".", 0, pos)
    start2 = max(text.rfind("!", 0, pos), text.rfind("?", 0, pos))
    start = max(start, start2)
    start = start + 1 if start != -1 else 0
    end_candidates = [i for i in (text.find(".", pos), text.find("!", pos), text.find("?", pos)) if i != -1]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    return text[start:end].strip()


# Volatile-fact keywords used both to decide time-sensitivity and to name the
# matched evidence in freshness findings (see VOLATILE_KEYWORDS_RE below).
GENERIC_NAME_WORDLIST = {
    # Extremely common English words that, used alone as a brand name, are
    # highly collision-prone without disambiguating signals. Heuristic only.
    "apple", "prime", "spark", "shift", "flow", "arc", "edge", "core", "pulse",
    "wave", "orbit", "nova", "atlas", "beacon", "bridge", "forge", "summit",
}

# Loosely matches any digit run with phone-like separators. Deliberately
# broad at this stage -- looks_like_phone() below does the real filtering --
# because the previous single-shot regex (`\+?\d[\d\-\s()]{7,}\d`) matched
# any sufficiently long dashed digit string, including plain ISO dates like
# "2026-04-23" pulled from sitemap <lastmod> content. Those got reported as
# `phone_mention` candidate facts, which is a false positive: a date is not
# a phone number.
PHONE_CANDIDATE_RE = re.compile(r"(?<![\d(])(\(?\+?\d[\d\-.\s()]{6,}\d\)?)(?!\d)")
ISO_DATE_RE = re.compile(r"^((?:19|20)\d{2})-(\d{2})-(\d{2})$")
US_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")


def looks_like_phone(candidate):
    """Distinguish an actual phone number from any other dashed/spaced digit
    run (dates, IDs, tracking numbers, zip+4, etc.) that a broad regex would
    also match."""
    c = candidate.strip()
    digits_only = re.sub(r"\D", "", c)
    if not (7 <= len(digits_only) <= 15):
        return False

    m = ISO_DATE_RE.match(c)
    if m:
        month, day = int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return False  # e.g. "2026-04-23" -- a date, not a phone number
    if US_DATE_RE.match(c):
        return False  # e.g. "04/23/2026"

    if c.startswith("+"):
        return True

    # Parenthesized area code: only a valid phone shape when the parens wrap
    # a 2-4 digit group at the very START of the candidate, immediately
    # followed by a separator and more digits, e.g. "(016) 707-1234". A
    # trailing, unseparated parenthetical like "016707(1)" or "016707(14)"
    # is a footnote/citation marker attached to a number, not an area code --
    # the previous blanket "any paren present" check accepted both shapes.
    if re.match(r"^\(\d{2,4}\)[\s\-.]+\d", c):
        return True
    if "(" in c or ")" in c:
        return False

    # No parens/plus: require a grouping shape that actually looks like a
    # phone number (2-5 groups of 2-4 digits each), and exclude the
    # specific 4-2-2 shape used by ISO dates even when the calendar-range
    # check above didn't catch an edge case (e.g. an out-of-range day/month
    # that still isn't a real phone number).
    groups = [g for g in re.split(r"[\s\-.]", c) if g]
    # Allow a short leading group (1-2 digits) for a trunk/country prefix
    # like the "1" in "1-888-649-2990"; interior/final groups still need to
    # look like real phone-number blocks (2-4 digits).
    if not (2 <= len(groups) <= 5 and all(g.isdigit() for g in groups)):
        return False
    if not all(1 <= len(g) <= 4 for g in groups[:-1]) or not (2 <= len(groups[-1]) <= 4):
        return False
    if (len(groups) >= 3 and len(groups[0]) == 4 and groups[0].startswith(("19", "20"))
            and 1 <= int(groups[1]) <= 12 and 1 <= int(groups[2]) <= 31):
        return False  # YYYY-MM-DD shape (optionally with trailing junk, e.g. an order ID)
    return True


def sev_rank(s):
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s, 4)


def find_dates_in_jsonld(page):
    found = []
    for block in page.get("json_ld_blocks", []):
        parsed = block.get("parsed")
        objs = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            for k, v in obj.items():
                if k.lower() in DATE_KEYS and isinstance(v, str):
                    found.append((k, v))
            graph = obj.get("@graph")
            if isinstance(graph, list):
                for sub in graph:
                    if isinstance(sub, dict):
                        for k, v in sub.items():
                            if k.lower() in DATE_KEYS and isinstance(v, str):
                                found.append((k, v))
    return found


def parse_date_loose(s):
    """Parse a date string that may be an HTTP Last-Modified header value
    (RFC 1123/850/asctime, e.g. "Wed, 21 Oct 2015 07:28:00 GMT") or a
    JSON-LD dateModified/datePublished value (ISO-8601, with or without a
    'Z' suffix, or a loose human-written date).

    The previous implementation only tried ISO-8601-shaped strftime formats,
    so it silently failed to parse the one format the `Last-Modified` HTTP
    header actually uses -- meaning any page whose *only* freshness signal
    was that standard header was always reported as having "no
    machine-readable freshness signal", a false positive.
    """
    if not s:
        return None
    s = s.strip()

    # HTTP-date formats (what Last-Modified actually sends).
    try:
        d = parsedate_to_datetime(s)
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d
    except Exception:
        pass

    # Full ISO-8601, including a trailing 'Z' (Python's fromisoformat wants
    # an explicit offset, not 'Z', prior to handling it natively).
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        pass

    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            d = datetime.strptime(s[:len(fmt) + 10].strip(), fmt)
            return d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def extract_same_as(page):
    links = []
    for block in page.get("json_ld_blocks", []):
        parsed = block.get("parsed")
        objs = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
        for obj in objs:
            if isinstance(obj, dict) and "sameAs" in obj:
                v = obj["sameAs"]
                links.extend(v if isinstance(v, list) else [v])
    return links


def extract_org_name(page):
    for block in page.get("json_ld_blocks", []):
        parsed = block.get("parsed")
        objs = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
        for obj in objs:
            if isinstance(obj, dict) and obj.get("@type") in ("Organization", "Corporation", "LocalBusiness", "Brand"):
                if obj.get("name"):
                    return obj["name"]
    return None


def looks_time_sensitive(page):
    """A page is time-sensitive when it shows evidence of a *volatile fact*
    (price, availability, a subscription term, an event/release date, etc.)
    -- not merely because some 4-digit number on the page happens to look
    like a year. Returns (is_time_sensitive, matched_snippet, is_concrete)
    so callers can cite the specific evidence rather than a generic
    "pricing/date-like content exists" claim, and can scale confidence by
    whether a concrete amount was found vs. only a bare keyword."""
    text = page.get("visible_text") or ""
    price_m = CURRENCY_PRICE_RE.search(text)
    if price_m:
        return True, price_m.group(0), True
    kw_m = VOLATILE_KEYWORDS_RE.search(text)
    if kw_m:
        return True, kw_m.group(0), False
    return False, None, False


def check(cache):
    findings = []
    # crawl.py excludes sitemaps/feeds/PDFs/etc. from cache["pages"] already;
    # this filter is defense in depth so freshness/fact-extraction never runs
    # against a non-HTML record (which has no visible_text/json_ld_blocks to
    # meaningfully check).
    pages = [p for p in cache.get("pages", []) if p.get("is_html", True)]
    now = datetime.now(timezone.utc)
    candidate_facts = []
    # Ordered, de-duplicated list rather than a set: picking "the" brand name
    # later via `next(iter(a_set))` is non-deterministic (set iteration order
    # depends on hash randomization), which violates the "deterministic"
    # requirement for this skill. A list preserves first-seen order.
    org_names_seen = []
    any_same_as_found = False
    entity_page_seen = False  # homepage/about page actually crawled (where sameAs is expected)
    no_freshness_pages = []  # aggregated after the loop into one finding, not one per page

    for p in pages:
        url = p.get("url", "<unknown>")
        page_type = classify_page_type(url, page=p)
        try:
            jsonld_dates = find_dates_in_jsonld(p)
            header_last_mod = p.get("headers", {}).get("Last-Modified") or p.get("headers", {}).get("last-modified")

            newest = None
            for _k, v in jsonld_dates:
                d = parse_date_loose(v)
                if d and (newest is None or d > newest):
                    newest = d
            header_date = parse_date_loose(header_last_mod) if header_last_mod else None
            if header_date and (newest is None or header_date > newest):
                newest = header_date

            time_sensitive, ts_evidence, ts_concrete = looks_time_sensitive(p)

            if newest is None and time_sensitive:
                # Collected and aggregated into a single finding after the loop
                # (see below) instead of emitted per-page here -- a 7-page site
                # with the same gap on every product page is one systemic
                # problem, not 7 separate findings, and repeating it 7x was
                # exactly the "noisy report" complaint on the previous revision.
                no_freshness_pages.append({"url": url, "evidence_snippet": ts_evidence, "concrete": ts_concrete})
            elif newest is not None:
                age_days = (now - newest).days
                if time_sensitive and age_days > STALE_DAYS_HARD:
                    findings.append({
                        "local_id": f"FC-STALE-HARD-{url}",
                        "title": "Freshness date is over 2 years old on time-sensitive content",
                        "severity": "high",
                        "evidence": f"{url}'s most recent modified/published date is "
                                     f"{newest.date().isoformat()} ({age_days} days old), on a page "
                                     f"referencing pricing or dated content.",
                        "suggested_action": {
                            "summary": "Review this page's factual claims (pricing, figures, dates) and "
                                        "update both the content and the dateModified field; a stale "
                                        "timestamp on time-sensitive content signals to assistants that "
                                        "the fact may no longer be trustworthy.",
                            "priority": "high",
                        },
                    })
                elif time_sensitive and age_days > STALE_DAYS_SOFT:
                    findings.append({
                        "local_id": f"FC-STALE-SOFT-{url}",
                        "title": "Freshness date is over a year old on time-sensitive content",
                        "severity": "medium",
                        "evidence": f"{url}'s most recent modified/published date is "
                                     f"{newest.date().isoformat()} ({age_days} days old).",
                        "suggested_action": {
                            "summary": "Confirm the content is still accurate and refresh the dateModified "
                                        "field when it's reviewed, even if the text itself doesn't change.",
                            "priority": "medium",
                        },
                    })

            # Copyright-year check (cheap, common staleness tell in footers).
            footer_years = re.findall(r"(?:©|copyright)\s*(\d{4})", p.get("visible_text", ""), re.I)
            if footer_years:
                latest_copyright = max(int(y) for y in footer_years)
                if now.year - latest_copyright >= 2:
                    findings.append({
                        "local_id": f"FC-OLD-COPYRIGHT-{url}",
                        "title": "Footer copyright year looks outdated",
                        "severity": "low",
                        "evidence": f"{url} shows a copyright year of {latest_copyright} "
                                     f"({now.year - latest_copyright} years old).",
                        "suggested_action": {
                            "summary": "Update the footer copyright year (or make it dynamic); it's a "
                                        "small, easily-noticed signal of an unmaintained site.",
                            "priority": "low",
                        },
                    })

            # Entity disambiguation signals.
            same_as = extract_same_as(p)
            if same_as:
                any_same_as_found = True
            org_name = extract_org_name(p)
            if org_name and org_name.strip() not in org_names_seen:
                org_names_seen.append(org_name.strip())
            if page_type in ("homepage", "about") and org_name:
                entity_page_seen = True

            # Candidate facts to hand to the agent for live cross-source corroboration.
            # Each fact carries entity/context/currency where available so the agent
            # doesn't just get a bare "$999" with no idea what it's the price of.
            visible_text = p.get("visible_text", "")
            if org_name:
                candidate_facts.append({
                    "type": "org_name", "value": org_name, "source_url": url,
                    "extraction_confidence": 0.9,
                })
            for m in list(CURRENCY_PRICE_RE.finditer(visible_text))[:3]:
                symbol_raw = m.group(1).lower().rstrip(".")
                currency = CURRENCY_SYMBOL_MAP.get(m.group(1).lower(), CURRENCY_SYMBOL_MAP.get(symbol_raw, "UNKNOWN"))
                try:
                    value = float(m.group(2).replace(",", ""))
                except ValueError:
                    value = None
                start = max(0, m.start() - 40)
                end = min(len(visible_text), m.end() + 40)
                context = visible_text[start:end].strip()
                fact_type = classify_fact_type(sentence_containing(visible_text, m.start()))
                candidate_facts.append({
                    "type": fact_type,
                    "entity": org_name or None,
                    "value": value,
                    "currency": currency,
                    "raw_text": m.group(0),
                    "context": context,
                    "source_url": url,
                    "page_type": page_type,
                    "extraction_confidence": 0.85 if currency != "UNKNOWN" else 0.5,
                })
            phone_candidates = PHONE_CANDIDATE_RE.findall(visible_text)
            phone_hits = 0
            for phm in phone_candidates:
                phm = phm.strip()
                if looks_like_phone(phm):
                    candidate_facts.append({
                        "type": "phone_mention", "value": phm, "source_url": url,
                        "extraction_confidence": 0.7,
                    })
                    phone_hits += 1
                    if phone_hits >= 2:
                        break
        except Exception as e:
            print(f"WARNING: skipping freshness/fact checks for {url} due to unexpected error: {e}",
                  file=sys.stderr)
            continue

    brand_guess = org_names_seen[0] if org_names_seen else None
    is_generic_name = bool(brand_guess) and brand_guess.strip().lower() in GENERIC_NAME_WORDLIST

    # Aggregate the "no freshness signal" gap across pages into a single
    # finding instead of one per URL. Confidence is scaled down from a flat
    # 0.85: the detector is still heuristic (page mentions a volatile term or
    # a currency amount, and no dateModified/Last-Modified was found) -- it
    # hasn't independently confirmed the content is actually stale, only
    # that a freshness signal is absent. A concrete currency amount found is
    # somewhat stronger evidence than a bare keyword match, so it gets the
    # higher end of the range; a page-only keyword gets the lower end.
    if no_freshness_pages:
        any_concrete = any(pg["concrete"] for pg in no_freshness_pages)
        confidence = 0.7 if any_concrete else 0.55
        sample = no_freshness_pages[:5]
        sample_lines = "; ".join(f"{pg['url']} (evidence: \"{pg['evidence_snippet']}\")" for pg in sample)
        findings.append({
            "local_id": "FC-NO-FRESHNESS-AGGREGATE",
            "title": "Time-sensitive pages lack machine-readable freshness signals",
            "severity": "medium",
            "evidence": (f"{len(no_freshness_pages)}/{len(pages)} crawled page(s) contain a specific "
                         f"volatile-fact signal (a price/currency amount or a term like "
                         f"'subscription'/'availability'/'discount') but expose neither a "
                         f"dateModified/datePublished JSON-LD field nor a Last-Modified header. "
                         f"Examples: {sample_lines}."),
            "suggested_action": {
                "summary": "Add dateModified/datePublished to JSON-LD and/or an accurate Last-Modified "
                            "header on pages carrying prices, offers, or availability claims, so assistants "
                            "can judge whether a specific fact (e.g. a price or cashback offer) is still "
                            "current before repeating it.",
                "priority": "medium",
            },
            "confidence": confidence,
            "affected_page_count": len(no_freshness_pages),
            "affected_pages": [pg["url"] for pg in no_freshness_pages],
        })

    if pages and not any_same_as_found and (entity_page_seen or is_generic_name):
        severity = "medium" if is_generic_name else "low"
        findings.append({
            "local_id": "FC-NO-SAMEAS",
            "title": "No sameAs / disambiguating entity links found",
            "severity": severity,
            "evidence": (f"An Organization/Brand entity ('{brand_guess}') was detected "
                         f"{'on the homepage/about page' if entity_page_seen else 'with a generic, collision-prone name'}, "
                         f"but no JSON-LD block on any crawled page includes a `sameAs` property linking to "
                         f"Wikidata, Wikipedia, LinkedIn, Crunchbase, or official social profiles."),
            "suggested_action": {
                "summary": "Add `sameAs` links on the Organization/LocalBusiness JSON-LD pointing to "
                            "authoritative external profiles for this entity. When multiple things share "
                            "a name, sameAs is one of the clearest machine-readable signals for which one "
                            "this site is.",
                "priority": severity,
            },
            "confidence": 0.9,
        })

    if brand_guess and is_generic_name and not any_same_as_found:
        findings.append({
            "local_id": "FC-GENERIC-NAME",
            "title": "Brand name is a common word with high collision risk and no disambiguation",
            "severity": "high",
            "evidence": f"Organization name '{brand_guess}' is a common English word likely to collide "
                         f"with unrelated entities, and no sameAs/disambiguation signal was found.",
            "suggested_action": {
                "summary": "Prioritize adding sameAs links and a distinguishing description (industry, "
                            "location, founding year) to the Organization schema so assistants don't "
                            "conflate this brand with unrelated same-named entities.",
                "priority": "high",
            },
        })

    for f in findings:
        f.setdefault("confidence", 0.85)
    findings.sort(key=lambda f: sev_rank(f["severity"]))
    return findings, candidate_facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.cache) as f:
        cache = json.load(f)

    findings, candidate_facts = check(cache)
    with open(args.out, "w") as f:
        json.dump({
            "skill": "freshness-corroboration",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "findings": findings,
            "candidate_facts_for_live_corroboration": candidate_facts[:15],
        }, f, indent=2)

    print(f"freshness-corroboration: {len(findings)} finding(s), "
          f"{len(candidate_facts)} candidate fact(s) -> {args.out}")


if __name__ == "__main__":
    main()
