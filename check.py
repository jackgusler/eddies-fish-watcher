"""
Eddie's Aquarium Centre stock watcher.

Once a day:
  1. Fetch https://www.eddiesaqua.com/whats-in-stock
  2. Scrape the "in stock as of <date>" text and the View Stock PDF URL.
  3. If the in-stock date string hasn't changed since last run, exit cleanly.
  4. Otherwise: download the PDF, extract text, split into
       - weekly specials (with regular + sale prices)
       - regular stock (fish/invert/plant names)
     Diff against last run. Commit the new extracted stock to the repo
     (which gives us a clickable GitHub diff), then notify via ntfy.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urljoin

import requests
from pypdf import PdfReader

STOCK_PAGE = "https://www.eddiesaqua.com/whats-in-stock"
STATE_FILE = Path("state.json")            # last-seen date + URL
STOCK_FILE = Path("stock.txt")             # normalized list, committed per-change
SPECIALS_FILE = Path("specials.txt")       # normalized specials w/ prices
PDF_CACHE = Path("latest.pdf")             # not committed (in .gitignore)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "")  # e.g. jackgusler/eddies-stock-watcher
COMMIT_SHA = os.environ.get("GITHUB_SHA", "")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(f"[watcher] {msg}", flush=True)


# ---------- Scrape the page -------------------------------------------------


@dataclass
class PageInfo:
    date_text: str       # e.g. "Thursday April 23RD"
    pdf_url: str         # absolute URL to the fish list PDF


def fetch_page() -> PageInfo:
    r = requests.get(STOCK_PAGE, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    html = r.text

    # The page always contains: "These are in stock as of <date>."
    m = re.search(r"in stock as of\s+([^.<\n]+)", html, re.IGNORECASE)
    if not m:
        raise RuntimeError("Could not find 'in stock as of' text on page.")
    date_text = m.group(1).strip().rstrip(".")

    # Find the View Stock link. It points at a /s/NEW-FISH-LIST-*.pdf on the
    # eddiesaqua domain (which then redirects to the Squarespace static host).
    m = re.search(
        r'href="([^"]+/NEW-?FISH-?LIST[^"]*\.pdf)"',
        html,
        re.IGNORECASE,
    )
    if not m:
        # Fall back: any PDF on the page.
        m = re.search(r'href="([^"]+\.pdf)"', html, re.IGNORECASE)
    if not m:
        raise RuntimeError("Could not find a PDF link on page.")

    pdf_url = urljoin(STOCK_PAGE, m.group(1))
    return PageInfo(date_text=date_text, pdf_url=pdf_url)


def download_pdf(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60,
                     allow_redirects=True)
    r.raise_for_status()
    return r.content


# ---------- Parse the PDF ---------------------------------------------------


@dataclass
class Special:
    name: str
    regular: str
    sale: str


def extract_text(pdf_bytes: bytes) -> str:
    PDF_CACHE.write_bytes(pdf_bytes)
    reader = PdfReader(str(PDF_CACHE))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def parse_specials(text: str) -> list[Special]:
    """
    Specials sections look like:
        Freshwater Specials 4/23/2026-4/30/2026 Regular
        Price
        Sale Price
        Sunset Honey Gourami 9.99 ea 7.99 ea
        Aru Picta Rainbow 2/39.99 2/31.99
        ...
        Saltwater Specials ...
        ...
        Select Coral Frags BOGO BUY 1 GET 1 FREE

    After the last specials section, regular stock begins with
    "Freshwater fish, Inverts, and Plants" or similar headers that contain
    no prices. We capture everything that has a $-style token twice on one
    line.
    """
    specials: list[Special] = []
    seen_heading = False
    # Walk line by line, only looking inside blocks that follow a
    # "* Specials *" heading, until we hit the stock list header.
    in_specials = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Start of any specials block
        if re.search(r"\bSpecials\b", line, re.IGNORECASE):
            in_specials = True
            seen_heading = True
            continue
        # End of specials: once we see the regular-stock header
        if re.match(r"Freshwater fish", line, re.IGNORECASE):
            break
        if not in_specials:
            continue

        # Match "<Name> <regular> <sale>", where prices look like
        # "9.99 ea", "2/39.99", "BUY 1 GET 1 FREE", etc.
        # Strategy: find the last two price-like tokens and take everything
        # before them as the name.
        m = re.match(
            r"^(?P<name>.+?)\s+"
            r"(?P<reg>\d+(?:\.\d+)?(?:\s*ea)?|\d+/\d+(?:\.\d+)?|BOGO)\s+"
            r"(?P<sale>\d+(?:\.\d+)?(?:\s*ea)?|\d+/\d+(?:\.\d+)?|"
            r"BUY[^$]*FREE)$",
            line,
        )
        if m:
            specials.append(Special(
                name=m.group("name").strip(),
                regular=m.group("reg").strip(),
                sale=m.group("sale").strip(),
            ))
        # Otherwise ignore — probably "Regular" / "Price" header fragments.

    if not seen_heading:
        log("Warning: no 'Specials' heading found in PDF text.")
    return specials


# Lines we never want to keep as stock items.
_NOISE = re.compile(
    r"^("
    r"Page \d+ of \d+"
    r"|Regular\s*Price"
    r"|Sale\s*Price"
    r"|Freshwater fish.*"
    r"|Saltwater Fish.*"
    r"|Some quantities.*"
    r"|All fish are.*"
    r"|The Eddie.*"
    r"|These are in stock.*"
    r"|New shipment.*"
    r"|LIVE PODS IN STOCK"
    r"|Marine Plants"
    r"|Lighting"
    r")",
    re.IGNORECASE,
)


def parse_stock_items(text: str) -> list[str]:
    """
    Everything after "Freshwater fish, Inverts, and Plants" is the stock.
    Each item is on its own line. Items that are available end with " Y".
    Section headers (e.g. "Angelfish", "CICHLIDS, MALAWI") are lines
    without a trailing Y — we drop those.

    We normalize to: uppercase, trimmed, trailing Y removed.
    """
    items: list[str] = []
    in_stock = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"Freshwater fish", line, re.IGNORECASE):
            in_stock = True
            continue
        if not in_stock:
            continue
        if _NOISE.match(line):
            continue
        # An item line ends with " Y" (availability flag). Anything else is
        # a category header — we skip it.
        if not re.search(r"\sY\s*$", line):
            continue
        name = re.sub(r"\sY\s*$", "", line).strip()
        # Collapse repeated whitespace from PDF extraction quirks.
        name = re.sub(r"\s{2,}", " ", name)
        # Some category headers carry a "Y" due to formatting in the PDF
        # (e.g. "VARIES ASSORTED POTTED PLANTS Y"). Anything that still
        # looks like an item is kept.
        items.append(name)

    # De-dupe while preserving order (a few fish appear twice in the PDF
    # under different category groupings — treat as one listing).
    seen = set()
    unique = []
    for it in items:
        if it not in seen:
            seen.add(it)
            unique.append(it)
    return unique


# ---------- State + diff ----------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(d: dict) -> None:
    STATE_FILE.write_text(json.dumps(d, indent=2) + "\n")


def load_previous_stock() -> set[str]:
    if not STOCK_FILE.exists():
        return set()
    return {ln.strip() for ln in STOCK_FILE.read_text().splitlines() if ln.strip()}


def load_previous_specials() -> dict[str, tuple[str, str]]:
    """Return {name: (regular, sale)}."""
    if not SPECIALS_FILE.exists():
        return {}
    out = {}
    for ln in SPECIALS_FILE.read_text().splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        # format: "<name>\t<regular>\t<sale>"
        parts = ln.split("\t")
        if len(parts) == 3:
            out[parts[0]] = (parts[1], parts[2])
    return out


def write_stock(items: list[str]) -> None:
    STOCK_FILE.write_text("\n".join(sorted(items)) + "\n")


def write_specials(specials: list[Special]) -> None:
    lines = [f"{s.name}\t{s.regular}\t{s.sale}" for s in specials]
    SPECIALS_FILE.write_text("\n".join(sorted(lines)) + "\n")


# ---------- Notify ----------------------------------------------------------


def build_diff_url() -> str:
    if REPO_SLUG and COMMIT_SHA:
        return f"https://github.com/{REPO_SLUG}/commit/{COMMIT_SHA}"
    return ""


def notify(
    date_text: str,
    added: list[str],
    removed: list[str],
    special_changes: list[str],
    diff_url: str,
) -> None:
    if not NTFY_TOPIC:
        log("NTFY_TOPIC not set; skipping notification.")
        return

    summary_bits = []
    if added:
        summary_bits.append(f"{len(added)} added")
    if removed:
        summary_bits.append(f"{len(removed)} removed")
    if special_changes:
        summary_bits.append(f"{len(special_changes)} special price change(s)")

    summary = ", ".join(summary_bits) if summary_bits else "minor update"

    # Body: a taste of the changes (first few of each) + the commit link.
    lines: list[str] = []
    if added:
        lines.append("Added:")
        lines.extend(f"  + {n}" for n in added[:8])
        if len(added) > 8:
            lines.append(f"  …and {len(added) - 8} more")
    if removed:
        if lines:
            lines.append("")
        lines.append("Removed:")
        lines.extend(f"  - {n}" for n in removed[:8])
        if len(removed) > 8:
            lines.append(f"  …and {len(removed) - 8} more")
    if special_changes:
        if lines:
            lines.append("")
        lines.append("Specials:")
        lines.extend(f"  * {c}" for c in special_changes[:8])

    if diff_url:
        if lines:
            lines.append("")
        lines.append(f"Full diff: {diff_url}")

    body = "\n".join(lines) if lines else f"Stock updated as of {date_text}."

    def latin1_safe(s: str) -> str:
        return s.encode("latin-1", errors="replace").decode("latin-1")

    headers = {
        "Title": latin1_safe(f"Eddie's stock update - {date_text}"),
        "Priority": "default",
        "Tags": "tropical_fish",
    }
    if diff_url:
        headers["Click"] = latin1_safe(diff_url)


    r = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    log(f"Sent ntfy notification ({summary}).")


# ---------- Main ------------------------------------------------------------


def main() -> int:
    log(f"Fetching {STOCK_PAGE}")
    info = fetch_page()
    log(f"Page says in-stock as of: {info.date_text!r}")
    log(f"PDF link: {info.pdf_url}")

    state = load_state()
    last_date = state.get("date_text", "")

    if last_date == info.date_text and STOCK_FILE.exists():
        log("No date change since last run. Nothing to do.")
        return 0

    log("Date changed (or first run). Downloading PDF.")
    pdf_bytes = download_pdf(info.pdf_url)
    log(f"Downloaded {len(pdf_bytes)} bytes.")

    text = extract_text(pdf_bytes)
    specials = parse_specials(text)
    stock = parse_stock_items(text)
    log(f"Parsed {len(specials)} specials and {len(stock)} stock items.")

    prev_stock = load_previous_stock()
    prev_specials = load_previous_specials()

    current_stock_set = set(stock)
    added = sorted(current_stock_set - prev_stock)
    removed = sorted(prev_stock - current_stock_set)

    # Specials comparisons
    current_specials = {s.name: (s.regular, s.sale) for s in specials}
    special_changes: list[str] = []
    for name, (reg, sale) in current_specials.items():
        if name not in prev_specials:
            special_changes.append(f"NEW: {name} — {reg} → {sale}")
        elif prev_specials[name] != (reg, sale):
            old_reg, old_sale = prev_specials[name]
            special_changes.append(
                f"{name}: {old_reg}→{old_sale} ⇒ {reg}→{sale}"
            )
    for name in prev_specials:
        if name not in current_specials:
            special_changes.append(f"GONE: {name}")

    log(f"Added: {len(added)}, Removed: {len(removed)}, "
        f"Special changes: {len(special_changes)}")

    # First run: no previous state, don't spam a notification with 300 "adds."
    is_first_run = not prev_stock and not prev_specials
    if is_first_run:
        log("First run — establishing baseline, skipping notification.")
    else:
        if added or removed or special_changes:
            notify(
                info.date_text, added, removed, special_changes,
                diff_url=build_diff_url(),
            )
        else:
            log("Date changed but no substantive content diff detected.")

    # Persist the new baseline for next run.
    write_stock(stock)
    write_specials(specials)
    save_state({
        "date_text": info.date_text,
        "pdf_url": info.pdf_url,
        "stock_count": len(stock),
        "specials_count": len(specials),
    })
    log("State saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())