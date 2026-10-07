import importlib
import os
import sys
from pathlib import Path

import requests
from config_manager import load_configuration, local_config_path

ROOT = Path(__file__).resolve().parent
load_configuration()


def mark(name, ok, detail=""):
    status = "OK" if ok else "CHECK"
    print(f"{name:<28} {status:<7} {detail}")
    return bool(ok)


def main():
    print("=" * 68)
    print(" NUNES INDIAMART AUTOMATION V2.7 - SYSTEM CHECK")
    print("=" * 68)
    core_ok = True

    core_ok &= mark("Python", sys.version_info >= (3, 10), sys.version.split()[0])
    mark("Private config", True, str(local_config_path()))

    modules = {
        "Flask": "flask",
        "Waitress": "waitress",
        "Playwright": "playwright",
        "Requests": "requests",
        "Pillow": "PIL",
        "ReportLab": "reportlab",
        "PyPDF": "pypdf",
        "python-dotenv": "dotenv",
    }
    for label, module in modules.items():
        try:
            importlib.import_module(module)
            core_ok &= mark(label, True)
        except Exception as exc:
            core_ok &= mark(label, False, str(exc))

    template = Path(os.getenv("PDF_TEMPLATE_PATH", "assets/template.pdf"))
    if not template.is_absolute():
        template = ROOT / template
    core_ok &= mark("PDF template", template.exists(), str(template))

    outputs = ROOT / "outputs"
    try:
        outputs.mkdir(exist_ok=True)
        probe = outputs / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        core_ok &= mark("Output write permission", True, str(outputs))
    except Exception as exc:
        core_ok &= mark("Output write permission", False, str(exc))

    # Browser launch is a strong local check but does not visit IndiaMART.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        core_ok &= mark("Playwright Chromium", True, "headless launch succeeded")
    except Exception as exc:
        core_ok &= mark("Playwright Chromium", False, str(exc))

    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3:1.7b")
    try:
        r = requests.get(ollama_url + "/api/tags", timeout=3)
        r.raise_for_status()
        names = [x.get("name", "") for x in r.json().get("models", [])]
        mark("Ollama", True, ollama_url)
        present = any(x == model or x.startswith(model + ":") for x in names)
        mark("Qwen model", present, model)
    except Exception as exc:
        mark("Ollama", False, str(exc))
        mark("Qwen model", False, model)

    sheet_configured = bool(os.getenv("APPS_SCRIPT_URL", "").strip() and os.getenv("APPS_SCRIPT_TOKEN", "").strip())
    if sheet_configured:
        try:
            from sheets_client import SheetBridge
            SheetBridge().ping()
            mark("Google Sheet bridge", True, "ping succeeded")
        except Exception as exc:
            mark("Google Sheet bridge", False, str(exc))
    else:
        mark("Google Sheet bridge", False, "optional - not configured")

    runtime = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NUNES_INDIAMART_AUTOMATION" / "runtime"
    state_file = Path(os.getenv("INDIAMART_STORAGE_STATE", str(runtime / "indiamart_storage_state.json")))
    mark("IndiaMART saved session", state_file.exists(), str(state_file))

    print("=" * 68)
    if core_ok:
        print("CORE SYSTEM READY")
        print("Optional CHECK items can be configured later (Ollama, Sheet, login).")
        return 0
    print("CORE SYSTEM NEEDS ATTENTION")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
