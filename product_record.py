import re
from copy import deepcopy

MODEL_HEADERS = {
    "model",
    "model no",
    "model no.",
    "model number",
    "model name",
    "model name/number",
    "model name / number",
    "part number",
    "part no",
    "part no.",
    "catalogue number",
    "catalog number",
    "cat no",
    "cat no.",
}

PRICE_HEADERS = {
    "price",
    "selling price",
    "product price",
    "rate",
    "unit price",
    "sale price",
    "sales price",
    "offer price",
    "selling rate",
}

JUNK_SPEC_HEADERS = {
    "company name", "seller", "seller name", "business name", "supplier name",
    "mobile", "phone", "address", "price", "selling price", "product price",
    "unit price", "rate", "view in hindi", "contact supplier", "call now",
    "get best price", "request callback", "send enquiry", "send inquiry",
    "view all", "more products from this seller",
}


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm_header(value):
    value = clean_text(value).lower()
    value = re.sub(r"\s*/\s*", "/", value)
    return value.strip(" :.-")


def clean_specs(specs, limit=30):
    cleaned = []
    seen = set()
    fragments = (
        "more products from this seller", "contact supplier", "call now",
        "get best price", "request callback", "send enquiry", "send inquiry",
        "view in hindi",
    )
    for item in specs or []:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"))
        value = clean_text(item.get("value"))
        if not name or not value:
            continue
        low = norm_header(name)
        if low in JUNK_SPEC_HEADERS or any(x in low for x in fragments):
            continue
        if value.lower() in {
            "contact supplier", "call now", "get best price", "request callback", "view all"
        }:
            continue
        key = (low, value.lower())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({"name": name, "value": value})
        if len(cleaned) >= limit:
            break
    return cleaned


def detect_model(specs):
    rows = [(norm_header(x.get("name")), clean_text(x.get("value"))) for x in specs or [] if isinstance(x, dict)]
    priority = [
        "model name/number", "model name / number", "model number", "model no",
        "model no.", "model name", "model", "part number", "part no", "part no.",
        "catalogue number", "catalog number", "cat no", "cat no.",
    ]
    for wanted in priority:
        wn = norm_header(wanted)
        for key, value in rows:
            if key == wn and value:
                return value
    for key, value in rows:
        if value and key.startswith("model"):
            return value
    return ""


def sync_model_spec(specs, model, add_if_missing=True):
    specs = clean_specs(specs)
    model = clean_text(model)
    result = []
    model_written = False
    for item in specs:
        name = clean_text(item.get("name"))
        value = clean_text(item.get("value"))
        if norm_header(name) in {norm_header(x) for x in MODEL_HEADERS}:
            if model and not model_written:
                result.append({"name": name or "Model", "value": model})
                model_written = True
            continue
        result.append({"name": name, "value": value})
    if model and add_if_missing and not model_written:
        result.insert(0, {"name": "Model", "value": model})
    return result[:30]


def extract_selling_price(row):
    if not isinstance(row, dict):
        return ""
    for key, value in row.items():
        if norm_header(key) in PRICE_HEADERS:
            text = clean_text(value)
            if text:
                return text
    return ""


def normalize_ai_status(status):
    status = dict(status or {})
    mode = clean_text(status.get("mode")).upper()
    attempted = bool(status.get("attempted") or mode or status.get("label") or status.get("model"))
    response_ok = bool(status.get("response_ok") or mode in {"QWEN_SUCCESS", "QWEN_WITH_FALLBACK"})
    status["attempted"] = attempted
    status["response_ok"] = response_ok
    status["mode"] = mode or ("PYTHON_FALLBACK" if attempted else "NOT_RUN")
    if not status.get("label"):
        if status["mode"] == "QWEN_SUCCESS":
            status["label"] = "Qwen3 Used Successfully"
        elif status["mode"] == "QWEN_WITH_FALLBACK":
            status["label"] = "Qwen3 + Python Fallback"
        elif status["mode"] == "PYTHON_FALLBACK":
            status["label"] = "Python Fallback Used"
        else:
            status["label"] = "AI has not run yet"
    return status


def new_record(product_name, model="", source_row="", sheet_row=None, sheet_name=""):
    product_name = clean_text(product_name)
    model = clean_text(model)
    sheet_row = dict(sheet_row or {}) if isinstance(sheet_row, dict) else {}
    return {
        "schema_version": 2,
        "record_product_name": product_name,
        "source": {
            "source_row": clean_text(source_row),
            "sheet_name": clean_text(sheet_name),
            "sheet_row": sheet_row,
            "product_name": product_name,
            "model": model,
            "selling_price": extract_selling_price(sheet_row),
        },
        "research": {
            "found": False,
            "title": "",
            "source_url": "",
            "discovery_url": "",
            "description": "",
            "specifications": [],
            "company_details": {},
            "image_urls": [],
            "image_candidates": [],
        },
        "approved": {
            "listing_name": product_name,
            "product_name": product_name,
            "model": model,
            "variant_name": f"{product_name} {model}".strip() if model else product_name,
            "description": "",
            "key_features": [],
            "applications": [],
            "specifications": [],
            "company_details": {},
        },
        "ai": normalize_ai_status({}),
        "assets": {
            "images": [],
            "image_web_urls": [],
            "image_source": "PENDING",
            "primary_image_slot": 0,
            "master_image_slot": 0,
            "image_locks": [False, False, False, False, False],
            "pdf_path": "",
            "pdf_web_url": "",
        },
        "indiamart": {
            "decision": "",
            "match": {},
            "seller_status": "",
            "seller_message": "",
            "url": "",
        },
        "status": "NEW",
        "timestamps": {},
    }


def deep_copy_record(record):
    return deepcopy(record or {})
