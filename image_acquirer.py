import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps


class ImageAcquirer:
    """
    NUNES smart product-image processor - V4.1.

    V4.1 rules:
    - If the operator uploads an image, that image is the MASTER reference.
    - The master is always kept as reference #1 and therefore drives Image 1.
    - IndiaMART images are downloaded concurrently for speed.
    - Candidate IndiaMART images are compared against the uploaded master and
      page relevance before they are allowed into Images 2-5.
    - If there are not enough trustworthy public matches, the system reuses the
      genuine master/local references rather than filling the gallery with
      unrelated products.
    """

    VALID_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0 Safari/537.36"
                )
            }
        )

    # ---------------------------------------------------------
    # DOWNLOAD / LOAD
    # ---------------------------------------------------------

    def _download_image(self, url):
        response = self.session.get(url, timeout=24)
        response.raise_for_status()
        content_type = (response.headers.get("content-type", "") or "").lower()
        if content_type and "image" not in content_type:
            raise RuntimeError("URL did not return an image.")
        if len(response.content) > 16 * 1024 * 1024:
            raise RuntimeError("Image file is too large.")
        image = Image.open(BytesIO(response.content))
        return ImageOps.exif_transpose(image).convert("RGB")

    def _load_local_image(self, path):
        path = Path(path)
        image = Image.open(path)
        return ImageOps.exif_transpose(image).convert("RGB")

    # ---------------------------------------------------------
    # VALIDATION / SCORING / DEDUPE
    # ---------------------------------------------------------

    def _is_usable(self, image):
        width, height = image.size
        if width < 300 or height < 300:
            return False
        ratio = width / max(1, height)
        if ratio > 3.5 or ratio < 0.28:
            return False
        return True

    def _score_image(self, image):
        width, height = image.size
        area_score = min(width * height, 4_000_000)
        ratio = width / max(1, height)
        shape_penalty = abs(1.0 - min(ratio, 1 / ratio))

        gray = image.convert("L")
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_energy = sum(edges.resize((64, 64)).getdata()) / (64 * 64)

        return area_score - (shape_penalty * 500_000) + (edge_energy * 350)

    def _average_hash(self, image, size=12):
        small = ImageOps.grayscale(image).resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(small.getdata())
        avg = sum(pixels) / max(1, len(pixels))
        return tuple(1 if pixel >= avg else 0 for pixel in pixels)

    def _edge_hash(self, image, size=12):
        edge = ImageOps.grayscale(image).filter(ImageFilter.FIND_EDGES)
        return self._average_hash(edge, size=size)

    def _color_vector(self, image, bins=8):
        thumb = image.copy().resize((96, 96), Image.Resampling.LANCZOS)
        hist = thumb.histogram()
        vector = []
        for channel in range(3):
            channel_hist = hist[channel * 256 : (channel + 1) * 256]
            for bucket in range(bins):
                start = int(bucket * 256 / bins)
                end = int((bucket + 1) * 256 / bins)
                vector.append(float(sum(channel_hist[start:end])))
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]

    def _hamming_distance(self, a, b):
        return sum(1 for x, y in zip(a, b) if x != y)

    def _visual_similarity(self, master, candidate):
        """Lightweight, dependency-free similarity for reference filtering."""
        a_hash = self._average_hash(master)
        b_hash = self._average_hash(candidate)
        bits = max(1, len(a_hash))
        hash_similarity = 1.0 - (self._hamming_distance(a_hash, b_hash) / bits)

        a_edge = self._edge_hash(master)
        b_edge = self._edge_hash(candidate)
        edge_similarity = 1.0 - (self._hamming_distance(a_edge, b_edge) / bits)

        av = self._color_vector(master)
        bv = self._color_vector(candidate)
        color_similarity = max(0.0, min(1.0, sum(x * y for x, y in zip(av, bv))))

        # Hash/edge are stronger identity signals than background color.
        value = (hash_similarity * 0.48) + (edge_similarity * 0.32) + (color_similarity * 0.20)
        return max(0.0, min(1.0, value))

    def _dedupe_candidates(self, candidates):
        unique = []
        hashes = []
        # Master must survive even when another image has a larger resolution.
        ordered = sorted(
            candidates,
            key=lambda item: (
                1 if item.get("source_type") == "MASTER_UPLOAD" else 0,
                float(item.get("combined_match_score", 0) or 0),
                float(item.get("score", 0) or 0),
            ),
            reverse=True,
        )
        for candidate in ordered:
            current_hash = self._average_hash(candidate["image"])
            duplicate = False
            for kept_hash in hashes:
                if self._hamming_distance(current_hash, kept_hash) <= 8:
                    duplicate = True
                    break
            if duplicate:
                continue
            unique.append(candidate)
            hashes.append(current_hash)
        return unique

    # ---------------------------------------------------------
    # PRODUCT SCANNING / CROP
    # ---------------------------------------------------------

    def _scan_product_bounds(self, image):
        rgb = image.convert("RGB")
        white = Image.new("RGB", rgb.size, (255, 255, 255))
        difference = ImageChops.difference(rgb, white).convert("L")
        mask = difference.point(lambda p: 255 if p > 25 else 0)
        mask = mask.filter(ImageFilter.MedianFilter(size=5))
        bbox = mask.getbbox()
        if not bbox:
            return rgb
        left, top, right, bottom = bbox
        product_width = right - left
        product_height = bottom - top
        if product_width < rgb.width * 0.08 or product_height < rgb.height * 0.08:
            return rgb
        margin_x = int(product_width * 0.08)
        margin_y = int(product_height * 0.08)
        left = max(0, left - margin_x)
        top = max(0, top - margin_y)
        right = min(rgb.width, right + margin_x)
        bottom = min(rgb.height, bottom + margin_y)
        return rgb.crop((left, top, right, bottom))

    # ---------------------------------------------------------
    # CANVAS / TRANSFORMS
    # ---------------------------------------------------------

    def _square_canvas(self, image, size=1200, padding=90, shift_x=0.0, shift_y=0.0, scale=1.0):
        image = image.copy()
        max_side = int((size - (padding * 2)) * max(0.60, min(scale, 1.15)))
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (size, size), "white")
        x = int((size - image.width) // 2 + shift_x * size * 0.12)
        y = int((size - image.height) // 2 + shift_y * size * 0.12)
        x = max(padding // 2, min(x, size - image.width - padding // 2))
        y = max(padding // 2, min(y, size - image.height - padding // 2))
        canvas.paste(image, (x, y))
        return canvas

    def _safe_zoom_crop(self, image, amount, horizontal_shift=0.0, vertical_shift=0.0):
        width, height = image.size
        amount = max(0.0, min(amount, 0.25))
        crop_w = int(width * (1 - amount))
        crop_h = int(height * (1 - amount))
        max_x = width - crop_w
        max_y = height - crop_h
        center_x = max_x / 2
        center_y = max_y / 2
        left = int(center_x + (horizontal_shift * max_x))
        top = int(center_y + (vertical_shift * max_y))
        left = max(0, min(left, max_x))
        top = max(0, min(top, max_y))
        return image.crop((left, top, left + crop_w, top + crop_h))

    def _quad_skew(self, image, direction="left"):
        image = image.copy()
        width, height = image.size
        inset = int(width * 0.14)
        top_inset = int(height * 0.06)
        if direction == "left":
            quad = (
                inset, top_inset,
                width, 0,
                width - inset, height,
                0, height - top_inset,
            )
        elif direction == "right":
            quad = (
                0, 0,
                width - inset, top_inset,
                width, height - top_inset,
                inset, height,
            )
        elif direction == "top":
            quad = (
                0, top_inset,
                width, top_inset,
                width - inset, height,
                inset, height,
            )
        else:
            return image
        return image.transform(
            (width, height),
            Image.Transform.QUAD,
            quad,
            resample=Image.Resampling.BICUBIC,
            fillcolor="white",
        )

    def _enhance_catalog(self, image, contrast=1.03, sharpness=1.08, brightness=1.01):
        image = ImageEnhance.Contrast(image).enhance(contrast)
        image = ImageEnhance.Sharpness(image).enhance(sharpness)
        image = ImageEnhance.Brightness(image).enhance(brightness)
        return image

    # ---------------------------------------------------------
    # SOURCE COLLECTION / REFERENCE MATCHING
    # ---------------------------------------------------------

    def _build_candidate(self, image, source_type, source_ref, metadata=None):
        if not self._is_usable(image):
            return None
        scanned = self._scan_product_bounds(image)
        if not self._is_usable(scanned):
            return None
        candidate = {
            "score": self._score_image(scanned),
            "image": scanned,
            "source_type": source_type,
            "source_ref": source_ref,
        }
        if isinstance(metadata, dict):
            candidate.update({k: v for k, v in metadata.items() if k not in {"image", "score"}})
        return candidate

    def _collect_master(self, master_file):
        if not master_file:
            return None
        try:
            path = Path(master_file)
            if not path.exists() or path.suffix.lower() not in self.VALID_SUFFIXES:
                return None
            return self._build_candidate(
                self._load_local_image(path),
                "MASTER_UPLOAD",
                str(path.resolve()),
                {"source_priority": 100, "page_match_score": 1.5, "image_rank": 0},
            )
        except Exception:
            return None

    def _collect_from_local_files(self, local_files):
        candidates = []
        for path in local_files or []:
            try:
                path = Path(path)
                if not path.exists() or path.suffix.lower() not in self.VALID_SUFFIXES:
                    continue
                candidate = self._build_candidate(
                    self._load_local_image(path),
                    "LOCAL",
                    str(path.resolve()),
                    {"source_priority": 80, "page_match_score": 1.2},
                )
                if candidate:
                    candidates.append(candidate)
            except Exception:
                continue
        return candidates

    def _normalize_url_item(self, item):
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("image_url") or "").strip()
            meta = dict(item)
            meta.pop("url", None)
            meta.pop("image_url", None)
            return url, meta
        return str(item or "").strip(), {}

    def _download_candidate(self, item):
        url, meta = self._normalize_url_item(item)
        if not url:
            return None
        try:
            image = self._download_image(url)
            meta.setdefault("source_priority", 50)
            candidate = self._build_candidate(image, "INDIAMART", url, meta)
            return candidate
        except Exception:
            return None

    def _collect_from_urls(self, urls):
        items = list(urls or [])[:40]
        if not items:
            return []
        workers = min(6, max(1, len(items)))
        candidates = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._download_candidate, item) for item in items]
            for future in as_completed(futures):
                try:
                    candidate = future.result()
                except Exception:
                    candidate = None
                if candidate:
                    candidates.append(candidate)
        return candidates

    def _reference_rank(self, master, candidates):
        if not master:
            # No operator image: keep public/local behavior but respect page relevance.
            for candidate in candidates:
                page_match = min(1.0, max(0.0, float(candidate.get("page_match_score", 0) or 0) / 1.5))
                candidate["reference_similarity"] = None
                candidate["combined_match_score"] = 0.65 * page_match + 0.35 * min(1.0, candidate["score"] / 4_000_000)
            return sorted(
                candidates,
                key=lambda x: (float(x.get("combined_match_score", 0)), float(x.get("score", 0))),
                reverse=True,
            )

        ranked = []
        master_image = master["image"]
        master["reference_similarity"] = 1.0
        master["combined_match_score"] = 1.0
        ranked.append(master)

        for candidate in candidates:
            # A duplicate copy of the exact uploaded master should not consume a public slot.
            if candidate.get("source_ref") == master.get("source_ref"):
                continue
            visual = self._visual_similarity(master_image, candidate["image"])
            page_match_raw = float(candidate.get("page_match_score", 0) or 0)
            page_match = min(1.0, max(0.0, page_match_raw / 1.5))
            quality = min(1.0, max(0.0, float(candidate.get("score", 0) or 0) / 4_000_000))

            if candidate.get("source_type") == "LOCAL":
                combined = (0.70 * visual) + (0.20 * page_match) + (0.10 * quality)
                accepted = visual >= 0.20
            else:
                try:
                    image_rank = max(1, int(candidate.get("image_rank") or 1))
                except Exception:
                    image_rank = 1
                rank_confidence = max(0.0, 1.0 - ((image_rank - 1) / 14.0))
                combined = (0.52 * visual) + (0.33 * page_match) + (0.10 * quality) + (0.05 * rank_confidence)
                # Accuracy first. Public images must come from a sufficiently
                # relevant IndiaMART product page; visual similarity alone is
                # never enough to accept an unrelated search result. Different
                # angles are allowed when page evidence is strong.
                accepted = (
                    page_match >= 0.30
                    and (visual >= 0.45 or (visual >= 0.30 and page_match >= 0.70))
                )
                if image_rank > 8 and visual < 0.55:
                    accepted = False

            candidate["reference_similarity"] = round(visual, 4)
            candidate["combined_match_score"] = round(combined, 4)
            candidate["reference_accepted"] = bool(accepted)
            if accepted:
                ranked.append(candidate)

        master_only = ranked[:1]
        others = sorted(
            ranked[1:],
            key=lambda x: (
                float(x.get("combined_match_score", 0)),
                float(x.get("page_match_score", 0) or 0),
                float(x.get("score", 0)),
            ),
            reverse=True,
        )
        return master_only + others

    def _save_reference_manifest(self, out_dir, selected):
        out_dir = Path(out_dir)
        refs_dir = out_dir / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for index, candidate in enumerate(selected, start=1):
            ref_path = refs_dir / f"reference_{index}.jpg"
            candidate["image"].save(ref_path, "JPEG", quality=95)
            manifest.append(
                {
                    "index": index,
                    "source_type": candidate.get("source_type", ""),
                    "source_ref": candidate.get("source_ref", ""),
                    "saved_path": str(ref_path.resolve()),
                    "score": round(float(candidate.get("score", 0)), 2),
                    "reference_similarity": candidate.get("reference_similarity"),
                    "combined_match_score": candidate.get("combined_match_score"),
                    "page_match_score": candidate.get("page_match_score"),
                    "page_title": candidate.get("page_title", ""),
                    "product_url": candidate.get("product_url", ""),
                    "image_rank": candidate.get("image_rank"),
                }
            )
        (out_dir / "reference_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ---------------------------------------------------------
    # 5 DIVERSE CATALOG IMAGES
    # ---------------------------------------------------------

    def _compose_variant(self, candidate, variant_kind, attempt=0):
        base = candidate["image"]
        cycle = attempt % 5
        shift_step = [0.0, 0.04, -0.04, 0.07, -0.07][cycle]
        zoom_step = [0.0, 0.02, 0.04, 0.06, 0.08][cycle]

        if variant_kind == "front_hero":
            # Keep the uploaded master composition as faithful as possible.
            padding = 80 if candidate.get("source_type") == "MASTER_UPLOAD" else 105 - cycle * 4
            return self._square_canvas(self._enhance_catalog(base), padding=padding, scale=1.0)

        if variant_kind == "three_quarter_left":
            img = self._quad_skew(base, "left")
            img = self._safe_zoom_crop(img, min(0.16, 0.05 + zoom_step), horizontal_shift=-0.08 + shift_step)
            return self._square_canvas(self._enhance_catalog(img, sharpness=1.10), padding=85 - cycle * 3, shift_x=-0.10 + shift_step)

        if variant_kind == "three_quarter_right":
            img = self._quad_skew(base, "right")
            img = self._safe_zoom_crop(img, min(0.16, 0.05 + zoom_step), horizontal_shift=0.08 - shift_step)
            return self._square_canvas(self._enhance_catalog(img, sharpness=1.10), padding=85 - cycle * 3, shift_x=0.10 - shift_step)

        if variant_kind == "elevated":
            img = self._quad_skew(base, "top")
            img = self._safe_zoom_crop(img, min(0.18, 0.08 + zoom_step), vertical_shift=-0.10 + shift_step)
            return self._square_canvas(self._enhance_catalog(img), padding=78 - cycle * 3, shift_y=-0.08 + shift_step)

        if variant_kind == "alternate_composition":
            img = self._safe_zoom_crop(base, min(0.22, 0.12 + zoom_step), horizontal_shift=0.18 - shift_step, vertical_shift=0.02 + shift_step / 2)
            img = self._enhance_catalog(img, contrast=1.05, sharpness=1.15, brightness=1.02)
            return self._square_canvas(img, padding=72 - cycle * 2, shift_x=0.12 - shift_step, shift_y=shift_step / 2, scale=1.04)

        return self._square_canvas(base)

    def _choose_candidate(self, candidates, index):
        if not candidates:
            raise RuntimeError("NO_GENUINE_PRODUCT_IMAGES_FOUND")
        if index < len(candidates):
            return candidates[index]
        return candidates[index % len(candidates)]

    def _create_five_catalog_images(self, candidates):
        kinds = [
            "front_hero",
            "three_quarter_left",
            "three_quarter_right",
            "elevated",
            "alternate_composition",
        ]
        variants = []
        for index, kind in enumerate(kinds):
            candidate = self._choose_candidate(candidates, index)
            variants.append((self._compose_variant(candidate, kind, attempt=0), kind, candidate))
        return variants

    # ---------------------------------------------------------
    # SAVE OUTPUTS
    # ---------------------------------------------------------

    def _save_five(self, candidates, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        selected = candidates[:5]
        self._save_reference_manifest(out_dir, selected)

        variants = self._create_five_catalog_images(selected)
        output_paths = []
        summary = []

        for index, (image, kind, candidate) in enumerate(variants, start=1):
            path = out_dir / f"product_{index}.jpg"
            image.save(path, "JPEG", quality=94, optimize=True)
            output_paths.append(str(path.resolve()))
            summary.append(
                {
                    "slot": index,
                    "variant": kind,
                    "source_type": candidate.get("source_type", ""),
                    "source_ref": candidate.get("source_ref", ""),
                    "reference_similarity": candidate.get("reference_similarity"),
                    "combined_match_score": candidate.get("combined_match_score"),
                    "output_path": str(path.resolve()),
                }
            )

        (out_dir / "variant_plan.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return output_paths

    def _manifest_candidates(self, out_dir):
        out_dir = Path(out_dir)
        manifest_path = out_dir / "reference_manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("REFERENCE_MANIFEST_NOT_FOUND")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        candidates = []
        for item in data:
            try:
                path = Path(item.get("saved_path") or "")
                if not path.exists():
                    continue
                image = self._load_local_image(path)
                candidate = self._build_candidate(
                    image,
                    item.get("source_type") or "REFERENCE",
                    item.get("source_ref") or str(path),
                    item,
                )
                if candidate:
                    candidates.append(candidate)
            except Exception:
                continue
        if not candidates:
            raise RuntimeError("NO_REFERENCE_IMAGES_AVAILABLE")
        return candidates

    def regenerate_slot(self, out_dir, slot, attempt=1):
        """Regenerate one output slot using another approved reference/composition."""
        slot = int(slot)
        if slot < 1 or slot > 5:
            raise ValueError("slot must be between 1 and 5")
        candidates = self._manifest_candidates(out_dir)
        kinds = [
            "front_hero",
            "three_quarter_left",
            "three_quarter_right",
            "elevated",
            "alternate_composition",
        ]
        candidate = candidates[(slot - 1 + int(attempt)) % len(candidates)]
        image = self._compose_variant(candidate, kinds[slot - 1], attempt=int(attempt))
        path = Path(out_dir) / f"product_{slot}.jpg"
        image.save(path, "JPEG", quality=94, optimize=True)
        return str(path.resolve())

    def output_similarity(self, image_paths):
        """Return pairwise similarity scores for the five generated outputs."""
        hashes = []
        paths = []
        for item in image_paths or []:
            path = Path(str(item or ""))
            if not path.exists():
                continue
            try:
                image = self._load_local_image(path)
                hashes.append(self._average_hash(image))
                paths.append(path)
            except Exception:
                continue
        pairs = []
        max_similarity = 0.0
        bits = 12 * 12
        for i in range(len(hashes)):
            for j in range(i + 1, len(hashes)):
                distance = self._hamming_distance(hashes[i], hashes[j])
                similarity = round(1.0 - (distance / bits), 4)
                max_similarity = max(max_similarity, similarity)
                pairs.append({
                    "a": i + 1,
                    "b": j + 1,
                    "similarity": similarity,
                    "status": "TOO_SIMILAR" if similarity >= 0.94 else ("SIMILAR" if similarity >= 0.86 else "OK"),
                })
        return {
            "pairs": pairs,
            "max_similarity": round(max_similarity, 4),
            "ok": all(x["status"] != "TOO_SIMILAR" for x in pairs),
        }

    # ---------------------------------------------------------
    # PUBLIC METHODS
    # ---------------------------------------------------------

    def from_reference_set(self, local_files=None, urls=None, out_dir=None, master_file=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        master = self._collect_master(master_file)
        candidates = []
        if master:
            candidates.append(master)

        master_ref = ""
        if master:
            master_ref = str(master.get("source_ref") or "")

        local_non_master = []
        for path in local_files or []:
            try:
                resolved = str(Path(path).resolve())
            except Exception:
                resolved = str(path)
            if master_ref and resolved == master_ref:
                continue
            local_non_master.append(path)

        others = []
        others.extend(self._collect_from_local_files(local_non_master))
        others.extend(self._collect_from_urls(urls))
        ranked = self._reference_rank(master, others)
        if master:
            candidates = ranked
        else:
            candidates = ranked

        unique = self._dedupe_candidates(candidates)
        # Reassert the master at slot 1 after de-duplication.
        if master:
            unique = [x for x in unique if x.get("source_type") != "MASTER_UPLOAD"]
            unique.insert(0, master)

        if not unique:
            raise RuntimeError("NO_GENUINE_PRODUCT_IMAGES_FOUND")
        return self._save_five(unique, out_dir)

    def from_urls(self, urls, out_dir):
        return self.from_reference_set(local_files=[], urls=urls, out_dir=out_dir)

    def from_local_file(self, source_file, out_dir):
        return self.from_reference_set(local_files=[], urls=[], out_dir=out_dir, master_file=source_file)

    def from_local_files(self, source_files, out_dir):
        master = source_files[0] if source_files else None
        return self.from_reference_set(local_files=source_files, urls=[], out_dir=out_dir, master_file=master)
