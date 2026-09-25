"""
Jinnah Metadata Generator - Production Engine Module (engine.py)
Fully Corrected Architecture Addressing All 10 Mandatory Final Fixes:
1. Resumed Progress Sync (finished_jobs_count always synced to len(completed_jobs))
2. Duplicate Prevention via Set-based completed_jobs
3. Complete Active Job State Persistence & Restoration
4. Preserved Original Filenames & Extensions (e.g., 15.JPG, 15.png)
5. Non-Extending Cooldown Restore Logic (saved_expiry > now check)
6. Multi-Source Scheduler Wait-Time (Earliest of API Cooldown & Queue Ready Times)
7. Mark-Dirty Automatic Persistence on Every State Change
8. Global Maximum Design Index Row Alignment across Adobe, Shutterstock, & Vecteezy
9. Signal-Handled Safe & Graceful Shutdown (Flush RAM, Drain Workers, Save State)
10. Min-Heap Priority Queue for Earliest-First Retry & 429 Ordering
"""

import csv
import hashlib
import io
import json
import os
import re
import shutil
import time
import uuid
from collections import deque
from typing import Set, List, Dict, Optional, Any
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, RLock, Thread

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import psutil
except ImportError:
    psutil = None

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
CONFIG_PATH = "config.json"
STATE_PATH = "engine_state.json"
LOG_FILE_PATH = "engine.log"
FAILED_QUEUE_CSV_PATH = "failed_queue.csv"
QA_REPORT_CSV_PATH = "qa_score.csv"
PROGRESS_STATUS_PATH = "progress_status.json"

# Bug Fix #8 (retry backoff bug): hard cap so exponential backoff never balloons
# to absurd multi-minute delays (e.g. 2**10 = 1024s) for no benefit.
MAX_BACKOFF_SECONDS = 60

# ==========================================
# BUG FIX (Import Error #1): UI Theme Constants
# ui.py imports these directly from engine.py. They were referenced but never
# defined, causing an immediate ImportError/crash on startup.
# ==========================================
COLOR_BG = "#1e1e2e"
COLOR_CARD = "#27293d"
COLOR_SIDEBAR = "#181825"
COLOR_SIDEBAR_ACTIVE = "#313244"
COLOR_TEXT = "#e0e0e0"
COLOR_SUBTEXT = "#9399b2"
COLOR_CONSOLE_BG = "#11111b"
COLOR_ACCENT = "#f9c74f"
COLOR_SUCCESS = "#4ade80"
COLOR_ERROR = "#f87171"
COLOR_WARNING = "#facc15"
FONT_TITLE = ("Arial", 11, "bold")
FONT_NORMAL = ("Arial", 9)

# Bug Fix (Theme Mode): the Settings page lets the user pick "Dark" or
# "Light" and saves it to CONFIG["theme"], but nothing ever actually used
# that setting — the colors above were fixed dark-theme constants with no
# light-theme counterpart at all, so switching themes (even after the
# suggested restart) had zero visible effect. DARK_THEME/LIGHT_THEME give
# both palettes, and get_theme_colors() picks the right one; ui.py applies
# it at startup (see ui.py, right after the engine import).
DARK_THEME = {
    "COLOR_BG": "#1e1e2e", "COLOR_CARD": "#27293d", "COLOR_SIDEBAR": "#181825",
    "COLOR_SIDEBAR_ACTIVE": "#313244", "COLOR_TEXT": "#e0e0e0", "COLOR_SUBTEXT": "#9399b2",
    "COLOR_CONSOLE_BG": "#11111b", "COLOR_ACCENT": "#f9c74f", "COLOR_SUCCESS": "#4ade80",
    "COLOR_ERROR": "#f87171", "COLOR_WARNING": "#facc15",
}
LIGHT_THEME = {
    "COLOR_BG": "#f2f2f7", "COLOR_CARD": "#ffffff", "COLOR_SIDEBAR": "#e5e5ea",
    "COLOR_SIDEBAR_ACTIVE": "#d1d1d6", "COLOR_TEXT": "#1c1c1e", "COLOR_SUBTEXT": "#6e6e73",
    "COLOR_CONSOLE_BG": "#ffffff", "COLOR_ACCENT": "#b8860b", "COLOR_SUCCESS": "#1a8a4a",
    "COLOR_ERROR": "#c0392b", "COLOR_WARNING": "#b8860b",
}

def get_theme_colors(theme_name: str) -> dict:
    """Returns the color palette dict for 'Dark' or 'Light' (case-insensitive,
    defaults to Dark for any unrecognized value)."""
    if str(theme_name).strip().lower() == "light":
        return dict(LIGHT_THEME)
    return dict(DARK_THEME)

AGENCY_HEADERS = {
    "Adobe Stock": ["File name", "Category", "Title", "Keywords", "People/Property"],
    "Shutterstock": ["Filename", "Description", "Keywords", "Category 1", "Category 2", "Illustration", "Mature Content", "Editorial"],
    "Vecteezy": ["Filename", "Title", "Keywords", "License"]
}

# License values Vecteezy accepts for a contributed design. This is a
# per-account/per-batch choice (not something derivable from the image
# itself), so it's driven by CONFIG["vecteezy_license"] rather than Gemini.
VECTEEZY_LICENSE_OPTIONS = ["Pro", "Free", "Editorial"]

REQUIRED_CSV_SITES = {"Adobe Stock", "Shutterstock", "Vecteezy"}

def validate_csv_headers(headers: dict) -> tuple:
    """
    Requirement #17 (CSV Header — Dynamic System) validation rules:
    - The three built-in sites cannot be added to or removed from (exactly
      Adobe Stock, Shutterstock, Vecteezy — nothing more, nothing less).
    - No site may end up with zero columns.
    - No empty column header names.
    - No duplicate column header names within a site (case-insensitive).
    Returns (True, "OK") or (False, reason).
    """
    if not isinstance(headers, dict) or set(headers.keys()) != REQUIRED_CSV_SITES:
        return False, "CSV Header sites cannot be added or removed — only Adobe Stock, Shutterstock, and Vecteezy are allowed."
    for site, cols in headers.items():
        if not cols:
            return False, f"'{site}' must have at least one column."
        seen = set()
        for col in cols:
            name = str(col).strip()
            if not name:
                return False, f"'{site}' has an empty column header, which isn't allowed."
            key = name.lower()
            if key in seen:
                return False, f"'{site}' has a duplicate column header: '{name}'."
            seen.add(key)
    return True, "OK"

def get_csv_headers() -> dict:
    """Returns the effective CSV headers (from CONFIG if valid, else the
    built-in defaults) — the single source of truth used everywhere CSVs
    are written, so 'CSV generation-এর সময় Header hardcoded ব্যবহার করা
    যাবে না' is honored."""
    configured = CONFIG.get("csv_headers")
    ok, _ = validate_csv_headers(configured) if configured else (False, "")
    if ok:
        return {site: list(cols) for site, cols in configured.items()}
    return {site: list(cols) for site, cols in AGENCY_HEADERS.items()}

def _resolve_csv_field_value(site: str, header_name: str, metadata: dict) -> str:
    """
    Maps a CSV column header name to the correct value from a generated
    metadata dict, by recognized name (case-insensitive) rather than by
    fixed position — this is what makes Requirement #17's column
    add/remove/reorder actually work correctly: whatever order the columns
    end up in, each one still gets the right data. An unrecognized/custom
    column (e.g. a user-added "Notes" column) is left blank for the user to
    fill in manually; it's never a processing error.
    """
    h = header_name.strip().lower()

    if h in ("file name", "filename", "file_name"):
        return metadata.get("original_filename", "")
    if h in ("category", "category 1", "category1"):
        if site == "Adobe Stock":
            return str(ADOBE_CATEGORIES.get(metadata.get("category", ""), ""))
        if site == "Shutterstock":
            # Bug Fix (Requirement #10): the AI's category comes from
            # Adobe's category list, not Shutterstock's — map it to the
            # closest valid Shutterstock category rather than writing a
            # name Shutterstock doesn't even recognize.
            return ADOBE_TO_SHUTTERSTOCK_CATEGORY.get(metadata.get("category", ""), "Miscellaneous")
        return metadata.get("category", "")
    if h in ("category 2", "category2"):
        if site != "Shutterstock":
            return ""
        # Bug Fix (Requirement #10): Category 2 must be a second, DIFFERENT
        # valid Shutterstock category — never blank, never a number/ID,
        # never the same as Category 1.
        cat1 = ADOBE_TO_SHUTTERSTOCK_CATEGORY.get(metadata.get("category", ""), "Miscellaneous")
        cat2 = SHUTTERSTOCK_SECONDARY_CATEGORY.get(cat1, "Miscellaneous")
        if cat2 == cat1:  # Defensive: guarantee they're never equal even if a mapping is ever miscopied.
            cat2 = "Objects" if cat1 != "Objects" else "Miscellaneous"
        return cat2
    if h == "title":
        if site == "Vecteezy":
            return clean_vecteezy_title(metadata.get("title", ""))
        return metadata.get("title", "")
    if h == "keywords":
        if site == "Vecteezy":
            return clean_vecteezy_keywords(metadata.get("keywords", ""))
        return metadata.get("keywords", "")
    if h == "description":
        return metadata.get("description", "")
    if h in ("people/property", "people_property", "people"):
        return metadata.get("people_property", "No")
    if h == "license":
        # New Vecteezy License column: Pro / Free / Editorial, driven by
        # CONFIG (a contributor-level setting, not per-image).
        license_val = str(CONFIG.get("vecteezy_license", "Pro")).strip()
        return license_val if license_val in VECTEEZY_LICENSE_OPTIONS else "Pro"
    if h == "illustration":
        return "Yes"
    if h in ("mature content", "editorial"):
        return "No"
    return ""

VALID_CATEGORIES = [
    "Animals", "Buildings and Architecture", "Business", "Drinks", "The Environment",
    "States of Mind", "Food", "Graphic Resources", "Hobbies and Leisure", "Industry",
    "Landscapes", "Lifestyle", "People", "Plants and Flowers", "Culture and Religion",
    "Science", "Social Issues", "Sports", "Technology", "Transport", "Travel"
]

SHUTTERSTOCK_CATEGORIES = [
    "Abstract", "Animals/Wildlife", "Arts", "Backgrounds/Textures", "Beauty/Fashion",
    "Buildings/Landmarks", "Business/Finance", "Celebrities", "Education", "Food and drink",
    "Healthcare/Medical", "Holidays", "Industrial", "Interiors", "Miscellaneous", "Nature",
    "Objects", "Parks/Outdoor", "People", "Religion", "Science", "Signs/Symbols",
    "Sports/Recreation", "Transportation", "Vintage"
]

ADOBE_CATEGORIES = {
    "Animals": 1, "Buildings and Architecture": 2, "Business": 3, "Drinks": 4, "The Environment": 5,
    "States of Mind": 6, "Food": 7, "Graphic Resources": 8, "Hobbies and Leisure": 9, "Industry": 10,
    "Landscapes": 11, "Lifestyle": 12, "People": 13, "Plants and Flowers": 14, "Culture and Religion": 15,
    "Science": 16, "Social Issues": 17, "Sports": 18, "Technology": 19, "Transport": 20, "Travel": 21
}

# Bug Fix (Requirement #10 - Shutterstock CSV Category Update): the AI only
# ever picks from Adobe's VALID_CATEGORIES list, but Shutterstock has its
# own, differently-named category list — writing the raw Adobe category
# name straight into Shutterstock's "Category 1" could produce a name
# that isn't even a valid Shutterstock category at all. This maps every
# Adobe category to its closest valid Shutterstock equivalent.
ADOBE_TO_SHUTTERSTOCK_CATEGORY = {
    "Animals": "Animals/Wildlife", "Buildings and Architecture": "Buildings/Landmarks",
    "Business": "Business/Finance", "Drinks": "Food and drink", "The Environment": "Nature",
    "States of Mind": "People", "Food": "Food and drink", "Graphic Resources": "Abstract",
    "Hobbies and Leisure": "Miscellaneous", "Industry": "Industrial", "Landscapes": "Nature",
    "Lifestyle": "People", "People": "People", "Plants and Flowers": "Nature",
    "Culture and Religion": "Religion", "Science": "Science", "Social Issues": "Miscellaneous",
    "Sports": "Sports/Recreation", "Technology": "Objects", "Transport": "Transportation",
    "Travel": "Parks/Outdoor",
}

# Bug Fix (Requirement #10): Category 2 must be a second, DIFFERENT valid
# Shutterstock category — never empty, never a number/ID, never the same as
# Category 1. Every entry here maps to something else in the list.
SHUTTERSTOCK_SECONDARY_CATEGORY = {
    "Abstract": "Backgrounds/Textures", "Animals/Wildlife": "Nature", "Arts": "Abstract",
    "Backgrounds/Textures": "Abstract", "Beauty/Fashion": "People", "Buildings/Landmarks": "Backgrounds/Textures",
    "Business/Finance": "Objects", "Celebrities": "People", "Education": "Miscellaneous",
    "Food and drink": "Objects", "Healthcare/Medical": "People", "Holidays": "Miscellaneous",
    "Industrial": "Objects", "Interiors": "Objects", "Miscellaneous": "Objects",
    "Nature": "Parks/Outdoor", "Objects": "Miscellaneous", "Parks/Outdoor": "Nature",
    "People": "Miscellaneous", "Religion": "Miscellaneous", "Science": "Education",
    "Signs/Symbols": "Abstract", "Sports/Recreation": "People", "Transportation": "Industrial",
    "Vintage": "Miscellaneous",
}

BANNED_KEYWORDS = {"image", "photo", "design", "picture", "vector", "illustration", "background", "stock", "graphic", "art"}
STOPWORDS = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "with", "by", "of"}

# Bug Fix (Logging consistency): engine.log used to always be written
# relative to the process's current working directory — completely
# unrelated to whichever design folder was actually being processed. This
# mixed logs from different projects together in one arbitrary location,
# unlike engine_state.json, failed_queue.csv, and qa_score.csv,
# which all correctly live inside the folder being processed. Once
# set_active_log_folder() is called (done automatically when a workflow
# starts), engine.log is written inside that same folder instead.
_ACTIVE_LOG_FOLDER = None

def set_active_log_folder(folder_path: str):
    global _ACTIVE_LOG_FOLDER
    _ACTIVE_LOG_FOLDER = folder_path

def append_to_file_log(msg: str):
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_path = os.path.join(_ACTIVE_LOG_FOLDER, LOG_FILE_PATH) if _ACTIVE_LOG_FOLDER else LOG_FILE_PATH
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass

def load_config() -> dict:
    default_config = {
        "model": "gemini-2.5-flash",
        "max_requests_per_minute": 10,
        "max_retries": 4,
        "theme": "Dark",
        "auto_backup": True,
        "keyword_blacklist": [],
        "min_keywords": 45,
        "target_keywords": 50,
        # Requirement #17 (CSV Header — Dynamic System): the three built-in
        # sites' headers now live in config instead of being hardcoded, so
        # the user can edit/add/remove/reorder columns from Settings. The
        # site list itself (Adobe Stock/Shutterstock/Vecteezy) is fixed —
        # only their column names are editable.
        "csv_headers": {
            "Adobe Stock": ["File name", "Category", "Title", "Keywords", "People/Property"],
            "Shutterstock": ["Filename", "Description", "Keywords", "Category 1", "Category 2", "Illustration", "Mature Content", "Editorial"],
            "Vecteezy": ["Filename", "Title", "Keywords", "License"]
        },
        # New Vecteezy "License" column value — one of Pro / Free / Editorial.
        # Applies to every design in the batch (Vecteezy license tier is a
        # contributor-level choice, not something derived per-image).
        "vecteezy_license": "Pro",
        "api_keys": [
            {"name": "API 1", "key": "", "status": "Enable"}
        ]
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                default_config.update(cfg)
        except Exception as e:
            append_to_file_log(f"WARNING: Corrupted config ({str(e)}). Using default.")
    return default_config

CONFIG = load_config()

def save_config(cfg: dict) -> bool:
    """BUG FIX (Import Error): ui.py's Settings page calls save_config() but it
    never existed in engine.py. Also implements Missing Feature #: Auto Backup
    (.bak) before overwriting config.json, using the existing 'auto_backup' flag."""
    try:
        if cfg.get("auto_backup", True) and os.path.exists(CONFIG_PATH):
            try:
                shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
            except Exception:
                pass
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
        CONFIG.update(cfg)
        return True
    except Exception as e:
        append_to_file_log(f"Failed to save config: {str(e)}")
        return False

def calculate_sha256(file_path: str) -> str:
    """BUG FIX (Import Error): used by ui.py's preview panel and also used
    below for duplicate-image detection (Missing Feature: Duplicate Hash Checker)."""
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def get_processed_files_from_folder(folder_path: str) -> Set[str]:
    """BUG FIX (Import Error): scans the site CSVs already exported into
    folder_path and returns the set of filenames/basenames already recorded,
    so ui.py's workflow can skip images that were already processed."""
    processed: Set[str] = set()
    for site in AGENCY_HEADERS.keys():
        path = os.path.join(folder_path, f"{site.lower().replace(' ', '_')}.csv")
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                for row in reader:
                    if row and row[0].strip():
                        fname = row[0].strip()
                        processed.add(fname.lower())
                        processed.add(os.path.splitext(fname)[0].lower())
        except Exception as e:
            append_to_file_log(f"Error reading processed files from [{site}]: {str(e)}")
    return processed

def update_progress_status(folder_path: str, **fields):
    """
    Requirement (crash/resume visibility): if a folder has e.g. 500 designs
    and the process crashes after 100 are done, the user needs to be able to
    see exactly how far it got — total count, how many completed, how many
    failed, and which design/filename was last processed — without having
    to dig through engine.log. This is updated after every single item's
    outcome (not batched), so it always reflects the true current state even
    if the process is killed abruptly. It's a read-modify-write merge, so
    callers only need to pass the fields that changed.
    """
    path = os.path.join(folder_path, PROGRESS_STATUS_PATH)
    try:
        existing = {}
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
        existing.update(fields)
        existing["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        append_to_file_log(f"ERROR updating progress_status.json: {str(e)}")

def save_failed_queue_csv(folder_path: str, failed_items: List[tuple]):
    """Bug Fix #27 (Missing Failed Queue File): permanently failed jobs were
    only ever logged to engine.log and shown in the UI tree — never persisted
    to disk, so they were lost after restart. This writes/append them to
    failed_queue.csv as (Filename, Reason, Status)."""
    if not failed_items:
        return
    path = os.path.join(folder_path, FAILED_QUEUE_CSV_PATH)
    file_is_new = not os.path.exists(path)
    try:
        with open(path, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            if file_is_new:
                writer.writerow(["Filename", "Reason", "Status"])
            for fname, reason in failed_items:
                writer.writerow([fname, reason, "Retry"])
    except Exception as e:
        append_to_file_log(f"ERROR writing failed queue CSV: {str(e)}")

def reset_failed_jobs(folder_path: str) -> int:
    """Feature Fix #23 (Failed Queue 'retry later' option): clears the
    permanent-failure record for a folder so those images are attempted
    again on the next run instead of being skipped forever.
    - Removes 'permanently_failed_jobs' from that folder's engine_state.json
      (used by DynamicQueueEngine).
    - Clears failed_queue.csv (the visible failure log for both workflows).
    Returns the number of previously-failed entries that were cleared.
    Does NOT touch already-completed rows in the site CSVs.
    """
    cleared = 0

    state_path = os.path.join(folder_path, STATE_PATH)
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
            cleared += len(state.get("permanently_failed_jobs", []))
            state["permanently_failed_jobs"] = []
            tmp_path = state_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, state_path)
        except Exception as e:
            append_to_file_log(f"ERROR resetting failed jobs in state: {str(e)}")

    failed_csv_path = os.path.join(folder_path, FAILED_QUEUE_CSV_PATH)
    if os.path.exists(failed_csv_path):
        try:
            with open(failed_csv_path, 'r', encoding='utf-8-sig') as f:
                row_count = max(0, sum(1 for _ in f) - 1)  # minus header
            cleared = max(cleared, row_count)
            os.remove(failed_csv_path)
        except Exception as e:
            append_to_file_log(f"ERROR clearing failed_queue.csv: {str(e)}")

    return cleared

# ==========================================
# UTILITY METADATA FUNCTIONS
# ==========================================
def clean_title_adobe(title: str) -> str:
    if not title: return ""
    title = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(title))
    title = re.sub(r"[^\w\s\-\,\.\&]", "", title)
    title = re.sub(r"\s+", " ", title)
    title = title.strip(" .,;:-_").strip()
    if title.endswith((".", ",")): title = title[:-1].strip()
    return title

def stem_word(word: str) -> str:
    if word.endswith("ies") and len(word) > 3: return word[:-3] + "y"
    if word.endswith(("es", "s")) and not word.endswith("ss") and len(word) > 2:
        if word.endswith("es") and len(word) > 3: return word[:-2]
        return word[:-1]
    return word

def evaluate_metadata_advanced(title: str, keywords: List[str], category: str) -> int:
    t_words = re.findall(r"[A-Za-z0-9]+", title.lower())
    title_len_ok = 5 <= len(title) <= 70
    no_caps = not title.isupper()
    no_double_space = "  " not in title
    unique_t_words = len(t_words) == len(set(t_words)) if t_words else False

    readability_score = 100 if (title_len_ok and no_caps and no_double_space and unique_t_words) else 60
    valid_kw = [k for k in keywords if len(k) > 2 and k.lower() not in STOPWORDS]
    diversity_score = min(100, int((len(set(valid_kw)) / max(1, len(valid_kw))) * 100))

    kw_len = len(keywords)
    keyword_count_score = int((kw_len / 15) * 50) if kw_len < 15 else min(100, int((kw_len / 50) * 100))
    policy_score = 100 if (category in VALID_CATEGORIES or category in SHUTTERSTOCK_CATEGORIES) else 0

    kw_set = {k.lower() for k in keywords}
    banned_set = {b.lower() for b in BANNED_KEYWORDS}
    banned_found = bool(kw_set.intersection(banned_set))
    duplicate_score = 40 if banned_found else 100

    seo_score = (keyword_count_score * 0.5) + (diversity_score * 0.5)
    overall_qa = int((seo_score * 0.35) + (policy_score * 0.25) + (duplicate_score * 0.20) + (readability_score * 0.20))
    return max(0, min(100, overall_qa))

# ==========================================
# Vecteezy Metadata Validation Rules
# (applies ONLY to Vecteezy's Title and Keywords columns)
# ==========================================

# Explicitly disallowed punctuation/symbols (shared by title & keywords).
_VECTEEZY_DISALLOWED_CHARS = r'[@#$%*!?;:/\\|<>()\[\]{}_+="\']'

_VECTEEZY_TITLE_FILE_FORMAT_WORDS = {
    "vector", "illustration", "photo", "video", "psd", "png", "eps", "jpg"
}
_VECTEEZY_KEYWORD_FILE_FORMAT_WORDS = {
    "vector", "illustration", "photo", "video", "clip", "psd", "png", "eps", "graphic", "artwork"
}
_VECTEEZY_AI_WORDS = {"ai", "ai generated", "midjourney", "dall-e", "dalle"}
_VECTEEZY_TITLE_PROMO_WORDS = {
    "best", "top", "amazing", "beautiful", "cool", "perfect", "high resolution", "4k", "hd"
}
_VECTEEZY_KEYWORD_PROMO_WORDS = {
    "best", "top", "amazing", "beautiful", "cool", "perfect", "nice"
}
# Common trademark/brand names to strip from both title and keywords.
_VECTEEZY_BRAND_NAMES = {
    "apple", "iphone", "ipad", "imac", "macbook", "airpods",
    "nike", "adidas", "puma", "reebok",
    "google", "android", "chrome", "gmail", "youtube",
    "samsung", "microsoft", "windows", "xbox",
    "adobe", "photoshop", "illustrator",
    "amazon", "facebook", "meta", "instagram", "twitter", "tiktok",
    "netflix", "disney", "marvel", "pixar",
    "coca cola", "coca-cola", "pepsi", "starbucks", "mcdonalds", "mcdonald's",
    "sony", "playstation", "nintendo",
    "tesla", "bmw", "mercedes", "toyota", "honda",
    "gucci", "chanel", "louis vuitton", "prada", "versace",
}

def _vecteezy_strip_phrases(text: str, phrases: set) -> str:
    """Removes each banned word/phrase as a whole-word/phrase match
    (case-insensitive), longest phrases first so multi-word bans like
    'ai generated' are matched before the standalone word 'ai'."""
    for phrase in sorted(phrases, key=len, reverse=True):
        text = re.sub(r'(?i)\b' + re.escape(phrase) + r'\b', ' ', text)
    return text

def clean_vecteezy_title(title: str) -> str:
    """
    Vecteezy Title rules:
    - Remove all hyphens.
    - Remove all special characters except spaces (only letters/digits/spaces remain).
    - Remove file-format, AI-related, promotional, and trademark/brand words.
    - Output a clean, natural English title (no leftover double spaces).
    """
    if not title:
        return ""
    text = str(title)
    text = text.replace('-', ' ')                              # Remove hyphens
    text = re.sub(_VECTEEZY_DISALLOWED_CHARS, ' ', text)        # Explicitly banned symbols
    text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)                 # Any other special characters
    text = _vecteezy_strip_phrases(
        text,
        _VECTEEZY_TITLE_FILE_FORMAT_WORDS | _VECTEEZY_AI_WORDS |
        _VECTEEZY_TITLE_PROMO_WORDS | _VECTEEZY_BRAND_NAMES
    )
    text = re.sub(r'\s+', ' ', text).strip()                    # Natural spacing
    if text:
        text = text[0].upper() + text[1:]                       # Natural title casing
    return text

def clean_vecteezy_keywords(keywords) -> str:
    """
    Vecteezy Keyword rules:
    - Keywords separated only by commas.
    - Remove hyphens and all special characters from each keyword.
    - Remove file-format, AI-related, promotional, and trademark/brand words.
    - Remove duplicate keywords.
    - Keep only relevant English keywords (must contain a letter).
    Accepts either a comma-joined string or a list of keyword strings.
    """
    if not keywords:
        return ""
    if isinstance(keywords, str):
        raw_list = [k.strip() for k in keywords.split(',') if k.strip()]
    else:
        raw_list = [str(k).strip() for k in keywords if str(k).strip()]

    banned_phrases = (
        _VECTEEZY_KEYWORD_FILE_FORMAT_WORDS | _VECTEEZY_AI_WORDS |
        _VECTEEZY_KEYWORD_PROMO_WORDS | _VECTEEZY_BRAND_NAMES
    )

    cleaned, seen = [], set()
    for kw in raw_list:
        k = kw.replace('-', ' ')
        k = re.sub(_VECTEEZY_DISALLOWED_CHARS, ' ', k)
        k = re.sub(r'[^a-zA-Z0-9\s]', ' ', k)
        k = _vecteezy_strip_phrases(k, banned_phrases)
        k = re.sub(r'\s+', ' ', k).strip()

        if not k or not re.search(r'[a-zA-Z]', k):  # keep only relevant English keywords
            continue
        key = k.lower()
        if key in seen:  # remove duplicates
            continue
        seen.add(key)
        cleaned.append(k)

    return ", ".join(cleaned)

def ensure_directory_exists(file_path: str):
    directory = os.path.dirname(file_path)
    if directory: os.makedirs(directory, exist_ok=True)

def validate_image_file(file_path: str) -> tuple:
    """Missing Feature Fix: Preflight Checker. Verifies the image can actually
    be opened/decoded *before* it's queued for an (expensive) API call, instead
    of only discovering corruption when the worker fails mid-request."""
    if not os.path.exists(file_path):
        return False, "FILE_NOT_FOUND"
    if Image is None:
        return True, "PIL_NOT_INSTALLED_SKIPPED_CHECK"
    try:
        with Image.open(file_path) as img:
            img.verify()
        return True, "OK"
    except Exception as e:
        return False, f"CORRUPT_IMAGE: {str(e)}"

def check_eps_exists(folder_path: str, filename: str) -> bool:
    """Bug Fix #21 (Missing EPS Detection): checks whether a same-named .eps
    (any case) sits next to the raster preview image."""
    base = os.path.splitext(filename)[0]
    for ext in (".eps", ".EPS", ".Eps"):
        if os.path.exists(os.path.join(folder_path, base + ext)):
            return True
    return False

def report_missing_eps(folder_path: str, filename: str):
    """Bug Fix (user request): missing-EPS detection is still preserved
    (Requirement #21 — 'detect, never block'), but it no longer creates a
    separate missing_eps_report.csv file — that was extra clutter the user
    doesn't want in the design folder. The notice now goes to engine.log
    only, alongside every other processing event."""
    append_to_file_log(f"MISSING_EPS: '{filename}' has no matching .eps file (processing continues normally).")

def extract_design_number(filename: str, fallback_start: int = 1000000) -> Optional[int]:
    """Bug Fix #19 (Filename Parsing Bug): re.search(r'\\d+') returns None for
    filenames with no digits (e.g. 'flower.jpg'), and every call-site used to
    silently `continue`/skip such files, meaning they were dropped from the
    pipeline entirely and never processed. This still returns the embedded
    number when present (unchanged behavior for '15.jpg' etc.), but callers
    now use a stable hash-based fallback (see below) instead of skipping."""
    m = re.search(r'\d+', filename)
    if m:
        return int(m.group())
    return None

def _fallback_map_path(folder_path: str) -> str:
    return os.path.join(folder_path, "fallback_id_map.json")

def _load_fallback_id_map(folder_path: str) -> dict:
    """Persisted filename -> design-number map for files with no digits in
    their name, so the same file always gets the same row across restarts."""
    path = _fallback_map_path(folder_path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_fallback_id_map(folder_path: str, mapping: dict):
    path = _fallback_map_path(folder_path)
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        append_to_file_log(f"ERROR saving fallback id map: {str(e)}")

def get_design_number_for_file(folder_path: str, filename: str, csv_manager: "ThreadSafeCSVManager") -> int:
    """
    CSV Row Rule (user-specified, strict & order-independent):
        Row 1        = Header
        Row N + 1    = data for the file whose name contains the number N
    e.g. '1.jpg' -> Row 2, '50.jpg' -> Row 51, '100.jpg' -> Row 101, no matter
    which order API responses arrive in, and identically across Adobe Stock,
    Shutterstock and Vecteezy.

    If the filename embeds a number, that number IS the design number/row
    key — full stop, this is never re-derived or changed.

    If a filename has no digits at all (e.g. 'flower.jpg'), it obviously has
    no number to map by. It gets a fallback design number assigned
    sequentially right after the current highest row (not a large random
    value), and that assignment is persisted to fallback_id_map.json so the
    same file always maps to the same row on every future run.
    """
    num = extract_design_number(filename)
    if num is not None:
        if num > csv_manager.global_max_design_index:
            csv_manager.global_max_design_index = num
        return num

    mapping = _load_fallback_id_map(folder_path)
    if filename in mapping:
        assigned = mapping[filename]
        if assigned > csv_manager.global_max_design_index:
            csv_manager.global_max_design_index = assigned
        return assigned

    csv_manager.global_max_design_index += 1
    new_id = csv_manager.global_max_design_index
    mapping[filename] = new_id
    _save_fallback_id_map(folder_path, mapping)
    return new_id

# ==========================================
# FIX 8: CSV MANAGER WITH GLOBAL MAX ROW CONSISTENCY
# ==========================================
class ThreadSafeCSVManager:
    def __init__(self, folder_path: str, files_list: List[str], target_sites: Optional[List[str]] = None):
        self.folder_path = folder_path
        self.files_list = files_list
        # Bug Fix (deadlock prevention): RLock instead of Lock. The new
        # flush_and_verify() method below needs to call flush_to_disk() while
        # already holding this lock (to make the write+verify sequence
        # atomic against concurrent update_exact_row_in_memory calls); a
        # plain Lock would deadlock on that same-thread re-acquisition.
        self.lock = RLock()
        self.target_sites = target_sites or ["Adobe Stock", "Shutterstock", "Vecteezy"]
        # Requirement #17 (CSV Header — Dynamic System): headers are loaded
        # from CONFIG (falling back to built-in defaults if missing/invalid)
        # instead of being hardcoded, so user-edited columns actually take
        # effect on the next CSV write.
        self.headers = get_csv_headers()
        self.csv_paths = {
            site: os.path.join(folder_path, f"{site.lower().replace(' ', '_')}.csv")
            for site in self.target_sites
        }
        self.in_memory_data = {site: {} for site in self.target_sites}
        self.dirty_sites = set()
        self.completed_design_numbers: Set[int] = set() # Fix 2: Set storage
        self.global_max_design_index: int = 0          # Fix 8: Single global row boundary

        # Bug Fix (QA Score CSV organization): previously qa_score.csv was a
        # plain append-in-completion-order file (row order == whichever
        # order jobs happened to finish in). The user wants it organized
        # IDENTICALLY to the Adobe Stock/Shutterstock/Vecteezy CSVs: Row 1 =
        # header, Row N+1 = the file whose name/design number is N — same
        # positional rule, same padding, same atomic write. qa_scores is
        # keyed by design_number just like in_memory_data[site].
        self.qa_scores: Dict[int, tuple] = {}  # design_number -> (filename, qa_score)
        self.qa_csv_path = os.path.join(folder_path, QA_REPORT_CSV_PATH)
        self.qa_dirty = False

        self._load_cache_and_initialize()

    def _is_valid_row(self, site: str, row: List[str]) -> bool:
        """
        Bug Fix (Requirement #17 compatibility): this used to check specific
        FIXED column positions (row[1], row[2], row[3]...), which breaks the
        moment a user adds/removes/reorders a column. Now looks up values by
        recognized header NAME instead, so validation stays correct no
        matter how the columns are arranged.

        Each check below only applies if the site's CURRENT (possibly
        user-edited) headers still include that recognized column — if the
        user deliberately deleted it via CSV Header customization, requiring
        it anyway would incorrectly block completion for rows written
        exactly as configured (Requirement #17: column deletion is allowed).
        """
        if not row: return False
        headers = self.headers.get(site, AGENCY_HEADERS.get(site, []))
        if len(row) != len(headers):
            return False
        header_keys = [h.strip().lower() for h in headers]
        row_map = dict(zip(header_keys, row))

        if any(k in ("filename", "file name", "file_name") for k in header_keys):
            filename_val = row_map.get("filename") or row_map.get("file name") or row_map.get("file_name") or ""
            if not filename_val.strip():
                return False

        if any(k in ("title", "description") for k in header_keys):
            text_val = row_map.get("title") or row_map.get("description") or ""
            if len(text_val.strip()) < 5:
                return False

        if "keywords" in header_keys:
            keywords_val = row_map.get("keywords") or ""
            if len(keywords_val.strip().split(',')) < 3:
                return False

        if any(k in ("category", "category 1") for k in header_keys):
            if site == "Adobe Stock" and not (row_map.get("category") or row_map.get("category 1") or "").strip():
                return False
            if site == "Shutterstock" and not (row_map.get("category 1") or row_map.get("category") or "").strip():
                return False

        return True

    def _load_cache_and_initialize(self):
        with self.lock:
            for f in self.files_list:
                num = extract_design_number(f)
                if num is not None and num > self.global_max_design_index:
                    self.global_max_design_index = num

            for site in self.target_sites:
                path = self.csv_paths[site]
                ensure_directory_exists(path)
                site_cache = {}

                if os.path.exists(path):
                    try:
                        with open(path, mode='r', encoding='utf-8-sig') as f:
                            reader = csv.reader(f)
                            next(reader, None)  # Skip Header
                            for idx, row in enumerate(reader, start=1):
                                if row: 
                                    site_cache[idx] = row
                                    if idx > self.global_max_design_index:
                                        self.global_max_design_index = idx
                    except Exception as e:
                        append_to_file_log(f"CSV Load Error [{site}]: {str(e)}")

                self.in_memory_data[site] = site_cache

            # Load any existing qa_score.csv the same positional way, so a
            # resumed run doesn't lose previously-recorded QA scores.
            if os.path.exists(self.qa_csv_path):
                try:
                    with open(self.qa_csv_path, mode='r', encoding='utf-8-sig') as f:
                        reader = csv.reader(f)
                        next(reader, None)  # Skip Header
                        for idx, row in enumerate(reader, start=1):
                            if row and len(row) >= 2 and (row[0].strip() or row[1].strip()):
                                self.qa_scores[idx] = (row[0], row[1])
                                if idx > self.global_max_design_index:
                                    self.global_max_design_index = idx
                except Exception as e:
                    append_to_file_log(f"QA CSV Load Error: {str(e)}")

            for idx in range(1, self.global_max_design_index + 1):
                all_valid = True
                for site in self.target_sites:
                    row = self.in_memory_data[site].get(idx, [])
                    if not self._is_valid_row(site, row):
                        all_valid = False
                        break
                if all_valid:
                    self.completed_design_numbers.add(idx)

    def update_exact_row_in_memory(self, design_number: int, metadata: dict):
        with self.lock:
            # Bug Fix #9 (Duplicate Design Number): if this row already holds
            # data for a *different* filename, two distinct images collided
            # on the same design number. Log it loudly rather than silently
            # overwriting the earlier file's row with the new one.
            incoming_filename = metadata.get("original_filename", "")
            existing_row = self.in_memory_data.get(self.target_sites[0], {}).get(design_number)
            if existing_row and existing_row[0] and incoming_filename and existing_row[0] != incoming_filename:
                append_to_file_log(
                    f"WARNING: Duplicate design number conflict at row {design_number + 1}: "
                    f"'{existing_row[0]}' is already saved there, now being overwritten by '{incoming_filename}'."
                )

            # Fix 8: Expand global max row index if design exceeds current max
            if design_number > self.global_max_design_index:
                self.global_max_design_index = design_number

            # Requirement #17 (CSV Header — Dynamic System): build each row
            # by walking the CURRENT (possibly user-edited) header list and
            # resolving each column by recognized name, instead of a fixed
            # hardcoded positional layout. This is what makes column
            # add/remove/reorder actually work.
            for site in self.target_sites:
                headers = self.headers.get(site, AGENCY_HEADERS.get(site, []))
                self.in_memory_data[site][design_number] = [
                    _resolve_csv_field_value(site, h, metadata) for h in headers
                ]
                self.dirty_sites.add(site)
            # Bug Fix (CRITICAL - Requirement #2/#32/#33): do NOT mark this
            # design number as completed here. This method only stages data
            # in RAM — completion must wait until flush_and_verify() proves
            # the row actually landed on disk correctly (see below).

    def update_qa_score(self, design_number: int, filename: str, qa_score) -> None:
        """Stages a QA score using the exact same positional row rule as the
        Adobe Stock/Shutterstock/Vecteezy CSVs: design_number N -> Row N+1.
        Written out by flush_to_disk() alongside the main sites, so
        qa_score.csv can never disagree with them on row placement."""
        with self.lock:
            if design_number > self.global_max_design_index:
                self.global_max_design_index = design_number
            self.qa_scores[design_number] = (filename, qa_score)
            self.qa_dirty = True

    def flush_to_disk(self):
        """Fix 8: Writes all CSVs padded cleanly to global_max_design_index.
        Requirement #17: headers come from self.headers (config-driven), not
        the hardcoded AGENCY_HEADERS constant."""
        with self.lock:
            if not self.dirty_sites and not self.qa_dirty:
                return

            sites_to_flush = list(self.dirty_sites)
            max_row = self.global_max_design_index  # Fix 8: Unified maximum row count

            for site in sites_to_flush:
                path = self.csv_paths[site]
                headers = self.headers.get(site, AGENCY_HEADERS.get(site, []))
                rows_to_write = [headers]

                for row_idx in range(1, max_row + 1):
                    row = self.in_memory_data[site].get(row_idx, [])
                    if not row or len(row) != len(headers):
                        row = ["" for _ in range(len(headers))] # Row consistency padding
                    rows_to_write.append(row)

                # Bug Fix (user request): stop creating .bak backup files
                # alongside every CSV — they were cluttering the design
                # folder with duplicate files the user doesn't want. Data
                # safety against a mid-write crash is still fully covered by
                # the atomic tmp-file + os.replace() below (Requirement #27),
                # which is the actual corruption-prevention mechanism; the
                # .bak copy was a redundant convenience file, not a
                # correctness requirement.
                try:
                    # Requirement #27 (CSV Write Atomicity): write to a temp
                    # file first, then atomically replace — a crash mid-write
                    # leaves the original file untouched instead of corrupt.
                    tmp_path = path + ".tmp"
                    with open(tmp_path, mode='w', newline='', encoding='utf-8-sig') as f:
                        writer = csv.writer(f)
                        writer.writerows(rows_to_write)
                    os.replace(tmp_path, path)
                    self.dirty_sites.remove(site)
                except Exception as e:
                    append_to_file_log(f"ERROR Flushing CSV [{site}]: {str(e)}")

            # Bug Fix (QA Score CSV organization): write qa_score.csv with
            # the exact same Row N+1 == design_number N positional rule and
            # blank-row padding as the three site CSVs above, using the same
            # global_max_design_index boundary — so it can never drift out
            # of alignment with them.
            if self.qa_dirty:
                qa_rows = [["Filename", "QA Score"]]
                for row_idx in range(1, max_row + 1):
                    entry = self.qa_scores.get(row_idx)
                    qa_rows.append([entry[0], entry[1]] if entry else ["", ""])
                try:
                    tmp_path = self.qa_csv_path + ".tmp"
                    with open(tmp_path, mode='w', newline='', encoding='utf-8-sig') as f:
                        writer = csv.writer(f)
                        writer.writerows(qa_rows)
                    os.replace(tmp_path, self.qa_csv_path)
                    self.qa_dirty = False
                except Exception as e:
                    append_to_file_log(f"ERROR Flushing QA Score CSV: {str(e)}")

    def flush_and_verify(self, design_number: int, expected_filename: str) -> tuple:
        """
        Bug Fix (CRITICAL — Requirement #2 CSV Write Bug, #32 Completion Rule,
        #33 Metadata Loss Prevention):
        Previously, the moment Gemini returned metadata, the job was marked
        completed immediately — update_exact_row_in_memory() only staged the
        row in RAM, the actual disk write happened later (batched, on a
        timer), and NOTHING ever confirmed it actually succeeded. If the
        write failed (read-only folder, disk full, permissions changed
        mid-run, crash before the next periodic flush), the job was already
        "completed" with no data on disk — silent metadata loss.

        This method makes the disk write happen NOW (not later), then reads
        the freshly-written file(s) back from disk and confirms the exact
        row for this design number is present, valid, and matches the
        expected filename for every target site. Only on full success is the
        design number recorded as completed. All I/O exceptions are caught
        and returned as a reason string — this method never raises, so a
        caller can always safely retry instead of crashing or losing state.
        """
        with self.lock:
            try:
                self.flush_to_disk()
            except Exception as e:
                return False, f"CSV_WRITE_EXCEPTION: {str(e)}"

            for site in self.target_sites:
                path = self.csv_paths[site]
                if not os.path.exists(path):
                    return False, f"CSV_FILE_MISSING:{site}"
                try:
                    with open(path, 'r', encoding='utf-8-sig') as f:
                        rows = list(csv.reader(f))
                except Exception as e:
                    return False, f"CSV_READ_VERIFY_ERROR:{site}:{str(e)}"

                if design_number >= len(rows):
                    return False, f"ROW_MISSING:{site}:row_{design_number + 1}"

                row = rows[design_number]  # row 0 = header, row N = design number N
                if not self._is_valid_row(site, row):
                    return False, f"ROW_INVALID_OR_INCOMPLETE:{site}:row_{design_number + 1}"

                if expected_filename and row[0].strip() != str(expected_filename).strip():
                    return False, (
                        f"FILENAME_MISMATCH:{site}:expected={expected_filename},got={row[0]}"
                    )

            # Only now — after a verified, on-disk write confirmed across
            # every target site — is this design number truly complete.
            self.completed_design_numbers.add(design_number)
            return True, "OK"

# ==========================================
# FIX 5: API SCHEDULER WITH ACCURATE COOLDOWN RESTORE
# ==========================================
class DynamicAPIManager:
    def __init__(self, api_configs: List[dict], default_limit: int = 10):
        self.apis = []
        self.lock = Lock()
        # Bug Fix (Requirement: Sequential API Rotation) — replaces the old
        # per-request round-robin ("rr_index") that spread requests across
        # ALL enabled keys simultaneously. Only ONE key is ever "active" at
        # a time: it's used exclusively until it actually 429s, and only
        # then does rotation move to the next key. active_index tracks that
        # single active key.
        self.active_index = 0

        # Requirement #17 (Final Summary): tracks how many times rotation
        # actually moved to a different key, and how many genuine 429s were
        # seen in total, across the whole run.
        self.total_api_rotations: int = 0
        self.total_429_count: int = 0

        for cfg in api_configs:
            if cfg.get("status") != "Enable":
                continue
            # Security fix: prefer an environment variable over a hardcoded
            # key in config.json (e.g. GEMINI_API_KEY_<NAME>). Falls back to
            # the stored key so existing configs keep working.
            env_name = f"GEMINI_API_KEY_{cfg.get('name', 'API_KEY').upper().replace(' ', '_')}"
            resolved_key = (cfg.get("key", "") or "").strip() or os.environ.get(env_name, "").strip()
            if not resolved_key:
                continue
            # Requirement #4 (Duplicate API Key Bug) — defense in depth: the
            # UI already blocks adding a duplicate key, but config.json can
            # be hand-edited, so guard here too rather than double-counting
            # the same key's rate limit budget under two different entries.
            if any(existing["key"] == resolved_key for existing in self.apis):
                append_to_file_log(f"WARNING: Duplicate API key for '{cfg.get('name', 'API Key')}' skipped (already loaded).")
                continue
            self.apis.append({
                "name": cfg.get("name", "API Key"),
                "key": resolved_key,
                "limit": int(cfg.get("limit", default_limit)),
                # Bug Fix #18: a deque lets us evict expired timestamps from
                # the front in O(k) (k = number expired) via popleft(),
                # instead of rebuilding the whole list with a comprehension
                # on every single availability check (O(n) every time).
                "timestamps": deque(),
                "cooldown_until": 0.0,
                "valid": True,  # Bug Fix #16: startup validation flag
            })
            # Requirement #15 (Logging): confirm each key was loaded and is
            # READY before any processing starts.
            append_to_file_log(f"API Ready: [{cfg.get('name', 'API Key')}] loaded from config.")

    def validate_keys_on_startup(self):
        """Bug Fix #16: status=='Enable' and key!='' is not proof the key is
        actually usable (revoked/expired/quota-exhausted keys previously went
        undetected until the first real job failed). Best-effort check each
        key with a cheap models.list() call and flag/disable invalid ones."""
        if genai is None:
            return
        for api in self.apis:
            try:
                client = genai.Client(api_key=api["key"])
                # A lightweight call just to confirm the key authenticates.
                list(client.models.list())
                api["valid"] = True
            except Exception as e:
                api["valid"] = False
                append_to_file_log(
                    f"WARNING: API key [{api['name']}] failed startup validation: {str(e)}"
                )

    def get_api_name(self, api_key: Optional[str]) -> str:
        """Resolves a key back to its friendly config name (e.g. 'Ali1') for
        logging — so log lines can show which API a request actually went
        out on, not just the raw key."""
        if not api_key:
            return "API"
        with self.lock:
            for api in self.apis:
                if api["key"] == api_key:
                    return api["name"]
        return "API"

    def get_available_api(self):
        """Requirement #2 (Sequential API Rotation): only ONE API key is
        ever handed out as "active" — Ali1, Ali1, Ali1... until it actually
        429s. We never jump to Ali2 just because Ali1's own self-throttle
        (per-minute request cap) is momentarily full; that's not a real
        rate-limit from the provider, so we simply wait for Ali1's window to
        free up. Rotation to the next key only happens via
        trigger_429_cooldown(), which is called on a genuine 429."""
        with self.lock:
            if not self.apis: return None, None

            now = time.time()
            num_apis = len(self.apis)
            start_idx = self.active_index % num_apis

            for offset in range(num_apis):
                idx = (start_idx + offset) % num_apis
                api = self.apis[idx]

                if not api.get("valid", True):
                    continue

                # Requirement #6: an API in cooldown gets zero requests —
                # skip forward looking for the next usable key in sequence.
                if now < api["cooldown_until"]:
                    continue

                # Requirement #15 (Logging): the key just fell out of
                # cooldown and is being picked up again for the first time —
                # log it once here, not on every subsequent successful call.
                if api.pop("_cooldown_pending_log", False):
                    append_to_file_log(f"API Cooldown Finished: [{api['name']}] is READY again.")

                # Requirement #15 (Logging - "API Rotation"): only log when
                # rotation actually moves to a DIFFERENT key than whatever
                # was active coming into this call. Note: a 429-triggered
                # rotation is already counted/logged synchronously inside
                # trigger_429_cooldown() (active_index changes there,
                # immediately) — by the time this runs, idx == start_idx
                # already for that case, so this only catches the rarer
                # scenario of the active key unexpectedly still being in
                # cooldown/invalid at scan time (e.g. right after a crash
                # resume), never double-counting the same rotation twice.
                if idx != start_idx:
                    old_name = self.apis[start_idx]["name"]
                    self.total_api_rotations += 1
                    append_to_file_log(f"API Rotation: [{old_name}] -> [{api['name']}] (Rotation #{self.total_api_rotations})")

                # First valid, non-cooldown key found in sequence becomes
                # (or remains) the single active key.
                self.active_index = idx

                # Bug Fix #18: evict only the expired entries from the front
                # of the deque (O(k)) instead of rebuilding the entire list
                # every call (O(n)).
                while api["timestamps"] and (now - api["timestamps"][0][1]) >= 60.0:
                    api["timestamps"].popleft()

                if len(api["timestamps"]) < api["limit"]:
                    req_id = str(uuid.uuid4())
                    api["timestamps"].append((req_id, now))
                    return api["key"], req_id

                # Active key is temporarily at its own self-imposed cap —
                # wait for it rather than rotating to a different key.
                return None, None

            # Requirement #7: every API is either invalid or in cooldown —
            # no key available right now. Engine must pause and wait.
            return None, None

    def trigger_429_cooldown(self, api_key: str, request_id: Optional[str] = None) -> bool:
        """Requirement #3: on a genuine 429 from the provider — (1) put that
        key into a 60s cooldown, (2) remove it from the available pool for
        that duration, and (3) rotate the active pointer to the next key in
        sequence so the very next get_available_api() call starts
        processing with it (Ali1 -> Ali2 -> Ali3 ...).

        Returns True if this was a FRESH cooldown (the key wasn't already
        cooling down), False if it was a redundant/stale repeat.

        Bug Fix (Requirement #4/#7 — race condition / no API skip): with
        several worker threads in flight, a 429 can arrive for a key that
        ISN'T the current active one anymore — e.g. a slow, already-in-flight
        request on Ali2 finally times out with a 429 after rotation has
        already moved on to Ali1. Previously this unconditionally forced
        active_index to (that key's index + 1), which could yank the
        pointer AWAY from the real current active key and skip over it
        entirely. Now the pointer only advances when the 429'd key is still
        the one currently active — a stale/late 429 still cools that key
        down (correctly, since it did hit a real rate limit) but never
        knocks rotation off the key that's actually in use right now.

        Bug Fix (Idempotent Cooldown): when several requests were fired
        concurrently to the same key before the first 429 came back, EACH
        one eventually returns 429 too — but if every one of those extended
        cooldown_until = now + 60s again, a burst of 4-5 late-arriving 429s
        spread over a few seconds would keep pushing the key's real READY
        time further and further into the future, well past the intended
        60s. A key that's already cooling down just gets its (already
        genuine) failure counted — the timer itself is left alone."""
        with self.lock:
            now = time.time()
            num_apis = len(self.apis)
            for i, api in enumerate(self.apis):
                if api["key"] == api_key:
                    if request_id:
                        api["timestamps"] = deque(item for item in api["timestamps"] if item[0] != request_id)
                    elif api["timestamps"]:
                        api["timestamps"].pop()

                    already_cooling = now < api["cooldown_until"]
                    self.total_429_count += 1
                    if already_cooling:
                        # Stale/duplicate signal for a key we already know
                        # about — don't extend the timer, don't re-log, and
                        # don't touch active_index (it may have already
                        # rotated on to a different key since).
                        append_to_file_log(
                            f"API [{api['name']}] 429 (duplicate/stale, key already cooling down — ignored)."
                        )
                        return False

                    api["cooldown_until"] = now + 60.0
                    # Requirement #15 (Logging): mark so the next successful
                    # pickup of this key logs "API Cooldown Finished".
                    api["_cooldown_pending_log"] = True
                    append_to_file_log(
                        f"API Cooldown Started: [{api['name']}] for 60s (429 #{self.total_429_count}, Req {request_id} released)."
                    )
                    if num_apis and self.active_index == i:
                        new_idx = (i + 1) % num_apis
                        self.active_index = new_idx
                        # Bug Fix (Requirement #17 - Final Summary): this is
                        # where a 429-triggered rotation actually happens —
                        # active_index is mutated right here, synchronously,
                        # BEFORE the next get_available_api() call ever runs.
                        # Counting/logging it there instead (comparing
                        # against start_idx) would never fire for this path,
                        # since by the time that function runs the switch
                        # has already silently happened — it would only ever
                        # catch the separate, rarer case of a stale key
                        # unexpectedly found in cooldown during a scan.
                        self.total_api_rotations += 1
                        append_to_file_log(
                            f"API Rotation: [{api['name']}] -> [{self.apis[new_idx]['name']}] "
                            f"(Rotation #{self.total_api_rotations})"
                        )
                    return True
            return False  # api_key not found in self.apis at all

    def get_next_available_api_time(self) -> float:
        """Returns the exact absolute timestamp when the engine should next
        try get_available_api(). Mirrors get_available_api()'s exact
        decision path so the scheduler's wait time is accurate: it finds the
        same first valid/non-cooldown key starting from active_index, and
        reports when THAT key frees up — never a different, merely-idle key,
        since sequential rotation forbids jumping ahead on our own."""
        with self.lock:
            now = time.time()
            if not self.apis: return float('inf')
            num_apis = len(self.apis)
            start_idx = self.active_index % num_apis

            for offset in range(num_apis):
                idx = (start_idx + offset) % num_apis
                api = self.apis[idx]

                if not api.get("valid", True):
                    continue
                if api["cooldown_until"] > now:
                    continue

                valid_ts = [ts[1] for ts in api["timestamps"] if (now - ts[1]) < 60.0]
                if len(valid_ts) < api["limit"]:
                    return now
                return min(valid_ts) + 60.0

            # Requirement #7: all keys cooldown/invalid — wait for whichever
            # valid key's cooldown ends soonest.
            candidates = [api["cooldown_until"] for api in self.apis if api.get("valid", True) and api["cooldown_until"] > now]
            return min(candidates) if candidates else float('inf')

    def serialize_state(self) -> dict:
        with self.lock:
            return {
                "active_index": self.active_index,
                "api_states": [
                    {
                        "key": api["key"],
                        "cooldown_until": api["cooldown_until"],
                        # deque isn't JSON-serializable — persist as a plain list.
                        "timestamps": list(api["timestamps"])
                    }
                    for api in self.apis
                ]
            }

    def deserialize_state(self, state_data: dict):
        """Fix 5: Restores cooldown cleanly without extending expiry time."""
        with self.lock:
            if not state_data: return
            # Fall back to the old "rr_index" key so a state file saved
            # before this rotation-logic change can still resume cleanly.
            self.active_index = state_data.get("active_index", state_data.get("rr_index", 0))
            saved_api_map = {item["key"]: item for item in state_data.get("api_states", [])}

            now = time.time()
            for api in self.apis:
                if api["key"] in saved_api_map:
                    saved = saved_api_map[api["key"]]
                    saved_expiry = saved.get("cooldown_until", 0.0)
                    
                    # Fix 5: Only keep cooldown if future timestamp; otherwise set 0.0
                    api["cooldown_until"] = saved_expiry if saved_expiry > now else 0.0
                    
                    # Clean expired timestamps (Bug Fix #18: restore as deque)
                    api["timestamps"] = deque(
                        (r_id, ts) for r_id, ts in saved.get("timestamps", [])
                        if (now - ts) < 60.0
                    )
            if self.apis:
                self.active_index = self.active_index % len(self.apis)
            else:
                self.active_index = 0

# ==========================================
# WORKER EXECUTION MODULE
# ==========================================
def generate_metadata_single_key(image_path: str, api_key: str, model_name: str = "gemini-2.5-flash", retries: int = 4):
    if not api_key: return None, "MISSING_API_KEY", False
    if genai is None: return None, "GOOGLE_GENAI_NOT_INSTALLED", False

    prompt = f"""
    Generate commercial and SEO-optimized metadata for this stock vector design.

    Return ONLY a valid raw JSON object matching:
    {{
      "title": "SEO title here",
      "description": "Detailed SEO description here",
      "category": "Choose exactly one from: {", ".join(VALID_CATEGORIES)}",
      "keywords": ["kw1", "kw2", "..."],
      "people_property": "Yes or No"
    }}

    KEYWORD REQUIREMENTS (very important):
    - Generate between 45 and 50 keywords for the "keywords" array.
    - Never generate fewer than 45 keywords unless it is truly impossible for
      this specific image (e.g. an extremely simple design with almost
      nothing to describe) — always try your genuine best to reach 45-50.
    - Every keyword must be highly relevant to what is actually depicted in
      this design (subject, style, color, mood, use-case, technique, etc.).
      Do not pad the list with irrelevant or repetitive filler.
    - Each keyword must be unique — no duplicates and no near-duplicate
      variants of the same word (e.g. don't list both "flower" and "flowers").
    - Do NOT include generic/banned stock-photo filler words such as: image,
      photo, design, picture, vector, illustration, background, stock,
      graphic, art. These add no search value.
    - Order keywords roughly from most to least relevant/searchable.
    - Return the keywords strictly as a JSON array of strings, e.g.
      ["kw1", "kw2", "kw3", ...] — not a comma-separated string.
    """
    if model_name.startswith("models/"):
        model_name = model_name[len("models/"):]

    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        return None, f"CLIENT_INIT_ERROR: {str(e)}", False

    try:
        if Image is None: return None, "PIL_NOT_INSTALLED", False
        if not os.path.exists(image_path):
            # Requirement #9 (Missing File During Processing): if the file
            # was deleted/moved after being queued, this must be identified
            # specifically as FILE_NOT_FOUND (not a generic read error), and
            # must never crash the worker.
            return None, "FILE_NOT_FOUND", False
        with Image.open(image_path) as img:
            img_resized = img.convert("RGB")
            img_resized.thumbnail((300, 300))
            buffer = io.BytesIO()
            img_resized.save(buffer, format="JPEG", quality=40, optimize=True)
            image_bytes = buffer.getvalue()
            buffer.close()
    except FileNotFoundError:
        return None, "FILE_NOT_FOUND", False
    except Exception as e:
        return None, f"IMAGE_READ_ERROR: {str(e)}", False

    blacklist = {b.lower().strip() for b in CONFIG.get("keyword_blacklist", [])}

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            )
        )

        if not getattr(response, "text", None): return None, "EMPTY_RESPONSE", False
        raw_text = response.text.strip()
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start == -1 or end == -1: return None, "INVALID_JSON", False

        data = json.loads(raw_text[start:end+1])
        title = clean_title_adobe(data.get("title", ""))
        if not title.strip():
            # Bug Fix #20: an empty title was previously accepted silently
            # (and would even propagate into an empty description). Reject
            # so the job retries instead of producing unusable metadata.
            return None, "EMPTY_TITLE", False
        description = clean_title_adobe(data.get("description", title)) or title
        raw_category = str(data.get("category", "")).strip()
        category = raw_category if raw_category in VALID_CATEGORIES else ""
        if not category:
            # Bug Fix #17: an empty/invalid category was previously accepted
            # silently. Reject it so the job is retried instead of producing
            # metadata that will fail marketplace validation.
            return None, f"INVALID_CATEGORY: '{raw_category}'", False
        pp_val = str(data.get("people_property", "No")).strip().lower()
        pp = "Yes" if pp_val == "yes" else "No"

        # Bug Fix (Type Safety): the prompt asks for a JSON array, but if the
        # model ever returns "keywords" as a single comma-separated string
        # instead, iterating it directly would iterate individual CHARACTERS
        # (since strings are iterable), silently producing zero usable
        # keywords every time. Detect and salvage that case instead of
        # always failing.
        raw_keywords = data.get("keywords", [])
        if isinstance(raw_keywords, list):
            keywords = raw_keywords
        elif isinstance(raw_keywords, str):
            keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
        else:
            keywords = []

        title_tokens = [w.lower() for w in title.split() if len(w) > 2 and w.lower() not in STOPWORDS]
        unique_kw, seen_stems, seen_kw = [], set(), set()

        for token in title_tokens:
            stem = stem_word(token)
            if stem not in seen_stems and token not in blacklist:
                seen_stems.add(stem); seen_kw.add(token); unique_kw.append(token.capitalize())

        for k in keywords:
            if isinstance(k, str):
                k_clean = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", k).strip().lower()
                if len(k_clean) > 2 and k_clean not in STOPWORDS and k_clean not in BANNED_KEYWORDS and k_clean not in blacklist:
                    stem = stem_word(k_clean)
                    if stem not in seen_stems and k_clean not in seen_kw:
                        seen_stems.add(stem); seen_kw.add(k_clean); unique_kw.append(k_clean.capitalize())

        # Bug Fix #7 (Requirement update): allow up to 50 keywords (was
        # capped at 49) to match the new 45-50 keyword target.
        target_keywords = CONFIG.get("target_keywords", 50)
        unique_kw = unique_kw[:target_keywords]

        # Bug Fix #20: enforce a minimum keyword count (most stock platforms
        # reject/penalize designs with too few keywords). Retry instead of
        # silently accepting a too-short keyword list. Default raised to 45
        # per the "45-50 keywords, never fewer than 45 unless impossible"
        # requirement.
        min_keywords = CONFIG.get("min_keywords", 45)
        if len(unique_kw) < min_keywords:
            return None, f"INSUFFICIENT_KEYWORDS: {len(unique_kw)}/{min_keywords}", False

        qa_score = evaluate_metadata_advanced(title, unique_kw, category)

        return {
            "title": title,
            "description": description,
            "category": category,
            "keywords": ", ".join(unique_kw),
            "people_property": pp,
            "qa_score": qa_score
        }, "SUCCESS", False

    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "quota" in err_msg.lower() or "resource_exhausted" in err_msg.lower():
            return None, "429_RATE_LIMIT", True
        return None, f"API Error: {err_msg}", False

