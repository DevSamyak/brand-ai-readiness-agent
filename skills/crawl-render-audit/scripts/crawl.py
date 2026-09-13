#!/usr/bin/env python3
"""
crawl.py -- Fetches a bounded set of pages from a site and caches everything
downstream audit scripts need (raw HTML, headers, parsed structured data,
link graph) into a single JSON file. This is the ONLY script in the whole
marketplace that touches the network -- crawl-render-audit, engagement-audit,
and freshness-corroboration all read the resulting cache instead of each
re-crawling the site, which keeps total audit runtime well under 5 minutes
and keeps request volume polite.

Usage:
    python crawl.py --url https://example.com --out /tmp/audit_cache.json \
        [--max-pages 8] [--timeout 10] [--user-agent "..."]

Respects robots.txt. Read-only: GET/HEAD requests only, no forms submitted,
no auth, no state-changing requests.
"""
import argparse
import json
import re
import sys
import time
import urllib.robotparser
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag

try:
    import requests
except ImportError:
    print("ERROR: this skill requires 'requests' and 'beautifulsoup4'. "
          "Install with: pip install requests beautifulsoup4 --break-system-packages",
          file=sys.stderr)
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: this skill requires 'beautifulsoup4'. "
          "Install with: pip install beautifulsoup4 --break-system-packages",
          file=sys.stderr)
    sys.exit(1)

DEFAULT_UA = ("Mozilla/5.0 (compatible; BrandAIReadinessAudit/1.0; "
              "+https://agentskills.io) AuditBot/1.0")

# Heuristic: paths that, if crawled internally, are worth prioritizing because
# an AI assistant answering a brand question is most likely to land here.
PRIORITY_PATH_HINTS = (
    "product", "products", "pricing", "price", "about", "docs",
    "documentation", "faq", "blog", "article", "news", "contact",
    "service", "services", "solutions",
)

# Content types that are unambiguously not an HTML page a human would read.
# Used as a fast-path before falling back to sniffing the body.
NON_HTML_CONTENT_TYPE_HINTS = (
    "pdf", "image/", "video/", "audio/", "application/json",
    "application/xml", "text/xml", "application/octet-stream",
    "application/zip", "font/", "application/rss+xml", "application/atom+xml",
)

MAX_SITEMAP_FETCHES = 15          # total, shared across all robots.txt sitemap candidates
MAX_SITEMAP_RECURSION_DEPTH = 3   # sitemap-index -> sitemap -> sitemap ... nesting
MAX_NON_HTML_RECORDS = 8          # bounded bookkeeping for non-page resources we bump into


def same_domain(url, root_netloc):
    try:
        return urlparse(url).netloc == root_netloc
    except Exception:
        return False


def normalize(url):
    url, _frag = urldefrag(url)
    return url.rstrip("/") if url.count("/") > 2 else url


def fetch(session, url, timeout):
    """GET a URL, returning (response_or_None, error_str_or_None)."""
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        return resp, None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def load_robots(session, base_url, timeout):
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    robots_text = None
    try:
        resp, err = fetch(session, robots_url, timeout)
        if resp is not None and resp.status_code == 200:
            robots_text = resp.text
            rp.parse(robots_text.splitlines())
        else:
            # No robots.txt / unreachable => treat as allow-all per RFC convention.
            rp.parse([])
    except Exception:
        rp.parse([])
    return rp, robots_url, robots_text


def is_sitemap_like_url(url):
    """True if a URL's path looks like it points at a sitemap file rather
    than a content page (e.g. '/sitemap.xml', '/products.sitemap.cc.xml')."""
    path = urlparse(url).path.lower()
    return path.endswith(".xml") or path.endswith(".xml.gz") or "sitemap" in path


def parse_sitemap_body(text):
    """Return (kind, locs): kind is 'index' (a <sitemapindex> pointing at more
    sitemaps), 'urlset' (a sitemap listing real pages), or 'unknown'."""
    lower = text.lower()
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.I | re.S)
    if "<sitemapindex" in lower:
        return "index", locs
    if "<urlset" in lower:
        return "urlset", locs
    return "unknown", locs


def resolve_sitemap(session, sm_url, timeout, visited, fetch_budget):
    """Recursively resolve a sitemap URL into a flat list of genuine
    content-page URLs (never a sitemap URL itself).

    Sitemaps are frequently nested: a top-level sitemap.xml is often a
    *sitemap index* pointing at further per-locale/per-section sitemap files,
    which may themselves be indexes. A previous version of this crawler read
    only the first level and queued whatever <loc> values it found as if they
    were ordinary web pages -- when those <loc> values were actually more
    sitemap files (e.g. '.../products.sitemap.cc.xml'), the crawler fetched
    and audited raw XML as if it were an HTML page, producing false
    "missing H1 / missing nav / missing viewport" findings. This function
    recurses until it only has real page URLs left.

    `visited` and `fetch_budget` (a 1-element list used as a mutable counter)
    are shared across every sitemap candidate we try, so total sitemap
    network calls stay bounded regardless of how many "Sitemap:" lines
    robots.txt declares.
    """
    top_sitemap_url = {"value": None}

    def _resolve(url, depth):
        if url in visited or depth > MAX_SITEMAP_RECURSION_DEPTH or fetch_budget[0] <= 0:
            return []
        visited.add(url)
        fetch_budget[0] -= 1
        resp, err = fetch(session, url, timeout)
        if resp is None or resp.status_code != 200:
            return []
        ctype = resp.headers.get("Content-Type", "")
        text = resp.text or ""
        looks_xml = ("xml" in ctype.lower() or text.strip().startswith("<?xml")
                     or "<urlset" in text.lower() or "<sitemapindex" in text.lower())
        if not looks_xml:
            return []
        if top_sitemap_url["value"] is None:
            top_sitemap_url["value"] = url
        kind, locs = parse_sitemap_body(text)
        page_urls = []
        for loc in locs:
            loc = loc.strip()
            if not loc:
                continue
            if kind == "index" or is_sitemap_like_url(loc):
                page_urls.extend(_resolve(loc, depth + 1))
            else:
                page_urls.append(loc)
        return page_urls

    urls = _resolve(sm_url, 0)
    return top_sitemap_url["value"], urls


def find_sitemap(session, base_url, robots_text, timeout):
    parsed = urlparse(base_url)
    candidates = []
    if robots_text:
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(line.split(":", 1)[1].strip())
    candidates.append(f"{parsed.scheme}://{parsed.netloc}/sitemap.xml")

    visited = set()
    fetch_budget = [MAX_SITEMAP_FETCHES]
    first_found_url = None
    all_urls = []
    seen_urls = set()

    for sm_url in candidates:
        if fetch_budget[0] <= 0 or len(all_urls) >= 200:
            break
        top_url, urls = resolve_sitemap(session, sm_url, timeout, visited, fetch_budget)
        if top_url and first_found_url is None:
            first_found_url = top_url
        for u in urls:
            if u not in seen_urls:
                seen_urls.add(u)
                all_urls.append(u)

    return first_found_url, all_urls[:200]


def looks_like_html_response(resp):
    """Sniff whether a fetched response is an HTML page a human/crawler would
    read, as opposed to a sitemap, feed, PDF, image, or JSON/XML API
    response that happened to be linked from a page. Generalizes beyond
    sitemaps: any non-HTML resource reached via a normal <a> link (e.g. a
    linked spec-sheet PDF) is caught here too, so it never gets run through
    HTML-structure checks like "missing H1" or "missing nav".
    """
    if resp is None:
        return False
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" in ctype:
        return True
    if any(hint in ctype for hint in NON_HTML_CONTENT_TYPE_HINTS):
        return False
    # No definitive content-type: sniff the body itself.
    try:
        snippet = (resp.text[:500] or "").lstrip().lower()
    except Exception:
        return False
    if snippet.startswith("<?xml") or snippet.startswith("<urlset") or snippet.startswith("<sitemapindex"):
        return False
    return snippet.startswith("<!doctype html") or "<html" in snippet


def extract_nonhtml_record(url, resp, elapsed_s):
    """Minimal record for a fetched resource that isn't an HTML page.
    Deliberately omits HTML-structure fields (headings, nav, viewport,
    json_ld_blocks, etc.) rather than defaulting them to "missing" -- a PDF
    or sitemap file was never supposed to have an H1, and reporting one
    absent there is a false positive, not a real finding.
    """
    return {
        "url": url,
        "status_code": resp.status_code if resp is not None else None,
        "final_url": resp.url if resp is not None else None,
        "headers": dict(resp.headers) if resp is not None else {},
        "fetch_time_seconds": round(elapsed_s, 3),
        "content_type": (resp.headers.get("Content-Type") if resp is not None else None),
        "is_html": False,
    }


def extract_page_data(url, resp, elapsed_s):
    """Parse one fetched HTML page into the structured record other skills consume."""
    html = resp.text if resp is not None else ""
    soup = BeautifulSoup(html, "html.parser") if html else BeautifulSoup("", "html.parser")

    # JSON-LD blocks MUST be captured before script tags are decomposed below
    # for the visible-text calculation -- decomposing first would remove every
    # <script type="application/ld+json"> tag from the tree, so this loop would
    # always find zero blocks regardless of what the page actually has. (This
    # was a real bug: it silently made "no JSON-LD" fire on every single page,
    # including ones with valid Organization/Product schema, because the
    # extraction ran against an already-emptied tree.)
    json_ld_blocks = []
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        parsed_ok, parsed_val, err = True, None, None
        try:
            parsed_val = json.loads(raw)
        except Exception as e:
            parsed_ok, err = False, str(e)
        json_ld_blocks.append({"raw_present": bool(raw.strip()), "parsed_ok": parsed_ok,
                                "parsed": parsed_val, "error": err})

    # Visible text = what a plain-text reader (or a crawler that doesn't run JS) sees.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()

    title = soup.title.string.strip() if soup.title and soup.title.string else None
    meta_desc = None
    md_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if md_tag and md_tag.get("content"):
        meta_desc = md_tag["content"].strip()

    robots_meta = None
    rm_tag = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    if rm_tag and rm_tag.get("content"):
        robots_meta = rm_tag["content"].strip().lower()

    canonical = None
    can_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
    if can_tag and can_tag.get("href"):
        canonical = can_tag["href"]

    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    has_viewport_meta = bool(viewport and viewport.get("content"))

    og_tags = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property", "")
        if prop.startswith("og:") and tag.get("content"):
            og_tags[prop] = tag["content"]

    images = []
    for img in soup.find_all("img"):
        images.append({"src": img.get("src"), "alt": img.get("alt"),
                        "has_alt_text": bool((img.get("alt") or "").strip())})

    videos = len(soup.find_all(["video"])) + len(
        [f for f in soup.find_all("iframe") if f.get("src") and
         re.search(r"youtube|vimeo|wistia", f.get("src"), re.I)]
    )

    headings = {f"h{i}": [h.get_text(strip=True) for h in soup.find_all(f"h{i}")] for i in range(1, 4)}

    nav_links = []
    for nav in soup.find_all(["nav"]):
        nav_links.extend([a.get("href") for a in nav.find_all("a") if a.get("href")])

    has_search_form = bool(soup.find("input", attrs={"type": re.compile("search", re.I)}) or
                            soup.find("form", attrs={"role": re.compile("search", re.I)}))

    has_breadcrumbs = bool(
        soup.find(attrs={"aria-label": re.compile("breadcrumb", re.I)}) or
        any((b.get("parsed") or {}).get("@type") == "BreadcrumbList"
            for b in json_ld_blocks if isinstance(b.get("parsed"), dict))
    )

    # SPA-shell heuristic: a near-empty <body> paired with a common client-side
    # mount point (id="root"/"app"/"__next") strongly suggests the real content
    # is assembled by JavaScript after load -- invisible to a plain-text crawler.
    body_tag = soup.find("body")
    body_raw_len = len(str(body_tag)) if body_tag else 0
    spa_mount_present = bool(soup.find(id=re.compile("^(root|app|__next|__nuxt)$", re.I)))
    text_len = len(visible_text)
    html_len = max(len(html), 1)
    text_to_html_ratio = text_len / html_len

    # Internal links for BFS + broken-link spot checks.
    links = []
    for a in soup.find_all("a", href=True):
        links.append(a["href"])

    all_text_lower = visible_text.lower()

    return {
        "url": url,
        "status_code": resp.status_code if resp is not None else None,
        "final_url": resp.url if resp is not None else None,
        "headers": dict(resp.headers) if resp is not None else {},
        "fetch_time_seconds": round(elapsed_s, 3),
        "content_type": (resp.headers.get("Content-Type") if resp is not None else None),
        "is_html": True,
        "title": title,
        "meta_description": meta_desc,
        "robots_meta": robots_meta,
        "x_robots_tag": (resp.headers.get("X-Robots-Tag") if resp is not None else None),
        "canonical": canonical,
        "has_viewport_meta": has_viewport_meta,
        "json_ld_blocks": json_ld_blocks,
        "og_tags": og_tags,
        "images": images,
        "image_count": len(images),
        "images_missing_alt": sum(1 for i in images if not i["has_alt_text"]),
        "video_embed_count": videos,
        "headings": headings,
        "nav_link_count": len(nav_links),
        "has_search_form": has_search_form,
        "has_breadcrumbs": has_breadcrumbs,
        "spa_mount_present": spa_mount_present,
        "body_raw_len": body_raw_len,
        "text_len": text_len,
        "html_len": html_len,
        "text_to_html_ratio": round(text_to_html_ratio, 4),
        "requires_js_phrase": bool(re.search(
            r"enable javascript|requires javascript|please turn on javascript", all_text_lower)),
        "links": links,
        "raw_html_excerpt": html[:20000],  # capped -- enough for downstream regex checks, bounds cache size
        "visible_text": visible_text[:20000],
    }


def crawl(start_url, max_pages, timeout, user_agent):
    parsed_start = urlparse(start_url)
    if not parsed_start.scheme:
        start_url = "https://" + start_url
        parsed_start = urlparse(start_url)
    root_netloc = parsed_start.netloc

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    rp, robots_url, robots_text = load_robots(session, start_url, timeout)
    sitemap_url, sitemap_urls = find_sitemap(session, start_url, robots_text, timeout)

    def allowed(u):
        try:
            return rp.can_fetch(user_agent, u)
        except Exception:
            return True

    queue = deque()
    seen = set()
    pages = []
    non_html_resources = []
    skipped_by_robots = []
    fetch_errors = []

    start_norm = normalize(start_url)
    queue.append(start_norm)
    seen.add(start_norm)

    # Seed a few sitemap/priority URLs early so a shallow crawl still reaches
    # the pages an AI assistant is most likely to actually cite. `sitemap_urls`
    # is already fully resolved to real page URLs (see resolve_sitemap) -- it
    # never contains sitemap files themselves.
    seed_candidates = [normalize(u) for u in sitemap_urls
                        if same_domain(u, root_netloc) and not is_sitemap_like_url(u)]
    seed_candidates.sort(key=lambda u: (0 if any(h in u.lower() for h in PRIORITY_PATH_HINTS) else 1))
    for u in seed_candidates:
        if u not in seen and len(seen) < max_pages * 3:
            queue.append(u)
            seen.add(u)

    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if not allowed(url):
            skipped_by_robots.append(url)
            continue
        t0 = time.time()
        resp, err = fetch(session, url, timeout)
        elapsed = time.time() - t0
        if err:
            fetch_errors.append({"url": url, "error": err})
            continue

        if not looks_like_html_response(resp):
            # A non-page resource (sitemap that slipped through, PDF, JSON/XML
            # API response, feed, etc.) reached via a normal link. Record it
            # for crawlability bookkeeping (e.g. broken-link checks) but never
            # run HTML-structure checks (H1/nav/viewport/JSON-LD) against it,
            # and never treat it as consuming the content-page budget or as a
            # source of further links to follow.
            if len(non_html_resources) < MAX_NON_HTML_RECORDS:
                non_html_resources.append(extract_nonhtml_record(url, resp, elapsed))
            continue

        data = extract_page_data(url, resp, elapsed)
        pages.append(data)

        if len(pages) < max_pages:
            for href in data["links"]:
                full = urljoin(url, href)
                full = normalize(full)
                if full in seen:
                    continue
                if not same_domain(full, root_netloc):
                    continue
                if urlparse(full).scheme not in ("http", "https"):
                    continue
                seen.add(full)
                queue.append(full)

    return {
        "start_url": start_url,
        "root_netloc": root_netloc,
        "robots_url": robots_url,
        "robots_txt_found": robots_text is not None,
        "robots_txt_excerpt": (robots_text[:4000] if robots_text else None),
        "sitemap_url": sitemap_url,
        "sitemap_url_count": len(sitemap_urls),
        "pages_crawled": len(pages),
        "pages": pages,
        "non_html_resources": non_html_resources,
        "skipped_by_robots": skipped_by_robots,
        "fetch_errors": fetch_errors,
    }


def main():
    ap = argparse.ArgumentParser(description="Crawl and cache a bounded set of pages for the AI-readiness audit.")
    ap.add_argument("--url", required=True, help="Site root URL to audit")
    ap.add_argument("--out", required=True, help="Path to write the JSON cache")
    ap.add_argument("--max-pages", type=int, default=8, help="Max pages to fetch (default 8)")
    ap.add_argument("--timeout", type=int, default=10, help="Per-request timeout in seconds")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    args = ap.parse_args()

    result = crawl(args.url, args.max_pages, args.timeout, args.user_agent)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, default=str)

    print(f"Crawled {result['pages_crawled']} page(s) from {args.url}", file=sys.stderr)
    print(f"robots.txt found: {result['robots_txt_found']} | sitemap found: {bool(result['sitemap_url'])} "
          f"| non-HTML resources skipped from content checks: {len(result['non_html_resources'])}",
          file=sys.stderr)
    if result["fetch_errors"]:
        print(f"{len(result['fetch_errors'])} fetch error(s)", file=sys.stderr)
    print(f"Cache written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
