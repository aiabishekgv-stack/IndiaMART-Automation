import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

from config_manager import load_configuration, local_config_path, save_local_setting

# Load per-PC configuration BEFORE importing workflow/browser modules.
load_configuration()
from waitress import serve

from automation import (
    INPUT_IMAGES,
    OUTPUTS,
    STATE,
    continue_to_indiamart,
    check_indiamart_connection,
    safe_name,
    save_review_and_build_pdf,
    setup_indiamart_login,
    prepare_product,
)
from sheets_client import SheetBridge
from image_studio import (
    auto_diversify,
    get_state as get_image_studio_state,
    regenerate_slot as regenerate_image_slot,
    regenerate_unlocked as regenerate_unlocked_images,
    set_lock as set_image_lock,
    reorder_images,
    set_primary as set_primary_image,
)
from production_manager import (
    create_backup, list_backups, restore_backup, list_history, read_record as read_product_record,
    write_record as write_product_record, completion_summary,
)



ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024


def _refresh_state_result_from_disk(product):
    try:
        path = OUTPUTS / safe_name(product) / "product_record.json"
        if path.exists():
            STATE.result = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass



def _common_value(row, names):
    if not isinstance(row, dict):
        return ""
    normalized = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = normalized.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _save_upload(product, file_obj):
    if not file_obj or not file_obj.filename:
        return None
    suffix = Path(file_obj.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        raise RuntimeError("Product image must be JPG, JPEG, PNG, WEBP or BMP.")
    folder = INPUT_IMAGES / safe_name(product)
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + suffix
    target = folder / name
    file_obj.save(target)
    return str(target.resolve())


def _sheet_config():
    apps_url = os.getenv("APPS_SCRIPT_URL", "").strip()
    token = os.getenv("APPS_SCRIPT_TOKEN", "").strip()
    direct_url = os.getenv(
        "GOOGLE_SHEET_URL",
        "https://docs.google.com/spreadsheets/d/1iGpbKroGTez2HNq0ps7shzn0OMvASn9-67UBPdogiJw/edit?usp=sharing",
    ).strip()
    direct_id = ""
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", direct_url)
    if match:
        direct_id = match.group(1)
    preferred_mode = os.getenv("GOOGLE_SHEET_MODE", "direct").strip().lower()
    if preferred_mode == "apps_script" and apps_url and token:
        mode = "apps_script"
    elif direct_id:
        mode = "direct"
    elif apps_url and token:
        mode = "apps_script"
    else:
        mode = ""
    return {
        "configured": bool(mode),
        "mode": mode,
        "url": apps_url if mode == "apps_script" else direct_url,
        "token": token,
        "sheet_id": direct_id,
    }


def _safe_sheet_url_hint(url):
    if not url:
        return ""
    if len(url) <= 48:
        return url
    return url[:30] + "…" + url[-14:]


def _validate_apps_script_url(url):
    url = str(url or "").strip()
    if not url:
        raise RuntimeError("Paste your Google Apps Script Web App /exec URL.")
    if not url.startswith("https://script.google.com/macros/s/"):
        raise RuntimeError(
            "This does not look like a Google Apps Script Web App URL. "
            "Use the deployed URL beginning with https://script.google.com/macros/s/."
        )
    if "/exec" not in url:
        raise RuntimeError("Use the deployed Apps Script Web App URL ending in /exec.")
    return url


def _is_local_admin_request():
    """Configuration writes are allowed only from the server PC itself."""
    remote = str(request.remote_addr or "").strip().lower()
    return remote in {"127.0.0.1", "::1", "localhost"}


def _local_admin_required():
    if _is_local_admin_request():
        return None
    return jsonify({
        "ok": False,
        "code": "LOCAL_ADMIN_REQUIRED",
        "message": (
            "This setting can only be changed from the server PC. "
            "Open http://127.0.0.1:5077 on the server and configure Google Sheet there."
        ),
    }), 403


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify(STATE.snapshot())


@app.post("/api/reset")
def api_reset():
    denied = _local_admin_required()
    if denied:
        return denied
    if STATE.running or STATE.login_busy:
        return jsonify({"ok": False, "message": "A process is still running."}), 409
    STATE.reset()
    return jsonify({"ok": True})


@app.get("/api/health")
def api_health():
    checks = {}

    template = Path(os.getenv("PDF_TEMPLATE_PATH", "assets/template.pdf"))
    if not template.is_absolute():
        template = ROOT / template
    checks["template_pdf"] = {"ok": template.exists(), "path": str(template)}

    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    ollama_model = os.getenv("OLLAMA_MODEL", "qwen3:1.7b")
    try:
        response = requests.get(ollama_url + "/api/tags", timeout=2.5)
        response.raise_for_status()
        models = [x.get("name", "") for x in response.json().get("models", [])]
        checks["ollama"] = {
            "ok": True,
            "model": ollama_model,
            "model_present": any(x == ollama_model or x.startswith(ollama_model + ":") for x in models),
        }
    except Exception as exc:
        checks["ollama"] = {"ok": False, "model": ollama_model, "error": str(exc)}

    sheet_cfg = _sheet_config()
    checks["google_sheet"] = {
        "ok": bool(sheet_cfg["configured"]),
        "configured": bool(sheet_cfg["configured"]),
        "mode": sheet_cfg.get("mode", ""),
        "sheet_id": sheet_cfg.get("sheet_id", ""),
    }

    localapp = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NUNES_INDIAMART_AUTOMATION" / "runtime"
    state_file = Path(os.getenv("INDIAMART_STORAGE_STATE", str(localapp / "indiamart_storage_state.json")))
    checks["indiamart_session"] = {"ok": state_file.exists(), "path": str(state_file)}

    essential = checks["template_pdf"]["ok"]
    return jsonify({"ok": essential, "checks": checks})


@app.get("/api/sheet/config")
def api_sheet_config():
    cfg = _sheet_config()
    can_configure = _is_local_admin_request()
    payload = {
        "ok": True,
        "configured": cfg["configured"],
        "mode": cfg.get("mode", ""),
        "sheet_id": cfg.get("sheet_id", ""),
        "can_configure": can_configure and cfg.get("mode") != "direct",
        "url_hint": _safe_sheet_url_hint(cfg["url"]),
        "message": (
            "Google Sheet auto-connected from the saved sharing link."
            if cfg.get("mode") == "direct"
            else (
                "Google Sheet is connected through Apps Script."
                if cfg["configured"]
                else (
                    "Google Sheet is optional. Configure it on this server PC or use manual entry."
                    if can_configure
                    else "Google Sheet is not configured on the server. Manual entry is available."
                )
            )
        ),
    }
    if can_configure:
        payload["config_location"] = str(local_config_path())
    return jsonify(payload)


@app.post("/api/sheet/config")
def api_sheet_config_save():
    denied = _local_admin_required()
    if denied:
        return denied
    if STATE.running:
        return jsonify({
            "ok": False,
            "message": "Wait for the current product process to finish.",
        }), 409

    data = request.get_json(silent=True) or {}
    try:
        url = _validate_apps_script_url(data.get("url"))
        token = str(data.get("token") or "").strip()
        if len(token) < 8:
            raise RuntimeError(
                "Enter the same Apps Script token used in apps_script/Code.gs "
                "(at least 8 characters)."
            )

        # Test BEFORE saving so a typo never breaks the working manual mode.
        bridge = SheetBridge(url=url, token=token)
        bridge.ping()

        # V2.4 stores private Sheet credentials under LOCALAPPDATA, never in
        # the shared project/NAS folder.
        save_local_setting("APPS_SCRIPT_URL", url)
        save_local_setting("APPS_SCRIPT_TOKEN", token)

        return jsonify({
            "ok": True,
            "configured": True,
            "url_hint": _safe_sheet_url_hint(url),
            "message": "Google Sheet connected successfully.",
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "configured": False,
            "message": str(exc),
        }), 400




@app.get("/api/sheet/tabs")
def api_sheet_tabs():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    try:
        return jsonify(SheetBridge().list_tabs())
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.get("/api/sheet/products")
def api_sheet_products():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    try:
        sheet_name = str(request.args.get("sheet") or "").strip()
        status_filter = str(request.args.get("status") or "all").strip()
        search = str(request.args.get("search") or "").strip()
        try:
            requested_limit = int(request.args.get("limit") or (300 if search else 1200))
        except Exception:
            requested_limit = 300 if search else 1200
        requested_limit = max(25, min(requested_limit, 10000))
        data = SheetBridge().list_products(
            sheet_name=sheet_name,
            status_filter=status_filter,
            search=search,
            limit=requested_limit,
        )
        return jsonify(data)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.get("/api/sheet/row")
def api_sheet_row():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    try:
        sheet_name = str(request.args.get("sheet") or "").strip()
        source_row = str(request.args.get("row") or "").strip()
        return jsonify(SheetBridge().get_row(source_row, sheet_name=sheet_name))
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.get("/api/sheet/next")
def api_sheet_next():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409

    cfg = _sheet_config()
    if not cfg["configured"]:
        return jsonify({
            "ok": False,
            "code": "GOOGLE_SHEET_NOT_CONFIGURED",
            "optional": True,
            "can_configure": _is_local_admin_request(),
            "message": (
                "Google Sheet is not connected yet. "
                "Connect it once, or simply enter the product manually."
            ),
        }), 428

    try:
        data = SheetBridge().next_product(sheet_name=str(request.args.get("sheet") or "").strip())
        if data.get("empty"):
            return jsonify({"ok": True, "empty": True, "message": data.get("message", "No unprocessed rows found.")})

        row = data.get("row") if isinstance(data.get("row"), dict) else data.get("data", {})
        if not isinstance(row, dict):
            row = {}
        product = _common_value(row, ["Product Name", "Product", "Keyword", "Search Keyword"])
        model = _common_value(row, ["Model", "Model Number", "Model No", "Model Name/Number"])
        source_row = str(data.get("source_row") or data.get("row_number") or "")
        return jsonify({
            "ok": True,
            "empty": False,
            "mode": data.get("mode", _sheet_config().get("mode", "")),
            "source_row": source_row,
            "sheet_name": data.get("sheet_name", str(request.args.get("sheet") or "").strip()),
            "product_name": product,
            "model": model,
            "row": row,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/prepare-product")
def api_prepare_product():
    if STATE.running:
        return jsonify({"ok": False, "message": "Another process is already running."}), 409
    try:
        product = str(request.form.get("product_name") or "").strip()
        if not product:
            return jsonify({"ok": False, "message": "Product Name is required."}), 400
        model = str(request.form.get("model") or "").strip()
        source_row = str(request.form.get("source_row") or "").strip()
        sheet_name = str(request.form.get("sheet_name") or "").strip()
        sheet_row_json = str(request.form.get("sheet_row_json") or "{}").strip()
        try:
            sheet_row = json.loads(sheet_row_json) if sheet_row_json else {}
        except Exception:
            sheet_row = {}
        if not isinstance(sheet_row, dict):
            sheet_row = {}

        uploaded = _save_upload(product, request.files.get("product_image"))
        payload = {
            "product_name": product,
            "model": model,
            "source_row": source_row,
            "sheet_name": sheet_name,
            "sheet_row": sheet_row,
        }
        thread = threading.Thread(target=prepare_product, args=(payload, uploaded), daemon=True)
        thread.start()
        return jsonify({"ok": True, "message": "Public research started."})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/save-review-build-pdf")
def api_save_review_build_pdf():
    if STATE.running:
        return jsonify({"ok": False, "message": "Another process is already running."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("record_product_name") or data.get("product_name") or "").strip()
    review = data.get("review") if isinstance(data.get("review"), dict) else data
    try:
        result = save_review_and_build_pdf(product, review)
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.get("/api/indiamart-connection")
def api_indiamart_connection():
    return jsonify({
        "ok": True,
        "connected": bool(STATE.login_connected),
        "busy": bool(STATE.login_busy),
        "source": STATE.login_source,
        "message": STATE.login_message,
    })


@app.post("/api/indiamart-autoconnect")
def api_indiamart_autoconnect():
    if STATE.running or STATE.login_busy:
        return jsonify({
            "ok": False,
            "connected": bool(STATE.login_connected),
            "busy": True,
            "message": "Another browser/process is already active.",
        }), 409
    result = check_indiamart_connection(try_chrome_profile=True)
    return jsonify({"ok": True, **result})


@app.post("/api/indiamart-login")
def api_indiamart_login():
    """Start the official IndiaMART login as a fallback from the first popup.

    The web-app popup stays open. A controlled Chrome window is opened only
    when automatic saved-session / existing-Chrome connection is unavailable.
    The user completes IndiaMART mobile/OTP directly on IndiaMART; the app never
    collects or stores the OTP.
    """
    if STATE.running or STATE.login_busy:
        return jsonify({
            "ok": False,
            "connected": bool(STATE.login_connected),
            "busy": True,
            "message": "Another browser/process is already active.",
        }), 409

    threading.Thread(target=setup_indiamart_login, daemon=True).start()
    return jsonify({
        "ok": True,
        "connected": False,
        "busy": True,
        "message": (
            "Official IndiaMART login is opening in Chrome. Complete mobile/OTP "
            "there. This first popup will detect the connection automatically."
        ),
    })


@app.post("/api/continue-indiamart")
def api_continue_indiamart():
    if STATE.running:
        return jsonify({"ok": False, "message": "Another process is already running."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("record_product_name") or data.get("product_name") or "").strip()
    if not product:
        return jsonify({"ok": False, "message": "Prepare a product first."}), 400
    threading.Thread(target=continue_to_indiamart, args=(product,), daemon=True).start()
    return jsonify({"ok": True, "message": "IndiaMART seller phase started."})


@app.get("/api/image-studio")
def api_image_studio():
    product = str(request.args.get("product") or "").strip()
    if not product:
        return jsonify({"ok": False, "message": "Product is required."}), 400
    try:
        return jsonify({"ok": True, "state": get_image_studio_state(product)})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/image-lock")
def api_image_lock():
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        state = set_image_lock(product, int(data.get("slot") or 0), bool(data.get("locked")))
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/image-regenerate")
def api_image_regenerate():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        state = regenerate_image_slot(product, int(data.get("slot") or 0))
        _refresh_state_result_from_disk(product)
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/image-regenerate-unlocked")
def api_image_regenerate_unlocked():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        state = regenerate_unlocked_images(product)
        _refresh_state_result_from_disk(product)
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/image-auto-diversify")
def api_image_auto_diversify():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        state = auto_diversify(product)
        _refresh_state_result_from_disk(product)
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/image-reorder")
def api_image_reorder():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        state = reorder_images(product, data.get("order") or [])
        _refresh_state_result_from_disk(product)
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/image-primary")
def api_image_primary():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        state = set_primary_image(product, int(data.get("slot") or 0))
        _refresh_state_result_from_disk(product)
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.get("/api/history")
def api_history():
    try:
        limit = int(request.args.get("limit") or 100)
    except Exception:
        limit = 100
    return jsonify({"ok": True, "items": list_history(limit)})


@app.post("/api/history/open")
def api_history_open():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        record = read_product_record(product)
        STATE.result = record
        STATE.product = product
        return jsonify({"ok": True, "result": record})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 404


@app.get("/api/backups")
def api_backups():
    product = str(request.args.get("product") or "").strip()
    return jsonify({"ok": True, "backups": list_backups(product) if product else []})


@app.post("/api/restore-backup")
def api_restore_backup():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the current process to finish."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        record = restore_backup(product, str(data.get("backup_name") or ""))
        STATE.result = record
        return jsonify({"ok": True, "result": record, "message": "Latest product checkpoint restored."})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


def _retry_pdf_worker(product, record):
    approved = dict(record.get("approved", {}) or {})
    review = {
        "listing_name": approved.get("listing_name", ""),
        "model": approved.get("model", ""),
        "description": approved.get("description", ""),
        "key_features": approved.get("key_features", []),
        "applications": approved.get("applications", []),
        "specifications": approved.get("specifications", []),
        "company_name": (approved.get("company_details", {}) or {}).get("company_name", ""),
        "company_location": (approved.get("company_details", {}) or {}).get("location", ""),
    }
    try:
        save_review_and_build_pdf(product, review)
    except Exception:
        pass


@app.post("/api/retry-failed-step")
def api_retry_failed_step():
    if STATE.running:
        return jsonify({"ok": False, "message": "Another process is already running."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        record = read_product_record(product)
        failure = record.get("last_failure", {}) or {}
        phase = str(data.get("phase") or failure.get("phase") or "").strip().upper()
        if not phase:
            raise RuntimeError("No failed step is recorded for this product.")
        if phase == "PUBLIC_RESEARCH":
            source = record.get("source", {}) or {}
            payload = {
                "product_name": source.get("product_name") or product,
                "model": source.get("model", ""),
                "source_row": source.get("source_row", ""),
                "sheet_name": source.get("sheet_name", ""),
                "sheet_row": source.get("sheet_row", {}) or {},
            }
            threading.Thread(target=prepare_product, args=(payload, None), daemon=True).start()
        elif phase == "BUILD_PDF":
            threading.Thread(target=_retry_pdf_worker, args=(product, record), daemon=True).start()
        elif phase == "INDIAMART_SELLER":
            threading.Thread(target=continue_to_indiamart, args=(product,), daemon=True).start()
        else:
            raise RuntimeError("This failed step cannot be retried automatically: " + phase)
        return jsonify({"ok": True, "phase": phase, "message": "Retrying only the failed step."})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.get("/api/selector-diagnostics")
def api_selector_diagnostics():
    product = str(request.args.get("product") or "").strip()
    if not product:
        return jsonify({"ok": False, "message": "Product is required."}), 400
    folder = OUTPUTS / safe_name(product)
    debug_file = folder / "selector_debug.txt"
    error_file = folder / "indiamart_error.txt"
    return jsonify({
        "ok": True,
        "available": debug_file.exists(),
        "controls": debug_file.read_text(encoding="utf-8", errors="replace").splitlines()[:120] if debug_file.exists() else [],
        "error": error_file.read_text(encoding="utf-8", errors="replace")[-5000:] if error_file.exists() else "",
        "screenshot_url": f"/outputs/{safe_name(product)}/ERROR.png" if (folder / "ERROR.png").exists() else "",
    })


@app.get("/api/completion")
def api_completion():
    product = str(request.args.get("product") or "").strip()
    try:
        return jsonify({"ok": True, "summary": completion_summary(product)})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 404


@app.post("/api/mark-manual-completed")
def api_mark_manual_completed():
    if STATE.running:
        return jsonify({"ok": False, "message": "Wait for the seller browser process to finish first."}), 409
    data = request.get_json(silent=True) or {}
    product = str(data.get("product") or "").strip()
    try:
        record = read_product_record(product)
        create_backup(product, "before_manual_complete")
        ind = record.setdefault("indiamart", {})
        ind["seller_status"] = "manual_completed"
        ind["seller_message"] = "Operator confirmed the IndiaMART review/submission was completed."
        record["status"] = "SELLER_COMPLETED"
        record.setdefault("timestamps", {})["manual_completed_at"] = datetime.now().isoformat(timespec="seconds")
        record.pop("last_failure", None)
        write_product_record(product, record)
        source = record.get("source", {}) or {}
        if source.get("source_row"):
            try:
                SheetBridge().log({
                    "source_row": source.get("source_row"),
                    "sheet_name": source.get("sheet_name", ""),
                    "product_name": (record.get("approved", {}) or {}).get("listing_name") or product,
                    "model": (record.get("approved", {}) or {}).get("model", ""),
                    "status": "manual_completed",
                    "decision": ind.get("decision", ""),
                    "url": ind.get("url", ""),
                    "message": ind.get("seller_message", ""),
                })
            except Exception:
                pass
        STATE.result = record
        return jsonify({"ok": True, "result": record, "summary": completion_summary(product)})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.get("/outputs/<path:filename>")
def output_file(filename):
    return send_from_directory(str(OUTPUTS), filename)


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("APP_PORT", "5077"))
    print(f"NUNES IndiaMART Automation V4.1: http://{host}:{port}")
    serve(app, host=host, port=port, threads=8)
