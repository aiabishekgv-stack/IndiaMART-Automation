import json
import os
import re
import shutil
import time
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import sync_playwright


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _page_looks_logged_in(page):
    """Fast IndiaMART seller-session check for any Playwright page."""
    if not page or page.is_closed():
        return False

    # A visible product search box is the strongest low-cost signal.
    for selector in (
        'input[placeholder*="search" i]',
        'input[type="search"]',
        'input[aria-label*="search" i]',
        'input[placeholder*="product" i]',
    ):
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=500):
                return True
        except Exception:
            pass

    try:
        url = (page.url or "").lower()
        body = _norm(page.locator("body").inner_text(timeout=1800))
    except Exception:
        return False

    seller_page = "seller.indiamart.com" in url
    product_words = any(
        word in body
        for word in (
            "manage products",
            "add product",
            "my products",
            "product catalogue",
            "product catalog",
        )
    )
    login_words = any(
        word in body
        for word in (
            "login with mobile",
            "enter mobile number",
            "verify otp",
            "enter otp",
        )
    )
    return seller_page and product_words and not login_words


class IndiaMartBrowser:
    """
    IndiaMART seller browser wrapper - V2.

    IMPORTANT FIX:
    The old build used launch_persistent_context() with one Chrome user-data
    directory. If the Login button left that Chrome open and Run 1 Product then
    tried to use the same profile, Chrome could immediately exit with code 21.

    V1.2 launches a normal browser context and stores only login state in a JSON
    file. This removes the profile-lock conflict while still remembering login.
    """

    def __init__(self, event=None):
        self.url = os.getenv(
            "INDIAMART_URL",
            "https://seller.indiamart.com/product/manageproducts/",
        )
        self.publish_mode = os.getenv("PUBLISH_MODE", "review").strip().lower()
        default_runtime = (
            Path(os.getenv("LOCALAPPDATA", str(Path.home())))
            / "NUNES_INDIAMART_AUTOMATION"
            / "runtime"
        )
        self.state_path = Path(
            os.getenv(
                "INDIAMART_STORAGE_STATE",
                str(default_runtime / "indiamart_storage_state.json"),
            )
        ).expanduser().resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        self.event = event or (lambda stage, msg, progress=None: None)
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self, headless=False):
        self.pw = sync_playwright().start()

        channel = os.getenv("BROWSER_CHANNEL", "chrome").strip().lower()
        try:
            if channel in {"", "playwright", "chromium"}:
                self.browser = self.pw.chromium.launch(headless=headless)
            else:
                self.browser = self.pw.chromium.launch(
                    headless=headless,
                    channel=channel,
                )
        except Exception as chrome_error:
            # Playwright Chromium is installed by SETUP.bat. If system Chrome is
            # blocked/crashes, fall back automatically instead of failing the run.
            self.event(
                "BROWSER",
                "System Chrome could not start; retrying with Playwright Chromium...",
                None,
            )
            try:
                self.browser = self.pw.chromium.launch(headless=headless)
            except Exception:
                raise chrome_error

        context_args = {
            "viewport": {"width": 1500, "height": 930},
            "accept_downloads": True,
        }
        if self.state_path.exists() and self.state_path.stat().st_size > 10:
            context_args["storage_state"] = str(self.state_path)

        try:
            self.context = self.browser.new_context(**context_args)
        except Exception:
            # A corrupt/old state file should never block the whole automation.
            if self.state_path.exists():
                bad = self.state_path.with_name("indiamart_storage_state.bad.json")
                try:
                    if bad.exists():
                        bad.unlink()
                    self.state_path.replace(bad)
                except Exception:
                    pass
            context_args.pop("storage_state", None)
            self.context = self.browser.new_context(**context_args)

        self.page = self.context.new_page()

        # IndiaMART authentication can replace the original page or open a new
        # tab/window. Always follow the newest live page so an auth redirect does
        # not look like the user closed the browser.
        try:
            self.context.on("page", self._remember_page)
        except Exception:
            pass

        return self.page

    def _remember_page(self, page):
        try:
            if page and not page.is_closed():
                self.page = page
        except Exception:
            pass

    def _refresh_active_page(self):
        """Return the newest live page in the current browser context."""
        try:
            if self.page and not self.page.is_closed():
                return self.page
        except Exception:
            pass

        try:
            live_pages = [p for p in self.context.pages if not p.is_closed()]
        except Exception:
            live_pages = []

        if live_pages:
            self.page = live_pages[-1]
            return self.page

        self.page = None
        return None

    def close(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.pw = None

    def save_session(self):
        if not self.context:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Newer Playwright versions can also persist IndexedDB-based auth.
            self.context.storage_state(path=str(self.state_path), indexed_db=True)
        except TypeError:
            self.context.storage_state(path=str(self.state_path))

    def check_logged_in_once(self, timeout_seconds=20):
        """Check a saved automation session without waiting for manual OTP."""
        if not self.page:
            self.start(True)

        page = self._refresh_active_page()
        if not page:
            page = self.context.new_page()
            self.page = page

        try:
            page.goto(
                self.url,
                wait_until="domcontentloaded",
                timeout=max(5000, int(timeout_seconds * 1000)),
            )
        except Exception:
            pass

        try:
            page.wait_for_timeout(1200)
        except Exception:
            time.sleep(1.2)

        self._refresh_active_page()
        ok = self._looks_logged_in()
        if ok:
            self.save_session()
        return ok

    def _copy_chrome_profile_snapshot(self, user_data_dir, profile):
        """
        Create a lightweight read-only-style snapshot of the user's Chrome
        profile in our local runtime directory.

        Why: normal Chrome keeps its live profile locked. Opening that same
        profile with Playwright can fail or spawn a sign-in page. A snapshot
        lets us reuse the already-authenticated IndiaMART cookies/storage
        without closing the user's normal Chrome and without modifying it.
        """
        runtime = self.state_path.parent
        snapshot_root = runtime / "chrome_profile_snapshot"
        try:
            if snapshot_root.exists():
                shutil.rmtree(snapshot_root, ignore_errors=True)
            snapshot_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None, "Could not create Chrome session snapshot folder."

        def copy_file(src, dst):
            try:
                if not src.exists() or not src.is_file():
                    return
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except Exception:
                # Chrome may be writing a nonessential file. Skip it rather
                # than failing the complete snapshot.
                pass

        # Chrome's encryption metadata is required to decrypt profile cookies
        # under the same Windows account.
        copy_file(user_data_dir / "Local State", snapshot_root / "Local State")

        src_profile = user_data_dir / profile
        dst_profile = snapshot_root / profile
        if not src_profile.exists():
            return None, f"Chrome profile not found: {src_profile}"

        # Copy only authentication/site-storage material, not caches/history.
        important_files = (
            "Preferences",
            "Secure Preferences",
            "Web Data",
            "Web Data-journal",
            "Login Data",
            "Login Data-journal",
        )
        for name in important_files:
            copy_file(src_profile / name, dst_profile / name)

        important_dirs = (
            "Network",          # Cookies
            "Local Storage",    # localStorage / leveldb
            "Session Storage",
            "IndexedDB",
            "SharedStorage",
        )
        ignored_names = {
            "Cache", "Code Cache", "GPUCache", "DawnCache", "GrShaderCache",
            "CacheStorage", "blob_storage",
        }

        for dirname in important_dirs:
            src_dir = src_profile / dirname
            if not src_dir.exists():
                continue
            for item in src_dir.rglob('*'):
                try:
                    rel = item.relative_to(src_profile)
                except Exception:
                    continue
                if any(part in ignored_names for part in rel.parts):
                    continue
                target = dst_profile / rel.relative_to(dirname)
                # The line above loses dirname; correct target explicitly.
                target = snapshot_root / profile / rel
                if item.is_dir():
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass
                elif item.is_file():
                    copy_file(item, target)

        return snapshot_root, "Chrome session snapshot created."

    def import_system_chrome_session(self, timeout_seconds=25):
        """
        Reuse the IndiaMART account already logged in to the user's normal
        Windows Chrome profile WITHOUT opening a visible sign-in page.

        V2.2 strategy:
          1. Detect the user's last-used Chrome profile.
          2. Copy only cookie/site-storage data to our local runtime snapshot.
          3. Open the snapshot headlessly at the normal seller Manage Products URL.
          4. If already authenticated, save Playwright storage_state for future runs.

        The user's live Chrome window/profile is never closed or modified.
        """
        enabled = os.getenv(
            "INDIAMART_CHROME_PROFILE_BRIDGE", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled or os.name != "nt":
            return False, "Existing Chrome account bridge is disabled or unsupported on this OS."

        localapp = Path(os.getenv("LOCALAPPDATA", str(Path.home())))
        default_dir = localapp / "Google" / "Chrome" / "User Data"
        configured = os.getenv("CHROME_USER_DATA_DIR", "").strip()
        user_data_dir = (
            Path(os.path.expandvars(configured)).expanduser()
            if configured else default_dir
        )

        profile_setting = os.getenv("CHROME_PROFILE_DIRECTORY", "auto").strip() or "auto"
        profile = profile_setting
        if profile_setting.lower() == "auto":
            profile = "Default"
            try:
                local_state = user_data_dir / "Local State"
                if local_state.exists():
                    data = json.loads(local_state.read_text(encoding="utf-8"))
                    profile = str(
                        (data.get("profile", {}) or {}).get("last_used")
                        or "Default"
                    ).strip() or "Default"
            except Exception:
                profile = "Default"

        if not user_data_dir.exists():
            return False, f"Chrome user-data directory not found: {user_data_dir}"

        snapshot_root, snapshot_message = self._copy_chrome_profile_snapshot(
            user_data_dir,
            profile,
        )
        if not snapshot_root:
            return False, snapshot_message

        pw = None
        context = None
        try:
            pw = sync_playwright().start()
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(snapshot_root),
                channel="chrome",
                headless=True,
                args=[
                    f"--profile-directory={profile}",
                    "--disable-background-networking",
                    "--disable-component-update",
                ],
                viewport={"width": 1450, "height": 900},
            )

            pages = [x for x in context.pages if not x.is_closed()]
            page = pages[-1] if pages else context.new_page()
            page.goto(
                self.url,
                wait_until="domcontentloaded",
                timeout=max(5000, int(timeout_seconds * 1000)),
            )
            page.wait_for_timeout(1600)

            if not _page_looks_logged_in(page):
                return False, (
                    "Your normal Chrome profile was found, but the copied session "
                    "is not currently authenticated on the IndiaMART seller page. "
                    "Open IndiaMART in normal Chrome, confirm you are logged in, then press Connect again."
                )

            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                context.storage_state(path=str(self.state_path), indexed_db=True)
            except TypeError:
                context.storage_state(path=str(self.state_path))

            return True, f"Connected to the IndiaMART account from existing Chrome profile: {profile}"
        except Exception as exc:
            return False, "Could not reuse the existing Chrome IndiaMART session: " + str(exc)
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass

    def _first_visible(self, selectors, timeout_each=900):
        if not self.page or self.page.is_closed():
            return None
        for selector in selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.count() and loc.is_visible(timeout=timeout_each):
                    return loc
            except Exception:
                pass
        return None

    def _product_search_box(self):
        return self._first_visible(
            [
                'input[placeholder*="search" i]',
                'input[type="search"]',
                'input[aria-label*="search" i]',
                'input[placeholder*="product" i]',
            ],
            timeout_each=650,
        )


    def _wait_for_search_box(self, seconds=20):
        deadline = time.time() + seconds

        while time.time() < deadline:
            box = self._product_search_box()

            if box:
                return box

            if not self.page or self.page.is_closed():
                return None

            self.page.wait_for_timeout(500)

        return None


    def _product_words(self, text):
        """
        Return meaningful normalized words used
        for product matching.
        """

        return {
            word
            for word in re.findall(
                r"[a-z0-9]+",
                _norm(text),
            )
            if len(word) > 2
        }

    def _product_match_score(self, query, candidate):
        """
        Compare the requested product name against one actual
        visible product/result name.

        Returns a score from 0.0 to 1.0.
        """

        query_norm = _norm(query)
        candidate_norm = _norm(candidate)

        if not query_norm or not candidate_norm:
            return 0.0

        # Exact normalized match.
        if query_norm == candidate_norm:
            return 1.0

        query_words = self._product_words(
            query_norm
        )

        candidate_words = self._product_words(
            candidate_norm
        )

        if not query_words:
            return 0.0

        common_words = (
            query_words
            & candidate_words
        )

        word_score = (
            len(common_words)
            / len(query_words)
        )

        sequence_score = SequenceMatcher(
            None,
            query_norm,
            candidate_norm,
        ).ratio()

        # A direct phrase match is strong evidence.
        phrase_score = 0.0

        if query_norm in candidate_norm:
            phrase_score = 0.98

        elif (
            candidate_norm in query_norm
            and len(candidate_norm) >= 8
        ):
            phrase_score = 0.90

        # Require meaningful word overlap.
        if word_score < 0.50:
            sequence_score *= 0.70

        return round(
            max(
                word_score,
                sequence_score,
                phrase_score,
            ),
            4,
        )

    def _collect_product_result_names(self):
        """
        Collect likely product names from visible IndiaMART
        search-result/product elements.

        Important:
        We intentionally do NOT use the entire page body.
        """

        candidates = []

        selectors = [
            '[data-testid*="product" i]',
            '[class*="product-name" i]',
            '[class*="product_name" i]',
            '[class*="product-title" i]',
            '[class*="productTitle" i]',
            '[class*="prd-name" i]',
            '[class*="prd_name" i]',
            'a[href*="product" i]',
            'a[href*="manageproducts" i]',
        ]

        for selector in selectors:
            try:
                locators = self.page.locator(
                    selector
                )

                count = min(
                    locators.count(),
                    80,
                )

                for i in range(count):
                    try:
                        locator = locators.nth(i)

                        if not locator.is_visible():
                            continue

                        text = (
                            locator.inner_text(
                                timeout=700
                            )
                            or ""
                        ).strip()

                        if not text:
                            text = (
                                locator.get_attribute(
                                    "title"
                                )
                                or ""
                            ).strip()

                        text = re.sub(
                            r"\s+",
                            " ",
                            text,
                        ).strip()

                        # Ignore UI labels and giant containers.
                        if len(text) < 4:
                            continue

                        if len(text) > 180:
                            continue

                        lower = text.lower()

                        bad_text = {
                            "add product",
                            "add new product",
                            "add variant",
                            "manage products",
                            "search",
                            "logout",
                            "login",
                        }

                        if lower in bad_text:
                            continue

                        if text not in candidates:
                            candidates.append(
                                text
                            )

                    except Exception:
                        continue

            except Exception:
                continue

        return candidates

    def _find_best_product_match(
        self,
        query,
    ):
        """
        Return the strongest actual product-result match.

        Safer behaviour:
        if no reliable result is found, return matched=False.
        """

        candidates = (
            self._collect_product_result_names()
        )

        best_name = ""
        best_score = 0.0

        for candidate in candidates:
            score = self._product_match_score(
                query,
                candidate,
            )

            if score > best_score:
                best_score = score
                best_name = candidate

        # Conservative threshold.
        #
        # We prefer creating a new product over incorrectly
        # adding a variant to an unrelated product.
        matched = best_score >= 0.78

        return {
            "matched": matched,
            "score": round(
                best_score,
                2,
            ),
            "matched_name": best_name,
            "candidates": candidates[:30],
        }

    def _looks_logged_in(self):
        if not self._refresh_active_page():
            return False
        return _page_looks_logged_in(self.page)

    def ensure_logged_in(self, timeout_seconds=600):
        if not self.page:
            self.start(False)

        page = self._refresh_active_page()
        if not page:
            page = self.context.new_page()
            self.page = page

        try:
            page.goto(self.url, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            # A redirect may replace/close the original page. Reattach below.
            pass

        time.sleep(1.6)
        self._refresh_active_page()

        if self._looks_logged_in():
            self.save_session()
            return True

        self.event(
            "INDIAMART_LOGIN",
            "IndiaMART login/OTP is required. Complete it in the opened browser; this run will continue automatically.",
            None,
        )

        deadline = time.time() + timeout_seconds
        no_page_since = None

        while time.time() < deadline:
            # IndiaMART may close the first auth page and replace it with another.
            # Follow any live replacement page instead of terminating immediately.
            page = self._refresh_active_page()

            if page is None:
                if no_page_since is None:
                    no_page_since = time.time()

                # Give IndiaMART several seconds to complete a redirect/window swap.
                if time.time() - no_page_since < 12:
                    time.sleep(0.5)
                    continue

                # If the browser itself is still alive, recreate the seller page
                # rather than closing the login session.
                try:
                    if self.browser and self.browser.is_connected():
                        page = self.context.new_page()
                        self.page = page
                        page.goto(self.url, wait_until="domcontentloaded", timeout=90000)
                        no_page_since = None
                        time.sleep(1.0)
                        continue
                except Exception:
                    pass

                raise RuntimeError(
                    "IndiaMART browser was closed before login completed. Run IndiaMART Login again and finish the login/OTP."
                )

            no_page_since = None

            if self._looks_logged_in():
                self.save_session()
                self.event("INDIAMART_LOGIN", "IndiaMART login detected and saved.", None)
                return True

            # Use normal sleep rather than page.wait_for_timeout(); the latter
            # throws when IndiaMART replaces the auth page mid-redirect.
            time.sleep(1.0)

        raise RuntimeError(
            "Timed out waiting for IndiaMART login. Run again and finish login/OTP within 10 minutes."
        )

    def _click_text(self, labels):
        for label in labels:
            for exact in (True, False):
                try:
                    loc = self.page.get_by_text(label, exact=exact).first
                    if loc.count() and loc.is_visible(timeout=1000):
                        loc.click(timeout=5000)
                        return True
                except Exception:
                    pass
        return False

    def _fill(self, labels, value):
        if value is None or not str(value).strip():
            return False
        value = str(value)

        for label in labels:
            try:
                loc = self.page.get_by_label(label, exact=False).first
                if loc.count() and loc.is_visible(timeout=800):
                    loc.fill(value)
                    return True
            except Exception:
                pass

        for label in labels:
            selectors = [
                f'input[placeholder*="{label}" i]',
                f'textarea[placeholder*="{label}" i]',
                f'input[aria-label*="{label}" i]',
                f'textarea[aria-label*="{label}" i]',
            ]
            loc = self._first_visible(selectors)
            if loc:
                try:
                    loc.fill(value)
                    return True
                except Exception:
                    pass
        return False

    def search_product(self, name):
        # Avoid a second navigation when run_one() has already authenticated.
        if not self._looks_logged_in():
            self.ensure_logged_in(timeout_seconds=600)
        search = self._wait_for_search_box(20)
        if not search:
            raise RuntimeError(
                "IndiaMART is open, but the Product search box was not found. "
                "The run saved an ERROR.png screenshot so the selector can be calibrated."
            )

        search.fill(name)
        search.press("Enter")
        self.page.wait_for_timeout(2600)

        match_result = (
            self._find_best_product_match(
                name
            )
        )

        matched = match_result[
            "matched"
        ]

        score = match_result[
            "score"
        ]

        matched_name = match_result[
            "matched_name"
        ]

        if matched:
            self.event(
                "PRODUCT_MATCH",
                (
                    "Existing IndiaMART product matched: "
                    f"{matched_name} "
                    f"(score {score})"
                ),
                None,
            )

        else:
            self.event(
                "PRODUCT_MATCH",
                (
                    "No reliable existing product match "
                    f"was found for '{name}'. "
                    f"Best score: {score}. "
                    "New Product mode will be used."
                ),
                None,
            )

        description = ""
        if matched:
            for selector in [
                '[class*="description" i]',
                '[data-testid*="description" i]',
                "textarea",
            ]:
                try:
                    locs = self.page.locator(selector)
                    for i in range(min(locs.count(), 6)):
                        txt = (
                            locs.nth(i).input_value()
                            if selector == "textarea"
                            else locs.nth(i).inner_text(timeout=800)
                        )
                        if len((txt or "").strip()) > 80:
                            description = txt.strip()
                            break
                    if description:
                        break
                except Exception:
                    pass

        # -------------------------------------------------
        # Collect genuine product image URLs visible on the
        # IndiaMART product/search page.
        # -------------------------------------------------
        image_urls = []

        try:
            images = self.page.locator("img")

            for i in range(images.count()):
                try:
                    img = images.nth(i)

                    src = (
                        img.get_attribute("src")
                        or img.get_attribute("data-src")
                        or img.get_attribute("data-original")
                        or ""
                    ).strip()

                    if not src:
                        continue

                    if src.startswith("data:"):
                        continue

                    lower_src = src.lower()
                    bad_words = [
                        "logo",
                        "icon",
                        "sprite",
                        "avatar",
                        "profile",
                        "loader",
                        "loading",
                        "placeholder",
                        "favicon",
                    ]

                    if any(word in lower_src for word in bad_words):
                        continue

                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("/"):
                        from urllib.parse import urljoin
                        src = urljoin(self.page.url, src)

                    if not src.startswith(("http://", "https://")):
                        continue

                    if src not in image_urls:
                        image_urls.append(src)

                    if len(image_urls) >= 20:
                        break

                except Exception:
                    continue

        except Exception:
            image_urls = []

        return {
            "matched": matched,
            "score": round(
                score,
                2,
            ),
            "matched_name": matched_name,
            "match_candidates": (
                match_result.get(
                    "candidates",
                    [],
                )
            ),
            "description": description,
            "image_urls": image_urls,
            "url": self.page.url,
        }

    def _click_action_control(self, labels):
        """Find and click IndiaMART Add Product / Variant controls robustly.

        IndiaMART changes the seller UI frequently.  Search the main page and
        every live iframe using accessible roles, text, aria/title, hrefs and a
        final DOM semantic scan for controls containing add + product/variant.
        """
        if not self.page or self.page.is_closed():
            return False

        expanded = list(labels)
        if any("variant" in str(x).lower() for x in labels):
            expanded += [
                "Add New Variant", "Add Product Variant", "Add a Variant",
                "Create Variant", "New Variant", "Add Similar",
                "Add Similar Product",
            ]
        else:
            expanded += [
                "Add Products", "Add New Products", "Add a New Product",
                "Add Product/Service", "Add New Product/Service",
                "Add Product & Service", "Add New Product & Service",
                "Add Service", "Post Product", "Post a Product",
                "Create Product", "New Product",
            ]
        # Stable de-duplication.
        expanded = list(dict.fromkeys(x for x in expanded if x))

        contexts = [self.page]
        try:
            contexts += [f for f in self.page.frames if f != self.page.main_frame]
        except Exception:
            pass

        href_patterns = [
            "addproduct", "add-product", "add_product", "addprod",
            "addnewproduct", "newproduct", "postproduct", "product/add",
            "product/new", "create-product", "createproduct", "variant",
        ]

        for ctx in contexts:
            for label in expanded:
                for exact in (True, False):
                    for getter in (
                        lambda c=ctx, l=label, e=exact: c.get_by_role("button", name=l, exact=e).first,
                        lambda c=ctx, l=label, e=exact: c.get_by_role("link", name=l, exact=e).first,
                        lambda c=ctx, l=label, e=exact: c.get_by_text(l, exact=e).first,
                    ):
                        try:
                            loc = getter()
                            if loc.count() and loc.is_visible(timeout=500):
                                loc.scroll_into_view_if_needed(timeout=2000)
                                loc.click(timeout=5000)
                                return True
                        except Exception:
                            pass

                safe = str(label).replace('"', '\"')
                for selector in (
                    f'[aria-label*="{safe}" i]',
                    f'[title*="{safe}" i]',
                    f'[data-testid*="{safe}" i]',
                ):
                    try:
                        loc = ctx.locator(selector).first
                        if loc.count() and loc.is_visible(timeout=500):
                            loc.scroll_into_view_if_needed(timeout=2000)
                            loc.click(timeout=5000)
                            return True
                    except Exception:
                        pass

            for pat in href_patterns:
                try:
                    loc = ctx.locator(f'a[href*="{pat}" i]').first
                    if loc.count() and loc.is_visible(timeout=500):
                        loc.scroll_into_view_if_needed(timeout=2000)
                        loc.click(timeout=5000)
                        return True
                except Exception:
                    pass

            # Last-resort semantic DOM scan.  This catches controls whose
            # visible wording changes but still semantically says add/product.
            try:
                loc = ctx.locator(
                    'button, a, [role="button"], input[type="button"], input[type="submit"]'
                )
                for i in range(min(loc.count(), 250)):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible(timeout=80):
                            continue
                        txt = " ".join(filter(None, [
                            (el.inner_text(timeout=150) or "").strip(),
                            (el.get_attribute("value") or "").strip(),
                            (el.get_attribute("aria-label") or "").strip(),
                            (el.get_attribute("title") or "").strip(),
                        ])).lower()
                        href = (el.get_attribute("href") or "").lower()
                        combined = txt + " " + href
                        wants_variant = any("variant" in str(x).lower() for x in labels)
                        semantic_match = (
                            ("add" in combined and ("product" in combined or "service" in combined))
                            or (wants_variant and "variant" in combined)
                            or ("post" in combined and "product" in combined)
                            or ("create" in combined and "product" in combined)
                        )
                        if semantic_match:
                            el.scroll_into_view_if_needed(timeout=2000)
                            el.click(timeout=5000)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
        return False

    def _visible_control_debug(self):
        values = []
        if not self.page or self.page.is_closed():
            return values
        try:
            title = self.page.title()
        except Exception:
            title = ""
        values.append("PAGE=" + str(self.page.url))
        if title:
            values.append("TITLE=" + title)

        contexts = [("MAIN", self.page)]
        try:
            for idx, frame in enumerate(self.page.frames):
                if frame != self.page.main_frame:
                    contexts.append((f"FRAME{idx}", frame))
        except Exception:
            pass

        selectors = 'button, a, [role="button"], input[type="button"], input[type="submit"]'
        for prefix, ctx in contexts:
            try:
                locs = ctx.locator(selectors)
                for i in range(min(locs.count(), 180)):
                    try:
                        loc = locs.nth(i)
                        if not loc.is_visible(timeout=80):
                            continue
                        text = (
                            loc.inner_text(timeout=150)
                            or loc.get_attribute("value")
                            or loc.get_attribute("aria-label")
                            or loc.get_attribute("title")
                            or ""
                        ).strip()
                        href = (loc.get_attribute("href") or "").strip()
                        item = f"{prefix}: " + (text + (" -> " + href if href else "")).strip()
                        if item.strip() and item not in values:
                            values.append(item)
                    except Exception:
                        pass
            except Exception:
                pass
        return values[:120]

    def open_add_form(self, matched):
        labels = (
            [
                "Add Variant",
                "Add variant",
                "Add Similar Product",
                "Add Similar",
                "Add New Variant",
            ]
            if matched
            else [
                "Add Product",
                "Add New Product",
                "+ Add Product",
                "Add a Product",
                "Add New",
            ]
        )

        clicked = self._click_action_control(labels)

        if not clicked and not matched:
            self.event(
                "INDIAMART_FORM",
                "Returning to Manage Products and retrying...",
                None,
            )

            returned = self._click_action_control(
                [
                    "Manage Products",
                    "Products",
                    "My Products",
                    "Product Management",
                ]
            )

            if returned:
                try:
                    self.page.wait_for_timeout(1800)
                except Exception:
                    pass

                clicked = self._click_action_control(labels)

        if not clicked and not matched:
            try:
                self.page.goto(
                    self.url,
                    wait_until="domcontentloaded",
                    timeout=90000,
                )
                self.page.wait_for_timeout(1800)
                clicked = self._click_action_control(labels)
            except Exception:
                pass

        if not clicked:
            debug = self._visible_control_debug()

            self.event(
                "INDIAMART_SELECTOR_DEBUG",
                "Visible controls: " + " | ".join(debug),
                None,
            )

            raise RuntimeError(
                "Could not find the IndiaMART Add Product / Add Variant control. "
                "Check ERROR.png and the INDIAMART_SELECTOR_DEBUG log."
            )

        self.page.wait_for_timeout(1800)


    def _selected_file_names(self, locator):
        """Return filenames currently attached to a browser file input."""
        try:
            return locator.evaluate(
                """
                el => Array.from(el.files || []).map(f => f.name)
                """
            )
        except Exception:
            return []


    def _set_files_any_visibility(self, selectors, files):
        """Attach files and verify the browser accepted the expected filenames."""
        expected_names = [
            Path(path).name
            for path in files
        ]

        for selector in selectors:
            try:
                locs = self.page.locator(selector)

                for i in range(locs.count()):
                    try:
                        loc = locs.nth(i)
                        loc.set_input_files(files)
                        self.page.wait_for_timeout(600)

                        selected_names = self._selected_file_names(loc)

                        selected_lower = [str(name).lower() for name in selected_names]
                        expected_lower = [str(name).lower() for name in expected_names]

                        # V4.1: verify the browser retained the exact file order.
                        # product_1.jpg must remain first/primary, followed by 2-5.
                        if selected_names and selected_lower == expected_lower:
                            return {
                                "ok": True,
                                "selected_names": selected_names,
                                "expected_names": expected_names,
                                "order_verified": True,
                            }

                    except Exception:
                        continue

            except Exception:
                continue

        return {
            "ok": False,
            "selected_names": [],
            "expected_names": expected_names,
        }


    def _upload_pdf_verified(self, pdf_path):
        """Attach the brochure PDF and verify the file input received it."""
        if not pdf_path:
            return {
                "ok": False,
                "reason": "No PDF path supplied.",
            }

        pdf_file = Path(pdf_path)

        if not pdf_file.exists():
            return {
                "ok": False,
                "reason": "PDF file does not exist: " + str(pdf_file),
            }

        expected_name = pdf_file.name.lower()

        try:
            all_files = self.page.locator('input[type="file"]')
            count = all_files.count()
        except Exception as exc:
            return {
                "ok": False,
                "reason": str(exc),
            }

        for i in range(count):
            loc = all_files.nth(i)

            try:
                accept = (
                    loc.get_attribute("accept")
                    or ""
                ).lower()

                if "image" in accept and "pdf" not in accept:
                    continue

                looks_like_document = (
                    "pdf" in accept
                    or "document" in accept
                    or "application" in accept
                )

                if not looks_like_document:
                    continue

                loc.set_input_files(str(pdf_file))
                self.page.wait_for_timeout(600)

                selected_names = self._selected_file_names(loc)
                selected_lower = {
                    str(name).lower()
                    for name in selected_names
                }

                if expected_name in selected_lower:
                    return {
                        "ok": True,
                        "selected_names": selected_names,
                    }

            except Exception:
                continue

        return {
            "ok": False,
            "reason": (
                "No IndiaMART PDF/document input accepted the brochure."
            ),
        }


    def fill_listing(self, content, row, images, pdf_path):
        listing_name = content.get("variant_name") or content.get("listing_name")
        self._fill(["Product Name", "Product/Service Name", "Name"], listing_name)

                # -------------------------------------------------
        # SELLING PRICE
        #
        # Only customer-facing selling price fields are
        # allowed here.
        #
        # IMPORTANT:
        # "Purchase Price" is intentionally NOT included.
        # We must never publish supplier/internal cost.
        # -------------------------------------------------

        price = ""

        price_keys = {
            "price",
            "selling price",
            "product price",
            "rate",
            "unit price",
            "sale price",
            "sales price",
            "offer price",
            "selling rate",
        }

        for key, value in row.items():

            normalized_key = _norm(
                str(key)
            )

            if normalized_key not in price_keys:
                continue

            value_text = str(
                value
                or ""
            ).strip()

            if not value_text:
                continue

            price = value_text
            break

        if price:

            price_filled = self._fill(
                [
                    "Price",
                    "Product Price",
                    "Selling Price",
                    "Selling Price / Unit",
                    "Price / Unit",
                    "Unit Price",
                    "Rate",
                ],
                price,
            )

            if price_filled:
                self.event(
                    "PRICE",
                    (
                        "Selling price filled in "
                        f"IndiaMART: {price}"
                    ),
                    None,
                )

            else:
                self.event(
                    "PRICE",
                    (
                        "Selling price was available "
                        f"({price}) but IndiaMART price "
                        "field was not found."
                    ),
                    None,
                )

        else:
            self.event(
                "PRICE",
                (
                    "No selling price found in source row. "
                    "IndiaMART price was left blank."
                ),
                None,
            )
        description = content.get("description", "")
        if content.get("key_features"):
            description += "\n\nKey Features:\n" + "\n".join(
                "- " + str(x) for x in content["key_features"]
            )
        if content.get("applications"):
            description += "\n\nApplications:\n" + "\n".join(
                "- " + str(x) for x in content["applications"]
            )
        self._fill(["Description", "Product Description"], description)

        for spec in list(content.get("specifications") or [])[:12]:
            if isinstance(spec, dict):
                self._fill([str(spec.get("name", ""))], spec.get("value", ""))

                # =================================================
        # IMAGE UPLOAD + VERIFICATION
        # =================================================

        if images:

            valid_images = []

            for image_path in images:

                path = Path(
                    image_path
                )

                if path.exists():
                    valid_images.append(
                        str(path.resolve())
                    )

            if not valid_images:
                raise RuntimeError(
                    "Product images were supplied, "
                    "but none of the image files exist."
                )

            image_result = (
                self._set_files_any_visibility(
                    [
                        (
                            'input[type="file"]'
                            '[accept*="image" i]'
                        ),
                        (
                            'input[type="file"]'
                            '[multiple]'
                        ),
                    ],
                    valid_images,
                )
            )

            if not image_result.get(
                "ok"
            ):
                raise RuntimeError(
                    "IndiaMART image upload "
                    "verification failed. "
                    "The browser did not retain "
                    "the expected product images."
                )

            self.event(
                "UPLOAD_IMAGES",
                (
                    "Verified "
                    f"{len(valid_images)} "
                    "product image file(s): "
                    + ", ".join(
                        image_result.get(
                            "selected_names",
                            [],
                        )
                    )
                ),
                None,
            )

            self.page.wait_for_timeout(
                1200
            )

        else:

            self.event(
                "UPLOAD_IMAGES",
                (
                    "No product images supplied. "
                    "Image upload was skipped."
                ),
                None,
            )

                # =================================================
        # PDF UPLOAD + VERIFICATION
        # =================================================

        if pdf_path:

            pdf_result = (
                self._upload_pdf_verified(
                    pdf_path
                )
            )

            if not pdf_result.get(
                "ok"
            ):
                raise RuntimeError(
                    "IndiaMART PDF upload "
                    "verification failed: "
                    + str(
                        pdf_result.get(
                            "reason",
                            "Unknown error",
                        )
                    )
                )

            self.event(
                "UPLOAD_PDF",
                (
                    "Product PDF verified: "
                    + ", ".join(
                        pdf_result.get(
                            "selected_names",
                            [],
                        )
                    )
                ),
                None,
            )

            self.page.wait_for_timeout(
                1000
            )

        else:

            self.event(
                "UPLOAD_PDF",
                (
                    "No PDF supplied. "
                    "Brochure upload was skipped."
                ),
                None,
            )

        self.save_session()

    def _click_final_publish_control(self):
        """
        Click only explicit final publish/submit controls.
        Generic Save buttons are intentionally excluded.
        """
        allowed_labels = [
            "Save & Submit",
            "Save and Submit",
            "Submit Product",
            "Submit for Approval",
            "Publish Product",
            "Publish",
            "Post Product",
            "Post Now",
        ]

        for label in allowed_labels:
            try:
                btn = self.page.get_by_role(
                    "button",
                    name=label,
                    exact=True,
                ).first

                if (
                    btn.count()
                    and btn.is_visible(timeout=700)
                    and btn.is_enabled()
                ):
                    btn.scroll_into_view_if_needed()
                    btn.click(timeout=5000)
                    return label
            except Exception:
                pass

            try:
                link = self.page.get_by_role(
                    "link",
                    name=label,
                    exact=True,
                ).first

                if (
                    link.count()
                    and link.is_visible(timeout=700)
                ):
                    link.scroll_into_view_if_needed()
                    link.click(timeout=5000)
                    return label
            except Exception:
                pass

        return ""


    def _verify_publish_success(self, timeout_seconds=20):
        """Verify IndiaMART shows evidence of a successful submission."""
        deadline = time.time() + timeout_seconds

        success_phrases = [
            "product submitted",
            "product added successfully",
            "product published",
            "submitted successfully",
            "successfully added",
            "successfully submitted",
            "product is under review",
            "pending approval",
            "awaiting approval",
        ]

        while time.time() < deadline:
            try:
                body = _norm(
                    self.page.locator("body").inner_text(timeout=1500)
                )

                for phrase in success_phrases:
                    if phrase in body:
                        return {
                            "ok": True,
                            "evidence": phrase,
                        }
            except Exception:
                pass

            try:
                url = (self.page.url or "").lower()

                if (
                    "manageproducts" in url
                    or "manage-products" in url
                ):
                    search_box = self._product_search_box()

                    if search_box:
                        return {
                            "ok": True,
                            "evidence": "Returned to Manage Products page.",
                        }
            except Exception:
                pass

            time.sleep(0.75)

        return {
            "ok": False,
            "evidence": "",
        }


    def finalize(self):
        if self.publish_mode != "auto":
            self.event(
                "INDIAMART_REVIEW",
                (
                    "Listing filled successfully. Automatic publishing is disabled. "
                    "Review the IndiaMART form manually."
                ),
                None,
            )

            return {
                "status": "review_required",
                "message": "Listing is filled and ready for manual review.",
            }

        self.event(
            "AUTO_PUBLISH",
            (
                "Auto Publish is enabled. Searching only for an explicit "
                "final Submit/Publish control."
            ),
            None,
        )

        clicked_label = self._click_final_publish_control()

        if not clicked_label:
            raise RuntimeError(
                "Auto Publish stopped safely. No explicit final Submit/Publish "
                "button was found. Generic Save buttons were ignored."
            )

        self.event(
            "AUTO_PUBLISH",
            "Clicked final control: " + clicked_label,
            None,
        )

        self.page.wait_for_timeout(1500)

        verification = self._verify_publish_success(
            timeout_seconds=20
        )

        if not verification.get("ok"):
            self.event(
                "AUTO_PUBLISH_VERIFY",
                (
                    "Final button was clicked, but IndiaMART success "
                    "could not be verified."
                ),
                None,
            )

            raise RuntimeError(
                "IndiaMART final Submit/Publish button was clicked, "
                "but successful publication could not be verified. "
                "Check the open browser manually."
            )

        evidence = (
            verification.get("evidence")
            or "Success confirmed."
        )

        self.event(
            "AUTO_PUBLISH_VERIFY",
            "IndiaMART submission verified: " + evidence,
            None,
        )

        self.save_session()

        return {
            "status": "published",
            "message": "IndiaMART submission verified.",
            "button": clicked_label,
            "evidence": evidence,
        }


    def wait_until_operator_closes_review(self, max_seconds=3600):
        deadline = time.time() + max_seconds
        while time.time() < deadline:
            if not self.page or self.page.is_closed():
                return
            self.page.wait_for_timeout(700)
        raise RuntimeError("Review browser stayed open for more than one hour.")

    def screenshot(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not self.page or self.page.is_closed():
            return ""
        self.page.screenshot(path=str(path), full_page=True)
        return str(Path(path).resolve())
