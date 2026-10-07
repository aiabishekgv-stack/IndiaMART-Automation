import json
import os
import re
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from playwright.sync_api import sync_playwright


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


class PublicIndiaMartResearch:
    """
    Public/read-only IndiaMART research.

    No seller login is used.

    This version:
    - discovers IndiaMART public pages and IndiaMART-managed supplier microsites
    - extracts product details, company details, specifications, and public images
    - returns data for preview + template PDF
    """

    def __init__(self, event=None):
        self.event = event or (lambda stage, msg, progress=None: None)
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self, headless=True):
        self.pw = sync_playwright().start()
        channel = os.getenv("BROWSER_CHANNEL", "chrome").strip().lower()

        try:
            if channel in {"", "playwright", "chromium"}:
                self.browser = self.pw.chromium.launch(headless=headless)
            else:
                self.browser = self.pw.chromium.launch(headless=headless, channel=channel)
        except Exception:
            self.browser = self.pw.chromium.launch(headless=headless)

        self.context = self.browser.new_context(
            viewport={"width": 1450, "height": 900},
            locale="en-IN",
        )
        self.page = self.context.new_page()
        return self.page

    def close(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.pw = None

    def _query_variants(self, query):
        query = re.sub(r"\s+", " ", str(query or "").strip())
        variants = []

        def add(value):
            value = re.sub(r"\s+", " ", str(value or "").strip())
            if value and value.lower() not in {x.lower() for x in variants}:
                variants.append(value)

        add(query)
        if re.search(r"\bndt\b", query, re.I):
            add("Non Destructive Testing Equipment")
            add("NDT Equipment")
            add("Non Destructive Testing")
        return variants

    def _tokens(self, query):
        stop = {
            "equipment", "equipments", "machine", "machines", "instrument", "instruments",
            "india", "indiamart", "product", "products", "testing", "test"
        }
        raw = [x for x in re.findall(r"[a-z0-9]+", _norm(query)) if len(x) >= 2]
        useful = [x for x in raw if x not in stop]
        return useful or raw

    def _score_text(self, query, text):
        tokens = self._tokens(query)
        if not tokens:
            return 0.0
        hay = _norm(text)
        hits = sum(1 for t in tokens if t in hay)
        phrase_bonus = 0.4 if _norm(query) in hay else 0
        return min(1.5, hits / len(tokens) + phrase_bonus)

    def _clean_result_url(self, href):
        href = str(href or "").strip()
        if not href:
            return ""
        if "duckduckgo.com/l/" in href:
            try:
                absolute = "https:" + href if href.startswith("//") else href
                parsed = urlparse(absolute)
                params = parse_qs(parsed.query)
                if params.get("uddg"):
                    href = unquote(params["uddg"][0])
            except Exception:
                pass
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/"):
            href = urljoin(self.page.url, href)
        return href

    def _excluded_domain(self, url):
        host = urlparse(url).netloc.lower()
        excluded = [
            "google.", "bing.com", "duckduckgo.com", "youtube.com", "facebook.com",
            "instagram.com", "linkedin.com", "x.com", "twitter.com", "tradeindia.com",
            "amazon.", "flipkart.", "justdial.com", "alibaba.com"
        ]
        return any(x in host for x in excluded)

    def _is_indiamart_product_result(self, url):
        low = str(url or "").lower()
        host = urlparse(str(url or "")).netloc.lower()
        return (
            "/proddetail/" in low
            and "indiamart" in host
        )

    def _collect_search_result_links(self, query, product_only=True):
        """Collect real IndiaMART product-detail results, never city/category links."""
        results = []
        seen = set()
        anchors = self.page.locator("a[href]")

        for i in range(min(anchors.count(), 420)):
            try:
                a = anchors.nth(i)
                href = self._clean_result_url(a.get_attribute("href"))
                if not href.startswith(("http://", "https://")):
                    continue
                if product_only and not self._is_indiamart_product_result(href):
                    continue
                if self._excluded_domain(href):
                    continue
                if href in seen:
                    continue

                text = ""
                try:
                    text = a.inner_text(timeout=400).strip()
                except Exception:
                    pass
                if not text:
                    try:
                        text = (a.get_attribute("title") or a.get_attribute("aria-label") or "").strip()
                    except Exception:
                        text = ""

                score = self._score_text(query, text + " " + href)
                if score <= 0:
                    continue

                seen.add(href)
                results.append({
                    "url": href,
                    "text": text,
                    "score": score,
                    "search_rank": len(results) + 1,
                })
            except Exception:
                continue

        # Keep the result page order as the primary signal, with relevance as a tie-breaker.
        results.sort(key=lambda x: (-x["score"], x["search_rank"]))
        return results

    def _search_indiamart_direct(self, query):
        url = "https://dir.indiamart.com/search.mp?ss=" + quote_plus(query) + "&prdsrc=1"
        self.event("PUBLIC_SEARCH", "IndiaMART search: " + query, None)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
            self.page.wait_for_timeout(2600)
            return self._collect_search_result_links(query, product_only=True)
        except Exception:
            return []

    def _search_duckduckgo(self, query):
        # Fallback equivalent to searching: "<product name> indiamart".
        search_text = f'"{query}" indiamart product'
        url = "https://html.duckduckgo.com/html/?q=" + quote_plus(search_text)
        self.event("PUBLIC_SEARCH", "Web fallback: " + query + " indiamart", None)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
            self.page.wait_for_timeout(1800)
            return self._collect_search_result_links(query, product_only=True)
        except Exception:
            return []

    def _discover_candidates(self, query):
        """
        New focused discovery:
        1. IndiaMART's own search page.
        2. If needed, web-search "product name indiamart".
        3. Return only real /proddetail/ pages.
        """
        results = []
        seen = set()

        for variant in self._query_variants(query):
            direct = self._search_indiamart_direct(variant)
            for item in direct:
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                item["query_variant"] = variant
                item["discovery_method"] = "INDIAMART_SEARCH"
                results.append(item)
                if len(results) >= 6:
                    break

            if len(results) < 3:
                fallback = self._search_duckduckgo(variant)
                for item in fallback:
                    if item["url"] in seen:
                        continue
                    seen.add(item["url"])
                    item["query_variant"] = variant
                    item["discovery_method"] = "PRODUCT_NAME_INDIAMART_WEB_SEARCH"
                    results.append(item)
                    if len(results) >= 6:
                        break

            if len(results) >= 3:
                break

        results.sort(key=lambda x: (-x.get("score", 0), x.get("search_rank", 999)))
        return results[:6]

    def _page_text(self):
        try:
            return self.page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    def _is_indiamart_managed_page(self):
        url = (self.page.url or "").lower()
        if "indiamart.com" in url or "imimg.com" in url:
            return True
        body = _norm(self._page_text())
        markers = [
            "indiamart trust seal verified",
            "developed and managed by indiamart intermesh limited",
            "indiamart intermesh",
            "trustseal verified",
        ]
        return any(m in body for m in markers)

    def _same_domain(self, url):
        return urlparse(url).netloc.lower() == urlparse(self.page.url).netloc.lower()

    def _collect_internal_links(self, query):
        links = []
        seen = set()
        anchors = self.page.locator("a[href]")
        for i in range(min(anchors.count(), 350)):
            try:
                a = anchors.nth(i)
                href = self._clean_result_url(a.get_attribute("href"))
                if not href.startswith(("http://", "https://")):
                    continue
                if not self._same_domain(href):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                text = ""
                try:
                    text = a.inner_text(timeout=350).strip()
                except Exception:
                    pass

                if text.lower() in {"chennai", "delhi", "mumbai", "thane", "bengaluru", "pune", "india"}:
                    continue

                # prefer product-ish links
                bonus = 0
                low = href.lower()
                if any(x in low for x in ["/proddetail/", ".html", "product", "equipment"]):
                    bonus += 0.2

                score = self._score_text(query, text + " " + href) + bonus
                if score <= 0:
                    continue
                links.append({"url": href, "text": text, "score": score})
            except Exception:
                continue
        links.sort(key=lambda x: x["score"], reverse=True)
        return links[:20]

    def _meta(self, selector):
        try:
            loc = self.page.locator(selector).first
            if loc.count():
                return (loc.get_attribute("content") or "").strip()
        except Exception:
            pass
        return ""

    def _first_text(self, selectors):
        for selector in selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.count():
                    text = loc.inner_text(timeout=1000).strip()
                    if text:
                        return text
            except Exception:
                pass
        return ""

    def _walk_json(self, obj):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from self._walk_json(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from self._walk_json(value)

    def _jsonld_products(self):
        products = []
        scripts = self.page.locator('script[type="application/ld+json"]')
        for i in range(min(scripts.count(), 30)):
            try:
                raw = scripts.nth(i).text_content()
                if not raw:
                    continue
                data = json.loads(raw)
                for obj in self._walk_json(data):
                    obj_type = obj.get("@type")
                    types = obj_type if isinstance(obj_type, list) else [obj_type]
                    if any(str(t).lower() == "product" for t in types if t):
                        products.append(obj)
            except Exception:
                continue
        return products

    def _is_junk_spec(self, name, value):
        name_clean = re.sub(r"\s+", " ", str(name or "")).strip()
        value_clean = re.sub(r"\s+", " ", str(value or "")).strip()
        low = name_clean.lower().strip(" :.-")
        value_low = value_clean.lower().strip()

        exact_junk = {
            "company name", "seller", "seller name", "business name", "supplier name",
            "mobile", "phone", "address", "price", "selling price", "product price",
            "unit price", "rate", "view in hindi", "contact supplier", "call now",
            "get best price", "request callback", "send enquiry", "send inquiry",
            "view all", "more products from this seller",
        }
        if low in exact_junk:
            return True

        junk_fragments = (
            "more products from this seller", "contact supplier", "call now",
            "get best price", "request callback", "send enquiry", "send inquiry",
            "view in hindi",
        )
        if any(x in low for x in junk_fragments):
            return True

        if value_low in {
            "contact supplier", "call now", "get best price", "request callback", "view all"
        }:
            return True

        # Reject obvious price-card rows while preserving normal numeric technical values.
        if low in {"price", "rate", "unit price", "selling price", "product price"}:
            return True

        return False

    def _merge_specs(self, groups):
        result, seen = [], set()
        for group in groups:
            for item in group:
                if not isinstance(item, dict):
                    continue
                name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
                value = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()
                if not name or not value:
                    continue
                if len(name) > 120 or len(value) > 500:
                    continue
                if self._is_junk_spec(name, value):
                    continue
                key = (name.lower(), value.lower())
                if key in seen:
                    continue
                seen.add(key)
                result.append({"name": name, "value": value})
        return result[:30]

    def _specs_jsonld(self, products):
        out = []
        for product in products:
            props = product.get("additionalProperty") or []
            if isinstance(props, dict):
                props = [props]
            for prop in props:
                if isinstance(prop, dict):
                    out.append({
                        "name": prop.get("name") or prop.get("propertyID") or "",
                        "value": prop.get("value") or "",
                    })
        return out

    def _specs_tables(self):
        specs = []
        rows = self.page.locator("table tr")
        for i in range(min(rows.count(), 180)):
            try:
                cells = rows.nth(i).locator("th,td")
                if cells.count() < 2:
                    continue
                name = cells.nth(0).inner_text(timeout=500).strip()
                value = cells.nth(1).inner_text(timeout=500).strip()
                specs.append({"name": name, "value": value})
            except Exception:
                continue
        return specs

    def _specs_text_pairs(self):
        body = self._page_text()
        lines = [re.sub(r"\s+", " ", x).strip() for x in body.splitlines() if x.strip()]
        specs = []
        start = -1
        markers = {
            "product details", "product details:", "product specification",
            "product specifications", "specifications"
        }
        for i, line in enumerate(lines):
            if _norm(line) in markers:
                start = i + 1
                break
        if start < 0:
            return []

        segment = lines[start:start + 60]
        i = 0
        while i + 1 < len(segment):
            name = segment[i]
            value = segment[i + 1]
            if len(name) <= 80 and len(value) <= 200 and not name.endswith("."):
                specs.append({"name": name, "value": value})
                i += 2
            else:
                i += 1
        return specs

    def _images_jsonld(self, products):
        urls = []
        for product in products:
            image = product.get("image")
            if isinstance(image, str):
                urls.append(image)
            elif isinstance(image, list):
                for item in image:
                    if isinstance(item, str):
                        urls.append(item)
                    elif isinstance(item, dict):
                        url = item.get("url") or item.get("contentUrl")
                        if url:
                            urls.append(str(url))
            elif isinstance(image, dict):
                url = image.get("url") or image.get("contentUrl")
                if url:
                    urls.append(str(url))
        return urls

    def _images_dom(self, query):
        try:
            raw = self.page.evaluate("""() => Array.from(document.images).map(img => ({
                src: img.currentSrc || img.src || img.dataset.src || img.dataset.original || "",
                alt: img.alt || "",
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0
            }))""")
        except Exception:
            raw = []

        ranked = []
        for item in raw:
            src = str(item.get("src") or "").strip()
            if not src:
                continue
            src = urljoin(self.page.url, src)
            lower = src.lower()
            bad = ["logo", "icon", "sprite", "avatar", "profile", "favicon", "loader", "placeholder", "whatsapp", "playstore", "appstore", "qr"]
            if any(x in lower for x in bad):
                continue
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            if width and height and (width < 220 or height < 220):
                continue
            alt = str(item.get("alt") or "")
            score = self._score_text(query, alt) * 10_000_000 + min(width * height, 5_000_000)
            ranked.append({"url": src, "score": score})
        ranked.sort(key=lambda x: x["score"], reverse=True)
        return [x["url"] for x in ranked]

    def _merge_urls(self, groups):
        out, seen = [], set()
        for group in groups:
            for url in group:
                url = str(url or "").strip()
                if not url:
                    continue
                url = urljoin(self.page.url, url)
                if not url.startswith(("http://", "https://")):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                out.append(url)
        return out[:25]

    def _extract_company_details(self):
        body = self._page_text()
        lines = [re.sub(r"\s+", " ", x).strip() for x in body.splitlines() if x.strip()]

        company_name = ""
        location = ""

        # Company-name evidence from visible public page elements.
        candidates = [
            '[class*="company" i]',
            '[class*="seller" i]',
            '[class*="supplier" i]',
            'a[href*="company"]',
            'a[href*="supplier"]',
            'h2',
            'h3',
        ]
        for selector in candidates:
            try:
                loc = self.page.locator(selector)
                for i in range(min(loc.count(), 8)):
                    text = loc.nth(i).inner_text(timeout=500).strip()
                    if 2 <= len(text) <= 80 and not any(
                        x in text.lower()
                        for x in ["contact", "mobile", "price", "product", "about company"]
                    ):
                        if any(ch.isalpha() for ch in text):
                            company_name = text
                            break
                if company_name:
                    break
            except Exception:
                continue

        # Strong structured location selectors first.
        location_selectors = [
            '[itemprop="addressLocality"]',
            '[itemprop="addressRegion"]',
            '[itemprop="address"]',
            '[class*="location" i]',
            '[class*="address" i]',
            '[class*="city" i]',
        ]
        for selector in location_selectors:
            if location:
                break
            try:
                locs = self.page.locator(selector)
                for i in range(min(locs.count(), 12)):
                    try:
                        candidate = re.sub(
                            r"\s+",
                            " ",
                            locs.nth(i).inner_text(timeout=450).strip(),
                        ).strip(" ,-")
                        low = candidate.lower()
                        if (
                            2 <= len(candidate) <= 160
                            and not any(x in low for x in ["contact supplier", "mobile", "phone", "price"])
                        ):
                            location = candidate
                            break
                    except Exception:
                        continue
            except Exception:
                continue

        # JSON-LD address is preferred over free-text guessing.
        if not location:
            try:
                scripts = self.page.locator('script[type="application/ld+json"]')
                for script_index in range(min(scripts.count(), 30)):
                    try:
                        raw = scripts.nth(script_index).text_content()
                        if not raw:
                            continue
                        data = json.loads(raw)
                        for obj in self._walk_json(data):
                            address = obj.get("address")
                            if not isinstance(address, dict):
                                continue
                            city = str(address.get("addressLocality") or "").strip()
                            region = str(address.get("addressRegion") or "").strip()
                            country = str(address.get("addressCountry") or "").strip()
                            parts = []
                            for value in (city, region, country):
                                if value and value not in parts:
                                    parts.append(value)
                            if parts:
                                location = ", ".join(parts)
                                break
                        if location:
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # Label/value pairs visible in page text.
        for i, line in enumerate(lines[:180]):
            low = line.lower().strip(" :.-")
            if not company_name and low in {"company name", "seller name", "business name", "supplier name"} and i + 1 < len(lines):
                company_name = lines[i + 1]
            if (
                not location
                and low in {"address", "location", "city", "business location", "seller location", "company location", "registered address"}
                and i + 1 < len(lines)
            ):
                candidate = re.sub(r"\s+", " ", lines[i + 1]).strip()
                if 2 <= len(candidate) <= 160:
                    location = candidate

        # Title fallback for company.
        if not company_name:
            title = (
                self._meta('meta[property="og:site_name"]')
                or self._meta('meta[property="og:title"]')
                or self._first_text(["title"])
            )
            title = re.sub(r"\s+", " ", title).strip()
            if " - " in title:
                parts = [x.strip() for x in title.split(" - ") if x.strip()]
                if len(parts) >= 2:
                    company_name = parts[-1]
            if not company_name and title and len(title) <= 80:
                company_name = title

        if not company_name:
            host = urlparse(self.page.url).netloc.split(":")[0]
            first = host.split(".")[0].replace("-", " ").replace("_", " ").strip()
            if first:
                company_name = first.title()

        # Labelled free-text fallback, then conservative city/state fallback.
        if not location:
            patterns = [
                r"(?:location|city|business location|seller location|company location)\s*[:\-]\s*([A-Za-z][A-Za-z .,'()/-]{2,100})",
                r"(?:address|registered address)\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9 .,'()/#-]{5,160})",
            ]
            for pattern in patterns:
                match = re.search(pattern, body, flags=re.I)
                if match:
                    candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ,-")
                    if candidate:
                        location = candidate
                        break

        if not location:
            text = " | ".join(lines[:120])
            match = re.search(r"\b([A-Za-z .-]{2,40}),\s*([A-Za-z .-]{2,40})\b", text)
            if match:
                location = f"{match.group(1).strip()}, {match.group(2).strip()}"

        return {
            "company_name": company_name.strip(),
            "location": location.strip(),
            "source_url": self.page.url,
            "source_domain": urlparse(self.page.url).netloc,
            "source_type": "public_indiamart_managed",
        }

    def _extract_current_page(self, query):
        products = self._jsonld_products()
        title = (
            self._first_text(["h1", '[itemprop="name"]', '[class*="product-name" i]', '[class*="product_title" i]'])
            or self._meta('meta[property="og:title"]')
            or (str(products[0].get("name") or "").strip() if products else "")
        )
        description = (
            self._meta('meta[name="description"]')
            or self._meta('meta[property="og:description"]')
            or (str(products[0].get("description") or "").strip() if products else "")
            or self._first_text(['[itemprop="description"]', '[class*="description" i]', '[class*="desc" i]'])
        )
        specs = self._merge_specs([
            self._specs_jsonld(products),
            self._specs_tables(),
            self._specs_text_pairs(),
        ])
        og_image = self._meta('meta[property="og:image"]')
        images = self._merge_urls([
            [og_image] if og_image else [],
            self._images_jsonld(products),
            self._images_dom(query),
        ])
        company_details = self._extract_company_details()
        match = self._score_text(query, title + " " + description[:600])
        quality = match * 100 + min(len(specs), 12) * 4 + min(len(images), 10) * 5 + (12 if description else 0)
        # prefer pages with specs or company details
        if company_details.get("company_name"):
            quality += 10
        return {
            "product_url": self.page.url,
            "title": title,
            "description": description,
            "specifications": specs,
            "image_urls": images,
            "company_details": company_details,
            "match_score": match,
            "quality_score": quality,
        }

    def _description_evidence_packet(self, scanned):
        """Build compact multi-result evidence for the local AI."""
        blocks = []
        seen = set()

        for idx, item in enumerate(scanned[:6], start=1):
            description = re.sub(r"\s+", " ", str(item.get("description") or "")).strip()
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            if not description:
                continue
            key = description.lower()
            if key in seen:
                continue
            seen.add(key)
            # Enough context for Qwen while keeping CPU inference fast.
            blocks.append(
                f"INDIAMART RESULT {idx}\n"
                f"TITLE: {title[:180]}\n"
                f"DESCRIPTION: {description[:850]}"
            )

        return "\n\n".join(blocks)[:5200]

    def research(self, query):
        """
        Focused product research:
          IndiaMART search -> top 3-6 product-detail results -> scan each ->
          aggregate factual descriptions for local Qwen synthesis.

        No seller login and no internal city/category crawling.
        """
        if not self.page:
            self.start(headless=True)

        candidates = self._discover_candidates(query)
        if not candidates:
            return {
                "found": False,
                "query": query,
                "product_url": "",
                "title": "",
                "description": "",
                "source_descriptions": [],
                "scanned_results": [],
                "scan_count": 0,
                "specifications": [],
                "image_urls": [],
                "image_candidates": [],
                "company_details": {},
                "message": "No relevant IndiaMART product-detail results were found.",
            }

        # V4.1: scan fewer high-quality result pages by default for speed.
        # The limit is configurable, but remains bounded so public research cannot
        # turn into a long crawl.
        try:
            configured_scan_target = int(os.getenv("PUBLIC_SCAN_RESULTS", "4"))
        except Exception:
            configured_scan_target = 4
        scan_target = min(max(2, configured_scan_target), 6, len(candidates))
        try:
            result_wait_ms = max(250, min(2500, int(os.getenv("PUBLIC_RESULT_WAIT_MS", "900"))))
        except Exception:
            result_wait_ms = 900
        scanned = []

        for idx, candidate in enumerate(candidates[:scan_target], start=1):
            url = candidate["url"]
            label = (candidate.get("text") or url)[:100]
            self.event(
                "PUBLIC_RESULT",
                f"Scanning IndiaMART result {idx}/{scan_target}: {label}",
                None,
            )

            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
                self.page.wait_for_timeout(result_wait_ms)

                if not self._is_indiamart_managed_page():
                    continue

                result = self._extract_current_page(query)
                result["search_result_url"] = url
                result["search_result_rank"] = idx
                result["discovery_method"] = candidate.get("discovery_method", "")

                # Reject clearly unrelated product pages.
                if result.get("match_score", 0) <= 0:
                    continue

                if result.get("description") or result.get("specifications"):
                    scanned.append(result)
            except Exception as exc:
                self.event(
                    "PUBLIC_RESULT",
                    f"Result {idx} skipped: {type(exc).__name__}",
                    None,
                )
                continue

        if not scanned:
            return {
                "found": False,
                "query": query,
                "product_url": "",
                "title": "",
                "description": "",
                "source_descriptions": [],
                "scanned_results": [],
                "scan_count": 0,
                "specifications": [],
                "image_urls": [],
                "image_candidates": [],
                "company_details": {},
                "message": "IndiaMART results were found, but no usable product evidence was extracted.",
            }

        # Best single page supplies specs/images/company. Multiple pages supply text evidence.
        best = max(scanned, key=lambda x: x.get("quality_score", 0))
        evidence_packet = self._description_evidence_packet(scanned)

        source_descriptions = [
            {
                "rank": item.get("search_result_rank", 0),
                "title": item.get("title", ""),
                "url": item.get("product_url", ""),
                "description": re.sub(r"\s+", " ", str(item.get("description") or "")).strip(),
            }
            for item in scanned[:6]
            if item.get("description")
        ]

        # V4.1: collect gallery images from ALL matching IndiaMART product pages,
        # not only the single highest-quality page. Each image keeps its page
        # relevance metadata so the uploaded master image can filter/rank it later.
        image_candidates = []
        seen_image_urls = set()
        page_order = sorted(
            scanned,
            key=lambda x: (x.get("match_score", 0), x.get("quality_score", 0)),
            reverse=True,
        )
        for page_item in page_order:
            for image_rank, image_url in enumerate(page_item.get("image_urls", [])[:14], start=1):
                image_url = str(image_url or "").strip()
                if not image_url or image_url in seen_image_urls:
                    continue
                seen_image_urls.add(image_url)
                image_candidates.append({
                    "url": image_url,
                    "page_title": page_item.get("title", ""),
                    "product_url": page_item.get("product_url", ""),
                    "page_match_score": page_item.get("match_score", 0),
                    "page_quality_score": page_item.get("quality_score", 0),
                    "search_result_rank": page_item.get("search_result_rank", 0),
                    "image_rank": image_rank,
                })
                if len(image_candidates) >= 40:
                    break
            if len(image_candidates) >= 40:
                break

        self.event(
            "PUBLIC_EXTRACT",
            (
                f"Scanned {len(scanned)} IndiaMART product results and collected "
                f"{len(image_candidates)} gallery image candidates; sending combined descriptions to local AI."
            ),
            None,
        )

        return {
            "found": True,
            "query": query,
            "product_url": best.get("product_url", ""),
            "title": best.get("title", ""),
            "description": evidence_packet or best.get("description", ""),
            "source_descriptions": source_descriptions,
            "scanned_results": [
                {
                    "rank": x.get("search_result_rank", 0),
                    "title": x.get("title", ""),
                    "url": x.get("product_url", ""),
                    "match_score": x.get("match_score", 0),
                    "quality_score": x.get("quality_score", 0),
                }
                for x in scanned[:6]
            ],
            "scan_count": len(scanned),
            "specifications": best.get("specifications", []),
            # Backward-compatible flat list plus rich multi-page candidates.
            "image_urls": [item["url"] for item in image_candidates],
            "image_candidates": image_candidates,
            "company_details": best.get("company_details", {}),
            "match_score": best.get("match_score", 0),
            "quality_score": best.get("quality_score", 0),
            "discovery_url": best.get("search_result_url", ""),
            "message": (
                f"Top {len(scanned)} IndiaMART product results scanned; "
                "descriptions combined for local AI synthesis."
            ),
        }

