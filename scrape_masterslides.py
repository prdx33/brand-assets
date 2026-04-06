#!/usr/bin/env python3
"""
Scraper for themasterslides.com/inspiration
Downloads slide deck JPG images and organizes them by company/deck name.

Usage:
    pip install requests beautifulsoup4 playwright
    playwright install chromium
    python scrape_masterslides.py

The script uses Playwright (headless browser) since the site blocks simple HTTP requests.
"""

import os
import re
import sys
import time
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://themasterslides.com/inspiration"
OUTPUT_DIR = Path("master-slides-inspiration")
YEAR_FILTER = {2025, 2026}  # Only download decks from these years
SCROLL_PAUSE = 2.0          # Seconds to wait between scrolls (for lazy loading)
MAX_SCROLLS = 100           # Max scroll attempts to load all content
REQUEST_DELAY = 0.5         # Seconds between image downloads (be polite)
LOG_LEVEL = logging.INFO

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tms-scraper")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are problematic in filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(".- ")
    return name[:200]  # cap length


def make_folder_name(index_no: str, company: str, deck_type: str) -> str:
    """Create a clean folder name like '129-refresh-studio-capabilities-deck'."""
    parts = []
    if index_no:
        parts.append(index_no.zfill(3))
    if company:
        parts.append(sanitize_filename(company))
    if deck_type:
        parts.append(sanitize_filename(deck_type))
    folder = "-".join(parts) if parts else "unknown"
    return re.sub(r"-+", "-", folder).lower().replace(" ", "-")


def download_image(url: str, dest: Path, session) -> bool:
    """Download a single image. Returns True on success."""
    if dest.exists():
        log.debug(f"  Already exists: {dest.name}")
        return True
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type and not url.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        ):
            log.warning(f"  Skipping non-image response: {content_type} for {url}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        log.info(f"  Downloaded: {dest.name} ({len(resp.content) // 1024}KB)")
        return True
    except Exception as e:
        log.error(f"  Failed to download {url}: {e}")
        return False


# ---------------------------------------------------------------------------
# Parsing helpers — tries multiple strategies to extract deck info
# ---------------------------------------------------------------------------


def extract_year_from_text(text: str) -> int | None:
    """Try to find a year (2020-2029) in text."""
    match = re.search(r"\b(202[0-9])\b", text)
    return int(match.group(1)) if match else None


def extract_index_info(text: str) -> tuple[str | None, str | None, str | None]:
    """
    Parse text like '(Research) Index No. 129 Capabilities Deck 2024 – Refresh Studio'
    Returns (index_no, deck_type, company)
    """
    # Pattern: Index No. NNN <deck description> – <company>
    m = re.search(
        r"Index\s+No\.?\s*(\d+)\s+(.+?)(?:\s*[–—-]\s*(.+?))?$",
        text,
        re.IGNORECASE,
    )
    if m:
        index_no = m.group(1)
        deck_desc = m.group(2).strip()
        company = m.group(3).strip() if m.group(3) else None
        return index_no, deck_desc, company
    return None, None, None


def parse_deck_entry(element, page) -> dict | None:
    """
    Given a DOM element representing a deck card/entry, extract metadata.
    This tries multiple selector strategies since we don't know the exact HTML.
    """
    info = {
        "title": "",
        "index_no": None,
        "deck_type": None,
        "company": None,
        "year": None,
        "image_urls": [],
        "link": None,
    }

    # Try to get text content
    try:
        text = element.inner_text(timeout=2000)
    except Exception:
        text = ""

    info["title"] = text.strip()

    # Extract structured info from title text
    idx, deck, company = extract_index_info(text)
    info["index_no"] = idx
    info["deck_type"] = deck
    info["company"] = company
    info["year"] = extract_year_from_text(text)

    # Find all images within this element
    try:
        imgs = element.query_selector_all("img")
        for img in imgs:
            src = img.get_attribute("src") or ""
            srcset = img.get_attribute("srcset") or ""
            data_src = img.get_attribute("data-src") or ""

            # Prefer high-res from srcset
            best_url = ""
            if srcset:
                # Pick the largest from srcset
                parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
                best_w = 0
                for part in parts:
                    if len(part) >= 2 and part[1].endswith("w"):
                        w = int(part[1][:-1])
                        if w > best_w:
                            best_w = w
                            best_url = part[0]
                    elif len(part) >= 1 and not best_url:
                        best_url = part[0]

            url = best_url or data_src or src
            if url and not url.startswith("data:"):
                info["image_urls"].append(url)
    except Exception as e:
        log.debug(f"  Error getting images: {e}")

    # Find link
    try:
        link = element.query_selector("a")
        if link:
            href = link.get_attribute("href")
            if href:
                info["link"] = href
    except Exception:
        pass

    return info if info["image_urls"] else None


# ---------------------------------------------------------------------------
# Main scraping logic
# ---------------------------------------------------------------------------


def scrape_with_playwright():
    """Use Playwright to render the JS-heavy page and extract deck info."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "Playwright is required. Install it:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )
        sys.exit(1)

    import requests

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )

    log.info(f"Launching browser and navigating to {BASE_URL}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Navigate
        page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        log.info("Page loaded. Scrolling to load all content...")

        # Scroll to bottom to trigger lazy loading
        prev_height = 0
        for i in range(MAX_SCROLLS):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(SCROLL_PAUSE)
            curr_height = page.evaluate("document.body.scrollHeight")
            if curr_height == prev_height:
                # Try clicking "Load More" button if it exists
                try:
                    load_more = page.query_selector(
                        'button:has-text("Load More"), '
                        'a:has-text("Load More"), '
                        'button:has-text("Show More"), '
                        '[class*="load-more"], '
                        '[class*="loadMore"]'
                    )
                    if load_more and load_more.is_visible():
                        load_more.click()
                        time.sleep(SCROLL_PAUSE)
                        curr_height = page.evaluate("document.body.scrollHeight")
                    else:
                        log.info(f"Reached bottom after {i + 1} scrolls.")
                        break
                except Exception:
                    log.info(f"Reached bottom after {i + 1} scrolls.")
                    break
            prev_height = curr_height

        # ---------------------------------------------------------------
        # Strategy 1: Look for common card/grid item patterns
        # ---------------------------------------------------------------
        card_selectors = [
            "[class*='card']",
            "[class*='item']",
            "[class*='deck']",
            "[class*='slide']",
            "[class*='post']",
            "[class*='entry']",
            "[class*='grid'] > div",
            "[class*='gallery'] > div",
            "[class*='collection'] > div",
            "article",
            ".inspiration-item",
            "[data-index]",
        ]

        entries = []
        used_selector = None

        for selector in card_selectors:
            try:
                elements = page.query_selector_all(selector)
                if len(elements) >= 5:  # Likely the right selector
                    log.info(
                        f"Found {len(elements)} items with selector: {selector}"
                    )
                    used_selector = selector
                    for el in elements:
                        info = parse_deck_entry(el, page)
                        if info:
                            entries.append(info)
                    if entries:
                        break
            except Exception:
                continue

        # ---------------------------------------------------------------
        # Strategy 2: Fallback — grab ALL images on the page
        # ---------------------------------------------------------------
        if not entries:
            log.info(
                "Card-based extraction didn't work. "
                "Falling back to collecting all images..."
            )
            all_imgs = page.query_selector_all("img")
            log.info(f"Found {len(all_imgs)} images total on page.")

            # Group consecutive images that might belong to the same deck
            current_group = {"image_urls": [], "title": "", "year": None}
            for img in all_imgs:
                src = (
                    img.get_attribute("data-src")
                    or img.get_attribute("src")
                    or ""
                )
                alt = img.get_attribute("alt") or ""
                srcset = img.get_attribute("srcset") or ""

                if not src or src.startswith("data:"):
                    continue

                # Skip tiny images (icons, logos)
                try:
                    width = img.get_attribute("width")
                    height = img.get_attribute("height")
                    if width and int(width) < 100:
                        continue
                    if height and int(height) < 100:
                        continue
                except (ValueError, TypeError):
                    pass

                # Best URL from srcset
                best_url = src
                if srcset:
                    parts = [
                        p.strip().split() for p in srcset.split(",") if p.strip()
                    ]
                    best_w = 0
                    for part in parts:
                        if len(part) >= 2 and part[1].endswith("w"):
                            w = int(part[1][:-1])
                            if w > best_w:
                                best_w = w
                                best_url = part[0]

                # Check if alt text has index info
                idx, deck, company = extract_index_info(alt)
                year = extract_year_from_text(alt)

                if idx:
                    # New deck entry
                    if current_group["image_urls"]:
                        entries.append(current_group)
                    current_group = {
                        "image_urls": [best_url],
                        "title": alt,
                        "index_no": idx,
                        "deck_type": deck,
                        "company": company,
                        "year": year,
                        "link": None,
                    }
                else:
                    current_group["image_urls"].append(best_url)
                    if not current_group.get("title") and alt:
                        current_group["title"] = alt
                    if not current_group.get("year") and year:
                        current_group["year"] = year

            if current_group["image_urls"]:
                entries.append(current_group)

        # ---------------------------------------------------------------
        # Strategy 3: Check for JSON/API data in page scripts
        # ---------------------------------------------------------------
        if not entries:
            log.info("Checking for embedded JSON data...")
            scripts = page.query_selector_all("script")
            for script in scripts:
                try:
                    content = script.inner_text()
                    # Look for JSON arrays/objects with image URLs
                    json_matches = re.findall(
                        r'(?:__NEXT_DATA__|window\.__data__|props|initialData)\s*=\s*({.+?});',
                        content,
                        re.DOTALL,
                    )
                    for json_str in json_matches:
                        try:
                            data = json.loads(json_str)
                            log.info(
                                f"Found embedded JSON data ({len(json_str)} chars)"
                            )
                            # Save raw data for manual inspection
                            raw_path = OUTPUT_DIR / "_raw_data.json"
                            raw_path.parent.mkdir(parents=True, exist_ok=True)
                            raw_path.write_text(
                                json.dumps(data, indent=2, default=str)
                            )
                            log.info(f"Saved raw JSON to {raw_path}")
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    pass

        # ---------------------------------------------------------------
        # Also save the full page HTML for debugging
        # ---------------------------------------------------------------
        debug_dir = OUTPUT_DIR / "_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        html_content = page.content()
        (debug_dir / "page.html").write_text(html_content)
        log.info(f"Saved page HTML to {debug_dir / 'page.html'}")

        # Take a screenshot for reference
        page.screenshot(path=str(debug_dir / "page.png"), full_page=True)
        log.info(f"Saved screenshot to {debug_dir / 'page.png'}")

        browser.close()

    # -------------------------------------------------------------------
    # Filter by year and download
    # -------------------------------------------------------------------
    log.info(f"\nFound {len(entries)} deck entries total.")

    # Filter to 2025/2026
    filtered = []
    no_year = []
    for entry in entries:
        year = entry.get("year")
        if year and year in YEAR_FILTER:
            filtered.append(entry)
        elif not year:
            no_year.append(entry)

    log.info(
        f"  {len(filtered)} entries match years {YEAR_FILTER}"
    )
    log.info(
        f"  {len(no_year)} entries have no detected year (will be included in 'unknown-year' folder)"
    )

    # Include no-year entries in a separate folder (user can sort later)
    all_to_download = filtered + no_year

    if not all_to_download:
        log.warning(
            "No entries found to download. Check _debug/page.html to inspect the page structure.\n"
            "You may need to adjust the card selectors in the script."
        )
        return

    # -------------------------------------------------------------------
    # Download images
    # -------------------------------------------------------------------
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    total_downloaded = 0

    # Save manifest
    manifest = []

    for i, entry in enumerate(all_to_download):
        year = entry.get("year")
        year_str = str(year) if year else "unknown-year"
        index_no = entry.get("index_no", "")
        company = entry.get("company", "")
        deck_type = entry.get("deck_type", "")
        title = entry.get("title", f"deck-{i+1}")

        folder_name = make_folder_name(index_no, company, deck_type)
        if not folder_name or folder_name == "unknown":
            folder_name = sanitize_filename(title)[:80] or f"deck-{i+1:03d}"

        deck_dir = OUTPUT_DIR / year_str / folder_name
        deck_dir.mkdir(parents=True, exist_ok=True)

        log.info(
            f"\n[{i+1}/{len(all_to_download)}] {title}"
        )
        log.info(f"  -> {deck_dir}")

        manifest_entry = {
            "title": title,
            "index_no": index_no,
            "company": company,
            "deck_type": deck_type,
            "year": year,
            "folder": str(deck_dir),
            "images": [],
        }

        for j, img_url in enumerate(entry["image_urls"]):
            # Make URL absolute
            if not img_url.startswith("http"):
                img_url = urljoin(BASE_URL, img_url)

            # Determine file extension
            parsed = urlparse(img_url)
            ext = Path(parsed.path).suffix or ".jpg"
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
                ext = ".jpg"

            filename = f"slide-{j+1:02d}{ext}"
            dest = deck_dir / filename

            if download_image(img_url, dest, session):
                total_downloaded += 1
                manifest_entry["images"].append(
                    {"url": img_url, "file": str(dest)}
                )

            time.sleep(REQUEST_DELAY)

        manifest.append(manifest_entry)

    # Save manifest
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    log.info(f"\nSaved manifest to {manifest_path}")

    log.info(
        f"\nDone! Downloaded {total_downloaded} images into {OUTPUT_DIR}/"
    )
    log.info(f"Deck entries processed: {len(all_to_download)}")


# ---------------------------------------------------------------------------
# Alternative: Simple requests + BeautifulSoup approach
# ---------------------------------------------------------------------------


def scrape_with_requests():
    """
    Simpler approach using requests + BeautifulSoup.
    May not work if the site requires JavaScript rendering.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        log.error("Install dependencies: pip install requests beautifulsoup4")
        sys.exit(1)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    log.info(f"Fetching {BASE_URL} ...")
    resp = session.get(BASE_URL, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Save HTML for debugging
    debug_dir = OUTPUT_DIR / "_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "page.html").write_text(resp.text)
    log.info(f"Saved raw HTML to {debug_dir / 'page.html'}")

    # Check for Next.js data
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data:
        try:
            data = json.loads(next_data.string)
            raw_path = debug_dir / "next_data.json"
            raw_path.write_text(json.dumps(data, indent=2, default=str))
            log.info(f"Found Next.js data! Saved to {raw_path}")
            # Try to extract from pageProps
            page_props = data.get("props", {}).get("pageProps", {})
            log.info(f"pageProps keys: {list(page_props.keys())}")
        except Exception as e:
            log.warning(f"Failed to parse __NEXT_DATA__: {e}")

    # Find all images
    images = soup.find_all("img")
    log.info(f"Found {len(images)} <img> tags")

    for img in images[:10]:
        log.info(f"  src={img.get('src', '')[:100]}")
        log.info(f"  alt={img.get('alt', '')[:100]}")

    if len(images) < 5:
        log.warning(
            "Very few images found. The site likely requires JavaScript.\n"
            "Try the Playwright approach instead:\n"
            "  python scrape_masterslides.py --playwright"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    use_playwright = "--playwright" in sys.argv or "-p" in sys.argv
    use_simple = "--simple" in sys.argv or "-s" in sys.argv

    if use_simple:
        scrape_with_requests()
    elif use_playwright or True:  # Default to Playwright
        scrape_with_playwright()

    print(
        f"\n{'='*60}\n"
        f"Output directory: {OUTPUT_DIR.resolve()}\n"
        f"{'='*60}\n"
        f"\nStructure:\n"
        f"  {OUTPUT_DIR}/\n"
        f"    2025/\n"
        f"      129-refresh-studio-capabilities-deck/\n"
        f"        slide-01.jpg\n"
        f"        slide-02.jpg\n"
        f"      130-equals-series-a-pitch-deck/\n"
        f"        slide-01.jpg\n"
        f"    2026/\n"
        f"      ...\n"
        f"    unknown-year/\n"
        f"      ...\n"
        f"    manifest.json\n"
        f"    _debug/\n"
        f"      page.html\n"
        f"      page.png\n"
    )
