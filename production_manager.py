import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"


def safe_name(text):
    text = str(text or "")
    value = "_".join(
        "".join(c if (c.isalnum() or c in " -_.") else "_" for c in text).split()
    )[:90]
    return value or "product"


def product_folder(product):
    return OUTPUTS / safe_name(product)


def record_path(product):
    return product_folder(product) / "product_record.json"


def read_record(product):
    path = record_path(product)
    if not path.exists():
        raise RuntimeError("Prepared product record not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def write_record(product, record):
    path = record_path(product)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def create_backup(product, label="checkpoint"):
    """Create a small recovery snapshot without copying large generated images/PDFs."""
    path = record_path(product)
    if not path.exists():
        return ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = product_folder(product) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{ts}_{safe_name(label)}.json"
    shutil.copy2(path, target)
    return str(target.resolve())


def list_backups(product, limit=20):
    folder = product_folder(product) / "backups"
    if not folder.exists():
        return []
    items = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        items.append({
            "name": path.name,
            "path": str(path.resolve()),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return items


def restore_backup(product, backup_name=""):
    backups = list_backups(product, limit=100)
    if not backups:
        raise RuntimeError("No recovery backup is available for this product.")
    chosen = None
    if backup_name:
        for item in backups:
            if item["name"] == backup_name:
                chosen = item
                break
    if chosen is None:
        chosen = backups[0]
    current = record_path(product)
    if current.exists():
        create_backup(product, "before_restore")
    shutil.copy2(chosen["path"], current)
    return read_record(product)


def mark_failure(product, phase, stage, message):
    path = record_path(product)
    if not path.exists():
        return
    record = read_record(product)
    record["last_failure"] = {
        "phase": str(phase or ""),
        "stage": str(stage or ""),
        "message": str(message or ""),
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    write_record(product, record)


def clear_failure(product):
    path = record_path(product)
    if not path.exists():
        return
    record = read_record(product)
    record.pop("last_failure", None)
    write_record(product, record)


def list_history(limit=100):
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    rows = []
    for folder in OUTPUTS.iterdir():
        if not folder.is_dir():
            continue
        path = folder / "product_record.json"
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source = record.get("source", {}) or {}
        approved = record.get("approved", {}) or {}
        indiamart = record.get("indiamart", {}) or {}
        ts = record.get("timestamps", {}) or {}
        failure = record.get("last_failure", {}) or {}
        rows.append({
            "record_product_name": record.get("record_product_name") or source.get("product_name") or folder.name,
            "listing_name": approved.get("listing_name") or source.get("product_name") or folder.name,
            "model": approved.get("model") or source.get("model") or "",
            "status": record.get("status", ""),
            "sheet_name": source.get("sheet_name", ""),
            "source_row": source.get("source_row", ""),
            "seller_status": indiamart.get("seller_status", ""),
            "seller_url": indiamart.get("url", ""),
            "decision": indiamart.get("decision", ""),
            "last_failure": failure,
            "completed_at": ts.get("manual_completed_at") or (ts.get("seller_at") if record.get("status") == "SELLER_COMPLETED" else ""),
            "updated_at": ts.get("manual_completed_at") or ts.get("seller_at") or ts.get("approved_at") or ts.get("prepared_at") or datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        })
    rows.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return rows[:max(1, min(int(limit or 100), 1000))]


def completion_summary(product):
    record = read_record(product)
    source = record.get("source", {}) or {}
    approved = record.get("approved", {}) or {}
    assets = record.get("assets", {}) or {}
    indiamart = record.get("indiamart", {}) or {}
    return {
        "product": approved.get("listing_name") or record.get("record_product_name"),
        "model": approved.get("model", ""),
        "sheet_name": source.get("sheet_name", ""),
        "source_row": source.get("source_row", ""),
        "images": len(assets.get("images") or []),
        "pdf_ready": bool(assets.get("pdf_path")),
        "decision": indiamart.get("decision", ""),
        "seller_status": indiamart.get("seller_status", ""),
        "seller_message": indiamart.get("seller_message", ""),
        "seller_url": indiamart.get("url", ""),
        "completed_at": (record.get("timestamps", {}) or {}).get("seller_at", ""),
        "last_failure": record.get("last_failure", {}),
    }
