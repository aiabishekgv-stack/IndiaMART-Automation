import json
from datetime import datetime
from pathlib import Path

from image_acquirer import ImageAcquirer
from production_manager import create_backup

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"


def safe_name(text):
    text = str(text or "")
    value = "_".join(
        "".join(c if (c.isalnum() or c in " -_.") else "_" for c in text).split()
    )[:90]
    return value or "product"


def _folder(product):
    return OUTPUTS / safe_name(product)


def _record_path(product):
    return _folder(product) / "product_record.json"


def _read_record(product):
    path = _record_path(product)
    if not path.exists():
        raise RuntimeError("Prepared product record not found. Prepare the product first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_record(product, record):
    path = _record_path(product)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _image_paths(product, record):
    images = []
    for item in (record.get("assets", {}).get("images") or []):
        path = Path(str(item or ""))
        if path.exists():
            images.append(str(path.resolve()))
    images_dir = _folder(product) / "images"
    if len(images) < 5 and images_dir.exists():
        images = []
        for slot in range(1, 6):
            path = images_dir / f"product_{slot}.jpg"
            if path.exists():
                images.append(str(path.resolve()))
    return images


def _reference_items(product):
    images_dir = _folder(product) / "images"
    manifest = images_dir / "reference_manifest.json"
    result = []
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            data = []
        for item in data:
            saved = Path(str(item.get("saved_path") or ""))
            if not saved.exists():
                continue
            result.append({
                "index": int(item.get("index") or len(result) + 1),
                "source_type": str(item.get("source_type") or "REFERENCE"),
                "source_ref": str(item.get("source_ref") or ""),
                "web_url": f"/outputs/{safe_name(product)}/images/references/{saved.name}",
            })
    return result


def _normalize_assets(record):
    assets = record.setdefault("assets", {})
    locks = assets.get("image_locks")
    if not isinstance(locks, list) or len(locks) != 5:
        locks = [False, False, False, False, False]
    attempts = assets.get("image_regeneration_attempts")
    if not isinstance(attempts, list) or len(attempts) != 5:
        attempts = [0, 0, 0, 0, 0]
    assets["image_locks"] = [bool(x) for x in locks]
    assets["image_regeneration_attempts"] = [int(x or 0) for x in attempts]
    return assets


def _refresh_image_urls(product, record):
    assets = _normalize_assets(record)
    images = _image_paths(product, record)
    assets["images"] = images
    assets["image_web_urls"] = [
        f"/outputs/{safe_name(product)}/images/{Path(path).name}" for path in images
    ]
    return images


def _invalidate_pdf(record):
    assets = _normalize_assets(record)
    if assets.get("pdf_path") or assets.get("pdf_web_url"):
        assets["pdf_path"] = ""
        assets["pdf_web_url"] = ""
        record["status"] = "REVIEW_READY"
        record.setdefault("timestamps", {}).pop("approved_at", None)
        record.setdefault("timestamps", {})["images_changed_at"] = datetime.now().isoformat(timespec="seconds")


def get_state(product):
    record = _read_record(product)
    assets = _normalize_assets(record)
    images = _refresh_image_urls(product, record)
    analyzer = ImageAcquirer()
    similarity = analyzer.output_similarity(images)
    assets["image_similarity"] = similarity
    assets["reference_count"] = len(_reference_items(product))
    _write_record(product, record)
    return {
        "record_product_name": product,
        "image_web_urls": assets.get("image_web_urls", []),
        "locks": assets["image_locks"],
        "attempts": assets["image_regeneration_attempts"],
        "references": _reference_items(product),
        "similarity": similarity,
        "images_updated_at": record.get("timestamps", {}).get("images_updated_at", ""),
    }


def set_lock(product, slot, locked):
    record = _read_record(product)
    assets = _normalize_assets(record)
    slot = int(slot)
    if slot < 1 or slot > 5:
        raise ValueError("Image slot must be 1 to 5.")
    assets["image_locks"][slot - 1] = bool(locked)
    _write_record(product, record)
    return get_state(product)


def regenerate_slot(product, slot, ignore_lock=False):
    create_backup(product, "before_image_regenerate")
    record = _read_record(product)
    assets = _normalize_assets(record)
    slot = int(slot)
    if slot < 1 or slot > 5:
        raise ValueError("Image slot must be 1 to 5.")
    if assets["image_locks"][slot - 1] and not ignore_lock:
        raise RuntimeError(f"Image {slot} is locked. Unlock it first.")

    assets["image_regeneration_attempts"][slot - 1] += 1
    attempt = assets["image_regeneration_attempts"][slot - 1]
    ImageAcquirer().regenerate_slot(_folder(product) / "images", slot, attempt=attempt)
    _invalidate_pdf(record)
    _refresh_image_urls(product, record)
    record.setdefault("timestamps", {})["images_updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_record(product, record)
    return get_state(product)


def regenerate_unlocked(product):
    create_backup(product, "before_image_regenerate_all")
    record = _read_record(product)
    assets = _normalize_assets(record)
    changed = []
    acquirer = ImageAcquirer()
    for slot in range(1, 6):
        if assets["image_locks"][slot - 1]:
            continue
        assets["image_regeneration_attempts"][slot - 1] += 1
        attempt = assets["image_regeneration_attempts"][slot - 1]
        acquirer.regenerate_slot(_folder(product) / "images", slot, attempt=attempt)
        changed.append(slot)
    if not changed:
        raise RuntimeError("All five images are locked. Unlock at least one image.")
    _invalidate_pdf(record)
    _refresh_image_urls(product, record)
    record.setdefault("timestamps", {})["images_updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_record(product, record)
    state = get_state(product)
    state["changed_slots"] = changed
    return state


def auto_diversify(product, threshold=0.94, max_rounds=3):
    """Regenerate unlocked images that remain too similar to another image."""
    create_backup(product, "before_auto_diversify")
    record = _read_record(product)
    assets = _normalize_assets(record)
    acquirer = ImageAcquirer()
    changed = []

    for _ in range(max_rounds):
        images = _refresh_image_urls(product, record)
        similarity = acquirer.output_similarity(images)
        bad_slots = []
        for pair in similarity.get("pairs", []):
            if pair.get("similarity", 0) >= threshold:
                candidate_slot = int(pair["b"])
                if not assets["image_locks"][candidate_slot - 1]:
                    bad_slots.append(candidate_slot)
                elif not assets["image_locks"][int(pair["a"]) - 1]:
                    bad_slots.append(int(pair["a"]))
        bad_slots = sorted(set(bad_slots))
        if not bad_slots:
            break
        for slot in bad_slots:
            assets["image_regeneration_attempts"][slot - 1] += 1
            attempt = assets["image_regeneration_attempts"][slot - 1]
            acquirer.regenerate_slot(_folder(product) / "images", slot, attempt=attempt)
            changed.append(slot)

    if changed:
        _invalidate_pdf(record)
        _refresh_image_urls(product, record)
        record.setdefault("timestamps", {})["images_updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_record(product, record)
    state = get_state(product)
    state["changed_slots"] = sorted(set(changed))
    return state


def reorder_images(product, order):
    """Reorder the five images without regenerating them. Order is 1-based slots."""
    create_backup(product, "before_image_reorder")
    record = _read_record(product)
    assets = _normalize_assets(record)
    images = _image_paths(product, record)
    if len(images) < 5:
        raise RuntimeError("Five product images are required before reordering.")
    try:
        order = [int(x) for x in order]
    except Exception:
        raise RuntimeError("Invalid image order.")
    if sorted(order) != [1, 2, 3, 4, 5]:
        raise RuntimeError("Image order must contain slots 1,2,3,4,5 exactly once.")

    old_locks = list(assets["image_locks"])
    old_attempts = list(assets["image_regeneration_attempts"])
    assets["images"] = [images[i - 1] for i in order]
    assets["image_locks"] = [old_locks[i - 1] for i in order]
    assets["image_regeneration_attempts"] = [old_attempts[i - 1] for i in order]
    assets["image_web_urls"] = [
        f"/outputs/{safe_name(product)}/images/{Path(path).name}" for path in assets["images"]
    ]
    assets["primary_image_slot"] = 1
    _invalidate_pdf(record)
    record.setdefault("timestamps", {})["images_updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_record(product, record)
    return get_state(product)


def set_primary(product, slot):
    """Move a selected image to slot 1 so it becomes PDF/listing primary image."""
    slot = int(slot)
    if slot < 1 or slot > 5:
        raise RuntimeError("Primary image slot must be 1 to 5.")
    if slot == 1:
        state = get_state(product)
        state["primary_slot"] = 1
        return state
    order = [slot] + [x for x in range(1, 6) if x != slot]
    state = reorder_images(product, order)
    state["primary_slot"] = 1
    return state
