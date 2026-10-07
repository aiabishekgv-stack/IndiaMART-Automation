# NUNES IndiaMART Product Automation V2.5 Production

V2.5 is the production-hardening update to the V2.3 friendly workflow.

## Normal workflow

1. Start the app. It checks/reuses the existing IndiaMART account from Chrome/local saved session.
2. Enter a Product Name manually or load the next Google Sheet row.
3. Click **Public Research & Prepare**.
4. Click **Next → Template**.
5. Review/edit product, model, company, location, description, features, applications and specifications.
6. Click **Approve & Build PDF**.
7. Review the stable embedded PDF.
8. Click **Continue to IndiaMART Seller**.
9. Keep `PUBLISH_MODE=review` until live seller-form behavior has been validated.

## Quick start

Extract the ZIP, then run:

```text
SETUP.bat
TEST.bat
START.bat
```

Dashboard:

```text
http://127.0.0.1:5077
```

## Private settings — important V2.5 change

V2.5 stores private configuration on the server PC at:

```text
%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\config\.env
```

This prevents Google Apps Script URL/token values from being written into a shared NAS/project directory.

IndiaMART login state remains local at:

```text
%LOCALAPPDATA%\NUNES_INDIAMART_AUTOMATION\runtime\indiamart_storage_state.json
```

## Google Sheet

Google Sheet is optional. Manual product entry works without it.

Best setup method:

1. On the **server PC**, open `http://127.0.0.1:5077`.
2. Click **Load Next from Google Sheet**.
3. Paste the deployed Apps Script `/exec` URL and token.
4. V2.5 tests the connection before saving it.
5. Settings are stored only in the private LocalAppData config.

Remote LAN clients cannot change these credentials. Once configured on the server, they can use **Load Next from Google Sheet** normally.

If an old version already contains working Sheet credentials, run:

```text
IMPORT_OLD_SHEET_SETTINGS.ps1
```

It shows matching old configurations newest-first and lets you select one.

## IndiaMART account

The app first tries its own saved session. If necessary it bridges the account already logged into normal Windows Chrome. It does not deliberately open a separate registration/sign-in page. Chrome/IndiaMART session reuse must be verified on the actual Windows server PC.

## Product safety rules

Only customer-facing selling-price headers are eligible for IndiaMART price auto-fill:

```text
Price
Selling Price
Product Price
Rate
Unit Price
Sale Price
Sales Price
Offer Price
Selling Rate
```

Purchase Price, Supplier Price, Buying Price, Cost Price and Internal Cost are intentionally excluded.

## Publishing

Default:

```text
PUBLISH_MODE=review
```

Use review mode for real products until current IndiaMART selectors and field mapping are confirmed. Auto mode uses explicit publish/submit controls only and never treats a generic Save button as final publishing.

## NAS / office mode

Keep the project source on the NAS if desired. The Python venv, private config and IndiaMART session remain local on the server PC.

Run:

```text
START_LAN.bat
```

Office clients browse to:

```text
http://SERVER-PC-IP:5077
```

Configure Google Sheet only from the server PC using `http://127.0.0.1:5077`.

## Diagnostics

Run `TEST.bat`. It checks Python/packages, PDF template, write permission, Playwright Chromium, Ollama/Qwen, optional Google Sheet bridge and IndiaMART saved session.

## Optional config ACL hardening

`HARDEN_LOCAL_CONFIG.ps1` can restrict the LocalAppData config directory to the current Windows user. Run it only if permitted by your Windows/IT policy.

## Safe release

`BUILD_SAFE_RELEASE.ps1` creates a clean distributable ZIP excluding project `.env`, login state, outputs, caches and runtime data.

## Live-site limitation

IndiaMART may change its seller UI at any time. The offline pipeline can be validated automatically, but the seller flow must be tested with the real account in `review` mode. If a selector fails, use the generated `ERROR.png` and selector-debug logs to calibrate only the affected selector logic.


## V2.5 — Login-first popup

The first popup is now the IndiaMART account page. It tries automatic connection first. If the existing Chrome session cannot be reused, press **Open IndiaMART Login**. The official IndiaMART Chrome window opens for mobile/OTP, while the NUNES popup stays on screen and automatically detects successful login. OTP is never entered into or stored by the NUNES app.

Recommended first test: keep `PUBLISH_MODE=review`, complete the first popup, prepare one product manually, approve/build PDF, then continue to IndiaMART Seller and verify fields before submitting.


## V2.6 Smart Image Variations

This version improves the 5-image pipeline. The system now scans all available real product images from uploaded/local account sources and public research URLs, removes duplicates, keeps the strongest references, and generates five intentionally different product images for IndiaMART-style catalogue use. It also saves `reference_manifest.json` and `variant_plan.json` inside each product image folder for verification.


## V2.7 Image Studio

V2.7 adds direct control over the five generated product images. Each slot has **Lock** and **Regenerate** controls. Use **Regenerate Unlocked** to refresh only the images you have not locked, or **Auto Diversify** to detect very similar output pairs and regenerate only the unlocked duplicate slots. **Show References** displays the real source images used to build the five outputs.

Image changes deliberately invalidate the previous PDF. Press **Approve & Build PDF** again after changing images so the reviewed PDF and IndiaMART posting remain synchronized.

### Recommended image workflow
1. Prepare the product.
2. Open Template Review.
3. Lock the images you like.
4. Regenerate weak images individually.
5. Press Auto Diversify if the five outputs still look too similar.
6. Approve & Build PDF only after the final five are accepted.


## V2.8 Background Research

Public research, including DuckDuckGo fallback searches and public IndiaMART product-page scanning, now runs **headless in the background** by default. Users should no longer see a DuckDuckGo or public research Chrome window during normal product preparation.

`PUBLIC_HEADLESS=true` is the normal setting. Set it to `false` only temporarily when debugging public-research selectors.

The IndiaMART seller/login browser remains separate because seller posting may require an interactive authenticated browser.

## V2.9 Reference PDF Match

The brochure generator is now calibrated to `assets/reference_brochure.pdf` (Manual Heat Press Machine SF00005470). It follows the same two-page structure and measured typography instead of the older generic PDF layout.

Page 1 now uses a single-line product/model title, a larger reference-size product frame, and seven specification rows. Page 2 targets roughly the same description length and text size as the reference brochure, then prints up to 16 concise Key Features above the existing NUNES office footer.

Applications are still retained for IndiaMART listing data but are not printed in the brochure, matching the reference PDF.

## V3.0 Auto Google Sheet Link

This build is preconfigured for the supplied Google Sheet:

`https://docs.google.com/spreadsheets/d/1iGpbKroGTez2HNq0ps7shzn0OMvASn9-67UBPdogiJw/edit?usp=sharing`

Normal product loading no longer needs an Apps Script Web App URL or token. After the IndiaMART login gate, V3.0 automatically loads the next product row. The **Load Next from Google Sheet** button remains available.

For direct mode, set the Google Sheet's sharing to **Anyone with the link -> Viewer**. Direct mode is remotely read-only; V3.0 remembers completed rows locally on the server PC. The optional Apps Script bridge is only needed if you want status/URL write-back into the Google Sheet itself.


## V3.2 Production Controls

V3.2 adds failed-step retry, Recent Products history, automatic product-record checkpoints, recovery, stronger duplicate protection, selector diagnostics, Image Studio ordering/primary-image selection, and an IndiaMART completion summary. In review mode, use **I Submitted / Completed Review** only after you actually finish the IndiaMART seller review/submission.

Google Sheet direct-link mode is read-only at Google. The app therefore tracks completion locally. If the optional Apps Script bridge is configured, the existing `apps_script/Code.gs` can write IndiaMART Status, Decision, URL and Updated At back into the Sheet.


## V3.3 Google Sheet Dropdown Fix

The Sheet browser now detects non-standard product-column headers and title rows automatically. Search is performed server-side across the entire shared Sheet, so a product such as **Corona Generators** can be found even when it is beyond the first 1,200/5,000 rows. Repeated searches use a short in-memory cache for speed. The UI displays the detected product column and automatically selects a single or exact match.
