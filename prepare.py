#!/usr/bin/env python3
"""
Prepares Pinterest bulk-upload CSV from the Airtable Pinterest Content Calendar.

Steps:
  1. Parse the Airtable CSV.
  2. For each Pinterest Pin row, pick the best image source:
       a. "Selected Image" attachment URL (Airtable, freshest)
       b. "Ady Attachment" attachment URL (fallback for row 222)
       c. "Image URL" Google Drive URL (last resort)
  3. Download each image to ./images/<ContentID>.jpg.
  4. Emit pinterest_upload.csv shaped per Pinterest spec:
       Title, Media URL, Pinterest board, Thumbnail, Description, Link, Publish date, Keywords
     Media URL is left as a placeholder we patch later (after pushing images to GitHub).
"""

from __future__ import annotations

import csv
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).parent
SRC_CSV = Path("/Users/sashadimitrevic/Downloads/Pintrest Content Calendar-All.csv")
IMAGES_DIR = ROOT / "images"
OUT_CSV = ROOT / "pinterest_upload.csv"
MANIFEST = ROOT / "manifest.csv"  # bookkeeping: content_id, source, download_status

BOARD_NAME = "Beyul Retreat"
PLACEHOLDER_HOST = "__PLACEHOLDER__"  # we'll sed this after pushing to GitHub

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def extract_paren_url(field: str) -> str | None:
    """Pull the first https:// URL inside parentheses from a field like 'foo.jpg (https://...)'."""
    if not field:
        return None
    m = re.search(r"\((https?://[^\s)]+)\)", field)
    return m.group(1) if m else None


def extract_gdrive_id(field: str) -> str | None:
    """Pull a Google Drive file ID from a /file/d/FILE_ID/view-style URL."""
    if not field:
        return None
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)/", field)
    return m.group(1) if m else None


def gdrive_download_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def gdrive_thumbnail_url(file_id: str) -> str:
    # Public viewer/thumbnail endpoint — high-res, doesn't require auth for shared files.
    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w2000"


def download(url: str, dest: Path) -> tuple[bool, str]:
    """Download URL to dest. Returns (ok, message)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            # Google Drive sometimes returns an HTML interstitial instead of the file.
            # If the response looks like HTML, fail so we can fall back.
            head = data[:512].lower()
            if b"<html" in head or b"<!doctype html" in head:
                return False, f"got HTML interstitial ({len(data)} bytes)"
            if len(data) < 1024:
                return False, f"suspiciously small ({len(data)} bytes)"
            dest.write_bytes(data)
            return True, f"ok ({len(data)} bytes)"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    # Cut at the last word boundary before the limit.
    cut = text[: limit - 1]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def parse_title_and_body(caption: str) -> tuple[str, str]:
    """First non-blank line = title; remainder = body."""
    if not caption:
        return "", ""
    lines = caption.split("\n")
    title_line = ""
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            title_line = ln.strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    return title_line, body


def hashtags_to_keywords(hashtag_field: str) -> str:
    """Convert '#Foo #BarBaz' to 'Foo, Bar Baz' (split camelCase for readability)."""
    if not hashtag_field:
        return ""
    tags = re.findall(r"#([A-Za-z0-9_]+)", hashtag_field)
    out = []
    for t in tags:
        # Split camelCase and letter↔digit: "TeamRetreat2026" → "Team Retreat 2026"
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)
        spaced = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", spaced)
        spaced = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", spaced)
        out.append(spaced)
    # Dedupe preserving order.
    seen = set()
    deduped = []
    for k in out:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            deduped.append(k)
    return ", ".join(deduped)


def extract_link_from_caption(caption: str) -> str:
    """Find the beyulretreat.com URL referenced in the caption, if any."""
    if not caption:
        return ""
    m = re.search(r"(?:https?://)?(?:www\.)?beyulretreat\.com[^\s\"'<>]*", caption)
    if m:
        url = m.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        # Clean trailing punctuation.
        url = url.rstrip(".,)]")
        return url
    return ""


def main() -> int:
    IMAGES_DIR.mkdir(exist_ok=True)
    rows_out = []
    manifest_rows = []

    with SRC_CSV.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            platform = (row.get("Platform") or "").strip()
            if platform.lower() != "pinterest":
                continue
            content_id = (row.get("ContentID") or "").strip()
            if not content_id:
                continue

            caption = row.get("Caption/Copy") or ""
            title, body = parse_title_and_body(caption)
            title = truncate(title, 100)

            # Photo credit at end of description (Pinterest desc is 500 chars).
            photo_credit = (row.get("Photo Credit") or "").strip()
            credit_suffix = f"\n\nPhoto: {photo_credit}" if photo_credit else ""
            body_budget = 500 - len(credit_suffix)
            description = truncate(body, max(1, body_budget)) + credit_suffix

            link = extract_link_from_caption(caption) or "https://www.beyulretreat.com"
            keywords = hashtags_to_keywords(row.get("Hashtags") or "")

            # Choose image source.
            selected_img = row.get("Selected Image") or ""
            ady_img = row.get("Ady Attachment") or ""
            # Google Drive URLs land in "Photo Options" for some rows, "Image URL" for others.
            gdrive_field = " ".join([row.get("Image URL") or "", row.get("Photo Options") or ""])

            sources = []
            url_a = extract_paren_url(selected_img)
            if url_a:
                sources.append(("airtable_selected", url_a))
            url_b = extract_paren_url(ady_img)
            if url_b:
                sources.append(("airtable_ady", url_b))
            gd_id = extract_gdrive_id(gdrive_field)
            if gd_id:
                sources.append(("gdrive_thumb", gdrive_thumbnail_url(gd_id)))
                sources.append(("gdrive_download", gdrive_download_url(gd_id)))

            dest = IMAGES_DIR / f"{content_id}.jpg"
            ok = False
            msg = "no source"
            used_source = ""
            if dest.exists() and dest.stat().st_size > 1024:
                ok = True
                msg = f"cached ({dest.stat().st_size} bytes)"
                used_source = "cache"
            else:
                for src_name, src_url in sources:
                    ok, msg = download(src_url, dest)
                    if ok:
                        used_source = src_name
                        break
                    print(f"  [{content_id}] {src_name}: {msg}", file=sys.stderr)

            manifest_rows.append({
                "content_id": content_id,
                "status": "ok" if ok else "FAILED",
                "source": used_source,
                "message": msg,
                "title": title,
            })

            print(f"[{content_id}] {'OK ' if ok else 'FAIL'} via {used_source or '-'}: {msg}")

            # Media URL placeholder — we patch in the final GitHub raw URL after pushing.
            media_url = f"{PLACEHOLDER_HOST}/{content_id}.jpg"

            rows_out.append({
                "Title": title,
                "Media URL": media_url,
                "Pinterest board": BOARD_NAME,
                "Thumbnail": "",
                "Description": description,
                "Link": link,
                "Publish date": "",  # publish immediately per user choice
                "Keywords": keywords,
            })

    # Write the Pinterest CSV.
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["Title", "Media URL", "Pinterest board", "Thumbnail", "Description", "Link", "Publish date", "Keywords"],
        )
        w.writeheader()
        w.writerows(rows_out)

    # Write manifest.
    with MANIFEST.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["content_id", "status", "source", "message", "title"])
        w.writeheader()
        w.writerows(manifest_rows)

    failed = [m for m in manifest_rows if m["status"] != "ok"]
    print(f"\nTotal Pinterest rows: {len(rows_out)}")
    print(f"Images downloaded: {len(manifest_rows) - len(failed)}")
    print(f"Failed downloads: {len(failed)}")
    if failed:
        print("\nFailed rows:")
        for m in failed:
            print(f"  {m['content_id']}: {m['message']} — {m['title']}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
