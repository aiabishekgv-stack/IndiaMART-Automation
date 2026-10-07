import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from PIL import Image, ImageDraw

from image_acquirer import ImageAcquirer
from pdf_builder import build_product_pdf
from product_record import extract_selling_price, new_record, normalize_ai_status, sync_model_spec


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("NUNES V2 offline smoke test")

    record = new_record(
        "NDT Testing Equipment",
        "ST2016",
        "12",
        {"Product Name": "NDT Testing Equipment", "Model": "ST2016", "Selling Price": "24500", "Purchase Price": "10000"},
    )
    assert_true(record["source"]["selling_price"] == "24500", "selling price extraction failed")
    assert_true(extract_selling_price({"Purchase Price": "999"}) == "", "purchase price must never be used")

    synced = sync_model_spec([
        {"name": "Model Name/Number", "value": "OLD"},
        {"name": "Material", "value": "Steel"},
    ], "ST2016")
    assert_true(synced[0]["value"] == "ST2016", "model synchronization failed")

    ai = normalize_ai_status({"mode": "QWEN_SUCCESS", "label": "Qwen3 Used Successfully", "model": "qwen3:1.7b"})
    assert_true(ai["attempted"] and ai["response_ok"], "AI status normalization failed")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "source.jpg"
        im = Image.new("RGB", (1200, 900), "white")
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((250, 180, 950, 720), radius=70, fill=(50, 90, 145), outline=(20, 40, 70), width=8)
        d.rectangle((520, 310, 690, 560), fill=(225, 235, 245), outline=(20, 40, 70), width=5)
        im.save(src, quality=95)

        images = ImageAcquirer().from_local_file(src, td / "images")
        assert_true(len(images) == 5, "five image variants were not created")
        assert_true(all(Path(x).exists() for x in images), "one or more image variants are missing")

        product = {
            "listing_name": "NDT Testing Equipment",
            "variant_name": "NDT Testing Equipment ST2016",
            "description": "Portable equipment prepared for material inspection from approved evidence.",
            "key_features": ["Portable format", "Model: ST2016", "Manual operation"],
            "applications": ["Material inspection", "Industrial quality checks"],
            "specifications": synced,
        }
        pdf = Path(build_product_pdf(product, images[0], td / "test_brochure.pdf"))
        assert_true(pdf.exists() and pdf.stat().st_size > 1000, "PDF was not generated")

    print("PASS: product record")
    print("PASS: price safety")
    print("PASS: model consistency")
    print("PASS: AI status normalization")
    print("PASS: five-image pipeline")
    print("PASS: PDF pipeline")
    print("OFFLINE SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
