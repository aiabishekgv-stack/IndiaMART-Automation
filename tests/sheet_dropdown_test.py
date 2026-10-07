import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sheets_client import SheetBridge, _DIRECT_ROWS_CACHE


class DemoSheet(SheetBridge):
    def __init__(self, csv_text):
        self.mode = "direct"
        self.sheet_name = "final sheet"
        self.csv_text = csv_text
        self.sheet_id = "v33-test"
        self.progress_file = ROOT / "data" / "v33_test_progress.json"

    def _download_csv(self, sheet_name=None):
        return self.csv_text

    def _completed_set(self, sheet_name=None):
        return set()


def main():
    _DIRECT_ROWS_CACHE.clear()
    rows = [
        "NUNES PRODUCT MASTER,,,,",
        "S No.,ITEM DESCRIPTION,MODEL NO.,SALES RATE,Status",
    ]
    for i in range(1, 6005):
        product = "Corona Generators" if i == 5900 else f"Instrument Product {i}"
        rows.append(f"{i},{product},M{i},{1000+i},")
    bridge = DemoSheet("\n".join(rows) + "\n")

    first_page = bridge.list_products(limit=1200)
    assert not any(x["product_name"] == "Corona Generators" for x in first_page["products"])

    search = bridge.list_products(search="corona generator", limit=300)
    match = next(x for x in search["products"] if x["product_name"] == "Corona Generators")
    assert search["columns"]["product"] == "ITEM DESCRIPTION"

    loaded = bridge.get_row(match["source_row"])
    assert loaded["product_name"] == "Corona Generators"
    assert loaded["model"] == "M5900"

    print("PASS: non-standard product header detection")
    print("PASS: title-row detection")
    print("PASS: full-sheet remote search")
    print("PASS: exact source-row loading")
    print("GOOGLE SHEET DROPDOWN TEST PASSED")


if __name__ == "__main__":
    main()
