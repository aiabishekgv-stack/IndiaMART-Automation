import json
import os
import re
import time

import requests


class ProductAI:
    """Local Ollama product-content generator with evidence-locked fallbacks."""

    MODEL_HEADERS = (
        "model name/number",
        "model name / number",
        "model number",
        "model no",
        "model no.",
        "model name",
        "model",
        "part number",
        "part no",
        "part no.",
        "catalogue number",
        "catalog number",
        "cat no",
        "cat no.",
    )

    APPLICATION_HEADERS = {
        "application",
        "applications",
        "usage",
        "usage/application",
        "usage / application",
        "use",
        "uses",
        "suitable for",
        "suitable application",
    }

    JUNK_HEADERS = {
        "",
        "product name",
        "product",
        "name",
        "keyword",
        "status",
        "processed",
        "remarks",
        "remark",
        "price",
        "selling price",
        "product price",
        "rate",
        "unit price",
        "view in hindi",
        "contact supplier",
        "call now",
        "get best price",
        "request callback",
        "send enquiry",
        "send inquiry",
        "view all",
        "more products from this seller",
        "company name",
        "seller",
        "seller name",
        "business name",
        "supplier name",
        "mobile",
        "phone",
        "address",
    }

    def __init__(self):
        self.base_url = os.getenv(
            "OLLAMA_URL",
            "http://127.0.0.1:11434",
        ).rstrip("/")
        self.model = os.getenv(
            "OLLAMA_MODEL",
            "qwen3:1.7b",
        )
        try:
            self.timeout = max(10, int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120")))
        except Exception:
            self.timeout = 120

    def _clean_text(self, value):
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _norm_header(self, value):
        value = self._clean_text(value).lower()
        value = re.sub(r"\s*/\s*", "/", value)
        value = re.sub(r"[^a-z0-9/ ._-]+", "", value)
        return value.strip()

    def _is_junk_pair(self, key, value):
        key_norm = self._norm_header(key)
        value_norm = self._clean_text(value).lower()

        if key_norm in self.JUNK_HEADERS:
            return True

        junk_fragments = (
            "more products from this seller",
            "contact supplier",
            "call now",
            "get best price",
            "request callback",
            "send enquiry",
            "send inquiry",
            "view in hindi",
        )
        if any(x in key_norm for x in junk_fragments):
            return True

        if value_norm in {
            "view all",
            "contact supplier",
            "call now",
            "get best price",
            "request callback",
        }:
            return True

        # Price rows are not product specifications.
        if key_norm in {"price", "rate", "unit price", "selling price", "product price"}:
            return True

        return False

    def _detect_model(self, row):
        normalized = []
        for key, value in (row or {}).items():
            key_norm = self._norm_header(key)
            value = self._clean_text(value)
            if key_norm and value:
                normalized.append((key_norm, value))

        for wanted in self.MODEL_HEADERS:
            wanted_norm = self._norm_header(wanted)
            for key_norm, value in normalized:
                if key_norm == wanted_norm:
                    return value

        # Conservative fallback for headers that clearly start with Model.
        for key_norm, value in normalized:
            if key_norm.startswith("model ") or key_norm.startswith("model-"):
                return value

        return ""

    def _valid_source_items(self, row):
        specs = []
        seen = set()

        for key, value in (row or {}).items():
            key = self._clean_text(key)
            value = self._clean_text(value)
            if not key or not value:
                continue

            key_norm = self._norm_header(key)
            if key_norm in self.APPLICATION_HEADERS:
                continue
            if self._is_junk_pair(key, value):
                continue

            pair = (key_norm, value.lower())
            if pair in seen:
                continue
            seen.add(pair)

            specs.append({"name": key, "value": value})

        return specs[:24]

    def _extract_explicit_applications(self, row):
        results = []
        seen = set()

        for key, value in (row or {}).items():
            key_norm = self._norm_header(key)
            value = self._clean_text(value)
            if key_norm not in self.APPLICATION_HEADERS or not value:
                continue

            for piece in re.split(r"[\n,;|]+", value):
                piece = self._clean_text(piece)
                if not piece:
                    continue
                low = piece.lower()
                if low not in seen:
                    seen.add(low)
                    results.append(piece)

        return results[:8]

    def _extract_json(self, text):
        text = str(text or "").strip()
        text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        try:
            return json.loads(text)
        except Exception:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except Exception:
                return {}

    def _safe_description(self, product_name, specifications, existing_description=""):
        product_name = self._clean_text(product_name)
        parts = [f"{product_name} is supplied for the stated product requirements."]
        if specifications:
            spec_text = "; ".join(
                f"{x['name']}: {x['value']}" for x in specifications[:8]
            )
            parts.append("Key available specifications include " + spec_text + ".")
        if existing_description:
            # Do not blindly reproduce scraped marketing text; only signal evidence exists.
            parts.append("Additional product information was available in the public source listing.")
        return " ".join(parts)

    def _safe_features(self, specifications):
        return [
            f"{x['name']}: {x['value']}"
            for x in specifications[:16]
            if x.get("name") and x.get("value")
        ]

    def _safe_applications(self, explicit_applications):
        return [self._clean_text(x) for x in explicit_applications if self._clean_text(x)][:8]

    def _applications_from_public_description(self, existing_description):
        """Evidence-only fallback: extract sentences that explicitly state product use."""
        text = str(existing_description or "")
        text = re.sub(r"INDIAMART RESULT \d+", " ", text, flags=re.I)
        text = re.sub(r"TITLE:\s*[^\n]+", " ", text, flags=re.I)
        text = re.sub(r"DESCRIPTION:\s*", "", text, flags=re.I)
        sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
        markers = (
            "used for", "used in", "used to", "suitable for", "designed for",
            "ideal for", "application", "applications", "intended for",
            "commonly used", "helps to", "used as",
        )
        out, seen = [], set()
        for sentence in sentences:
            sentence = self._clean_text(sentence)
            low = sentence.lower()
            if len(sentence) < 12 or not any(marker in low for marker in markers):
                continue
            # Keep the statement evidence-backed and reasonably short.
            sentence = sentence[:220].rstrip(" ,;:-")
            if low not in seen:
                seen.add(low)
                out.append(sentence)
            if len(out) >= 6:
                break
        return out

    def _clean_list(self, value, max_items=8):
        if isinstance(value, str):
            value = re.split(r"[\n|]+", value)
        if not isinstance(value, list):
            return []
        out, seen = [], set()
        for item in value:
            item = self._clean_text(item)
            item = re.sub(r"^[\-•*\d.)\s]+", "", item).strip()
            if not item:
                continue
            low = item.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(item)
            if len(out) >= max_items:
                break
        return out

    def create_content(self, product_name, row, existing_description=""):
        product_name = self._clean_text(product_name)
        specifications = self._valid_source_items(row)
        explicit_applications = self._extract_explicit_applications(row)
        detected_model = self._detect_model(row)

        evidence = {
            "product_name": product_name,
            "detected_model": detected_model,
            "specifications": specifications,
            "explicit_applications": explicit_applications,
            "public_description": self._clean_text(existing_description)[:5200],
        }

        prompt = f"""
You are NUNES Product Intelligence running locally.

Use ONLY the evidence below. Do not invent specifications, brands, models, certifications,
applications, industries, performance claims, materials, accessories, warranties, or features.
You may rewrite factual evidence into concise professional catalogue language.

EVIDENCE:
{json.dumps(evidence, ensure_ascii=False, indent=2)}

TASK:
Create one concise IndiaMART-ready content package.

RULES:
- listing_name: keep the supplied product name unless the evidence provides a clearer matching title.
- model: use only detected_model. Never create a model.
- variant_name: product name plus model only when a model exists.
- description: create approximately 100-120 words, ideally 4-6 professional factual sentences. Keep it concise enough to fit the two-page NUNES brochure reference. Do not copy seller marketing word-for-word.
- key_features: create up to 16 short, one-line factual bullets from specifications and clearly supported statements in the scanned descriptions. Prefer 12-16 when enough evidence exists; never invent facts just to reach a count.
- applications: use explicit_applications when present. Otherwise you MAY create applications only when the scanned descriptions clearly state a use, purpose, process, or suitable application.
- Never invent an industry or application that is not supported by the scanned public descriptions or explicit application fields.
- If no use/application evidence exists anywhere, return an empty applications array.
- Never output price, seller CTA text, "View in Hindi", company-contact text, or unrelated page text.
- Return JSON only. No reasoning.

Return exactly:
{{
  "listing_name": "",
  "model": "",
  "variant_name": "",
  "description": "",
  "key_features": [],
  "applications": []
}}
"""

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "think": False,
            "options": {
                "temperature": 0.1,
                "top_p": 0.5,
                "num_ctx": 4096,
            },
        }

        start = time.perf_counter()
        ai_data = {}
        ai_error = ""
        response_chars = 0

        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw = response.json().get("message", {}).get("content", "")
            response_chars = len(str(raw or ""))
            ai_data = self._extract_json(raw)
        except Exception as exc:
            ai_error = f"{type(exc).__name__}: {exc}"

        elapsed = round(time.perf_counter() - start, 2)

        ai_fields = []
        fallback_fields = []

        listing_name = self._clean_text(ai_data.get("listing_name"))
        if listing_name:
            ai_fields.append("listing_name")
        else:
            listing_name = product_name
            fallback_fields.append("listing_name")

        model = detected_model  # deterministic evidence always wins

        variant_name = self._clean_text(ai_data.get("variant_name"))
        if variant_name:
            ai_fields.append("variant_name")
        else:
            variant_name = f"{listing_name} {model}".strip() if model else listing_name
            fallback_fields.append("variant_name")

        description = self._clean_text(ai_data.get("description"))
        if description:
            ai_fields.append("description")
        else:
            description = self._safe_description(product_name, specifications, existing_description)
            fallback_fields.append("description")

        key_features = self._clean_list(ai_data.get("key_features"), 16)
        if key_features:
            ai_fields.append("key_features")
        else:
            key_features = self._safe_features(specifications)
            fallback_fields.append("key_features")

        applications = self._clean_list(ai_data.get("applications"), 8)
        if applications:
            ai_fields.append("applications")
        else:
            if explicit_applications:
                applications = self._safe_applications(explicit_applications)
            if not applications:
                applications = self._applications_from_public_description(existing_description)
            fallback_fields.append("applications")

        if ai_data and not ai_error:
            if fallback_fields:
                mode = "QWEN_WITH_FALLBACK"
                label = "Qwen3 + Python Fallback"
            else:
                mode = "QWEN_SUCCESS"
                label = "Qwen3 Used Successfully"
        else:
            mode = "PYTHON_FALLBACK"
            label = "Python Fallback Used"

        return {
            "listing_name": listing_name,
            "model": model,
            "variant_name": variant_name,
            "description": description,
            "key_features": key_features,
            "applications": applications,
            "specifications": specifications,
            "image_prompt_base": "Commercial catalog photograph of " + product_name,
            "ai_status": {
                "provider": "ollama",
                "model": self.model,
                "attempted": True,
                "response_ok": bool(ai_data) and not ai_error,
                "mode": mode,
                "label": label,
                "response_time_seconds": elapsed,
                "ai_fields": ai_fields,
                "fallback_fields": fallback_fields,
                "error": ai_error,
                "raw_response_chars": response_chars,
            },
        }
