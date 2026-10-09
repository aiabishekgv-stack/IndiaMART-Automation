import json
import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

from ai_client import ProductAI
from image_acquirer import ImageAcquirer
from indiamart import IndiaMartBrowser
from pdf_builder import build_product_pdf
from product_record import (
    clean_specs,
    clean_text,
    detect_model,
    extract_selling_price,
    new_record,
    normalize_ai_status,
    sync_model_spec,
)
from public_indiamart import PublicIndiaMartResearch
from sheets_client import SheetBridge
from production_manager import create_backup, mark_failure, clear_failure


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
INPUT_IMAGES = ROOT / "input_images"
LOGS = ROOT / "logs"
if not os.getenv("VERCEL"):
    for folder in (OUTPUTS, INPUT_IMAGES, LOGS):
        folder.mkdir(parents=True, exist_ok=True)


def safe_name(text):
    text = str(text or "")
    value = "_".join(
        "".join(c if (c.isalnum() or c in " -_.") else "_" for c in text).split()
    )[:90]
    return value or "product"


class State:
    def __init__(self):
        self._lock = threading.RLock()
        self.login_busy = False
        self.login_connected = False
        self.login_source = ""
        self.login_message = "Not checked yet."
        self.reset()

    def reset(self):
        with getattr(self, "_lock", threading.RLock()):
            self.running = False
            self.phase = "IDLE"
            self.stage = "IDLE"
            self.progress = 0
            self.product = ""
            self.logs = []
            self.result = None
            self.error = None
            self.models = {}

    def begin(self, phase, product="", preserve_result=None):
        with self._lock:
            self.running = True
            self.phase = phase
            self.stage = phase
            self.progress = 0
            self.product = str(product or "")
            self.logs = []
            self.result = preserve_result
            self.error = None
            self.models = {}

    def push(self, stage, msg, progress=None):
        with self._lock:
            self.stage = stage
            if progress is not None:
                self.progress = max(0, min(100, int(progress)))
            self.logs.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "stage": str(stage),
                "message": str(msg),
            })
            self.logs = self.logs[-300:]

    def snapshot(self):
        with self._lock:
            return {
                "running": self.running,
                "login_busy": self.login_busy,
                "login_connected": self.login_connected,
                "login_source": self.login_source,
                "login_message": self.login_message,
                "phase": self.phase,
                "stage": self.stage,
                "progress": self.progress,
                "product": self.product,
                "logs": list(self.logs),
                "result": self.result,
                "error": self.error,
                "models": dict(self.models),
            }


STATE = State()
INDIAMART_LOCK = threading.Lock()


def _browser_event(stage, msg, progress=None):
    STATE.push(stage, msg, progress)


def _product_folder(product):
    folder = OUTPUTS / safe_name(product)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _record_path(product):
    return _product_folder(product) / "product_record.json"


def _legacy_record_path(product):
    return _product_folder(product) / "prepared_product.json"


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_record(product):
    path = _record_path(product)
    if not path.exists():
        legacy = _legacy_record_path(product)
        if legacy.exists():
            data = json.loads(legacy.read_text(encoding="utf-8"))
            # V2 deliberately does not silently reinterpret complex V1 state.
            raise RuntimeError(
                "A V1 prepared record exists for this product. Run Public Research & Prepare once in V2."
            )
        raise RuntimeError("Prepared product record not found. Run Public Research & Prepare first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _web_paths(product, images, pdf_path):
    folder = safe_name(product)
    image_urls = [f"/outputs/{folder}/images/{Path(path).name}" for path in images]
    pdf_url = f"/outputs/{folder}/{Path(pdf_path).name}" if pdf_path else ""
    return image_urls, pdf_url


def _recover_images(product, existing):
    good = []
    for item in existing or []:
        p = Path(str(item or ""))
        if p.exists() and p.is_file():
            good.append(str(p.resolve()))
    images_dir = _product_folder(product) / "images"
    if not good and images_dir.exists():
        for pattern in ("product_*.jpg", "product_*.png", "product_*.webp"):
            found = [x for x in sorted(images_dir.glob(pattern)) if x.is_file()]
            if found:
                good.extend(str(x.resolve()) for x in found[:5])
                break
    if not good:
        for name in ("source_original.jpg", "source_original.png", "source_original.webp"):
            p = images_dir / name
            if p.exists():
                good.append(str(p.resolve()))
                break
    return good


def _parse_lines(value, limit=12):
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").replace("\r", "").split("\n")
    result = []
    seen = set()
    for item in items:
        text = clean_text(str(item or "").lstrip("-•* "))
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _parse_specs(value):
    if isinstance(value, list):
        return clean_specs(value)
    specs = []
    for line in str(value or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, val = line.split(":", 1)
        elif "=" in line:
            name, val = line.split("=", 1)
        else:
            continue
        name, val = clean_text(name), clean_text(val)
        if name and val:
            specs.append({"name": name, "value": val})
    return clean_specs(specs)


def _find_local_product_images(product):
    folder = INPUT_IMAGES / safe_name(product)
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            results.append(path)
    return results


def _build_ai_evidence_row(product, model, sheet_row, specs):
    row = {"Product Name": product}
    if model:
        row["Model"] = model
    if isinstance(sheet_row, dict):
        for key, value in sheet_row.items():
            key = clean_text(key)
            if key and key not in row and clean_text(value):
                row[key] = value
    for spec in specs[:24]:
        name = clean_text(spec.get("name"))
        value = clean_text(spec.get("value"))
        if name and value and name not in row:
            row[name] = value
    return row


def prepare_product(form_data, uploaded_image_path=None):
    """Phase 1: public evidence -> Qwen/fallback -> images -> operator review. No seller login, no PDF."""
    if STATE.running:
        return

    form_data = dict(form_data or {})
    product = clean_text(form_data.get("product_name"))
    model = clean_text(form_data.get("model"))
    source_row = clean_text(form_data.get("source_row"))
    sheet_name = clean_text(form_data.get("sheet_name"))
    sheet_row = form_data.get("sheet_row") if isinstance(form_data.get("sheet_row"), dict) else {}
    if not product:
        return

    record = new_record(product, model, source_row, sheet_row, sheet_name=sheet_name)
    STATE.begin("PUBLIC_RESEARCH", product, preserve_result=record)
    folder = _product_folder(product)
    research_browser = None

    try:
        query = " ".join(x for x in (product, model) if x).strip()
        STATE.push("INPUT", "Public research started quietly in the background. No search-browser window will be shown.", 5)

        research_browser = PublicIndiaMartResearch(event=_browser_event)
        public_headless = os.getenv("PUBLIC_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}
        research_browser.start(headless=public_headless)
        research = research_browser.research(query)
        research["specifications"] = clean_specs(research.get("specifications", []))

        if not model:
            model = detect_model(research["specifications"])
            if model:
                STATE.push("MODEL_DETECTED", f"Model detected from public evidence: {model}", 28)

        research["specifications"] = sync_model_spec(research["specifications"], model)
        company_details = dict(research.get("company_details", {}) or {})
        company_details.setdefault("company_name", "")
        company_details.setdefault("location", "")
        company_details.setdefault("source_url", research.get("product_url", "") or "")
        company_details.setdefault("source_domain", "")

        record["source"]["model"] = model
        record["research"] = {
            "found": bool(research.get("found")),
            "title": research.get("title", ""),
            "source_url": research.get("product_url", ""),
            "discovery_url": research.get("discovery_url", "") or research.get("search_url", ""),
            "description": research.get("description", ""),
            "specifications": research["specifications"],
            "company_details": company_details,
            "image_urls": list(research.get("image_urls", []) or []),
            "image_candidates": list(research.get("image_candidates", []) or []),
        }
        record["timestamps"]["researched_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(folder / "public_research.json", research)
        STATE.push(
            "PUBLIC_EXTRACT",
            f"Research complete: {len(research['specifications'])} specifications, "
            f"{len(record['research'].get('image_candidates') or record['research']['image_urls'])} image candidates.",
            36,
        )

        STATE.push("LOCAL_AI", "Qwen3 is organizing only the available evidence.", 45)
        ai = ProductAI()
        ai_row = _build_ai_evidence_row(product, model, sheet_row, research["specifications"])
        content = ai.create_content(product, ai_row, research.get("description", ""))
        ai_status = normalize_ai_status(content.get("ai_status", {}))
        content["ai_status"] = ai_status

        # Deterministic values win over generated wording.
        approved_model = model or clean_text(content.get("model"))
        listing_name = clean_text(content.get("listing_name")) or product
        content["product_name"] = listing_name
        content["listing_name"] = listing_name
        content["model"] = approved_model
        content["variant_name"] = f"{listing_name} {approved_model}".strip() if approved_model else listing_name
        content["specifications"] = sync_model_spec(
            content.get("specifications") or research["specifications"], approved_model
        )
        content["company_details"] = company_details

        record["ai"] = ai_status
        record["approved"] = {
            "listing_name": listing_name,
            "product_name": listing_name,
            "model": approved_model,
            "variant_name": content["variant_name"],
            "description": clean_text(content.get("description")),
            "key_features": _parse_lines(content.get("key_features", []), 16),
            "applications": _parse_lines(content.get("applications", []), 8),
            "specifications": content["specifications"],
            "company_details": company_details,
        }
        record["timestamps"]["ai_at"] = datetime.now().isoformat(timespec="seconds")
        STATE.models = {
            "text": f"ollama/{ai.model}",
            "research": "public_indiamart_no_seller_login",
            "image": "multi_reference_diverse_catalog_variants",
            "ai_status": ai_status,
        }
        STATE.push(
            "AI_STATUS" if ai_status.get("response_ok") else "AI_FALLBACK",
            ai_status.get("label") or ai_status.get("mode"),
            55,
        )
        _write_json(folder / "content.json", record["approved"])

        image_acquirer = ImageAcquirer()
        images = []
        image_source = "PENDING"

        local_reference_files = []
        seen_local = set()

        if uploaded_image_path:
            uploaded_path = Path(uploaded_image_path)
            if uploaded_path.exists():
                resolved = str(uploaded_path.resolve())
                if resolved not in seen_local:
                    local_reference_files.append(uploaded_path)
                    seen_local.add(resolved)

        for path in _find_local_product_images(product):
            try:
                resolved = str(path.resolve())
            except Exception:
                resolved = str(path)
            if resolved not in seen_local:
                local_reference_files.append(path)
                seen_local.add(resolved)

        # V4.1: keep the newly uploaded image as the master visual reference.
        # Rich IndiaMART candidates carry page-match metadata from every matched
        # product page. ImageAcquirer filters/ranks them against the master.
        public_candidates = list(record["research"].get("image_candidates") or [])
        if not public_candidates:
            public_candidates = list(record["research"].get("image_urls") or [])

        master_image = None
        if uploaded_image_path and Path(uploaded_image_path).exists():
            master_image = Path(uploaded_image_path)

        if local_reference_files or public_candidates:
            STATE.push(
                "PRODUCT_IMAGES",
                (
                    f"Using the uploaded image as master reference and checking "
                    f"{len(public_candidates)} IndiaMART gallery image candidate(s). "
                    "Only visually/relevantly matching images will be accepted for slots 2-5."
                    if master_image else
                    f"Scanning {len(local_reference_files)} local/account image(s) and "
                    f"{len(public_candidates)} IndiaMART image candidate(s)."
                ),
                65,
            )
            try:
                images = image_acquirer.from_reference_set(
                    local_files=local_reference_files,
                    urls=public_candidates,
                    out_dir=folder / "images",
                    master_file=master_image,
                )
                if master_image and public_candidates:
                    image_source = "UPLOADED_MASTER_PLUS_MATCHED_INDIAMART"
                elif local_reference_files and public_candidates:
                    image_source = "LOCAL_PLUS_PUBLIC_REFERENCES"
                elif local_reference_files:
                    image_source = "LOCAL_REFERENCE_SET"
                else:
                    image_source = "PUBLIC_REFERENCE_SET"
            except Exception as exc:
                STATE.push("IMAGE_WARNING", "Reference images could not be processed: " + str(exc), 75)

        images = _recover_images(product, images)
        image_web_urls, _ = _web_paths(product, images, "")
        record["assets"].update({
            "images": images,
            "image_web_urls": image_web_urls,
            "image_source": image_source,
            "primary_image_slot": 1 if images else 0,
            "master_image_slot": 1 if (images and uploaded_image_path) else 0,
            # Protect the uploaded master from automatic regeneration. The admin
            # can still unlock it manually if a deliberate change is required.
            "image_locks": [True, False, False, False, False] if (images and uploaded_image_path) else [False, False, False, False, False],
            "pdf_path": "",
            "pdf_web_url": "",
        })
        record["status"] = "REVIEW_READY" if images else "REVIEW_READY_IMAGE_PENDING"
        record["timestamps"]["prepared_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(_record_path(product), record)
        clear_failure(product)
        create_backup(product, "prepared")
        STATE.result = record
        STATE.push(
            "REVIEW_READY",
            "Preparation complete. Review/edit the right panel. The PDF has not been created yet.",
            100,
        )
    except Exception as exc:
        STATE.error = str(exc)
        STATE.push("ERROR", str(exc), 100)
        try:
            if record and not _record_path(product).exists():
                _write_json(_record_path(product), record)
            mark_failure(product, "PUBLIC_RESEARCH", STATE.stage, str(exc))
            (folder / "prepare_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
    finally:
        if research_browser:
            research_browser.close()
        STATE.running = False


def save_review_and_build_pdf(record_product_name, review_data):
    """Phase 1B: save operator-approved fields, synchronize model, then build the PDF."""
    if STATE.running:
        raise RuntimeError("Another process is already running")
    product = clean_text(record_product_name)
    if not product:
        raise RuntimeError("Product name is required")

    record = _read_record(product)
    create_backup(product, "before_pdf")
    STATE.begin("BUILD_PDF", product, preserve_result=record)
    folder = _product_folder(product)
    review_data = dict(review_data or {})

    try:
        approved = dict(record.get("approved", {}) or {})
        company_details = dict(approved.get("company_details", {}) or record.get("research", {}).get("company_details", {}) or {})

        listing_name = clean_text(review_data.get("listing_name") or approved.get("listing_name") or product)
        model = clean_text(review_data["model"]) if "model" in review_data else clean_text(approved.get("model"))
        description = clean_text(review_data.get("description") or approved.get("description"))
        features = _parse_lines(review_data.get("key_features", approved.get("key_features", [])), 16)
        applications = _parse_lines(review_data.get("applications", approved.get("applications", [])), 8)
        specs = _parse_specs(review_data.get("specifications", approved.get("specifications", [])))
        if not model:
            model = detect_model(specs)
        specs = sync_model_spec(specs, model)

        if "company_name" in review_data:
            company_details["company_name"] = clean_text(review_data.get("company_name"))
        if "company_location" in review_data:
            company_details["location"] = clean_text(review_data.get("company_location"))
        if review_data.get("source_url"):
            company_details["source_url"] = clean_text(review_data.get("source_url"))

        approved.update({
            "listing_name": listing_name,
            "product_name": listing_name,
            "model": model,
            "variant_name": f"{listing_name} {model}".strip() if model else listing_name,
            "description": description,
            "key_features": features,
            "applications": applications,
            "specifications": specs,
            "company_details": company_details,
        })
        record["approved"] = approved
        record["research"]["specifications"] = sync_model_spec(record.get("research", {}).get("specifications", []), model)

        images = _recover_images(product, record.get("assets", {}).get("images", []))
        if not images:
            raise RuntimeError("NO_IMAGE_FOR_PDF: Prepare again or upload one genuine product image.")

        STATE.push("SAVE_REVIEW", "Approved review values saved. Building the product PDF.", 25)
        pdf_path = folder / (safe_name(listing_name) + "_brochure.pdf")
        built_pdf = Path(build_product_pdf(
            approved,
            images[0],
            pdf_path,
            company_details=company_details,
            source_url=record.get("research", {}).get("source_url", ""),
        ))
        if not built_pdf.exists() or built_pdf.stat().st_size < 1000:
            raise RuntimeError("PDF_BUILD_OUTPUT_INVALID")

        image_web_urls, pdf_web_url = _web_paths(product, images, built_pdf)
        record["assets"].update({
            "images": images,
            "image_web_urls": image_web_urls,
            "pdf_path": str(built_pdf.resolve()),
            "pdf_web_url": pdf_web_url,
        })
        record["status"] = "READY_FOR_INDIAMART"
        record["timestamps"]["approved_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(_record_path(product), record)
        clear_failure(product)
        create_backup(product, "pdf_ready")
        _write_json(folder / "content.json", approved)
        STATE.result = record
        STATE.push("PDF_READY", "Approved PDF is ready. You can now continue to IndiaMART Seller.", 100)
        return record
    except Exception as exc:
        STATE.error = str(exc)
        STATE.push("ERROR", str(exc), 100)
        try:
            mark_failure(product, "BUILD_PDF", STATE.stage, str(exc))
            (folder / "pdf_build_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
    finally:
        STATE.running = False


def check_indiamart_connection(try_chrome_profile=True):
    """
    Fast startup connection flow used by the first-screen login popup.

    Priority:
      1. Reuse the automation's previously saved IndiaMART storage state.
      2. Reuse the user's existing Windows Chrome IndiaMART session through
         a local snapshot bridge (normal Chrome remains open and untouched).
      3. Return connected=False without opening a new sign-in page.
    """
    if STATE.running or STATE.login_busy:
        return {
            "connected": bool(STATE.login_connected),
            "busy": True,
            "source": STATE.login_source,
            "message": "Another browser/process is already active.",
        }

    STATE.login_busy = True
    STATE.login_message = "Checking saved IndiaMART login..."
    browser = None
    try:
        with INDIAMART_LOCK:
            browser = IndiaMartBrowser(event=_browser_event)

            if browser.state_path.exists() and browser.state_path.stat().st_size > 10:
                try:
                    browser.start(True)
                    if browser.check_logged_in_once(timeout_seconds=18):
                        STATE.login_connected = True
                        STATE.login_source = "saved_session"
                        STATE.login_message = "IndiaMART connected automatically."
                        return {
                            "connected": True,
                            "busy": False,
                            "source": STATE.login_source,
                            "message": STATE.login_message,
                        }
                except Exception:
                    pass
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = None

            if try_chrome_profile:
                bridge = IndiaMartBrowser(event=_browser_event)
                ok, message = bridge.import_system_chrome_session(timeout_seconds=22)
                if ok:
                    # Verify the newly imported state using a clean automation context.
                    verify = IndiaMartBrowser(event=_browser_event)
                    try:
                        verify.start(True)
                        verified = verify.check_logged_in_once(timeout_seconds=18)
                    finally:
                        verify.close()
                    if verified:
                        STATE.login_connected = True
                        STATE.login_source = "chrome_profile"
                        STATE.login_message = "IndiaMART connected from your Chrome login."
                        return {
                            "connected": True,
                            "busy": False,
                            "source": STATE.login_source,
                            "message": STATE.login_message,
                        }
                STATE.login_message = message or "Automatic Chrome connection was not available."

            STATE.login_connected = False
            STATE.login_source = ""
            if not STATE.login_message:
                STATE.login_message = "Existing Chrome account was not connected. Open IndiaMART in normal Chrome, confirm the account is logged in, then press Connect Existing Chrome Account."
            return {
                "connected": False,
                "busy": False,
                "source": "",
                "message": STATE.login_message,
            }
    except Exception as exc:
        STATE.login_connected = False
        STATE.login_source = ""
        STATE.login_message = str(exc)
        return {
            "connected": False,
            "busy": False,
            "source": "",
            "message": str(exc),
        }
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        STATE.login_busy = False


def setup_indiamart_login():
    if STATE.running or STATE.login_busy:
        return
    STATE.login_busy = True
    STATE.login_connected = False
    STATE.login_message = "Opening IndiaMART login..."
    browser = None
    try:
        STATE.push("INDIAMART_LOGIN", "Opening IndiaMART seller login. Complete mobile/OTP in the browser.", STATE.progress)
        with INDIAMART_LOCK:
            browser = IndiaMartBrowser(event=_browser_event)
            browser.start(False)
            browser.ensure_logged_in(timeout_seconds=600)
            browser.save_session()
            STATE.login_connected = True
            STATE.login_source = "manual_login"
            STATE.login_message = "IndiaMART connected. Future starts will reconnect automatically."
            STATE.push("INDIAMART_LOGIN", "IndiaMART login completed and the local session was saved.", STATE.progress)
    except Exception as exc:
        STATE.login_connected = False
        STATE.login_source = ""
        STATE.login_message = "IndiaMART login failed: " + str(exc)
        STATE.error = str(exc)
        STATE.push("INDIAMART_LOGIN", "IndiaMART login setup failed: " + str(exc), STATE.progress)
    finally:
        if browser:
            browser.close()
        STATE.login_busy = False


def _log_sheet_result(record):
    source = record.get("source", {}) or {}
    if not source.get("source_row"):
        return
    try:
        bridge = SheetBridge()
        result = bridge.log({
            "source_row": source.get("source_row"),
            "sheet_name": source.get("sheet_name", ""),
            "product_name": record.get("approved", {}).get("listing_name") or source.get("product_name"),
            "model": record.get("approved", {}).get("model", ""),
            "status": record.get("indiamart", {}).get("seller_status", ""),
            "decision": record.get("indiamart", {}).get("decision", ""),
            "url": record.get("indiamart", {}).get("url", ""),
            "message": record.get("indiamart", {}).get("seller_message", ""),
        })
        if isinstance(result, dict) and result.get("local_only"):
            STATE.push("SHEET_LOG", "Sheet row completion saved locally; the next row will load automatically.", 100)
        else:
            STATE.push("SHEET_LOG", "Google Sheet row updated.", 100)
    except Exception as exc:
        STATE.push("SHEET_LOG_WARNING", "Could not update Google Sheet: " + str(exc), 100)


def continue_to_indiamart(product_name):
    """Phase 2: approved record -> seller catalogue check -> fill -> review/verified publish."""
    if STATE.running:
        return
    product = clean_text(product_name)
    record = _read_record(product)
    create_backup(product, "before_seller")
    STATE.begin("INDIAMART_SELLER", product, preserve_result=record)
    folder = _product_folder(product)
    browser = None

    try:
        if record.get("status") != "READY_FOR_INDIAMART":
            raise RuntimeError("PREPARED_PDF_REQUIRED: Approve the review and build the PDF first.")
        approved = dict(record.get("approved", {}) or {})
        model = clean_text(approved.get("model"))
        images = _recover_images(product, record.get("assets", {}).get("images", []))
        pdf_path = clean_text(record.get("assets", {}).get("pdf_path"))
        if not images:
            raise RuntimeError("PREPARED_IMAGE_REQUIRED")
        if not pdf_path or not Path(pdf_path).exists():
            raise RuntimeError("PREPARED_PDF_REQUIRED")

        STATE.push("SELLER_LOGIN", "Verifying your existing IndiaMART account before opening the seller page.", 10)
        with INDIAMART_LOCK:
            # Never open a visible OTP/sign-in page from the seller workflow.
            # First verify the saved session headlessly. If needed, refresh it
            # from the user's already-logged-in normal Chrome profile snapshot.
            probe = IndiaMartBrowser(event=_browser_event)
            try:
                connected = False
                if probe.state_path.exists() and probe.state_path.stat().st_size > 10:
                    try:
                        probe.start(True)
                        connected = probe.check_logged_in_once(timeout_seconds=18)
                    except Exception:
                        connected = False
                    finally:
                        probe.close()

                if not connected:
                    bridge = IndiaMartBrowser(event=_browser_event)
                    ok, message = bridge.import_system_chrome_session(timeout_seconds=25)
                    if not ok:
                        STATE.login_connected = False
                        STATE.login_message = message
                        raise RuntimeError(
                            "INDIAMART_EXISTING_ACCOUNT_NOT_CONNECTED: "
                            + str(message)
                        )

                    verify = IndiaMartBrowser(event=_browser_event)
                    try:
                        verify.start(True)
                        connected = verify.check_logged_in_once(timeout_seconds=18)
                    finally:
                        verify.close()

                if not connected:
                    raise RuntimeError(
                        "INDIAMART_EXISTING_ACCOUNT_NOT_CONNECTED: "
                        "Open the seller account in normal Chrome and press Connect Existing Chrome Account first."
                    )
            finally:
                try:
                    probe.close()
                except Exception:
                    pass

            # The visible browser is opened only after authentication has already
            # been verified, so it should land directly on Manage Products rather
            # than showing India's sign-in/registration page.
            browser = IndiaMartBrowser(event=_browser_event)
            browser.start(False)
            if not browser.check_logged_in_once(timeout_seconds=20):
                raise RuntimeError(
                    "INDIAMART_SESSION_LOST: The saved account session expired. "
                    "Reconnect the existing Chrome account from the first popup."
                )
            STATE.login_connected = True
            STATE.login_source = STATE.login_source or "existing_chrome_account"
            STATE.login_message = "Existing IndiaMART account connected."

            search_query = " ".join(x for x in (approved.get("listing_name") or product, model) if x).strip()
            STATE.push("ACCOUNT_SEARCH", "Checking your own IndiaMART catalogue for an existing product/variant.", 25)
            match = browser.search_product(search_query)
            browser.screenshot(folder / "01_search.png")
            decision = "ADD_VARIANT" if match.get("matched") else "ADD_NEW_PRODUCT"
            duplicate_guard = {
                "existing_match_found": bool(match.get("matched")),
                "matched_name": match.get("matched_name", ""),
                "match_score": match.get("score", 0),
                "action": decision,
                "protected": True,
            }
            if match.get("matched"):
                STATE.push(
                    "DUPLICATE_GUARD",
                    "Existing catalogue match found. New Product is blocked; Add Variant mode will be used.",
                    38,
                )
            else:
                STATE.push("DUPLICATE_GUARD", "No reliable duplicate found. New Product mode is allowed.", 38)
            STATE.push("DECISION", f"{decision}; match score {match.get('score', 0)}", 40)

            row = dict(record.get("source", {}).get("sheet_row", {}) or {})
            row["Product Name"] = approved.get("listing_name") or product
            if model:
                row["Model"] = model
            price = extract_selling_price(row)
            STATE.push(
                "PRICE",
                f"Selling price loaded from source row: {price}" if price else "No selling price found; IndiaMART price will remain blank.",
                52,
            )

            browser.open_add_form(bool(match.get("matched")))
            STATE.push("INDIAMART_FORM", "Filling approved content, specifications, five images and PDF.", 65)
            browser.fill_listing(approved, row, images, pdf_path)
            browser.screenshot(folder / "02_filled_form.png")
            STATE.push("FINALIZE", "Finishing the IndiaMART review/publish step.", 88)
            final = browser.finalize()
            browser.screenshot(folder / "03_final.png")

            record["indiamart"] = {
                "decision": decision,
                "match": match,
                "duplicate_guard": duplicate_guard,
                "seller_status": final.get("status", ""),
                "seller_message": final.get("message", ""),
                "url": browser.page.url if browser.page else "",
            }
            record["timestamps"]["seller_at"] = datetime.now().isoformat(timespec="seconds")
            if final.get("status") == "review_required":
                record["status"] = "SELLER_REVIEW_REQUIRED"
            elif final.get("status"):
                record["status"] = "SELLER_COMPLETED"
            record.pop("last_failure", None)
            _write_json(_record_path(product), record)
            create_backup(product, "seller_result")
            _write_json(folder / "run_result.json", record)
            STATE.result = record

            if final.get("status") == "review_required":
                STATE.push("REVIEW_REQUIRED", "Seller form is filled. Review manually; close the seller browser when finished.", 100)
                browser.wait_until_operator_closes_review(max_seconds=3600)
                STATE.push("DONE", "Seller review browser closed.", 100)
            else:
                STATE.push("DONE", final.get("message", "IndiaMART step completed."), 100)

        _log_sheet_result(record)
    except Exception as exc:
        STATE.error = str(exc)
        STATE.push("ERROR", str(exc), 100)
        try:
            mark_failure(product, "INDIAMART_SELLER", STATE.stage, str(exc))
            (folder / "indiamart_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            if browser and browser.page and not browser.page.is_closed():
                browser.screenshot(folder / "ERROR.png")
                try:
                    controls = browser._visible_control_debug()
                    (folder / "selector_debug.txt").write_text("\n".join(controls), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            pass
    finally:
        if browser:
            browser.close()
        STATE.running = False
