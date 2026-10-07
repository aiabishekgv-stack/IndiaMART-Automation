import io
import os
import re
from pathlib import Path

from PIL import Image as PILImage
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


BLUE = HexColor("#2f3595")
PINK = HexColor("#e6008c")
BLACK = HexColor("#111111")
LIGHT_ROW = HexColor("#f4f5f8")
GRID = HexColor("#d9d9d9")
LEFT_TEXT = HexColor("#767d89")

# Measurements taken from the user's reference brochure:
# Manual Heat Press Machine SF00005470.pdf
REF_TITLE_SIZE = 23.46
REF_SPEC_HEADING_SIZE = 13.54
REF_SPEC_TEXT_SIZE = 10.50
REF_DESC_HEADING_SIZE = 15.53
REF_KEY_HEADING_SIZE = 12.44
REF_BODY_SIZE = 9.39
REF_BODY_LEADING = 19.805
REF_BODY_CHARSPACE = 0.95
REF_FEATURE_CHARSPACE = 0.55
REF_DESCRIPTION_WORDS = 115
REF_FEATURE_COUNT = 16
REF_SPEC_COUNT = 7


_FONT_READY = False
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"


def _register_reference_fonts():
    global _FONT_READY, FONT_REGULAR, FONT_BOLD
    if _FONT_READY:
        return
    candidates = [
        (
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
        ),
        (
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ),
        (
            Path("/usr/share/fonts/truetype/croscore/Arimo-Regular.ttf"),
            Path("/usr/share/fonts/truetype/croscore/Arimo-Bold.ttf"),
        ),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            try:
                pdfmetrics.registerFont(TTFont("NunesArial", str(regular)))
                pdfmetrics.registerFont(TTFont("NunesArialBold", str(bold)))
                FONT_REGULAR = "NunesArial"
                FONT_BOLD = "NunesArialBold"
                break
            except Exception:
                pass
    _FONT_READY = True


def _template_path():
    configured = os.getenv("PDF_TEMPLATE_PATH", "assets/template.pdf").strip()
    path = Path(configured).resolve()
    if not path.exists():
        raise RuntimeError(f"PDF template not found: {path}")
    return path


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _word_count(text):
    return len(re.findall(r"\b[\w×/-]+\b", str(text or "")))


def _trim_words(text, max_words=REF_DESCRIPTION_WORDS):
    text = _clean_text(text)
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        return text
    clipped = " ".join(words[:max_words]).rstrip(" ,;:-")
    if clipped and clipped[-1] not in ".!?":
        clipped += "."
    return clipped



def _tracked_width(text, font_name, font_size, char_space=0.0):
    text = str(text or "")
    return stringWidth(text, font_name, font_size) + max(0, len(text) - 1) * char_space

def _wrap_words(text, font_name, font_size, max_width, char_space=0.0):
    words = _clean_text(text).split()
    lines, current = [], ""
    for word in words:
        candidate = word if not current else current + " " + word
        if _tracked_width(candidate, font_name, font_size, char_space) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_single_line(text, font_name, font_size, max_width, min_size=7.8):
    text = _clean_text(text)
    size = float(font_size)
    while size > min_size and stringWidth(text, font_name, size) > max_width:
        size -= 0.25
    if stringWidth(text, font_name, size) <= max_width:
        return text, size
    while text and stringWidth(text + "...", font_name, size) > max_width:
        text = text[:-1]
    return text.rstrip() + "...", size


def _draw_title(c, title):
    # Remove the placeholder title from the base template.
    c.setFillColor(white)
    c.rect(25, 652, 545, 58, stroke=0, fill=1)

    title = _clean_text(title) or "Product"
    size = REF_TITLE_SIZE
    font = FONT_BOLD
    max_width = 540
    while size > 13 and stringWidth(title, font, size) > max_width:
        size -= 0.35
    c.setFillColor(BLUE)
    c.setFont(font, size)
    c.drawCentredString(297.75, 666, title)


def _draw_product_image(c, image_path):
    if not image_path or not Path(image_path).exists():
        return
    # Match the reference brochure's framed image area. Cover the smaller
    # box printed in the older blank template, then draw one clean reference-size box.
    box_x, box_y, box_w, box_h = 161.7, 410.6, 257.9, 228.5
    c.setFillColor(white)
    c.rect(155, 404, 272, 242, stroke=0, fill=1)
    with PILImage.open(image_path) as im:
        iw, ih = im.size
    scale = min((box_w - 18) / iw, (box_h - 18) / ih)
    w, h = iw * scale, ih * scale
    x = box_x + (box_w - w) / 2
    y = box_y + (box_h - h) / 2
    c.drawImage(ImageReader(image_path), x, y, width=w, height=h,
                preserveAspectRatio=True, mask="auto")
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.8)
    c.rect(box_x, box_y, box_w, box_h, stroke=1, fill=0)


def _draw_reference_spec_table(c, specs):
    # Cover the old 5-row blank table from assets/template.pdf.
    c.setFillColor(white)
    c.rect(42, 45, 520, 355, stroke=0, fill=1)

    c.setFillColor(PINK)
    c.setFont(FONT_BOLD, REF_SPEC_HEADING_SIZE)
    c.drawString(29.4, 378.0, "PRODUCT SPECIFICATION")

    specs = [s for s in list(specs or []) if isinstance(s, dict)][:REF_SPEC_COUNT]
    while len(specs) < REF_SPEC_COUNT:
        specs.append({"name": "", "value": ""})

    x0 = 59.0
    xmid = 297.48
    x1 = 536.07
    top_y = 362.05
    row_h = 27.7605

    for i, spec in enumerate(specs):
        y_top = top_y - i * row_h
        y_bottom = y_top - row_h
        c.setFillColor(LIGHT_ROW if i % 2 == 0 else white)
        c.rect(x0, y_bottom, x1 - x0, row_h, stroke=0, fill=1)

        c.setStrokeColor(black if i > 0 else GRID)
        c.setLineWidth(0.55)
        c.line(x0, y_bottom, x1, y_bottom)

        name = _clean_text(spec.get("name"))
        value = _clean_text(spec.get("value"))
        name, nsize = _fit_single_line(name, FONT_REGULAR, REF_SPEC_TEXT_SIZE, 215)
        value, vsize = _fit_single_line(value, FONT_BOLD, REF_SPEC_TEXT_SIZE, 215)
        baseline = y_bottom + 9.4

        c.setFillColor(LEFT_TEXT)
        c.setFont(FONT_REGULAR, nsize)
        c.drawString(65.0, baseline, name)

        c.setFillColor(BLACK)
        c.setFont(FONT_BOLD, vsize)
        c.drawString(303.5, baseline, value)

    c.setStrokeColor(GRID)
    c.setLineWidth(0.55)
    c.rect(x0, top_y - REF_SPEC_COUNT * row_h, x1 - x0, REF_SPEC_COUNT * row_h,
           stroke=1, fill=0)
    c.line(xmid, top_y - REF_SPEC_COUNT * row_h, xmid, top_y)


def _page1_overlay(product, image_path, width, height):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    title = product.get("variant_name") or product.get("listing_name") or product.get("product_name")
    _draw_title(c, title)
    _draw_product_image(c, image_path)
    specs = product.get("brochure_specifications") or product.get("specifications") or []
    _draw_reference_spec_table(c, specs)
    c.save()
    buf.seek(0)
    return buf


def _normalize_feature_pool(product):
    features = []
    seen = set()

    def add(value):
        text = _clean_text(value).lstrip("-•* ")
        if not text:
            return
        low = text.lower()
        if low in seen:
            return
        seen.add(low)
        features.append(text)

    for item in product.get("key_features") or []:
        add(item)

    # If Qwen supplied fewer than the reference count, specifications are safe,
    # evidence-backed fallback bullets rather than invented claims.
    for spec in product.get("specifications") or []:
        if len(features) >= REF_FEATURE_COUNT:
            break
        if isinstance(spec, dict):
            name = _clean_text(spec.get("name"))
            value = _clean_text(spec.get("value"))
            if name and value:
                add(f"{name}: {value}")

    return features[:REF_FEATURE_COUNT]


def _fit_description_to_reference(text):
    text = _trim_words(text, REF_DESCRIPTION_WORDS)
    # The reference brochure uses 9 body lines before Key Features.
    # If the selected font is slightly wider, trim only as much as needed.
    while text:
        lines = _wrap_words(text, FONT_REGULAR, REF_BODY_SIZE, 552, REF_BODY_CHARSPACE)
        if len(lines) <= 9:
            return text, lines
        words = text.split()
        if len(words) <= 85:
            return text, lines[:9]
        text = " ".join(words[:-1]).rstrip(" ,;:-") + "."
    return "", []


def _page2_overlay(product, width, height):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))

    # Preserve the branded office footer from the supplied blank template, but
    # clear the content area so its fixed Key Features heading does not remain.
    c.setFillColor(white)
    c.rect(0, 145, width, height - 145, stroke=0, fill=1)

    # PRODUCT DESCRIPTION - exact reference font size/placement.
    c.setFillColor(PINK)
    c.setFont(FONT_BOLD, REF_DESC_HEADING_SIZE)
    c.drawString(25.33, 806.0, "PRODUCT DESCRIPTION")

    desc, lines = _fit_description_to_reference(product.get("description", ""))
    c.setFillColor(BLACK)
    y = 780.0
    for line in lines[:9]:
        t = c.beginText(25.33, y)
        t.setFont(FONT_REGULAR, REF_BODY_SIZE)
        t.setCharSpace(REF_BODY_CHARSPACE)
        t.textLine(line)
        c.drawText(t)
        y -= REF_BODY_LEADING

    # Reference heading begins immediately after the description block.
    key_y = y - 2.0
    c.setFillColor(PINK)
    c.setFont(FONT_BOLD, REF_KEY_HEADING_SIZE)
    c.drawString(25.33, key_y, "Key Features")

    feature_y = key_y - 22.0
    features = _normalize_feature_pool(product)
    c.setFillColor(BLACK)
    for feature in features[:REF_FEATURE_COUNT]:
        feature = _clean_text(feature)
        fsize = REF_BODY_SIZE
        while fsize > 8.0 and _tracked_width(feature, FONT_REGULAR, fsize, REF_FEATURE_CHARSPACE) > 515:
            fsize -= 0.25
        while feature and _tracked_width(feature + "...", FONT_REGULAR, fsize, REF_FEATURE_CHARSPACE) > 515:
            feature = feature[:-1]
        if feature and feature[-1] not in ".!?" and len(feature) < 8:
            feature = feature.rstrip()
        c.setFillColor(BLACK)
        c.circle(32.0, feature_y + 3.0, 1.35, stroke=0, fill=1)
        t = c.beginText(41.0, feature_y)
        t.setFont(FONT_REGULAR, fsize)
        t.setCharSpace(REF_FEATURE_CHARSPACE)
        t.textLine(feature)
        c.drawText(t)
        feature_y -= REF_BODY_LEADING
        if feature_y < 245:
            break

    c.save()
    buf.seek(0)
    return buf


def build_product_pdf(product, image_path, output_path, company_details=None, source_url=""):
    """
    Build a two-page product brochure matching the user's reference PDF style.

    Reference measurements:
    - Page 1: one-line product title, framed image, 7-row specifications table.
    - Page 2: ~115-word description, 9.39 pt body text, 16 concise feature bullets,
      and the existing NUNES office footer from assets/template.pdf.
    - Applications remain available for IndiaMART listing data but are deliberately
      not printed in the brochure because the reference brochure does not show them.
    """
    _register_reference_fonts()

    template_path = _template_path()
    template = PdfReader(str(template_path))
    if len(template.pages) < 2:
        raise RuntimeError("template.pdf must contain at least two pages.")

    writer = PdfWriter()

    page1 = template.pages[0]
    w1 = float(page1.mediabox.width)
    h1 = float(page1.mediabox.height)
    overlay1 = PdfReader(_page1_overlay(product, image_path, w1, h1)).pages[0]
    page1.merge_page(overlay1)
    writer.add_page(page1)

    page2 = template.pages[1]
    w2 = float(page2.mediabox.width)
    h2 = float(page2.mediabox.height)
    overlay2 = PdfReader(_page2_overlay(product, w2, h2)).pages[0]
    page2.merge_page(overlay2)
    writer.add_page(page2)

    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)

    return str(output)
