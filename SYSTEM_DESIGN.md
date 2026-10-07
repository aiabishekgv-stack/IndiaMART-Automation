# NUNES IndiaMART Automation V2.4 — System Design

## Flow

`Manual/Sheet → Public Research → Evidence cleaning → Qwen/Ollama → 5 images → Review → Approved PDF → IndiaMART catalogue match → Add Variant/New Product → verified uploads → review/publish`

## Separation of responsibilities

- `app.py`: HTTP API/UI integration and localhost-only admin controls.
- `config_manager.py`: per-PC private configuration loading/saving.
- `automation.py`: orchestration/state/product lifecycle.
- `product_record.py`: canonical product record, model sync and selling-price safety.
- `public_indiamart.py`: public/read-only product evidence extraction.
- `ai_client.py`: local Qwen content generation with deterministic fallback.
- `image_acquirer.py`: genuine source image → five catalogue variants.
- `pdf_builder.py`: approved two-page product PDF.
- `indiamart.py`: seller session, catalogue matching, form filling and upload verification.
- `sheets_client.py`: Apps Script bridge.

## Private vs shared storage

Shared project/NAS:
- source code
- HTML/template assets
- `.env.example`
- docs

Local server PC:
- `%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\venv`
- `%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\config\.env`
- `%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\runtime\indiamart_storage_state.json`

## LAN security boundary

Credential configuration is localhost-only. Remote clients can use normal product workflow but cannot POST new Google Sheet credentials or reset server state.

## V2.9 PDF reference profile

`pdf_builder.py` is calibrated from `assets/reference_brochure.pdf`. The layout profile uses the measured A4 coordinates, seven-row specification table, 23.46 pt product title, 15.53 pt description heading, 12.44 pt Key Features heading, 9.39 pt body text, and approximately 115 brochure-description words.
