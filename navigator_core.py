# navigator_core.py
# Pure, dependency-free helpers shared by the ASAP Powertools app.
#
# WHY THIS MODULE EXISTS
# ----------------------
# navigator.py imports Playwright and tkinter at module load, which means it
# can only run where a browser and a display are available. That makes the
# genuinely pure logic inside it (filename sanitising, variable substitution,
# date parsing, credential-key derivation, retry timings) impossible to unit
# test in CI or on a headless box.
#
# Everything in here is deliberately free of Playwright/tkinter imports — only
# the Python standard library — so it can be imported and tested anywhere, and
# so navigator.py has a single source of truth to delegate to. navigator.py
# keeps its existing method names as thin wrappers around these functions, so
# no call sites change.

import argparse
import copy
import csv
import json
import logging
import os
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse, unquote


# ----- Logging --------------------------------------------------------------
#
# A single named logger for the whole app. The console handler is quiet by
# default (INFO) so a normal run isn't buried in diagnostics; set the
# ASAP_POWERTOOLS_DEBUG env var (1/true/yes/on) to make the console verbose. The
# rotating FILE handler always captures DEBUG, so an unattended overnight batch
# leaves a complete trail you can read afterwards — the single most useful
# reliability win for a tool that runs while nobody is watching.

log = logging.getLogger("navigator")

# Filename used for the rotating log inside the output folder.
LOG_FILENAME = "asap-powertools.log"


def debug_enabled():
    """True when ASAP_POWERTOOLS_DEBUG is set to a truthy value."""
    return os.environ.get("ASAP_POWERTOOLS_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on")


def setup_logging(log_dir=None, debug=None, to_console=True):
    """Configure the 'navigator' logger. Idempotent — safe to call again.

    - console handler: level INFO normally, DEBUG when `debug` (or the
      ASAP_POWERTOOLS_DEBUG env var) is set.
    - file handler (only if `log_dir` is given): always DEBUG, rotating at
      ~2 MB with 5 backups, written as asap-powertools.log in the output folder.
    """
    if debug is None:
        debug = debug_enabled()

    log.setLevel(logging.DEBUG)  # the handlers decide what actually emits
    # Drop any handlers from a previous call so we don't double-log.
    for h in list(log.handlers):
        log.removeHandler(h)
    log.propagate = False

    if to_console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if debug else logging.INFO)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                          datefmt="%H:%M:%S"))
        log.addHandler(ch)

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = RotatingFileHandler(
                os.path.join(log_dir, LOG_FILENAME),
                maxBytes=2_000_000, backupCount=5, encoding="utf-8")
            fh.setLevel(logging.DEBUG)  # file always keeps the full trail
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(fh)
        except Exception:
            # A missing/locked log dir must never stop the app from running.
            pass

    log.debug("logging initialised (console_debug=%s, log_dir=%r)",
              debug, log_dir)
    return log


def dlog(msg):
    """Emit a diagnostic line. `msg` is already-formatted text (often built
    with f-strings containing %-bearing reprs like URLs), so it is passed as a
    pre-rendered argument — never as a logging format string — to avoid
    'not enough arguments for format string' errors."""
    log.debug("%s", msg)


# ----- Retry / backoff timings ----------------------------------------------
#
# The same "try, wait a bit, try again with growing pauses" shape appeared in
# several places with subtly different literal tuples. Naming them in one place
# keeps the timing behaviour identical while making it obvious — and tunable —
# what each retry budget is. Values are milliseconds passed to Playwright's
# wait_for_timeout at the call site.

# Waiting for a target element to render on a heavy page (~5.6s total).
TARGET_RETRY_BACKOFF_MS = (300, 500, 800, 1200, 1500, 1500)
# Waiting for a Kendo widget / its popup to be ready (~4.1s total).
KENDO_BACKOFF_MS = (0, 300, 500, 800, 1200, 1500)
# Waiting for BOTH Telerik date fields to re-render after a postback (~7s).
SWAP_DATES_BACKOFF_MS = (0, 300, 500, 800, 1200, 1500, 1500, 1200)


# ----- Filenames ------------------------------------------------------------

# Filenames that look like the server gave the browser nothing useful: plain
# UUIDs (8-4-4-4-12 hex), long hex blobs, or generic "download"/"untitled".
JUNK_FILENAME_RE = re.compile(
    r"^("
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{16,}"
    r"|download(?:\s*\(\d+\))?"
    r"|untitled"
    r"|file"
    r")(\.[a-z0-9]{1,5})?$",
    re.IGNORECASE,
)

# content-type -> extension, for the formats real users download from web apps.
MIME_EXT = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "text/html": ".html",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def sanitize_filename(name):
    """Strip filesystem-hostile characters; collapse whitespace; cap length."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name)
    name = re.sub(r"\s+", "_", name).strip("._ ")
    return name[:120] or "download"


def unique_path(path, exists=os.path.exists):
    """Return `path`, or `path (1)`, `path (2)`, … so we never clobber an
    existing file. `exists` is injectable purely so tests can drive it."""
    if not exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        cand = f"{base} ({n}){ext}"
        if not exists(cand):
            return cand
        n += 1


def guess_extension_from_url(url, default=".bin"):
    """Best-effort extension from a URL path (e.g. /a/b/report.pdf -> .pdf).
    Falls back to `default` when the URL carries no usable extension."""
    try:
        _, ext = os.path.splitext(urlparse(url or "").path)
        if ext and len(ext) <= 6:
            return ext.lower()
    except Exception:
        pass
    return default


def smart_filename(suggested, url, title="", now=None):
    """Pick a useful filename when the server's suggestion is a UUID or junk.

    Pure version of the original BrowserWorker._smart_filename: it takes the
    plain strings the caller already has (the Download's suggested filename and
    url, plus the page title) rather than a Playwright Download object, so it
    can be unit tested. Order of preference: server suggestion -> URL path ->
    page title -> hostname + timestamp. Always returns something with an
    extension when one can be guessed.
    """
    now = now or datetime.now()
    suggested = (suggested or "").strip()
    if suggested and not JUNK_FILENAME_RE.match(suggested):
        return sanitize_filename(suggested)

    # Carry over the extension from the (junk) suggestion if it had one.
    _, sugg_ext = os.path.splitext(suggested)

    # 1. URL path tail: /reports/transcript.pdf -> transcript.pdf
    try:
        parsed = urlparse(url or "")
        path_tail = unquote(os.path.basename(parsed.path) or "")
        base, ext = os.path.splitext(path_tail)
        if base and not JUNK_FILENAME_RE.match(path_tail):
            if not ext and sugg_ext:
                ext = sugg_ext
            return sanitize_filename(base + (ext or ""))
    except Exception:
        pass

    # 2. Page title — useful for things like "View Transcript Report".
    title = (title or "").strip()
    if title:
        ext = sugg_ext or guess_extension_from_url(url)
        return sanitize_filename(title) + ext

    # 3. Hostname + timestamp as a last resort.
    try:
        host = urlparse(url or "").hostname or ""
    except Exception:
        host = ""
    host = host.replace("www.", "")
    ts = now.strftime("%Y%m%d-%H%M%S")
    stem = f"{host}_{ts}" if host else f"download_{ts}"
    ext = sugg_ext or guess_extension_from_url(url)
    return sanitize_filename(stem) + ext


# ----- Template variable substitution ---------------------------------------

VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def substitute_vars(template, values):
    """Return a deep copy of the template steps with {{var}} markers replaced.

    Only touches the string fields that legitimately carry variables (url,
    value, label). Replacement is whitespace-tolerant, so {{ studentid }} and
    {{studentid}} both resolve.
    """
    steps = copy.deepcopy(template)
    for step in steps:
        for key in ("url", "value", "label"):
            v = step.get(key)
            if isinstance(v, str) and "{{" in v:
                v = VAR_RE.sub(
                    lambda m: str(values.get(m.group(1), m.group(0))), v)
                step[key] = v
    return steps


def template_vars(template):
    """Return the set of {{var}} names referenced anywhere in a template.
    Used by the CLI/UI to know which columns a CSV must supply."""
    found = set()
    for step in template or []:
        for key in ("url", "value", "label"):
            v = step.get(key)
            if isinstance(v, str):
                found.update(VAR_RE.findall(v))
    return found


# ----- Date parsing (transcript date-swap) ----------------------------------

def parse_date(d):
    """Parse a date string in the formats the ASAP date fields use. Returns a
    datetime.date or None."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime((d or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


# ----- Credentials key derivation -------------------------------------------

def credential_key(url_or_host, field_name):
    """Build the 'host|field' lookup key for a credential. Accepts a full URL
    or a bare host; the host is lowercased and the field stripped/lowercased so
    lookups are stable regardless of how the caller spelled them."""
    try:
        host = urlparse(url_or_host).hostname or url_or_host
    except Exception:
        host = url_or_host
    host = (host or "").lower()
    name = (field_name or "").strip().lower()
    return f"{host}|{name}"


# ----- CLI input parsing -----------------------------------------------------
#
# Pure helpers behind the headless CLI (navigator.py batch/resolve/merge-pdf).
# Kept here so they're testable without importing the browser/GUI module.

def read_tokens(arg):
    """Return a de-duplicated, order-preserving list of tokens from either a
    file path (one token per line) or an inline comma/whitespace-separated
    string. Empty/whitespace input yields an empty list."""
    if not arg:
        return []
    if os.path.isfile(arg):
        with open(arg, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = arg
    out, seen = [], set()
    for chunk in text.replace(",", "\n").split():
        s = chunk.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def read_csv_rows(path, id_column="studentid"):
    """Read a CSV into a list of {column: value} dicts for a multi-variable
    batch. Each row must carry a non-empty value in `id_column`; that value is
    also copied to a 'studentid' key so a template's {{studentid}} resolves
    regardless of the column's actual name. Rows lacking an id are skipped."""
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = {(k or "").strip(): ("" if v is None else str(v).strip())
                   for k, v in raw.items()}
            sid = row.get(id_column, "").strip()
            if not sid:
                continue
            row["studentid"] = sid
            rows.append(row)
    return rows


def load_template_steps(path):
    """Load a recording JSON and return its steps list. Accepts either the
    {"steps": [...]} wrapper the app saves or a bare list."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        raise ValueError("Template must be a JSON object with a 'steps' list.")
    return steps


def build_cli_parser():
    """Build the argparse parser for the headless CLI. With no subcommand the
    app launches its GUI; the subcommands run unattended."""
    parser = argparse.ArgumentParser(
        prog="asap-powertools",
        description="ASAP Powertools — record, replay, and batch ASAP "
                    "Connected transcript exports. With no subcommand, "
                    "launches the GUI.")
    sub = parser.add_subparsers(dest="command")

    pb = sub.add_parser("batch",
                        help="Run a template per student (headless-capable).")
    pb.add_argument("--template", required=True,
                    help="Recording JSON to run for each student.")
    src = pb.add_mutually_exclusive_group(required=True)
    src.add_argument("--ids",
                     help="Student IDs: a file (one per line) or a comma list.")
    src.add_argument("--csv",
                     help="CSV whose columns supply {{var}} values per row "
                          "(multi-variable batch).")
    pb.add_argument("--id-column", default="studentid",
                    help="CSV column holding the student id (default: studentid).")
    pb.add_argument("--headless", action="store_true",
                    help="Run Chromium without a visible window.")
    pb.add_argument("--output",
                    help="Output folder for PDFs, CSV logs, and the log file.")

    pr = sub.add_parser("resolve",
                        help="Resolve emails to student IDs; optionally batch.")
    pr.add_argument("--emails", required=True,
                    help="Emails: a file (one per line) or a comma list.")
    pr.add_argument("--template",
                    help="Recording JSON (required with --auto-batch).")
    pr.add_argument("--auto-batch", action="store_true",
                    help="After resolving, batch-run the template for the "
                         "cleanly-resolved students.")
    pr.add_argument("--headless", action="store_true",
                    help="Run Chromium without a visible window.")
    pr.add_argument("--output", help="Output folder.")

    pm = sub.add_parser("merge-pdf",
                        help="Merge per-student transcript PDFs into one file.")
    pm.add_argument("--dir", help="Folder to scan (default: output folder).")
    pm.add_argument("--out",
                    help="Output PDF (default: <dir>/transcripts_combined.pdf).")
    pm.add_argument("--pattern", default="_transcript",
                    help="Only merge PDFs whose name contains this "
                         "(default: _transcript).")
    return parser


# ----- PDF merge (optional feature) -----------------------------------------
#
# After a batch run the output folder holds one PDF per student. Merging them
# into a single document (plus a CSV index) is handy for printing or filing.
# pypdf is imported lazily so this module stays dependency-free and the feature
# simply reports a clear message when pypdf isn't installed.

def find_pdfs(folder, contains="_transcript"):
    """Return sorted absolute paths of .pdf files in `folder` whose name
    contains `contains` (case-insensitive). Pass contains="" to match every
    PDF. A missing folder yields an empty list rather than raising."""
    out = []
    needle = (contains or "").lower()
    try:
        names = sorted(os.listdir(folder))
    except (FileNotFoundError, NotADirectoryError):
        return out
    for fn in names:
        low = fn.lower()
        if low.endswith(".pdf") and (not needle or needle in low):
            out.append(os.path.join(folder, fn))
    return out


def merge_pdfs(pdf_paths, out_path):
    """Merge `pdf_paths` into a single PDF at `out_path`.

    Requires pypdf (imported lazily). Raises RuntimeError — with an install
    hint — when pypdf is missing or when there are no readable inputs.
    Unreadable individual PDFs are skipped with a warning. Returns the number
    of files actually merged.
    """
    if not pdf_paths:
        raise RuntimeError("No PDF files to merge.")
    try:
        from pypdf import PdfWriter
    except ImportError:
        raise RuntimeError(
            "Merging PDFs needs the 'pypdf' package. Install it with:\n"
            "    pip install pypdf")

    writer = PdfWriter()
    merged = 0
    for p in pdf_paths:
        try:
            writer.append(p)
            merged += 1
        except Exception as e:
            log.warning("merge_pdfs: skipped unreadable PDF %r (%s)", p, e)
    if merged == 0:
        raise RuntimeError("None of the input PDFs could be read.")
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "wb") as f:
        writer.write(f)
    log.info("merge_pdfs: wrote %d page-sets to %s", merged, out_path)
    return merged


# ----- Step formatting (used by the GUI step list) --------------------------

def format_step(step):
    """Human-readable one-line description of a recorded step."""
    t = step.get("type", "?")
    role = step.get("role") or ""
    name = step.get("name") or ""
    value = step.get("value")
    key = step.get("key")
    nth = step.get("nth", 0) or 0
    nth_str = f" #{nth+1}" if nth > 0 else ""
    if t == "press":
        return (f"press [{key}] on [{role}] '{name}'{nth_str}"
                if role else f"press [{key}]")
    if t == "navigate":
        return f"navigate → {step.get('url','')}"
    if t == "download":
        return f"⤓ downloaded file: {step.get('filename', '?')}"
    if t == "close_page":
        return "✕ close popup / current tab"
    if t == "swap_dates":
        return "swap dates if needed (diploma must be later than graduation)"
    if t == "select_kendo":
        loc = step.get("locators") or {}
        tgt = step.get("label") or step.get("value") or "?"
        idn = loc.get("id") or loc.get("id_suffix") or "?"
        return f"select_kendo [{idn}] = {tgt!r}"
    if t in ("copy", "paste", "cut", "select_all", "undo", "redo"):
        label = t.replace("_", " ")
        return (f"{label} on [{role}] '{name}'{nth_str}"
                if role else label)
    base = f"{t} [{role}] '{name}'{nth_str}"
    if t == "select_option":
        label = step.get("label")
        if label:
            base += f" = {label!r}"
        elif value is not None:
            base += f" = {value!r}"
        return base
    if t == "fill":
        if step.get("secret"):
            kind = step.get("secret_kind", "secret")
            if value is None:
                base += f" = ••••• ({kind}, redacted)"
            else:
                base += f" = ••••• ({kind}, CAPTURED — unsafe)"
        elif value is not None:
            base += f" = {value!r}"
    return base
