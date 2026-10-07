import csv
import html
import io
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests


DEFAULT_GOOGLE_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1iGpbKroGTez2HNq0ps7shzn0OMvASn9-67UBPdogiJw/edit?usp=sharing"
)
DEFAULT_SHEET_NAME = "final sheet"

PRODUCT_HEADERS = [
    "Product Name", "Product", "Keyword", "Search Keyword",
    "Product Name / Search Keyword", "Product/Search Keyword",
    "Product Title", "Item Name", "Item", "Material Name",
    "Product / Service", "Product/Service", "Product Service",
    "Product Description", "Description",
]
MODEL_HEADERS = [
    "Model", "Model Number", "Model No", "Model Name/Number",
    "Model Name", "Model No.", "Model/Model Number", "Model / Model Number",
]
PRICE_HEADERS = [
    "Selling Price", "Price", "Product Price", "Rate", "Unit Price",
    "Sale Price", "Sales Price", "Offer Price", "Selling Rate",
]
STATUS_HEADERS = [
    "IndiaMART Status", "Status", "Upload Status",
    "IndiaMART Upload Status", "IndiaMART State",
]

# Short in-memory cache. The web UI may search repeatedly while the user types;
# re-downloading a very large public Sheet for every keystroke is slow.
_DIRECT_ROWS_CACHE = {}
_DIRECT_ROWS_CACHE_TTL = 45
_DIRECT_ROWS_CACHE_MAX = 6


def _extract_sheet_id(url):
    text = str(url or "").strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else ""


def _normalize(value):
    return str(value or "").strip()


def _header_key(value):
    """Normalize Sheet headers so punctuation/spacing differences do not break matching."""
    text = _normalize(value).lower()
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact_header(value):
    return re.sub(r"[^a-z0-9]+", "", _normalize(value).lower())


def _header_score(header, names):
    key = _header_key(header)
    compact = _compact_header(header)
    if not key:
        return -1

    best = -1
    for name in names:
        target = _header_key(name)
        target_compact = _compact_header(name)
        if not target:
            continue
        if key == target:
            best = max(best, 100)
            continue
        if compact == target_compact:
            best = max(best, 98)
            continue

        # Strong phrase match handles headers such as
        # "PRODUCT NAME / SEARCH KEYWORD" and "Product Name (Required)".
        if len(target) >= 7 and (target in key or key in target):
            best = max(best, 82)

        ht = set(key.split())
        nt = set(target.split())
        overlap = len(ht & nt)
        if len(nt) == 1 and next(iter(nt), "") in ht:
            best = max(best, 72)
        elif overlap >= 2:
            coverage = overlap / max(1, len(nt))
            if coverage >= 0.66:
                best = max(best, int(45 + 40 * coverage))

    return best


def _header_index(headers, names):
    scored = []
    for idx, header in enumerate(headers):
        score = _header_score(header, names)
        if score >= 0:
            scored.append((score, idx))
    if not scored:
        return -1
    scored.sort(reverse=True)
    score, idx = scored[0]
    # Require a reasonable match so unrelated columns are not selected.
    return idx if score >= 58 else -1


def _product_header_index(headers):
    """Find the most likely product-name column while rejecting price/status/model fields."""
    positives = {"product", "item", "material", "keyword", "title", "service", "description", "name"}
    hard_negatives = {
        "price", "rate", "cost", "purchase", "status", "date", "qty", "quantity",
        "brand", "category", "company", "supplier", "email", "phone", "mobile",
        "url", "link", "image", "amount", "gst", "hsn",
    }

    best = (-999, -1)
    for i, header in enumerate(headers):
        key = _header_key(header)
        if not key:
            continue
        tokens = set(key.split())

        alias_score = _header_score(header, PRODUCT_HEADERS)
        score = alias_score if alias_score >= 0 else 0

        # Product price/status/image/etc. must never win merely because they
        # contain the word "product".
        score -= len(tokens & hard_negatives) * 90

        if "model" in tokens and not ({"product", "item"} & tokens):
            score -= 70
        elif "model" in tokens:
            score -= 25

        score += len(tokens & positives) * 10
        if "product" in tokens and "name" in tokens:
            score += 65
        elif "item" in tokens and "name" in tokens:
            score += 55
        elif "material" in tokens and "name" in tokens:
            score += 50
        elif "product" in tokens and "service" in tokens:
            score += 45
        elif "keyword" in tokens:
            score += 40
        elif "product" in tokens:
            score += 25
        elif "item" in tokens or "material" in tokens:
            score += 22

        if score > best[0]:
            best = (score, i)

    return best[1] if best[0] >= 45 else -1


def _infer_product_column_from_rows(headers, rows):
    """
    Last-resort inference for legacy Sheets whose product column has an
    unexpected header. It uses column content and rejects obvious price,
    supplier, date, status, URL, and numeric columns.
    """
    if not headers or not rows:
        return -1

    negative_tokens = {
        "price", "rate", "cost", "purchase", "status", "date", "qty", "quantity",
        "brand", "category", "company", "supplier", "email", "phone", "mobile",
        "url", "link", "image", "amount", "gst", "hsn", "sno", "serial", "id",
    }
    positive_tokens = {"product", "item", "material", "keyword", "title", "service", "name"}

    best = (-999, -1)
    sample_rows = rows[:250]

    for idx, header in enumerate(headers):
        key = _header_key(header)
        tokens = set(key.split())
        if tokens & negative_tokens:
            continue

        values = []
        for row in sample_rows:
            value = _normalize(row.get(header))
            if value:
                values.append(value)

        if len(values) < 2:
            continue

        sample = values[:120]
        distinct_ratio = len({v.lower() for v in sample}) / max(1, len(sample))
        alpha_ratio = sum(1 for v in sample if re.search(r"[A-Za-z]", v)) / len(sample)
        numeric_ratio = sum(
            1 for v in sample
            if re.fullmatch(r"[\s₹$€£0-9,./%+\-]+", v or "")
        ) / len(sample)
        url_ratio = sum(1 for v in sample if "http://" in v.lower() or "https://" in v.lower()) / len(sample)
        avg_len = sum(len(v) for v in sample) / len(sample)

        score = 0
        score += distinct_ratio * 30
        score += alpha_ratio * 35
        score -= numeric_ratio * 70
        score -= url_ratio * 80
        if 3 <= avg_len <= 90:
            score += 20
        elif avg_len > 180:
            score -= 20

        score += len(tokens & positive_tokens) * 15

        # Favor earlier columns slightly; product-name columns are commonly near
        # the left side of operational sheets.
        score -= idx * 0.8

        if score > best[0]:
            best = (score, idx)

    return best[1] if best[0] >= 45 else -1


def _row_value(row, names):
    if not isinstance(row, dict):
        return ""
    keys = [k for k in row.keys() if not str(k).startswith("__")]
    idx = _header_index(keys, names)
    if idx >= 0:
        value = row.get(keys[idx])
        if _normalize(value):
            return _normalize(value)

    # Product-specific fuzzy fallback.
    if names is PRODUCT_HEADERS or names == PRODUCT_HEADERS:
        idx = _product_header_index(keys)
        if idx >= 0:
            value = row.get(keys[idx])
            if _normalize(value):
                return _normalize(value)
    return ""


class SheetBridge:
    """
    Google Sheet bridge.

    V3.1 keeps the existing direct shared-sheet mode and adds a friendly
    browser API for:
      - Sheet-tab dropdown
      - Product dropdown
      - Product search/filter
      - Load a specifically selected row
      - Existing Load Next Product behaviour

    Direct mode requires: Share -> Anyone with the link -> Viewer.
    """

    def __init__(self, url=None, token=None, sheet_url=None, sheet_name=None):
        self.apps_url = _normalize(url if url is not None else os.getenv("APPS_SCRIPT_URL", ""))
        self.token = _normalize(token if token is not None else os.getenv("APPS_SCRIPT_TOKEN", ""))
        self.sheet_url = _normalize(
            sheet_url if sheet_url is not None else os.getenv("GOOGLE_SHEET_URL", DEFAULT_GOOGLE_SHEET_URL)
        )
        self.sheet_name = _normalize(
            sheet_name if sheet_name is not None else os.getenv("GOOGLE_SHEET_NAME", DEFAULT_SHEET_NAME)
        ) or DEFAULT_SHEET_NAME
        self.sheet_id = _extract_sheet_id(self.sheet_url)

        preferred_mode = _normalize(os.getenv("GOOGLE_SHEET_MODE", "direct")).lower()
        if preferred_mode == "apps_script" and self.apps_url and self.token:
            self.mode = "apps_script"
        elif self.sheet_id:
            self.mode = "direct"
        elif self.apps_url and self.token:
            self.mode = "apps_script"
        else:
            raise RuntimeError(
                "Google Sheet is not connected. Set GOOGLE_SHEET_URL or configure the optional Apps Script bridge."
            )

        local_root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NUNES_INDIAMART_AUTOMATION"
        self.progress_file = local_root / "sheet" / f"{self.sheet_id or 'apps_script'}_progress.json"
        self.progress_file.parent.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"
            )
        })

    # ---------------------------------------------------------
    # Apps Script mode
    # ---------------------------------------------------------

    def _check(self, response):
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Google Sheet bridge failed")
        return data

    def _apps_ping(self):
        return self._check(self.session.get(
            self.apps_url,
            params={"action": "ping", "token": self.token},
            timeout=20,
        ))

    def _apps_next(self):
        return self._check(self.session.get(
            self.apps_url,
            params={"action": "next", "token": self.token},
            timeout=30,
        ))

    def _apps_log(self, data):
        return self._check(self.session.post(
            self.apps_url,
            json={"action": "log", "token": self.token, "data": data},
            timeout=30,
        ))

    # ---------------------------------------------------------
    # Direct Sheet helpers
    # ---------------------------------------------------------

    def _direct_csv_urls(self, sheet_name=None):
        base = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"
        requested = _normalize(sheet_name) or self.sheet_name
        urls = []
        if requested:
            urls.append(base + "/gviz/tq?tqx=out:csv&sheet=" + quote(requested, safe=""))
        # Only fall back to gid=0 when using the configured/default tab. For a
        # user-selected tab, silently returning the first sheet would be wrong.
        if not sheet_name or requested == self.sheet_name:
            urls.append(base + "/export?format=csv&gid=0")
        return urls

    def _looks_like_login_html(self, response):
        ctype = (response.headers.get("content-type", "") or "").lower()
        text = response.text[:1400].lower()
        return (
            "text/html" in ctype
            and (
                "accounts.google.com" in text
                or "sign in" in text
                or "servicelogin" in text
                or "<!doctype html" in text
            )
        )

    def _download_csv(self, sheet_name=None):
        errors = []
        for url in self._direct_csv_urls(sheet_name=sheet_name):
            try:
                response = self.session.get(url, timeout=30)
                if self._looks_like_login_html(response):
                    raise RuntimeError(
                        "The Google Sheet is not publicly readable. In Google Sheets choose Share -> "
                        "General access -> Anyone with the link -> Viewer."
                    )
                response.raise_for_status()
                text = response.content.decode("utf-8-sig", errors="replace")
                if not text.strip():
                    raise RuntimeError("Google Sheet returned an empty response.")
                if text.lstrip().startswith("<"):
                    raise RuntimeError(
                        "Google returned a web page instead of Sheet data. Share the Sheet as "
                        "Anyone with the link -> Viewer."
                    )
                return text
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError(errors[-1] if errors else "Could not read the Google Sheet.")

    def _direct_rows(self, sheet_name=None):
        """
        Read a shared Sheet tab.

        V3.3 no longer assumes row 1 is always the header row. It scans the
        first several rows and chooses the row that most strongly resembles a
        product table header. Each returned dict carries an internal
        __source_row__ value so the UI still loads the exact Google Sheet row.
        """
        selected_sheet = _normalize(sheet_name) or self.sheet_name
        cache_key = (self.sheet_id or "apps_script", selected_sheet)
        cached = _DIRECT_ROWS_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < _DIRECT_ROWS_CACHE_TTL:
            return list(cached[1]), [dict(row) for row in cached[2]]

        text = self._download_csv(sheet_name=sheet_name)
        reader = csv.reader(io.StringIO(text))
        raw_rows = list(reader)
        if not raw_rows:
            return [], []

        # Remove completely empty trailing rows but preserve original row numbers.
        indexed_rows = [
            (row_no, list(values))
            for row_no, values in enumerate(raw_rows, start=1)
            if any(_normalize(v) for v in values)
        ]
        if not indexed_rows:
            return [], []

        # Detect the most likely header row among the first 12 non-empty rows.
        best = None
        for row_no, values in indexed_rows[:12]:
            candidate_headers = [_normalize(v) for v in values]
            nonempty = sum(1 for h in candidate_headers if h)
            if nonempty == 0:
                continue

            product_idx = _product_header_index(candidate_headers)
            model_idx = _header_index(candidate_headers, MODEL_HEADERS)
            price_idx = _header_index(candidate_headers, PRICE_HEADERS)
            status_idx = _header_index(candidate_headers, STATUS_HEADERS)

            score = 0
            if product_idx >= 0:
                score += 120
            if model_idx >= 0:
                score += 25
            if price_idx >= 0:
                score += 25
            if status_idx >= 0:
                score += 10

            # Header rows usually have multiple unique text labels.
            normalized = [_header_key(h) for h in candidate_headers if _header_key(h)]
            score += min(nonempty, 12) * 2
            score += min(len(set(normalized)), 12)

            # A single-cell title such as "NUNES PRODUCT MASTER" is not a
            # table header even though it contains the word product.
            if nonempty == 1:
                score -= 160
            elif nonempty == 2:
                score -= 20

            # Penalize rows that look mostly numeric/data-like.
            numericish = 0
            for h in candidate_headers:
                s = _normalize(h).replace(",", "").replace("₹", "").replace(".", "")
                if s and s.replace("-", "").isdigit():
                    numericish += 1
            score -= numericish * 4

            candidate = (score, -row_no, row_no, candidate_headers)
            if best is None or candidate > best:
                best = candidate

        # If no strong header was detected, preserve legacy behavior and use
        # the first non-empty row. The fuzzy product-column detector below can
        # still recover many non-standard names.
        header_row_no = best[2] if best else indexed_rows[0][0]
        headers = best[3] if best else [_normalize(v) for v in indexed_rows[0][1]]

        # Give blank header cells stable synthetic names so legacy sheets do
        # not silently lose whole columns.
        normalized_headers = []
        seen_headers = {}
        for idx, header in enumerate(headers):
            base = _normalize(header) or f"Column {idx + 1}"
            count = seen_headers.get(base.lower(), 0) + 1
            seen_headers[base.lower()] = count
            normalized_headers.append(base if count == 1 else f"{base} ({count})")
        headers = normalized_headers

        result = []
        for row_no, values in indexed_rows:
            if row_no <= header_row_no:
                continue
            padded = list(values) + [""] * max(0, len(headers) - len(values))
            row = {headers[i]: padded[i] for i in range(len(headers))}
            row["__source_row__"] = row_no
            if any(_normalize(v) for k, v in row.items() if k != "__source_row__"):
                result.append(row)

        # Last-resort recovery: if the header text is unfamiliar, infer the
        # product column from its contents and expose a Product Name alias.
        if _product_header_index(headers) < 0 and result:
            inferred_idx = _infer_product_column_from_rows(headers, result)
            if inferred_idx >= 0:
                inferred_header = headers[inferred_idx]
                for row in result:
                    row["Product Name"] = row.get(inferred_header, "")
                    row["__detected_product_header__"] = inferred_header
                headers = list(headers) + ["Product Name"]

        # Keep the most recent few tab parses in memory so dropdown searches
        # remain fast even on large product registers.
        if len(_DIRECT_ROWS_CACHE) >= _DIRECT_ROWS_CACHE_MAX:
            oldest_key = min(_DIRECT_ROWS_CACHE, key=lambda key: _DIRECT_ROWS_CACHE[key][0])
            _DIRECT_ROWS_CACHE.pop(oldest_key, None)
        _DIRECT_ROWS_CACHE[cache_key] = (time.time(), list(headers), [dict(row) for row in result])
        return list(headers), [dict(row) for row in result]

    def _read_progress(self):
        try:
            if self.progress_file.exists():
                data = json.loads(self.progress_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"completed_rows": [], "last_loaded_row": ""}

    def _write_progress(self, data):
        self.progress_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _completed_set(self, sheet_name=None):
        progress = self._read_progress()
        selected = _normalize(sheet_name) or self.sheet_name
        by_sheet = progress.get("completed_by_sheet", {})
        if isinstance(by_sheet, dict) and isinstance(by_sheet.get(selected), list):
            return {str(x) for x in by_sheet.get(selected, [])}
        # Backward compatibility: V3.0 stored only row numbers. Treat those as
        # belonging to the configured/default sheet, never to every tab.
        if selected == self.sheet_name:
            return {str(x) for x in progress.get("completed_rows", [])}
        return set()

    def _status_header(self, headers):
        idx = _header_index(headers, STATUS_HEADERS)
        return headers[idx] if idx >= 0 else ""

    def _classify_row(self, row, source_row, status_header, completed):
        remote_status = _normalize(row.get(status_header)).upper() if status_header else ""
        local_done = str(source_row) in completed
        failed = remote_status in {"FAILED", "RETRY", "ERROR"}
        remote_done = bool(remote_status) and not failed
        uploaded = local_done or remote_done
        if failed:
            state = "failed"
        elif uploaded:
            state = "completed"
        else:
            state = "pending"
        return {
            "state": state,
            "uploaded": uploaded,
            "remote_status": remote_status,
            "local_completed": local_done,
        }

    # ---------------------------------------------------------
    # Sheet tab discovery
    # ---------------------------------------------------------

    def _configured_tabs(self):
        raw = _normalize(os.getenv("GOOGLE_SHEET_TABS", ""))
        if not raw:
            return []
        return [x.strip() for x in re.split(r"[,;\n]", raw) if x.strip()]

    def _discover_tabs_from_html(self):
        """Best-effort tab discovery from the public spreadsheet edit page."""
        if not self.sheet_id:
            return []
        url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit?usp=sharing"
        response = self.session.get(url, timeout=25)
        response.raise_for_status()
        raw = response.text
        decoded = html.unescape(raw)
        # Google changes the bootstrap payload from time to time. These patterns
        # intentionally cover both normal and backslash-escaped JSON forms.
        patterns = [
            r'"name"\s*:\s*"([^"\\]{1,120})"\s*,\s*"index"\s*:\s*\d+\s*,\s*"sheetId"\s*:\s*\d+',
            r'\\"name\\"\s*:\s*\\"([^\\"]{1,120})\\".*?\\"sheetId\\"\s*:\s*\d+',
            r'"title"\s*:\s*"([^"\\]{1,120})".{0,180}?"sheetId"\s*:\s*\d+',
            r'\\"title\\"\s*:\s*\\"([^\\"]{1,120})\\".{0,220}?\\"sheetId\\"\s*:\s*\d+',
        ]
        names = []
        seen = set()
        for source in (decoded, raw):
            for pattern in patterns:
                for match in re.finditer(pattern, source, flags=re.S):
                    name = match.group(1)
                    try:
                        name = bytes(name, "utf-8").decode("unicode_escape")
                    except Exception:
                        pass
                    name = _normalize(name.replace("\\/", "/"))
                    if not name or name.lower() in seen:
                        continue
                    # Avoid bootstrap object names that clearly are not tabs.
                    if len(name) > 100 or name.startswith("http"):
                        continue
                    seen.add(name.lower())
                    names.append(name)
        return names

    def list_tabs(self):
        if self.mode != "direct":
            # The current Apps Script bridge predates tab browsing. Keep the UI
            # usable by exposing its configured/default sheet.
            return {
                "ok": True,
                "mode": self.mode,
                "tabs": [self.sheet_name],
                "selected": self.sheet_name,
                "discovery": "configured",
            }

        names = []
        discovery = "html"
        try:
            names = self._discover_tabs_from_html()
        except Exception:
            names = []

        if not names:
            names = self._configured_tabs()
            discovery = "environment" if names else "fallback"

        if not names:
            names = [self.sheet_name or DEFAULT_SHEET_NAME]

        # Put configured/default tab first when it exists.
        preferred = self.sheet_name or DEFAULT_SHEET_NAME
        ordered = []
        for name in [preferred] + names:
            if name and name.lower() not in {x.lower() for x in ordered}:
                ordered.append(name)

        # Validate likely tabs lazily only when necessary; product loading will
        # return a clear error if a discovered stale tab name is not readable.
        return {
            "ok": True,
            "mode": self.mode,
            "tabs": ordered,
            "selected": preferred,
            "discovery": discovery,
        }

    # ---------------------------------------------------------
    # Product-browser API
    # ---------------------------------------------------------

    def list_products(self, sheet_name=None, status_filter="all", search="", limit=5000):
        if self.mode != "direct":
            raise RuntimeError("Product dropdown browsing currently requires direct shared-sheet mode.")

        selected_sheet = _normalize(sheet_name) or self.sheet_name
        headers, rows = self._direct_rows(selected_sheet)
        product_idx = _product_header_index(headers)
        if product_idx < 0:
            raise RuntimeError(
                f"Product Name column not found in sheet '{selected_sheet}'. "
                "Use Product Name, Product, Keyword, or Search Keyword."
            )
        product_header = headers[product_idx]
        model_header = headers[_header_index(headers, MODEL_HEADERS)] if _header_index(headers, MODEL_HEADERS) >= 0 else ""
        price_header = headers[_header_index(headers, PRICE_HEADERS)] if _header_index(headers, PRICE_HEADERS) >= 0 else ""
        status_header = self._status_header(headers)
        completed = self._completed_set(selected_sheet)
        wanted = _normalize(status_filter).lower() or "all"
        query = _normalize(search).lower()

        products = []
        counts = {"all": 0, "pending": 0, "completed": 0, "failed": 0, "uploaded": 0, "not_uploaded": 0}
        for zero_idx, row in enumerate(rows):
            source_row = int(row.get("__source_row__") or (zero_idx + 2))
            product = _normalize(row.get(product_header))
            if not product:
                continue
            meta = self._classify_row(row, source_row, status_header, completed)
            counts["all"] += 1
            counts[meta["state"]] = counts.get(meta["state"], 0) + 1
            counts["uploaded" if meta["uploaded"] else "not_uploaded"] += 1

            model = _normalize(row.get(model_header)) if model_header else ""
            price = _normalize(row.get(price_header)) if price_header else ""
            searchable = f"{product} {model} {source_row}".lower()
            if query and query not in searchable:
                continue
            if wanted == "pending" and meta["state"] != "pending":
                continue
            if wanted == "completed" and meta["state"] != "completed":
                continue
            if wanted == "failed" and meta["state"] != "failed":
                continue
            if wanted == "uploaded" and not meta["uploaded"]:
                continue
            if wanted in {"not_uploaded", "not uploaded"} and meta["uploaded"]:
                continue

            products.append({
                "source_row": source_row,
                "product_name": product,
                "model": model,
                "selling_price": price,
                "state": meta["state"],
                "uploaded": meta["uploaded"],
                "status": meta["remote_status"],
            })
            if len(products) >= max(1, min(int(limit or 5000), 10000)):
                break

        detected_product_header = ""
        for row in rows[:5]:
            if row.get("__detected_product_header__"):
                detected_product_header = _normalize(row.get("__detected_product_header__"))
                break
        return {
            "ok": True,
            "mode": self.mode,
            "sheet_name": selected_sheet,
            "products": products,
            "counts": counts,
            "total_rows": len(rows),
            "columns": {
                "product": detected_product_header or product_header,
                "model": model_header,
                "selling_price": price_header,
            },
        }

    def get_row(self, source_row, sheet_name=None):
        if self.mode != "direct":
            raise RuntimeError("Selected-row loading currently requires direct shared-sheet mode.")
        selected_sheet = _normalize(sheet_name) or self.sheet_name
        try:
            row_number = int(source_row)
        except Exception:
            raise RuntimeError("Invalid Google Sheet row number.")
        if row_number < 2:
            raise RuntimeError("Invalid Google Sheet row number.")

        headers, rows = self._direct_rows(selected_sheet)
        row = next(
            (item for item in rows if int(item.get("__source_row__") or -1) == row_number),
            None,
        )
        if row is None:
            raise RuntimeError(f"Row {row_number} was not found in sheet '{selected_sheet}'.")
        product = _row_value(row, PRODUCT_HEADERS)
        if not product:
            raise RuntimeError(
                f"Row {row_number} does not contain a recognizable product value. "
                "V3.3 automatically supports Product Name, Product, Item Name, Material Name, "
                "Product/Service, Keyword, and similar product columns."
            )
        public_row = {k: v for k, v in row.items() if not str(k).startswith("__")}
        return {
            "ok": True,
            "empty": False,
            "mode": self.mode,
            "sheet_name": selected_sheet,
            "source_row": row_number,
            "product_name": product,
            "model": _row_value(row, MODEL_HEADERS),
            "selling_price": _row_value(row, PRICE_HEADERS),
            "row": public_row,
        }

    # ---------------------------------------------------------
    # Existing direct mode operations
    # ---------------------------------------------------------

    def _direct_ping(self):
        headers, rows = self._direct_rows(self.sheet_name)
        return {
            "ok": True,
            "service": "NUNES_DIRECT_GOOGLE_SHEET",
            "mode": "direct",
            "sheet_id": self.sheet_id,
            "sheet_name": self.sheet_name,
            "columns": len(headers),
            "rows": len(rows),
        }

    def _direct_next(self, sheet_name=None):
        selected_sheet = _normalize(sheet_name) or self.sheet_name
        headers, rows = self._direct_rows(selected_sheet)
        product_idx = _product_header_index(headers)
        if product_idx < 0:
            raise RuntimeError(
                "Product Name column not found. Use a header named Product Name, Product, Keyword, or Search Keyword."
            )

        status_header = self._status_header(headers)
        progress = self._read_progress()
        completed = self._completed_set(selected_sheet)
        product_header = headers[product_idx]

        for zero_idx, row in enumerate(rows):
            source_row = int(row.get("__source_row__") or (zero_idx + 2))
            product = _normalize(row.get(product_header))
            if not product:
                continue
            meta = self._classify_row(row, source_row, status_header, completed)
            if meta["state"] == "completed":
                continue

            progress["last_loaded_row"] = source_row
            progress["last_loaded_sheet"] = selected_sheet
            self._write_progress(progress)
            return {
                "ok": True,
                "empty": False,
                "source_row": source_row,
                "row": {k: v for k, v in row.items() if not str(k).startswith("__")},
                "mode": "direct",
                "sheet_name": selected_sheet,
            }

        return {
            "ok": True,
            "empty": True,
            "message": f"No unprocessed product rows found in '{selected_sheet}'.",
            "mode": "direct",
            "sheet_name": selected_sheet,
        }

    def _direct_log(self, data):
        source_row = _normalize((data or {}).get("source_row"))
        sheet_name = _normalize((data or {}).get("sheet_name")) or self.sheet_name
        status = _normalize((data or {}).get("status")).lower()
        progress = self._read_progress()
        completed = self._completed_set(sheet_name)
        success_statuses = {"published", "completed", "success", "manual_completed", "uploaded", "done"}
        if source_row and status in success_statuses:
            completed.add(source_row)
        by_sheet = progress.get("completed_by_sheet", {})
        if not isinstance(by_sheet, dict):
            by_sheet = {}
        by_sheet[sheet_name] = sorted(completed, key=lambda x: int(x) if str(x).isdigit() else 10**9)
        progress["completed_by_sheet"] = by_sheet
        # Keep the legacy field in sync only for the configured/default tab.
        if sheet_name == self.sheet_name:
            progress["completed_rows"] = list(by_sheet[sheet_name])
        progress["last_result"] = dict(data or {})
        progress["last_result_sheet"] = sheet_name
        self._write_progress(progress)
        return {
            "ok": True,
            "mode": "direct",
            "local_only": True,
            "message": "Row completion recorded locally. Remote Sheet write-back requires the optional Apps Script bridge.",
        }

    # ---------------------------------------------------------
    # Public API
    # ---------------------------------------------------------

    def ping(self):
        return self._apps_ping() if self.mode == "apps_script" else self._direct_ping()

    def next_product(self, sheet_name=None):
        if self.mode == "apps_script":
            return self._apps_next()
        return self._direct_next(sheet_name=sheet_name)

    def log(self, data):
        return self._apps_log(data) if self.mode == "apps_script" else self._direct_log(data)
