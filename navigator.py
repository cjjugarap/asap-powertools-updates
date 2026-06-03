# navigator.py
# Browser automation recorder.
#
# You drive a real browser normally. The navigator watches and translates
# your interactions into semantic steps (role + name + value), highlighting
# the element you touched in its live element list. Edit / reorder / delete
# steps in the navigator before saving the recording as JSON.
#
# Setup (one time):
#   py -m pip install playwright
#   py -m playwright install chromium
#
# Run:
#   py navigator.py

import json
import os
import sys
import base64
import secrets
import subprocess
import time
import threading
import queue
from datetime import datetime

# Pure, dependency-free helpers (filename/var/date/credential logic, logging
# setup, retry timings). Kept in a separate module with no Playwright/tkinter
# imports so it can be unit-tested without a browser or a display; navigator.py
# delegates to it so there's a single, tested source of truth.
import navigator_core as core


def open_path(path):
    """Open a file or folder in the OS's default handler.

    Cross-platform: Windows uses os.startfile, macOS uses 'open',
    Linux uses 'xdg-open'. Failures are silent — the caller already has
    the text path visible to the user as a fallback.
    """
    try:
        if not path:
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: this is the standard way on Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def reveal_in_folder(path):
    """Open the folder containing `path` and select the file in it.

    Falls back to just opening the folder if 'select' isn't supported.
    """
    try:
        if not path:
            return
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if sys.platform.startswith("win"):
            # explorer /select,"C:\path\to\file.ext" highlights the file.
            if os.path.isdir(path):
                os.startfile(path)
            else:
                subprocess.Popen(["explorer", f"/select,{path}"])
        elif sys.platform == "darwin":
            if os.path.isdir(path):
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["open", "-R", path])  # -R = reveal
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception:
        pass
# tkinter and Playwright are imported tolerantly so that `import navigator`
# (and therefore the headless CLI: batch / resolve / merge-pdf) works on a box
# that lacks a display/tkinter or hasn't installed Playwright yet. When present
# — as on Windows/macOS — these are the normal imports and nothing changes.
# When absent, GUI classes still *define* (they only need tk.Toplevel as a base
# at class-definition time) but fail with a clear message if actually launched.
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    _HAS_TK = True
except Exception:  # pragma: no cover - exercised only on headless boxes
    _HAS_TK = False

    class _MissingTk:
        """Stand-in used only when tkinter can't be imported. Provides
        Toplevel=object so the GUI classes can still be defined; any real use
        raises a clear error pointing at the CLI."""
        Toplevel = object

        def __getattr__(self, name):
            raise RuntimeError(
                "tkinter is unavailable, so the GUI can't run. Install it "
                "(e.g. `sudo apt-get install python3-tk`) or use the CLI "
                "subcommands: batch / resolve / merge-pdf.")

    tk = ttk = messagebox = filedialog = _MissingTk()

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - exercised only before `pip install`
    sync_playwright = None


INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio",
    "combobox", "menuitem", "menuitemcheckbox", "menuitemradio",
    "tab", "switch", "searchbox", "slider", "spinbutton",
    "option",
}


# JavaScript injected into every page. Listens for clicks, changes, and
# special keys. For each event it figures out the user's likely target,
# computes its ARIA role + accessible name + nth-of-kind, and reports back
# to Python via the exposed __recorder_emit function.
JS_LISTENER = r"""
(() => {
  // We no longer guard against double-install: document.write replaces
  // the document but keeps the same window. If we guarded on
  // window.__recorder_installed, re-injection after document.write would
  // be skipped, leaving us with NO listeners on the new document. Instead,
  // we de-duplicate by attaching listeners with a sentinel that we check
  // before re-attaching.
  if (window.__recorder_installed_v2) {
    // Reinstall handlers on the (possibly new) document. The previous
    // ones may have died if document.write replaced this document.
  } else {
    window.__recorder_installed_v2 = true;
  }
  // Mark this document so we don't double-register on the SAME document.
  if (document.__recorder_doc_installed) return;
  document.__recorder_doc_installed = true;

  // Heartbeat on install: prove the binding actually works in THIS frame.
  // If the binding is broken, we'll see no heartbeat and no events even
  // though page.evaluate(typeof window.__recorder_emit) returns 'function'.
  try {
    window.__recorder_emit({
      type: '__heartbeat',
      url: location.href,
      title: document.title || '',
      readyState: document.readyState,
    });
  } catch (e) {
    try { console.error('[recorder] heartbeat call failed:', e); } catch (_) {}
  }

  const INTERACTIVE = new Set([
    'button','link','textbox','checkbox','radio','combobox',
    'menuitem','tab','switch','searchbox','slider','spinbutton','option'
  ]);

  function getRole(el) {
    if (!el || !el.tagName) return null;
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (['text','email','password','tel','url'].includes(type)) return 'textbox';
      if (type === 'search') return 'searchbox';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'number') return 'spinbutton';
      if (type === 'range') return 'slider';
      if (['submit','button','reset','image'].includes(type)) return 'button';
      return 'textbox';
    }
    return null;
  }

  function getName(el) {
    if (!el) return '';
    const attr = (n) => el.getAttribute && el.getAttribute(n);
    const lb = attr('aria-labelledby');
    if (lb) {
      const parts = lb.split(/\s+/).map(id => {
        const r = document.getElementById(id);
        return r ? (r.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    const al = attr('aria-label');
    if (al) return al.trim();
    if (el.id) {
      try {
        const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lbl) return (lbl.textContent || '').trim();
      } catch(_) {}
    }
    if (el.closest) {
      const wrap = el.closest('label');
      if (wrap) {
        const c = wrap.cloneNode(true);
        c.querySelectorAll('input, textarea, select').forEach(n => n.remove());
        return (c.textContent || '').trim();
      }
    }
    if (el.tagName === 'IMG') return attr('alt') || '';
    if (el.tagName === 'BUTTON' || el.tagName === 'A') {
      return (el.textContent || '').trim();
    }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      return attr('placeholder') || attr('title') || '';
    }
    return attr('title') || '';
  }

  function findInteractive(el) {
    let cur = el;
    while (cur && cur !== document.body && cur !== document) {
      const r = getRole(cur);
      if (r && INTERACTIVE.has(r)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }

  // Detect sensitive inputs that should not be recorded in plaintext.
  // Returns a kind string ("password", "credit-card-number", "cvc", "otp")
  // or null when the field is not sensitive.
  function isSecretField(el) {
    if (!el || !el.getAttribute) return null;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'password') return 'password';
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (ac === 'current-password' || ac === 'new-password') return 'password';
    if (ac === 'cc-number') return 'credit-card-number';
    if (ac === 'cc-csc' || ac === 'cc-cvc') return 'cvc';
    if (ac === 'one-time-code') return 'otp';
    // Heuristic for badly-built sites that use type=text for passwords.
    const name = (el.getAttribute('name') || '').toLowerCase();
    const id = (el.id || '').toLowerCase();
    if (name.includes('password') || id.includes('password')) return 'password';
    return null;
  }

  function computeNth(target, role, name) {
    // Walk DOM in document order, count elements with same (role, name)
    // before the target. This matches CDP's snapshot order.
    let n = 0;
    const all = document.getElementsByTagName('*');
    for (let i = 0; i < all.length; i++) {
      const el = all[i];
      if (el === target) return n;
      const r = getRole(el);
      if (r === role && getName(el) === name) n++;
    }
    return 0;
  }

  function emit(payload) {
    try {
      if (window.__recorder_emit) window.__recorder_emit(payload);
    } catch(e) {}
  }

  // Capture stable identifiers for an element. These survive across
  // different data (e.g. different students) far better than positional
  // nth indices, because they come from the page template, not the data.
  // For the actual interactive control we sometimes get a wrapper (e.g. a
  // Kendo <span> wrapping a hidden <select>); walk to the nearest element
  // that actually carries an id/name.
  function getLocators(target) {
    const loc = {};
    let el = target;
    // Climb up to 3 levels looking for id/name if the target itself lacks them.
    for (let depth = 0; el && depth < 4; depth++, el = el.parentElement) {
      const id = el.getAttribute && el.getAttribute('id');
      const nm = el.getAttribute && el.getAttribute('name');
      const testid = el.getAttribute && (el.getAttribute('data-testid')
        || el.getAttribute('data-test') || el.getAttribute('data-qa'));
      if (testid && !loc.testid) loc.testid = testid;
      if (id && !loc.id) {
        loc.id = id;
        // ASP.NET ids look like ctl00_Foo_ddlBar — the meaningful, stable
        // part is the trailing segment. Record it for suffix matching.
        const parts = id.split(/[_$]/);
        if (parts.length > 1) loc.id_suffix = parts[parts.length - 1];
      }
      if (nm && !loc.name_attr) {
        loc.name_attr = nm;
        const parts = nm.split(/[_$]/);
        if (parts.length > 1) loc.name_suffix = parts[parts.length - 1];
      }
      // Stop climbing once we have an id — that's specific enough.
      if (loc.id) break;
    }
    return loc;
  }

  function describe(target, type, extra) {
    const role = getRole(target);
    const name = getName(target);
    const nth = computeNth(target, role, name);
    const locators = getLocators(target);
    return Object.assign({ type, role, name, nth, locators }, extra || {});
  }

  // Click: record for buttons/links/etc. but NOT for checkbox/radio
  // (the change event for those is more semantic), and NOT for combobox
  // (the meaningful action is the select_option fired by the change event;
  // the click that opens the dropdown — and any stray click that closes it —
  // is noise that clutters the recording and isn't needed for replay).
  document.addEventListener('click', (e) => {
    const target = findInteractive(e.target);
    if (!target) return;
    const role = getRole(target);
    if (role === 'checkbox' || role === 'radio') return;
    if (role === 'combobox') return;
    emit(describe(target, 'click'));
  }, true);

  // Change: fires when a field loses focus with a new value, or when
  // a checkbox/radio/select changes. Gives us the final value.
  document.addEventListener('change', (e) => {
    const target = findInteractive(e.target);
    if (!target) return;
    const role = getRole(target);
    if (role === 'checkbox' || role === 'radio' || role === 'switch') {
      emit(describe(target, target.checked ? 'check' : 'uncheck'));
    } else if (role === 'combobox') {
      // Capture both the value (internal id, may vary across data sets)
      // AND the visible label (stable text like "High School Diploma").
      // Replay prefers the label because option values are often per-record
      // database ids that differ between students/accounts.
      let label = '';
      try {
        if (target.tagName === 'SELECT' && target.selectedOptions
            && target.selectedOptions.length) {
          label = (target.selectedOptions[0].textContent || '').trim();
        } else if (target.tagName === 'SELECT' && target.options
                   && target.selectedIndex >= 0) {
          label = (target.options[target.selectedIndex].textContent || '').trim();
        }
      } catch (_) {}
      emit(describe(target, 'select_option', { value: target.value, label: label }));
    } else if (['textbox','searchbox','spinbutton','slider'].includes(role)) {
      // Skip Telerik/Kendo internal mirror inputs. Widgets like RadDatePicker
      // keep a hidden companion input (named "Visually hidden input created
      // for functionality purposes.") that mirrors the visible field; it
      // fires its own change events that are pure plumbing, never something
      // a user means to replay.
      const nm = (getName(target) || '').toLowerCase();
      if (nm.includes('visually hidden input created')) return;
      const extra = { value: target.value };
      const secret = isSecretField(target);
      if (secret) {
        extra.secret = true;
        extra.secret_kind = secret;
      }
      emit(describe(target, 'fill', extra));
    }
  }, true);

  // Keydown: capture Enter/Tab/Escape as their own steps, plus any
  // modifier-key combination (Ctrl/Cmd/Alt + key). Plain typing falls
  // through silently — the `change` listener above records the final
  // field value, which is more useful than per-keystroke noise.
  //
  // Common shortcuts get auto-translated into semantic step types so the
  // recording reads naturally: Ctrl+C → copy, Ctrl+V → paste, Ctrl+X →
  // cut, Ctrl+A → select_all, Ctrl+Z → undo, Ctrl+Y → redo. Anything else
  // (Ctrl+F, Ctrl+Shift+K, Alt+Enter, etc.) is recorded as a `press` step
  // with a key string in Playwright's notation (e.g. "Control+Shift+K").
  document.addEventListener('keydown', (e) => {
    if (e.repeat || e.isComposing) return;
    if (['Control','Alt','Shift','Meta','OS'].includes(e.key)) return;  // ignore modifier keys alone

    const primaryMod = e.ctrlKey || e.metaKey;
    const hasModifier = primaryMod || e.altKey;
    const target = findInteractive(e.target);

    // Clipboard and undo/redo shortcuts (Ctrl+C/V/X/A/Z/Y) are deliberately
    // NOT recorded. They're no-ops at replay (the meaningful result is the
    // field's final value, captured by the `change`/fill handler), and
    // recording them just clutters the steps list. We swallow them here so
    // they never enter the recording in the first place.
    if (primaryMod && !e.altKey && !e.shiftKey) {
      const isClipboard = ['a','c','v','x','z','y'].includes(e.key.toLowerCase());
      if (isClipboard) return;
    }

    // Plain Enter/Tab/Escape (no modifier) as before.
    if (!hasModifier && ['Enter','Tab','Escape'].includes(e.key)) {
      if (target) emit(describe(target, 'press', { key: e.key }));
      else emit({ type: 'press', key: e.key, role: null, name: '', nth: 0 });
      return;
    }

    // Any other modifier combo → press step with Playwright-style notation.
    // Shift-alone is NOT a shortcut (it's just capitalization), so we
    // require ctrl/cmd/alt before recording.
    if (hasModifier) {
      const mods = [];
      if (e.ctrlKey) mods.push('Control');
      if (e.altKey) mods.push('Alt');
      if (e.shiftKey) mods.push('Shift');
      if (e.metaKey) mods.push('Meta');
      let key = e.key;
      if (key.length === 1) key = key.toUpperCase();
      const combo = mods.concat([key]).join('+');
      if (target) emit(describe(target, 'press', { key: combo }));
      else emit({ type: 'press', key: combo, role: null, name: '', nth: 0 });
    }
  }, true);

  // SPA navigation detection. The browser fires `framenavigated` only when
  // a new document loads — pushState / replaceState / popstate change the
  // URL without that, so we patch them ourselves. Deduplication happens
  // on the Python side.
  try {
    let lastUrl = location.href;
    const reportNav = () => {
      const url = location.href;
      if (url === lastUrl) return;
      lastUrl = url;
      emit({ type: 'navigate', url: url });
    };
    const origPush = history.pushState;
    history.pushState = function() {
      origPush.apply(this, arguments);
      setTimeout(reportNav, 0);
    };
    const origReplace = history.replaceState;
    history.replaceState = function() {
      origReplace.apply(this, arguments);
      setTimeout(reportNav, 0);
    };
    window.addEventListener('popstate', () => setTimeout(reportNav, 0));
  } catch (e) {}
})();
"""


# Internal URLs we never want to record as navigations.
INTERNAL_URL_PREFIXES = (
    "about:", "chrome:", "chrome-error:", "edge:", "data:", "view-source:",
)

# Where the user's persistent browser profile lives.
PROFILE_DIR = os.path.join(
    os.path.expanduser("~"), ".browser-recorder", "profile"
)

# Where downloaded files land (default; user-overridable via the GUI).
DEFAULT_OUTPUT_DIR = os.path.join(
    os.path.expanduser("~"), "Downloads", "browser-recorder"
)

# Small settings file (plain JSON) so the chosen output folder persists
# between runs. Lives in the dot-folder alongside the profile/credentials.
SETTINGS_FILE = os.path.join(
    os.path.expanduser("~"), ".browser-recorder", "settings.json"
)

def _load_output_dir():
    """Return the user's chosen output folder, or the default. The output
    folder is where downloads AND the date-swap CSV are written."""
    try:
        import json as _json
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            d = _json.load(f)
        out = d.get("output_dir")
        if out:
            return out
    except Exception:
        pass
    return DEFAULT_OUTPUT_DIR

def _save_output_dir(path):
    try:
        import json as _json
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        existing = {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                existing = _json.load(f)
        except Exception:
            pass
        existing["output_dir"] = path
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            _json.dump(existing, f, indent=2)
        return True
    except Exception:
        return False

# Where downloaded files land. Resolved at startup from settings.
DOWNLOADS_DIR = _load_output_dir()

# Encrypted credentials for replay. One file per machine, in the same
# dot-folder as the profile so it's easy to find but not in Downloads.
CREDENTIALS_FILE = os.path.join(
    os.path.expanduser("~"), ".browser-recorder", "credentials.enc"
)


# ----- Encrypted credentials store -----------------------------------------

class CredentialsStore:
    """A simple passphrase-encrypted credentials store.

    The file format is JSON:
        {
          "version": 1,
          "salt_b64": "...",          # PBKDF2 salt, base64
          "data_b64": "..."           # Fernet token wrapping the JSON dict
        }

    The wrapped data is a dict keyed by 'host|field_name' (lowercased),
    e.g. 'id.vancoplatform.com|password'. Values are the actual secret.

    Encryption uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived
    from the passphrase via PBKDF2-HMAC-SHA256 (200k iterations).
    """

    PBKDF2_ITERS = 200_000
    KEY_LEN = 32  # Fernet wants 32 bytes b64-encoded

    def __init__(self, path=CREDENTIALS_FILE):
        self.path = path
        self._data = {}  # decrypted dict, only present after unlock()
        self._fernet = None  # set after unlock()

    @staticmethod
    def _check_crypto():
        try:
            from cryptography.fernet import Fernet  # noqa: F401
            from cryptography.hazmat.primitives.kdf.pbkdf2 import (
                PBKDF2HMAC,
            )  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "The 'cryptography' package is required for encrypted "
                "credentials. Install with: py -m pip install cryptography"
            )

    def _derive_key(self, passphrase, salt):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.KEY_LEN,
            salt=salt,
            iterations=self.PBKDF2_ITERS,
        )
        return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

    def exists(self):
        return os.path.exists(self.path)

    def unlock(self, passphrase):
        """Decrypt the store. Returns True on success, False on bad pw."""
        self._check_crypto()
        from cryptography.fernet import Fernet, InvalidToken

        if not self.exists():
            # Brand new store: derive a fresh salt and create empty data.
            salt = secrets.token_bytes(16)
            self._fernet = Fernet(self._derive_key(passphrase, salt))
            self._salt = salt
            self._data = {}
            self.save()
            return True

        with open(self.path, "r", encoding="utf-8") as f:
            blob = json.load(f)
        salt = base64.b64decode(blob["salt_b64"])
        key = self._derive_key(passphrase, salt)
        fernet = Fernet(key)
        try:
            decrypted = fernet.decrypt(blob["data_b64"].encode("ascii"))
        except InvalidToken:
            return False
        self._data = json.loads(decrypted.decode("utf-8"))
        self._fernet = fernet
        self._salt = salt
        return True

    def save(self):
        if self._fernet is None:
            raise RuntimeError("Store not unlocked.")
        token = self._fernet.encrypt(
            json.dumps(self._data).encode("utf-8")
        ).decode("ascii")
        blob = {
            "version": 1,
            "salt_b64": base64.b64encode(self._salt).decode("ascii"),
            "data_b64": token,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Atomic write so a crash mid-save doesn't corrupt the store.
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, self.path)

    @staticmethod
    def key_for(url_or_host, field_name):
        """Build the lookup key for a credential."""
        return core.credential_key(url_or_host, field_name)

    def get(self, url_or_host, field_name):
        return self._data.get(self.key_for(url_or_host, field_name))

    def set(self, url_or_host, field_name, value):
        self._data[self.key_for(url_or_host, field_name)] = value
        self.save()

    def delete(self, url_or_host, field_name):
        self._data.pop(self.key_for(url_or_host, field_name), None)
        self.save()

    def list_entries(self):
        """Returns list of (host, field_name, value) tuples."""
        out = []
        for key, value in sorted(self._data.items()):
            try:
                host, name = key.split("|", 1)
            except ValueError:
                host, name = key, ""
            out.append((host, name, value))
        return out


# ----- Worker thread -------------------------------------------------------

class BrowserWorker(threading.Thread):
    """Runs Playwright on a background thread. Commands in, results out.

    Owns a persistent browser context and tracks every page in it. Whichever
    page fires the most recent user event becomes the "active page" — the
    one snapshots, commands, and the element list follow.
    """

    def __init__(self, cmd_q, result_q, headless=False):
        super().__init__(daemon=True)
        self.cmd_q = cmd_q
        self.result_q = result_q
        # Run Chromium without a visible window when True (headless CLI runs).
        # Defaults False so the GUI is unchanged.
        self.headless = headless
        # per-page state: {page: {"cdp": session, "last_fp": str|None}}
        self.pages = {}
        self.active_page = None
        self.context = None
        # Dedup: download URLs we've already started saving (with timestamp,
        # so the set doesn't grow unbounded).
        self._recent_downloads = {}
        self._batch_filename = None  # per-student PDF name during batch runs
        self._batch_downloaded = False  # set when a batch student downloads
        self.replaying = False
        # Set by the GUI (from the Tk thread) to request that an in-progress
        # batch or resolve stop at the next safe point. threading.Event is
        # safe to set/clear/read across threads without a lock.
        self._stop_event = threading.Event()

    def run(self):
        if sync_playwright is None:
            self.result_q.put(("err", "launch",
                "Playwright isn't installed. Run:\n"
                "    pip install playwright\n"
                "    python -m playwright install chromium"))
            return
        os.makedirs(PROFILE_DIR, exist_ok=True)
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        with sync_playwright() as p:
            # Persistent context: profile saved to disk, cookies survive
            # restarts, downloads use the server's intended filename via
            # the `download` event.
            #
            # Download UI flags: Playwright intercepts downloads and
            # save_as()'s them to our folder, which leaves Chrome's own
            # download bubble pointing at a temp UUID file the user can't
            # actually open. Disabling the bubble keeps the UX clean — the
            # navigator surfaces downloads itself.
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=self.headless,
                args=[
                    "--start-maximized",
                    "--disable-features=DownloadBubble,DownloadBubbleV2",
                ],
                no_viewport=True,
                accept_downloads=True,
            )
            self.context = context

            # expose_binding (not expose_function) so the callback receives
            # a `source` argument identifying which page fired the event.
            # That's how we follow the user across tabs.
            context.expose_binding("__recorder_emit", self._on_binding)
            context.add_init_script(JS_LISTENER)

            # Context-level download fallback. Popups sometimes route
            # download events through the context rather than the page that
            # opened them — this is the safety net.
            def on_ctx_download(download):
                self._dlog(f"context.on('download') fired: url={download.url!r} "
                           f"suggested={download.suggested_filename!r}")
                self._save_download(download, page=None)
            try:
                context.on("download", on_ctx_download)
            except Exception:
                pass  # older Playwright versions don't support context.on('download')

            # Attach our machinery to every page that exists now or appears
            # later (new tabs, popups via window.open / target="_blank").
            def on_new_page(new_page):
                self._dlog(f"context.on('page') fired -> url={new_page.url!r}")
                self._attach_page(new_page)
                # A new tab usually means the user just clicked something
                # that opened it — follow them there.
                self.active_page = new_page
                # Re-inject the listener directly into the new page, in case
                # add_init_script didn't run before the page started loading
                # (common with window.open popups that navigate immediately).
                self._inject_listener(new_page)
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=2000)
                    self._dlog(f"  domcontentloaded -> url={new_page.url!r}")
                except Exception as e:
                    self._dlog(f"  wait_for_load_state error: {e}")
                # Inject again post-load — covers redirects within the popup.
                self._inject_listener(new_page)
                # Probe: does the binding actually exist in this page?
                try:
                    has = new_page.evaluate(
                        "() => typeof window.__recorder_emit === 'function'"
                    )
                    self._dlog(f"  binding present after load: {has}")
                except Exception as e:
                    self._dlog(f"  binding probe error: {e}")
                try:
                    snap = self._snapshot_page(new_page)
                    self.result_q.put(("snapshot", "newpage", snap))
                except Exception:
                    pass
            context.on("page", on_new_page)

            for pg in context.pages:
                self._attach_page(pg)
                self._inject_listener(pg)
            if context.pages:
                self.active_page = context.pages[-1]

            last_poll = 0.0
            last_resurrect = 0.0

            while True:
                try:
                    cmd = self.cmd_q.get(timeout=0.05)
                except queue.Empty:
                    now = time.time()
                    page = self.active_page
                    if page is not None and page in self.pages:
                        try:
                            page.wait_for_timeout(50)
                        except Exception:
                            pass
                        if now - last_poll > 0.25:
                            last_poll = now
                            self._poll_fingerprint_active()
                    else:
                        time.sleep(0.05)
                    # Resurrector runs regardless of active_page state.
                    if now - last_resurrect > 1.5:
                        last_resurrect = now
                        self._resurrect_listeners()
                    continue

                if cmd is None:
                    break

                try:
                    self.handle(cmd)
                except Exception as e:
                    self.result_q.put(("err", cmd.get("action", "?"), str(e)))

            try:
                context.close()
            except Exception:
                pass

    # --- Per-page setup ---

    def _attach_page(self, page):
        if page in self.pages:
            return  # already attached

        try:
            cdp = self.context.new_cdp_session(page)
            cdp.send("Accessibility.enable")
            cdp.send("DOM.enable")
        except Exception:
            cdp = None
        self.pages[page] = {"cdp": cdp, "last_fp": None}

        # Belt-and-braces: also register the init script on the page itself,
        # so it runs on every subsequent navigation in this tab even if the
        # context-level registration somehow missed.
        try:
            page.add_init_script(JS_LISTENER)
        except Exception:
            pass

        # framenavigated for this page. Fires for the top frame AND each
        # child frame when they navigate. We use it to (a) record top-frame
        # navigations into the recording, and (b) re-inject our listener
        # into any frame that just got a fresh document (cross-document
        # navigations destroy the JS context, so the previous injection
        # is gone).
        def on_nav(frame, _pg=page):
            try:
                url = frame.url or ""
                is_top = (frame == _pg.main_frame)
                self._dlog(f"framenavigated: top={is_top} url={url!r}")
                # Re-inject for whichever frame just navigated — applies to
                # iframes too, which is the whole point.
                if url and not url.startswith(INTERNAL_URL_PREFIXES):
                    self._inject_listener(frame)
                # Only record top-frame nav as a recording step.
                if not is_top:
                    return
                if not url or url.startswith(INTERNAL_URL_PREFIXES):
                    return
                self.result_q.put(("navigation", None, {"url": url}))
                self.active_page = _pg
            except Exception as e:
                self._dlog(f"on_nav error: {e}")
        page.on("framenavigated", on_nav)

        # New iframes attaching to the page mid-life: inject into them too.
        def on_frameattached(frame, _pg=page):
            self._dlog(f"frameattached: {frame.url!r}")
            self._inject_listener(frame)
        try:
            page.on("frameattached", on_frameattached)
        except Exception:
            pass

        # 'load' fires whenever a frame finishes loading a new document.
        # Crucially, it ALSO fires when JS calls document.write(...) to
        # replace the document — which is a common ASP.NET WebForms pattern
        # for embedded forms. framenavigated doesn't fire for that, so this
        # is the only signal we have to re-inject.
        def on_frame_load(frame, _pg=page):
            self._dlog(f"frame load: top={frame == _pg.main_frame} url={frame.url!r}")
            self._inject_listener(frame)
            # Probe whether the binding actually exists in this frame.
            try:
                has = frame.evaluate(
                    "() => typeof window.__recorder_emit === 'function'"
                )
                self._dlog(f"  binding in frame {frame.url!r}: {has}")
            except Exception as e:
                self._dlog(f"  binding probe error in {frame.url!r}: {e}")
        try:
            # 'domcontentloaded' is the per-frame equivalent on context.
            # Page-level 'load' only fires for the top frame.
            page.on("domcontentloaded", lambda fr_or_pg, _pg=page:
                    on_frame_load(fr_or_pg if hasattr(fr_or_pg, "evaluate")
                                  else _pg.main_frame, _pg))
        except Exception:
            pass

        # Some popups emit a 'popup' event on the parent page too — if any
        # of *this* page's actions opens another tab, we'll catch it via
        # context.on('page') above, but listening here as well is harmless.
        def on_popup(popup_page, _pg=page):
            self._dlog(f"page.on('popup') fired from {_pg.url!r} -> {popup_page.url!r}")
            self._attach_page(popup_page)
            self._inject_listener(popup_page)
            self.active_page = popup_page
        try:
            page.on("popup", on_popup)
        except Exception:
            pass

        # Page closed: clean up; if it was active, fall back to another.
        def on_close(_pg=page):
            self._dlog(f"page closed: {_pg.url!r}")
            self.pages.pop(_pg, None)
            if self.active_page is _pg:
                self.active_page = next(iter(self.pages), None)
        page.on("close", on_close)

        # Downloads: save to our Downloads folder using a sensible filename
        # (server's suggestion if usable, otherwise inferred from URL/title).
        def on_download(download, _pg=page):
            self._dlog(f"page.on('download') fired on {_pg.url!r}: "
                       f"download_url={download.url!r} "
                       f"suggested={download.suggested_filename!r}")
            self.active_page = _pg
            self._save_download(download, page=_pg)
        page.on("download", on_download)

    def _resurrect_listeners(self):
        """Re-inject the listener into any frame where it's gone missing.

        Handles document.write replacing a frame's document silently —
        framenavigated doesn't fire for that, so our listener vanishes
        with no signal. This poll catches it.
        """
        for page in list(self.pages.keys()):
            try:
                frames = page.frames
            except Exception:
                continue
            for fr in frames:
                try:
                    has = fr.evaluate(
                        "() => document.__recorder_doc_installed === true"
                    )
                except Exception:
                    continue  # frame may be detached or cross-origin
                if not has:
                    self._dlog(f"resurrecting listener in frame {fr.url!r}")
                    self._inject_listener(fr)

    def _inject_listener(self, page_or_frame):
        """Inject the recorder listener.

        If given a Page, installs into the top frame AND every child iframe
        (recursively). If given a Frame, installs into that frame only.

        Iframes matter because page content is often rendered inside one —
        report viewers, payment widgets, embedded apps. Our listener has
        to be in each frame for events there to be recorded.
        """
        # Page has a .frames attribute; Frame does not. Detect by attribute.
        frames = getattr(page_or_frame, "frames", None)
        if frames is None:
            # It's a Frame: inject into just it.
            try:
                page_or_frame.evaluate(JS_LISTENER)
                self._dlog(f"  injected listener into frame {page_or_frame.url!r}")
            except Exception as e:
                self._dlog(f"  inject_listener frame error in {page_or_frame.url!r}: {e}")
            return

        # It's a Page: inject into every frame.
        for fr in frames:
            try:
                fr.evaluate(JS_LISTENER)
                self._dlog(f"  injected listener into frame {fr.url!r}")
            except Exception as e:
                # Frames that are about:blank or detached can throw — fine.
                self._dlog(f"  inject_listener skipped frame {fr.url!r}: {e}")

    @staticmethod
    def _unique_path(path):
        return core.unique_path(path)

    # Junk-filename detection and the content-type→extension map now live in
    # navigator_core (single, unit-tested source). Kept as class attributes so
    # existing references (self._JUNK_FILENAME / self._MIME_EXT) still resolve.
    _JUNK_FILENAME = core.JUNK_FILENAME_RE
    _MIME_EXT = core.MIME_EXT

    @staticmethod
    def _sanitize_filename(name):
        """Strip filesystem-hostile characters; collapse whitespace."""
        return core.sanitize_filename(name)

    def _smart_filename(self, download, page):
        """Pick a useful filename for `download` when the server's
        suggestion is a UUID or otherwise unhelpful.

        Reads the page title here (Playwright I/O) and hands the plain strings
        to navigator_core.smart_filename, which holds the unit-tested logic:
        server suggestion → URL path → page title → hostname → timestamp.
        """
        title = ""
        try:
            if page is not None:
                title = (page.title() or "").strip()
        except Exception:
            pass
        return core.smart_filename(
            suggested=(download.suggested_filename or ""),
            url=(download.url or ""),
            title=title,
        )

    def _guess_extension(self, download):
        """Best-effort extension from the download URL (falls back to .bin).
        Sync Playwright's Download object exposes no response headers, so the
        URL path is all we have to go on."""
        return core.guess_extension_from_url(getattr(download, "url", "") or "")

    def _save_download(self, download, page):
        """Save the download with a sensible filename and emit a step."""
        # Dedup: a single user download often fires BOTH page.on('download')
        # and context.on('download'), and Playwright may hand each handler a
        # DISTINCT Download object — so id(download) differs and can't dedup
        # them. Instead key on (url, suggested filename) within a short time
        # window, which is stable across both handlers for the same file.
        try:
            now = time.time()
            sig = (getattr(download, "url", ""),
                   getattr(download, "suggested_filename", ""))
            self._recent_downloads = {
                k: v for k, v in self._recent_downloads.items() if now - v < 30
            }
            if sig in self._recent_downloads:
                self._dlog("  download: duplicate event ignored "
                           f"(already saved {sig[1]!r})")
                return
            self._recent_downloads[sig] = now
        except Exception:
            pass

        try:
            # During a batch run we name the file for the current student so
            # downloads don't collide: <name>_<id>_transcript.pdf.
            override = getattr(self, "_batch_filename", None)
            if override:
                _, ext = os.path.splitext(download.suggested_filename or "")
                if not ext:
                    ext = self._guess_extension(download)
                name = self._sanitize_filename(override) + (ext or ".pdf")
            else:
                name = self._smart_filename(download, page)
            target = self._unique_path(os.path.join(DOWNLOADS_DIR, name))
            download.save_as(target)
            self._batch_downloaded = True  # mark success for batch loop
            self.result_q.put(("download", None, {
                "filename": os.path.basename(target),
                "path": target,
                "url": download.url,
            }))
        except Exception as e:
            self.result_q.put(("err", "download", str(e)))

    def _dlog(self, msg):
        """Diagnostic log. Routed through navigator_core's logger: the console
        shows it only when ASAP_POWERTOOLS_DEBUG is set (quiet by default), while the
        rotating file log in the output folder always captures the full trail —
        so an unattended overnight batch leaves something to read afterwards."""
        core.dlog(msg)

    # --- Binding from injected JS ---

    def _on_binding(self, source, payload):
        # source is dict-like with page/frame/context. We mark whichever
        # page fired the event as the active one — that's how we follow
        # the user from tab to tab.
        page = None
        frame = None
        try:
            if isinstance(source, dict):
                page = source.get("page")
                frame = source.get("frame")
            else:
                page = getattr(source, "page", None)
                frame = getattr(source, "frame", None)
        except Exception:
            pass
        page_url = ""
        frame_url = ""
        try: page_url = page.url if page is not None else ""
        except Exception: pass
        try: frame_url = frame.url if frame is not None else ""
        except Exception: pass
        is_iframe = bool(frame_url and page_url and frame_url != page_url)

        # __debug events: log to terminal but don't put on the result queue.
        # They exist purely to verify the listener is reaching frame events.
        if payload.get("type") == "__debug":
            html = payload.get("html", "")
            parent_class = payload.get("parent_class", "")
            self._dlog(
                f"RAW {payload.get('stage')!r} tag={payload.get('tag')!r} "
                f"input_type={payload.get('input_type')!r} "
                f"target_role={payload.get('target_role')!r} "
                f"target_name={payload.get('target_name')!r} "
                f"parent_class={parent_class!r} "
                f"html={html!r} "
                f"page_url={page_url!r}"
                + (f" iframe={frame_url!r}" if is_iframe else "")
            )
            if page is not None:
                self.active_page = page
            return None

        # __heartbeat: fired by the listener on install. Proves the binding
        # is actually callable from this specific frame's JS world.
        if payload.get("type") == "__heartbeat":
            self._dlog(f"HEARTBEAT from page_url={page_url!r} "
                       + (f"iframe={frame_url!r} " if is_iframe else "")
                       + f"frame_doc_url={payload.get('url')!r} "
                       f"readyState={payload.get('readyState')!r}")
            return None

        self._dlog(f"_on_binding fired: type={payload.get('type')!r} "
                   f"role={payload.get('role')!r} name={payload.get('name')!r} "
                   f"page_url={page_url!r}"
                   + (f" iframe={frame_url!r}" if is_iframe else ""))
        if page is not None:
            self.active_page = page
        # Tag the event with the page URL it happened on. The navigation
        # handler uses this to recognise postback reloads (a "navigation" to
        # the same url an action just happened on) and skip recording them.
        try:
            if isinstance(payload, dict) and page_url:
                payload.setdefault("page_url", page_url)
        except Exception:
            pass
        self.result_q.put(("event", None, payload))
        return None

    # --- Commands from the UI ---

    def handle(self, cmd):
        action = cmd["action"]
        page = self.active_page
        if page is None or page not in self.pages:
            raise RuntimeError("No active page to operate on.")

        if action == "goto":
            page.goto(cmd["url"], wait_until="domcontentloaded")
        elif action == "back":
            page.go_back(wait_until="domcontentloaded")
        elif action == "reload":
            page.reload(wait_until="domcontentloaded")
        elif action == "rescan":
            pass
        elif action == "replay":
            # Replay handles its own state and emits its own events.
            # No snapshot/state-mutation here — _execute_replay does it.
            self._execute_replay(cmd["steps"])
            return
        elif action == "replay_resume":
            # Continuation after a paused step; the worker doesn't carry
            # state, so the App tracks it. The "command" carries a fresh
            # tail to execute.
            self._execute_replay(cmd["steps"], starting_index=cmd.get("starting_index", 0))
            return
        elif action == "lookup_locators":
            # Find the DOM locators (id/name) for an element identified by
            # role+name+nth, so an element inserted from the left list can be
            # targeted robustly by id rather than by fragile position.
            loc = self._lookup_locators(page, cmd.get("role"),
                                        cmd.get("name", ""), cmd.get("nth", 0))
            self.result_q.put(("locators", cmd.get("token"), loc))
            return
        elif action == "set_output_dir":
            # The GUI changed the output folder; update the module global so
            # downloads and the swap CSV use the new location.
            global DOWNLOADS_DIR
            DOWNLOADS_DIR = cmd.get("path", DOWNLOADS_DIR)
            return
        elif action == "batch_run":
            self._execute_batch(cmd["template"], cmd["student_ids"])
            return
        elif action == "resolve_run":
            self._execute_resolve(cmd["emails"],
                                  template=cmd.get("template"),
                                  auto_batch=cmd.get("auto_batch", False))
            return
        else:
            raise ValueError(f"Unknown action: {action}")

        self._settle(page)
        snap = self._snapshot_page(page)
        # Keep this page's fingerprint up to date so the idle poll doesn't
        # immediately re-snapshot.
        cdp = self.pages.get(page, {}).get("cdp")
        if cdp is not None:
            try:
                self.pages[page]["last_fp"] = self._fingerprint(cdp)
            except Exception:
                pass
        self.result_q.put(("snapshot", action, snap))

    # --- Fingerprint poll for the active page ---

    def _poll_fingerprint_active(self):
        page = self.active_page
        if page is None or page not in self.pages:
            return
        cdp = self.pages[page].get("cdp")
        if cdp is None:
            return
        try:
            fp = self._fingerprint(cdp)
        except Exception:
            return
        if fp != self.pages[page].get("last_fp"):
            self.pages[page]["last_fp"] = fp
            try:
                snap = self._snapshot(page, cdp)
                self.result_q.put(("snapshot", "auto", snap))
            except Exception:
                pass

    def _snapshot_page(self, page):
        cdp = self.pages.get(page, {}).get("cdp")
        if cdp is None:
            return {"url": page.url, "elements": []}
        return self._snapshot(page, cdp)

    def _settle(self, page):
        try: page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception: pass
        # networkidle intentionally omitted: this app keeps background traffic
        # alive, so it rarely goes idle and just costs the full timeout.

    # ----- Replay executor -------------------------------------------------

    def _execute_replay(self, steps, starting_index=0):
        """Execute a list of recorded steps, emitting progress events."""
        total = len(steps)
        self._dlog(f"=== replay start: {total} steps, from index {starting_index} ===")
        self.result_q.put(("replay_start", None, {"total": total,
                                                  "starting_index": starting_index}))
        for i in range(starting_index, total):
            step = steps[i]
            self.result_q.put(("replay_step", None, {
                "index": i, "total": total, "step": step, "status": "running"
            }))
            try:
                ok = self._execute_one(step, i)
            except Exception as e:
                self._dlog(f"replay error at step {i}: {e}")
                ok = False

            if ok:
                self._dlog(f"step {i} OK")
                self.result_q.put(("replay_step", None, {
                    "index": i, "total": total, "step": step, "status": "done"
                }))
                continue

            self._dlog(f"step {i} could not execute -> PAUSE")
            self.result_q.put(("replay_paused", None, {
                "index": i, "total": total, "step": step,
                "reason": "Could not resolve or execute step.",
            }))
            return

        self._dlog("=== replay done ===")
        self.result_q.put(("replay_done", None, {"total": total}))

    def _resolve_one_email(self, email):
        """Search a single email and read the result rows directly from the
        Students.aspx list (no clicking through). Returns a list of dicts:
        [{id, name}, ...] — empty if no match, length>1 if multiple."""
        import urllib.parse as _url
        search_url = ("https://app.asapconnected.com/Students.aspx?s="
                      + _url.quote(email))
        page = self.active_page
        if page is None:
            return []
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            self._dlog(f"  resolve: navigate failed for {email}: {e}")
            return []
        # Give the results list a moment to render.
        try: page.wait_for_timeout(600)
        except Exception: pass

        # Scrape every result row's View link: its href carries Id=..., and
        # the name spans carry first/middle/last. The row id pattern is
        # ...rptStudents_ctrlN_btnView.
        js = r"""
        () => {
          const out = [];
          const links = document.querySelectorAll(
            'a[id*="rptStudents_ctrl"][id$="_btnView"]');
          links.forEach(a => {
            const href = a.getAttribute('href') || '';
            const m = href.match(/[?&]Id=(\d+)/i);
            const id = m ? m[1] : '';
            // Name spans live inside the link.
            const root = a;
            const pick = (suffix) => {
              const el = root.querySelector('[id$="' + suffix + '"]');
              return el ? (el.textContent || '').trim() : '';
            };
            const first = pick('lblfirstName');
            const mid   = pick('lblmName');
            const last  = pick('lbllName');
            const name = [first, mid, last].filter(Boolean).join(' ')
                          .replace(/\s+/g, ' ').trim();
            if (id) out.push({id, name});
          });
          return out;
        }
        """
        try:
            rows = page.evaluate(js)
        except Exception as e:
            self._dlog(f"  resolve: scrape failed for {email}: {e}")
            rows = []
        # De-dupe by id (same student could theoretically appear twice).
        seen = set()
        uniq = []
        for r in rows or []:
            if r.get("id") and r["id"] not in seen:
                seen.add(r["id"])
                uniq.append(r)
        return uniq

    def _execute_resolve(self, emails, template=None, auto_batch=False):
        """Resolve a list of emails to studentids by searching each one and
        reading the results list. Builds a roster CSV. Emails that resolve to
        exactly one student are 'resolved'; zero -> 'not_found'; multiple ->
        'multiple_matches' (recorded, not processed). If auto_batch and a
        template are given, the cleanly-resolved ids are then run through the
        batch processor in the same go."""
        import csv as _csv
        total = len(emails)
        self._stop_event.clear()  # fresh run; discard any prior stop request
        self.replaying = True
        self._dlog(f"=== resolve start: {total} emails ===")
        self.result_q.put(("resolve_start", None, {"total": total}))

        roster = []  # (email, student_id, student_name, status, note)
        clean_ids = []  # ids that resolved to exactly one student
        for n, email in enumerate(emails, start=1):
            email = str(email).strip()
            if not email:
                continue
            if self._stop_event.is_set():
                self._dlog(f"--- resolve stopped by user before "
                           f"{n}/{total} ({email}) ---")
                break
            self.result_q.put(("resolve_progress", None, {
                "current": n, "total": total, "email": email}))
            self._dlog(f"--- resolve {n}/{total}: {email} ---")

            matches = self._resolve_one_email(email)
            if len(matches) == 1:
                sid = matches[0]["id"]
                name = matches[0]["name"]
                roster.append((email, sid, name, "resolved", ""))
                clean_ids.append(sid)
                self._dlog(f"  resolve: {email} -> {sid} ({name})")
            elif len(matches) == 0:
                roster.append((email, "", "", "not_found",
                               "no student matched this email"))
                self._dlog(f"  resolve: {email} -> NOT FOUND")
            else:
                ids = "; ".join(m["id"] for m in matches)
                names = "; ".join(m["name"] for m in matches)
                roster.append((email, ids, names, "multiple_matches",
                               f"{len(matches)} records; needs manual merge"))
                self._dlog(f"  resolve: {email} -> MULTIPLE ({ids})")

        self.replaying = False

        # Write the roster CSV.
        roster_path = os.path.join(DOWNLOADS_DIR, "email_roster.csv")
        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            with open(roster_path, "w", encoding="utf-8", newline="") as f:
                w = _csv.writer(f)
                w.writerow(["email", "student_id", "student_name",
                            "status", "note"])
                w.writerows(roster)
        except Exception as e:
            self._dlog(f"  resolve: could not write roster csv: {e}")

        resolved = sum(1 for r in roster if r[3] == "resolved")
        not_found = sum(1 for r in roster if r[3] == "not_found")
        multiple = sum(1 for r in roster if r[3] == "multiple_matches")
        self._dlog(f"=== resolve done: {resolved} resolved, {not_found} "
                   f"not found, {multiple} multiple ===")
        will_batch = bool(auto_batch and template and clean_ids
                          and not self._stop_event.is_set())
        self.result_q.put(("resolve_done", None, {
            "total": total, "resolved": resolved, "not_found": not_found,
            "multiple": multiple, "roster_path": roster_path,
            "clean_ids": clean_ids,
            "stopped": self._stop_event.is_set(),
            "will_batch": will_batch}))

        # Chain straight into the batch for the cleanly-resolved students,
        # unless the user asked to stop during the resolve phase.
        if will_batch:
            self._dlog(f"  resolve: auto-running batch for {len(clean_ids)} "
                       f"cleanly-resolved students")
            self._execute_batch(template, clean_ids, _chained=True)

    def _execute_batch(self, template, student_ids, _chained=False):
        """Run the template steps once per student id. Unattended: a student
        that fails is recorded and the batch moves on. Emits progress events
        and writes a combined batch_results.csv."""
        import csv as _csv
        total = len(student_ids)
        # On a direct batch run, discard any prior stop request. When chained
        # from resolve, the flag was already managed there (a stop during
        # resolve prevents this call), so leave it alone.
        if not _chained:
            self._stop_event.clear()
        self.replaying = True  # suppress recording of our own actions
        self._dlog(f"=== batch start: {total} students ===")
        self.result_q.put(("batch_start", None, {"total": total}))

        results = []  # (timestamp, student_id, name, status, note)

        # Find the boundary between one-time "get into the classic app" setup
        # steps and the per-student work. Everything BEFORE the first step that
        # references {{studentid}} (i.e. the navigate to StudentDetail) is the
        # admin->classic crossing — it only needs to run once, for the first
        # student. After that we jump straight to each student's URL.
        def _has_var(step):
            for k in ("url", "value", "label"):
                v = step.get(k)
                if isinstance(v, str) and "{{studentid}}" in v:
                    return True
            return False
        per_student_start = 0
        for idx, st in enumerate(template):
            if _has_var(st):
                per_student_start = idx
                break
        self._dlog(f"  batch: per-student steps start at index "
                   f"{per_student_start} (steps before it run once)")
        self._batch_crossed = False  # set True once the admin->classic crossing runs

        for n, item in enumerate(student_ids, start=1):
            # Each item is either a bare student id (the common case — GUI or a
            # plain id list) or a dict of template variables (CSV-driven
            # multi-variable batch). Normalise to (sid, values): `values` feeds
            # {{var}} substitution, while `sid` keys the per-student filename,
            # the resume-skip check, and the admin→classic crossing, so it
            # always comes from the studentid variable.
            if isinstance(item, dict):
                values = {k: ("" if v is None else str(v))
                          for k, v in item.items()}
                sid = values.get("studentid", "").strip()
            else:
                sid = str(item).strip()
                values = {"studentid": sid}
            if not sid:
                continue

            # Stop requested from the GUI: finish the student that already
            # completed, stop before starting this one. Logged so the summary
            # makes clear the run was interrupted, not finished.
            if self._stop_event.is_set():
                self._dlog(f"--- batch stopped by user before student "
                           f"{n}/{total} (id={sid}) ---")
                break

            # Resumable batches: if this student's transcript already exists in
            # the output folder, skip them. We match any file containing the
            # id followed by '_transcript' before the extension, which covers
            # both "<name>_<id>_transcript.pdf" and "<id>_transcript.pdf"
            # (including the "(1)" suffixes _unique_path may add).
            existing = None
            try:
                for fn in os.listdir(DOWNLOADS_DIR):
                    low = fn.lower()
                    if (f"_{sid}_transcript" in low
                            or low.startswith(f"{sid}_transcript")):
                        existing = fn
                        break
            except Exception:
                existing = None
            if existing:
                self._dlog(f"--- batch student {n}/{total}: id={sid} "
                           f"-> already downloaded ({existing}); skipping ---")
                results.append((
                    datetime.now().isoformat(timespec="seconds"),
                    sid, "", "skipped", f"already downloaded: {existing}"))
                self.result_q.put(("batch_progress", None, {
                    "current": n, "total": total, "student_id": sid,
                    "status": "skipped"}))
                continue

            self.result_q.put(("batch_progress", None, {
                "current": n, "total": total, "student_id": sid,
                "status": "running"}))
            self._dlog(f"--- batch student {n}/{total}: id={sid} ---")

            # Substitute all template variables for this run: {{studentid}}
            # plus any extra columns supplied by a CSV-driven batch.
            steps = self._substitute_vars(template, values)

            student_name = ""
            status = "success"
            note = ""
            # Default the per-student filename to the id immediately, so a
            # download can NEVER collide with another student even if we fail
            # to read the student's name. If the name is captured below, we
            # upgrade this to "<name>_<id>_transcript".
            self._batch_filename = f"{sid}_transcript"
            self._batch_downloaded = False  # set by _save_download

            # The admin->classic crossing (steps 0..per_student_start-1) must
            # run once before any direct StudentDetail jump works. Tie it to
            # the first student we ACTUALLY execute — not n==1 — because with
            # resumable skipping the first executed student may be later in the
            # list. self._batch_crossed is set True after the crossing runs.
            if not getattr(self, "_batch_crossed", False):
                start_at = 0
            else:
                start_at = per_student_start

            # Self-healing: after the per-student navigate, confirm we actually
            # landed on THIS student's page. If not (e.g. session bounced us
            # elsewhere), re-run the full crossing once for this student before
            # giving up.
            nonlocal_state = {"name": "", "last_index": -1, "last_type": None}
            def run_from(start_index):
                for i in range(start_index, len(steps)):
                    step = steps[i]
                    nonlocal_state["last_index"] = i
                    nonlocal_state["last_type"] = step.get("type")
                    # Capture the student name, but ONLY once the browser is
                    # actually on THIS student's detail page. Reading too early
                    # (before the navigate) would grab the *previous* student's
                    # name, since their page is still loaded. We confirm by
                    # checking that this student's id is in the current URL.
                    if not nonlocal_state["name"] and self.active_page:
                        try:
                            cur_url = self.active_page.url or ""
                        except Exception:
                            cur_url = ""
                        if sid in cur_url and "StudentDetail" in cur_url:
                            nm = self._read_student_name(self.active_page)
                            if nm:
                                nonlocal_state["name"] = nm
                                self._batch_filename = \
                                    f"{nm}_{sid}_transcript"
                    ok = self._execute_one(step, i)
                    if not ok:
                        return False
                    # Once we've downloaded AND closed the popup, this student
                    # is fully done. Stop here rather than running any trailing
                    # steps (e.g. stray no-op download steps left in a template),
                    # which on the LAST student could otherwise stall the batch.
                    if step.get("type") == "close_page" and \
                            self._batch_downloaded:
                        return True
                return True

            try:
                completed = run_from(start_at)
                if start_at == 0:
                    # We just ran (or attempted) the crossing; don't run it
                    # again for later students.
                    self._batch_crossed = True
                # If we skipped the crossing but never reached this student's
                # page (jump didn't land), retry once from the very top.
                if not completed and not self._batch_downloaded and start_at > 0:
                    landed = False
                    try:
                        landed = (sid in (self.active_page.url or "")) \
                            if self.active_page else False
                    except Exception:
                        landed = False
                    if not landed:
                        self._dlog(f"  batch: student {sid} direct jump didn't "
                                   f"land; retrying with full crossing")
                        nonlocal_state["name"] = ""
                        self._batch_filename = f"{sid}_transcript"
                        completed = run_from(0)
                        self._batch_crossed = True

                student_name = nonlocal_state["name"]
                if not completed:
                    i = nonlocal_state["last_index"]
                    typ = nonlocal_state["last_type"]
                    if self._batch_downloaded:
                        note = (f"completed; stopped at trailing step {i} "
                                f"({typ})")
                        self._dlog(f"  batch: student {sid} already downloaded; "
                                   f"ignoring trailing step {i} failure")
                    else:
                        status = "failed"
                        note = f"step {i} ({typ}) could not execute"
                        self._dlog(f"  batch: student {sid} {note} -> "
                                   f"skipping to next student")
                if not self._batch_filename:
                    if student_name:
                        self._batch_filename = \
                            f"{student_name}_{sid}_transcript"
                    else:
                        self._batch_filename = f"{sid}_transcript"
            except Exception as e:
                status = "failed" if not self._batch_downloaded else "success"
                note = f"exception: {e}"
                student_name = nonlocal_state["name"]
                self._dlog(f"  batch: student {sid} errored: {e}")

            results.append((
                datetime.now().isoformat(timespec="seconds"),
                sid, student_name or "", status, note))
            self.result_q.put(("batch_progress", None, {
                "current": n, "total": total, "student_id": sid,
                "name": student_name, "status": status}))

        self._batch_filename = None
        self.replaying = False

        # Write the combined batch-results CSV.
        csv_path = os.path.join(DOWNLOADS_DIR, "batch_results.csv")
        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                w = _csv.writer(f)
                w.writerow(["timestamp", "student_id", "student_name",
                            "status", "note"])
                w.writerows(results)
        except Exception as e:
            self._dlog(f"  batch: could not write results csv: {e}")

        succeeded = sum(1 for r in results if r[3] == "success")
        failed = sum(1 for r in results if r[3] == "failed")
        skipped = sum(1 for r in results if r[3] == "skipped")
        self._dlog(f"=== batch done: {succeeded} ok, {failed} failed, "
                   f"{skipped} skipped ===")
        self.result_q.put(("batch_done", None, {
            "total": total, "succeeded": succeeded, "failed": failed,
            "skipped": skipped, "csv_path": csv_path,
            "stopped": self._stop_event.is_set(),
            "failed_ids": [r[1] for r in results if r[3] == "failed"]}))

    @staticmethod
    def _substitute_vars(template, values):
        """Return a deep copy of the template steps with {{var}} markers
        replaced by `values` (url/value/label fields only). Delegates to
        navigator_core.substitute_vars (whitespace-tolerant, unit-tested)."""
        return core.substitute_vars(template, values)

    def _execute_one(self, step, index):
        """Execute a single step. Returns True on success, False if the
        element couldn't be found or the action failed."""
        page = self.active_page
        if page is None or page not in self.pages:
            self._dlog(f"replay step {index}: no active page")
            return False

        t = step.get("type")
        self._dlog(f"replay step {index}: type={t!r} role={step.get('role')!r} "
                   f"name={step.get('name')!r} nth={step.get('nth')!r} "
                   f"on url={page.url!r}")

        # --- Navigation steps don't need element resolution -----------------
        if t == "navigate":
            url = step.get("url")
            if not url:
                return False
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                self._wait_for_change(page, settle_only=True)
                self._dlog(f"  navigated to {url!r}")
                return True
            except Exception as e:
                self._dlog(f"  navigate failed: {e}")
                return False

        if t == "download":
            self._dlog("  download step — skipped (side effect of click)")
            return True

        if t == "swap_dates":
            return self._execute_swap_dates(page, step)

        if t == "select_kendo":
            return self._execute_select_kendo(page, step)

        if t == "close_page":
            # Close the current page/popup (e.g. the transcript report window
            # after the download). The page.on('close') handler will move
            # active_page back to a remaining page automatically.
            try:
                try:
                    already_closed = page.is_closed()
                except Exception:
                    already_closed = False
                if already_closed:
                    self._dlog("  close_page: page already closed; skipping")
                    return True
                if len(self.pages) <= 1:
                    self._dlog("  close_page: only one page open; skipping "
                               "(won't close the last window)")
                    return True
                page.close()
                self._dlog("  close_page: closed current page")
                # Give the close handler a moment to repoint active_page.
                try:
                    time.sleep(0.3)
                except Exception:
                    pass
                return True
            except Exception as e:
                self._dlog(f"  close_page failed: {e}")
                # A failed close on an already-gone popup is not fatal; the
                # download already succeeded, so treat it as done.
                return True

        # --- Element-targeted steps -----------------------------------------
        role = step.get("role")
        name = step.get("name", "") or ""
        nth = step.get("nth", 0) or 0
        value = step.get("value")
        locators = step.get("locators") or {}

        if not role and t != "press":
            self._dlog(f"  step has no role; type={t!r} -> "
                       + ("skip" if t == "press" else "fail"))
            return t == "press"

        active_cdp = self.pages.get(page, {}).get("cdp")
        before_fp = None
        if active_cdp is not None:
            try:
                before_fp = self._fingerprint(active_cdp)
            except Exception:
                pass

        target = self._resolve_target(page, role, name, nth, locators)
        if target is None and t != "press":
            # The element may not have rendered yet — common on heavy pages
            # that assemble iframes/widgets after load. Retry with backoff
            # for up to ~6 seconds before giving up.
            self._dlog("  target not found; retrying with backoff")
            waited = 0
            for wait_ms in core.TARGET_RETRY_BACKOFF_MS:
                try:
                    page.wait_for_timeout(wait_ms)
                except Exception:
                    pass
                waited += wait_ms
                target = self._resolve_target(page, role, name, nth, locators)
                if target is not None:
                    self._dlog(f"  target appeared after {waited}ms")
                    break
            if target is None:
                self._dlog(f"  target STILL not found after {waited}ms -> pausing")
                return False
        if target is not None:
            self._dlog("  target resolved")

        try:
            if t == "click":
                target.scroll_into_view_if_needed(timeout=3000)
                target.click(timeout=5000)

            elif t == "fill":
                target.scroll_into_view_if_needed(timeout=3000)
                if value is None:
                    self._dlog("  fill value is None (unfilled secret) -> pausing")
                    return False
                # Clear first so any stray content (e.g. residue from an
                # earlier step) is wiped, then enter the recorded value.
                try:
                    target.fill("", timeout=2000)
                except Exception:
                    pass
                target.fill(value, timeout=5000)
                try: target.evaluate("(el) => el.blur()")
                except Exception: pass

            elif t == "check":
                target.scroll_into_view_if_needed(timeout=3000)
                target.check(timeout=5000)

            elif t == "uncheck":
                target.scroll_into_view_if_needed(timeout=3000)
                target.uncheck(timeout=5000)

            elif t == "select_option":
                target.scroll_into_view_if_needed(timeout=3000)
                label = step.get("label")
                # Prefer the visible LABEL ("High School Diploma") over the
                # value: option values are often per-record database ids that
                # differ between students, while the label is stable. Fall
                # back to value only if there's no label or the label fails.
                selected = False
                if label:
                    try:
                        target.select_option(label=label, timeout=1500)
                        selected = True
                        self._dlog(f"  select_option: by label {label!r}")
                    except Exception:
                        self._dlog(f"  select_option: label {label!r} not "
                                   f"found, trying value")
                if not selected and value is not None:
                    try:
                        target.select_option(value=value, timeout=1500)
                        selected = True
                        self._dlog(f"  select_option: by value {value!r}")
                    except Exception:
                        pass
                if not selected:
                    self._dlog(f"  select_option: neither label {label!r} nor "
                               f"value {value!r} found -> pausing")
                    return False

            elif t == "press":
                key = step.get("key", "")
                if not key:
                    return False
                if target is not None:
                    target.press(key, timeout=5000)
                else:
                    page.keyboard.press(key)

            elif t in ("copy", "cut", "paste"):
                # Clipboard operations are no-ops during replay. At record
                # time a paste inserted whatever the user had copied, but at
                # replay time the system clipboard holds something unrelated —
                # replaying Ctrl+V would dump that junk into the field. The
                # meaningful result of any paste is always captured by the
                # 'fill' (or 'change') step that follows it, so we skip these.
                self._dlog(f"  {t}: skipped (clipboard op, not meaningful in replay)")
                return True

            elif t == "select_all":
                self._dlog("  select_all: skipped (selection op, no effect to replay)")
                return True

            elif t == "undo":
                (target.press("Control+z", timeout=5000) if target is not None
                 else page.keyboard.press("Control+z"))

            elif t == "redo":
                (target.press("Control+y", timeout=5000) if target is not None
                 else page.keyboard.press("Control+y"))

            else:
                self._dlog(f"  unknown step type {t!r}; skipping")
                return True

        except Exception as e:
            self._dlog(f"  action {t!r} failed: {e}")
            return False

        self._dlog(f"  action {t!r} executed; waiting for settle")
        # Only actions that typically trigger a navigation/postback are worth
        # polling the page for a content change. Checkboxes and clipboard ops
        # don't navigate, so polling them just burns the full timeout waiting
        # for a change that never comes — settle quickly instead.
        likely_changes_page = t in (
            "click", "press", "select_option",
        )
        if likely_changes_page and active_cdp is not None and before_fp is not None:
            # 4s ceiling: a real postback changes the fingerprint within a
            # few hundred ms and returns immediately; this cap only bounds the
            # case where nothing actually changes.
            self._wait_for_change(page, before_fp=before_fp, cdp=active_cdp,
                                  max_wait_ms=4000)
        else:
            self._wait_for_change(page, settle_only=True)
        return True

    def _execute_select_kendo(self, page, step):
        """Select an option in a Kendo UI dropdownlist by its visible text.

        Kendo dropdownlists (data-role="dropdownlist") wrap a hidden <input>;
        the options are not native <option> elements, so Playwright's
        select_option doesn't apply. Instead we drive the widget through its
        own JS API: find the input by id, get its kendoDropDownList instance,
        and select the item whose text matches. We then fire change so the
        page reacts (these often trigger a postback/filter), and verify the
        widget's text now matches the target.

        Step fields:
          - locators.id  (or id_suffix): the underlying input id, e.g. cmbProgram
          - label (preferred) or value: the visible option text to choose
        """
        locators = step.get("locators") or {}
        target_text = step.get("label") or step.get("value") or ""
        if not target_text:
            self._dlog("  select_kendo: no label/value to select -> pausing")
            return False

        cid = locators.get("id")
        id_suffix = locators.get("id_suffix")
        if not cid and not id_suffix:
            self._dlog("  select_kendo: no element id in locators -> pausing")
            return False

        # JS run inside the page: locate the input, get its Kendo widget,
        # select by text, fire change. Returns the widget's resulting text,
        # or an error string prefixed with 'ERR:'.
        js = r"""
        (args) => {
          const [exactId, idSuffix, wantText] = args;
          function findInput() {
            if (exactId) {
              const el = document.getElementById(exactId);
              if (el) return el;
            }
            if (idSuffix) {
              const all = document.querySelectorAll('input[id]');
              for (const el of all) {
                if (el.id.endsWith(idSuffix)) return el;
              }
            }
            return null;
          }
          const input = findInput();
          if (!input) return 'ERR:input-not-found';
          // jQuery + Kendo must be present.
          if (!window.jQuery) return 'ERR:no-jquery';
          const $ = window.jQuery;
          const widget = $(input).data('kendoDropDownList')
                      || $(input).data('kendoComboBox');
          if (!widget) return 'ERR:no-kendo-widget';
          // Select by visible text (case-insensitive exact match).
          const want = (wantText || '').trim().toLowerCase();
          // Use widget.select(fn) to find matching item by its display text.
          try {
            widget.select(function(dataItem) {
              const t = (widget.options.dataTextField
                         ? dataItem[widget.options.dataTextField]
                         : dataItem.text || dataItem);
              return (String(t).trim().toLowerCase() === want);
            });
            widget.trigger('change');
          } catch (e) {
            return 'ERR:select-failed:' + e.message;
          }
          return widget.text();
        }
        """
        # The widget lives on the transcript popup. During a batch the popup
        # may not yet be the "active" page when this step runs (race between
        # the popup opening and us switching to it), so search across ALL open
        # pages, and retry with backoff to wait the popup out.
        def attempt():
            try:
                pages = list(self.pages.keys())
            except Exception:
                pages = [page]
            # Prefer a transcript/report popup if one is open.
            def page_rank(p):
                try:
                    u = p.url or ""
                except Exception:
                    u = ""
                return 0 if "Transcript" in u or "Report" in u else 1
            pages.sort(key=page_rank)
            for pg in pages:
                try:
                    frames = [pg.main_frame] + [
                        f for f in pg.frames if f != pg.main_frame]
                except Exception:
                    frames = [pg.main_frame]
                for fr in frames:
                    try:
                        r = fr.evaluate(js, [cid, id_suffix, target_text])
                    except Exception:
                        continue
                    if r and not str(r).startswith("ERR:input-not-found"):
                        # Found the widget on this page — make it active so
                        # subsequent steps (checkboxes, Filter, Print) target
                        # the right window.
                        self.active_page = pg
                        return r
            return None

        result = None
        for wait_ms in core.KENDO_BACKOFF_MS:
            if wait_ms:
                try: page.wait_for_timeout(wait_ms)
                except Exception: pass
            result = attempt()
            if result is not None:
                break
        if result is None:
            self._dlog("  select_kendo: widget input not found on any open "
                       "page (popup not ready?) -> pausing")
            return False
        if str(result).startswith("ERR:"):
            self._dlog(f"  select_kendo: {result} -> pausing")
            return False

        # Verify the widget text now matches the target (case-insensitive).
        if str(result).strip().lower() != target_text.strip().lower():
            self._dlog(f"  select_kendo: after select, widget shows "
                       f"{result!r} but wanted {target_text!r} -> pausing")
            return False

        self._dlog(f"  select_kendo: selected {target_text!r} (widget now "
                   f"shows {result!r})")
        self._wait_for_change(self.active_page or page, settle_only=True)
        return True

    def _execute_swap_dates(self, page, step):
        """Ensure the diploma date is LATER than the graduation date.

        Reads both Telerik date fields by their stable id suffixes
        (txtDiplomaDate / txtGraduationDate), compares them, and if the
        diploma date is earlier than graduation, swaps the two values.
        Equal dates are left alone. Every swap is logged with the student id.

        Returns True on success (whether or not a swap was needed), False if
        the fields couldn't be found or a swap didn't take effect.
        """
        diploma_suffix = step.get("diploma_suffix", "txtDiplomaDate")
        grad_suffix = step.get("graduation_suffix", "txtGraduationDate")

        # Find the two date inputs across all frames by id suffix. Telerik
        # renders the visible input with id "<base>_dateInput".
        def find_date_input(suffix):
            sel = f'input[id$="{suffix}_dateInput"]'
            try:
                frames = [page.main_frame] + [
                    f for f in page.frames if f != page.main_frame]
            except Exception:
                frames = [page.main_frame]
            for fr in frames:
                try:
                    loc = fr.locator(sel)
                    if loc.count() > 0:
                        return loc.first
                except Exception:
                    continue
            return None

        # Find both date inputs, with retry-and-backoff. Selecting the
        # program just before this step triggers an ASP.NET postback that
        # reloads the page; the date fields may not have re-rendered yet when
        # we first look. The two fields can also appear at slightly different
        # moments, so we wait until BOTH are present (or time out ~7s).
        diploma = grad = None
        waited = 0
        for wait_ms in core.SWAP_DATES_BACKOFF_MS:
            if wait_ms:
                try: page.wait_for_timeout(wait_ms)
                except Exception: pass
                waited += wait_ms
            diploma = find_date_input(diploma_suffix)
            grad = find_date_input(grad_suffix)
            if diploma is not None and grad is not None:
                if waited:
                    self._dlog(f"  swap_dates: both date fields present after "
                               f"{waited}ms")
                break
        if diploma is None or grad is None:
            self._dlog(f"  swap_dates: could not find both date fields after "
                       f"{waited}ms (diploma={'ok' if diploma else 'MISSING'}, "
                       f"graduation={'ok' if grad else 'MISSING'}) -> pausing")
            return False

        try:
            diploma_val = (diploma.input_value(timeout=3000) or "").strip()
            grad_val = (grad.input_value(timeout=3000) or "").strip()
        except Exception as e:
            self._dlog(f"  swap_dates: could not read values: {e}")
            return False

        self._dlog(f"  swap_dates: diploma={diploma_val!r} grad={grad_val!r}")

        # Date parsing lives in navigator_core (shared, unit-tested).
        parse = core.parse_date

        d_date = parse(diploma_val)
        g_date = parse(grad_val)
        if d_date is None or g_date is None:
            self._dlog(f"  swap_dates: couldn't parse one/both dates "
                       f"(diploma={diploma_val!r}, grad={grad_val!r}) -> pausing")
            return False

        # Rule: diploma must be LATER than graduation.
        if d_date > g_date:
            self._dlog("  swap_dates: diploma already later than graduation; "
                       "no change needed")
            return True
        if d_date == g_date:
            self._dlog("  swap_dates: dates equal; leaving alone")
            return True

        # Diploma is earlier → swap.
        self._dlog(f"  swap_dates: diploma earlier than graduation -> SWAPPING "
                   f"(diploma {diploma_val} <-> graduation {grad_val})")

        if not self._set_telerik_date(diploma, grad_val):
            self._dlog("  swap_dates: failed to set diploma field")
            return False
        if not self._set_telerik_date(grad, diploma_val):
            self._dlog("  swap_dates: failed to set graduation field")
            return False

        # Verify the swap actually took effect.
        try:
            page.wait_for_timeout(300)
            new_diploma = (diploma.input_value(timeout=3000) or "").strip()
            new_grad = (grad.input_value(timeout=3000) or "").strip()
        except Exception:
            new_diploma = new_grad = ""
        if parse(new_diploma) != g_date or parse(new_grad) != d_date:
            self._dlog(f"  swap_dates: swap did NOT take effect "
                       f"(diploma now {new_diploma!r}, grad now {new_grad!r}) "
                       f"-> pausing")
            return False

        self._dlog(f"  swap_dates: swap confirmed (diploma={new_diploma}, "
                   f"grad={new_grad})")

        # Log the swap, tied to the student id (from URL) and name (from page).
        student_name = self._read_student_name(page)
        self._log_date_swap(page.url, student_name, diploma_val, grad_val,
                            new_diploma, new_grad)
        return True

    def _read_student_name(self, page):
        """Best-effort read of the student's name from the StudentDetail page,
        for the swap audit log. Tries a few common spots; returns '' if not
        found (the log still records the id either way)."""
        js = r"""
        () => {
          const tryText = (sel) => {
            const el = document.querySelector(sel);
            return el ? (el.textContent || '').trim() : '';
          };
          // Common spots for the student name on ASAP StudentDetail pages.
          // The confirmed one is the bold span id ...txtStudentName.
          let n = tryText('#ContentPlaceHolder1_txtStudentName')
               || tryText('[id$="txtStudentName"]')
               || tryText('[id$="lblStudentName"]')
               || tryText('[id$="lblName"]');
          // Deliberately NOT falling back to h1/h2/title here: on this app
          // those held generic text ("Internal Records") rather than the name.
          return (n || '').replace(/\s+/g, ' ').trim();
        }
        """
        try:
            frames = [page.main_frame] + [
                f for f in page.frames if f != page.main_frame]
        except Exception:
            frames = [page.main_frame]
        for fr in frames:
            try:
                n = fr.evaluate(js)
            except Exception:
                continue
            if n:
                return n
        return ""

    def _set_telerik_date(self, locator, value):
        """Set a Telerik RadDatePicker text input to `value` and fire the
        events the widget needs to commit it (focus, input, change, blur).
        Returns True if the visible value reads back as `value`."""
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=3000)
            locator.fill("", timeout=2000)
            locator.fill(value, timeout=3000)
            # Telerik commits on change/blur; dispatch them explicitly.
            locator.evaluate("""(el, v) => {
                el.value = v;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur();
            }""", value)
            return True
        except Exception as e:
            self._dlog(f"    _set_telerik_date error: {e}")
            return False

    def _log_date_swap(self, url, student_name, old_diploma, old_grad,
                       new_diploma, new_grad):
        """Append a record of a date swap to a CSV file, tied to the student
        name and id. Writes a header row the first time so the file opens
        cleanly in Excel."""
        import re as _re
        import csv as _csv
        student_id = "unknown"
        m = _re.search(r"[?&]Id=(\d+)", url or "", _re.IGNORECASE)
        if not m:
            m = _re.search(r"StudentID=(\d+)", url or "", _re.IGNORECASE)
        if m:
            student_id = m.group(1)

        # The swap log lives alongside the downloads, in the user's chosen
        # output folder, so it's easy to find (not hidden).
        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        except Exception:
            pass
        log_path = os.path.join(DOWNLOADS_DIR, "date_swaps.csv")
        header = ["timestamp", "student_name", "student_id",
                  "diploma_before", "diploma_after",
                  "graduation_before", "graduation_after"]
        row = [datetime.now().isoformat(timespec="seconds"),
               student_name or "", student_id,
               old_diploma, new_diploma, old_grad, new_grad]
        def _write(path):
            need_header = not os.path.exists(path) \
                or os.path.getsize(path) == 0
            # newline="" is the correct setting for the csv module on Windows
            # so it doesn't insert blank lines between rows.
            with open(path, "a", encoding="utf-8", newline="") as f:
                w = _csv.writer(f)
                if need_header:
                    w.writerow(header)
                w.writerow(row)

        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
        except Exception:
            pass

        # The file is often open in Excel during a batch, which locks it on
        # Windows (PermissionError). Retry a few times, then fall back to a
        # sidecar file so the swap record is never silently lost.
        wrote = False
        for attempt in range(4):
            try:
                _write(log_path)
                wrote = True
                break
            except PermissionError:
                time.sleep(0.5)
            except Exception as e:
                self._dlog(f"  swap_dates: could not write log: {e}")
                break
        if wrote:
            self._dlog(f"  swap_dates: logged to {log_path}")
        else:
            # Locked the whole time — write to a timestamped sidecar instead.
            alt = os.path.join(
                os.path.dirname(log_path),
                "date_swaps_" + datetime.now().strftime("%Y%m%d") + ".csv")
            try:
                _write(alt)
                self._dlog(f"  swap_dates: main log was locked (open in "
                           f"Excel?); wrote to {alt} instead")
            except Exception as e:
                self._dlog(f"  swap_dates: could not write log (locked) "
                           f"and sidecar failed: {e}")

    def _wait_for_change(self, page, before_fp=None, cdp=None,
                        max_wait_ms=3000, settle_only=False):
        """Wait for the page to be ready for the next action.

        If `before_fp` is given, poll the accessibility tree until it differs
        (page content changed) or `max_wait_ms` elapses. Otherwise just do
        the standard load-state waits.
        """
        # Wait only for DOM-ready, not networkidle. networkidle waits for
        # 500ms of network silence, but chatty ASP.NET/Kendo pages keep
        # background traffic alive and often never go idle — so it burned the
        # full timeout (~2s) on almost every step. domcontentloaded is the
        # fast, reliable signal; genuine content changes are caught below by
        # the fingerprint poll.
        try: page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception: pass

        if settle_only or cdp is None or before_fp is None:
            return

        deadline = time.time() + (max_wait_ms / 1000.0)
        # A postback/navigation, if it's going to happen, begins almost
        # immediately. If the fingerprint hasn't changed within this short
        # grace window, the action very likely didn't change the page (e.g.
        # clicking into a textbox), so we stop waiting rather than polling the
        # full ceiling. The full ceiling still applies once a change starts.
        grace_deadline = time.time() + 1.2
        time.sleep(0.15)
        saw_change = False
        while time.time() < deadline:
            try:
                fp = self._fingerprint(cdp)
            except Exception:
                fp = None
            if fp and fp != before_fp:
                # Content changed. Give a tick more for full render.
                time.sleep(0.2)
                return
            if not saw_change and time.time() > grace_deadline:
                # No change within the grace window -> action didn't navigate.
                return
            time.sleep(0.2)
        # Timed out — that's fine; some actions genuinely don't change anything.

    def _resolve_by_locators(self, page, locators, role):
        """Resolve an element using stable identifiers captured at record
        time. Tries, in order: data-testid, exact id, id suffix, exact name
        attribute, name suffix. Searches the top frame and all child frames.
        Returns a Locator or None.
        """
        if not locators:
            return None

        # Build a list of (description, css_selector) to try in priority order.
        attempts = []
        testid = locators.get("testid")
        if testid:
            esc = testid.replace('"', '\\"')
            attempts.append(("data-testid", f'[data-testid="{esc}"], '
                                            f'[data-test="{esc}"], '
                                            f'[data-qa="{esc}"]'))
        cid = locators.get("id")
        if cid:
            attempts.append(("id exact", f'#{self._css_escape(cid)}'))
        id_suffix = locators.get("id_suffix")
        if id_suffix:
            # ASP.NET prefix drift: match ids ENDING in the suffix.
            attempts.append(("id suffix", f'[id$="{id_suffix}"]'))
        nm = locators.get("name_attr")
        if nm:
            esc = nm.replace('"', '\\"')
            attempts.append(("name exact", f'[name="{esc}"]'))
        name_suffix = locators.get("name_suffix")
        if name_suffix:
            attempts.append(("name suffix", f'[name$="{name_suffix}"]'))

        if not attempts:
            return None

        # Search top frame, then child frames.
        try:
            frames = [page.main_frame] + [
                f for f in page.frames if f != page.main_frame]
        except Exception:
            frames = [page.main_frame]

        for desc, sel in attempts:
            for fr in frames:
                try:
                    loc = fr.locator(sel)
                    cnt = loc.count()
                except Exception:
                    continue
                if cnt == 0:
                    continue
                # If multiple match (suffix matches can), prefer a visible one.
                for i in range(min(cnt, 5)):
                    cand = loc.nth(i)
                    try:
                        if cand.is_visible(timeout=800):
                            self._dlog(f"  resolve: by {desc} {sel!r} "
                                       f"(count={cnt}) in frame {fr.url!r}")
                            return cand
                    except Exception:
                        self._dlog(f"  resolve: by {desc} {sel!r} (not vis-checkable) "
                                   f"in frame {fr.url!r}")
                        return cand
        return None

    @staticmethod
    def _css_escape(ident):
        """Escape a string for use as a CSS #id selector. ASP.NET ids can
        contain characters (like '$') that need escaping in CSS."""
        out = []
        for ch in ident:
            if ch.isalnum() or ch in "-_":
                out.append(ch)
            else:
                out.append("\\" + ch)
        return out and "".join(out) or ident

    def _resolve_target(self, page, role, name, nth, locators=None):
        """Find the element matching role/name/nth on the active page.

        Tries stable identifiers (id, name attribute, data-testid) FIRST,
        since those survive across different data (e.g. different students)
        where positional nth indices do not. Falls back to the accessible
        name and positional strategies.

        Name-first and conservative for the fallbacks. For steps that HAVE a
        name we never fall back to a role-only match — 'any link at index N'
        almost always hits the wrong element. We'd rather pause and ask.
        """
        locators = locators or {}

        # ---- 0: stable identifiers (id / name attr / test id) -------------
        el = self._resolve_by_locators(page, locators, role)
        if el is not None:
            return el

        def first_visible(locator, want_nth):
            try:
                cnt = locator.count()
            except Exception as e:
                self._dlog(f"    count() error: {e}")
                return None
            indices = [want_nth, 0] if want_nth != 0 else [0]
            for idx in indices:
                if cnt > idx:
                    el = locator.nth(idx)
                    try:
                        if el.is_visible(timeout=1000):
                            return el, idx, cnt
                    except Exception:
                        return el, idx, cnt
            return None

        if name:
            # 1 & 2: top frame, role + name.
            try:
                loc = page.get_by_role(role, name=name)
                res = first_visible(loc, nth)
                if res:
                    el, idx, cnt = res
                    self._dlog(f"  resolve: role+name top count={cnt} used nth={idx}")
                    return el
                self._dlog(f"  resolve: role+name top count={loc.count()} "
                           f"(need {nth} or 0), none visible")
            except Exception as e:
                self._dlog(f"  resolve: role+name top error: {e}")

            # 2b: The recorded name may have come from a source Playwright's
            # accessible-name computation ranks differently — most commonly a
            # placeholder that's outranked by a title attribute. Try other
            # name sources on the top frame and in child frames.
            el = self._resolve_by_name_sources(page, role, name, nth)
            if el is not None:
                return el

            # 3: child frames, role + name.
            try:
                frames = [f for f in page.frames if f != page.main_frame]
            except Exception:
                frames = []
            for fr in frames:
                try:
                    loc = fr.get_by_role(role, name=name)
                    res = first_visible(loc, nth)
                    if res:
                        el, idx, cnt = res
                        self._dlog(f"  resolve: role+name frame {fr.url!r} "
                                   f"count={cnt} used nth={idx}")
                        return el
                except Exception:
                    continue

            # Named but not found: refuse role-only fallback.
            self._dlog("  resolve: named element not found; refusing role-only "
                       "fallback (would risk clicking the wrong element)")
            return None

        # 4: role-only — only when the step had no name. For unnamed
        # elements we require the EXACT nth: falling back to nth=0 would mean
        # "the 5th combobox is gone, so use the 1st," which is almost always
        # the wrong element. Better to pause and let the user decide.
        def exact_only(locator, want_nth):
            """For unnamed elements. Return (el, idx, cnt).

            Use the exact nth if it exists. If it doesn't but there's
            exactly ONE candidate, that's unambiguous — use it (handles the
            common case where the page now has fewer duplicates than at
            record time, e.g. one button vs. three). If the exact nth is
            missing AND there are multiple candidates, refuse — picking among
            several would be a guess.
            """
            try:
                cnt = locator.count()
            except Exception:
                return (None, None, 0)
            chosen_idx = None
            if cnt > want_nth:
                chosen_idx = want_nth
            elif cnt == 1:
                chosen_idx = 0
            if chosen_idx is None:
                return (None, None, cnt)
            el = locator.nth(chosen_idx)
            try:
                if el.is_visible(timeout=1000):
                    return (el, chosen_idx, cnt)
            except Exception:
                return (el, chosen_idx, cnt)
            return (None, None, cnt)

        try:
            loc = page.get_by_role(role)
            el, idx, cnt = exact_only(loc, nth)
            if el is not None:
                self._dlog(f"  resolve: role-only top count={cnt} used nth={idx}")
                return el
            self._dlog(f"  resolve: role-only top has {cnt} "
                       f"(need exact nth={nth}, and >1 candidate so won't guess)")
        except Exception as e:
            self._dlog(f"  resolve: role-only top error: {e}")

        try:
            frames = [f for f in page.frames if f != page.main_frame]
        except Exception:
            frames = []
        for fr in frames:
            try:
                loc = fr.get_by_role(role)
                el, idx, cnt = exact_only(loc, nth)
                if el is not None:
                    self._dlog(f"  resolve: role-only frame {fr.url!r} "
                               f"count={cnt} used nth={idx}")
                    return el
            except Exception:
                continue

        self._dlog("  resolve: no match anywhere")
        return None

    def _resolve_by_name_sources(self, page, role, name, nth):
        """Fallback resolution that mirrors the RECORDER's getName() logic.

        Our recorder computes an element's name from (in order): aria-label,
        associated <label>, button/link text, then placeholder/title. But
        Playwright's get_by_role uses the official ARIA accessible-name spec,
        where `title` outranks `placeholder`. So an <input placeholder="Quick
        Search" title="Required"> is named "Quick Search" by us but "Required"
        by Playwright, and get_by_role(name="Quick Search") finds nothing.

        This method searches every frame for an element whose name — computed
        the SAME way the recorder computes it — equals the recorded name.
        Returns a Playwright ElementHandle (not Locator) or None.
        """
        # JS runs in the page; returns the matching elements (by our naming).
        js = r"""
        ([role, wantName]) => {
          function getRole(el) {
            if (!el || !el.tagName) return null;
            const explicit = el.getAttribute && el.getAttribute('role');
            if (explicit) return explicit;
            const tag = el.tagName.toLowerCase();
            if (tag === 'button') return 'button';
            if (tag === 'a' && el.hasAttribute('href')) return 'link';
            if (tag === 'select') return 'combobox';
            if (tag === 'textarea') return 'textbox';
            if (tag === 'input') {
              const type = (el.getAttribute('type') || 'text').toLowerCase();
              if (['text','email','password','tel','url'].includes(type)) return 'textbox';
              if (type === 'search') return 'searchbox';
              if (type === 'checkbox') return 'checkbox';
              if (type === 'radio') return 'radio';
              if (type === 'number') return 'spinbutton';
              if (type === 'range') return 'slider';
              if (['submit','button','reset','image'].includes(type)) return 'button';
              return 'textbox';
            }
            return null;
          }
          function getName(el) {
            if (!el) return '';
            const attr = (n) => el.getAttribute && el.getAttribute(n);
            const al = attr('aria-label');
            if (al) return al.trim();
            if (el.id) {
              try {
                const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (lbl) return (lbl.textContent || '').trim();
              } catch(_) {}
            }
            if (el.closest) {
              const wrap = el.closest('label');
              if (wrap) {
                const c = wrap.cloneNode(true);
                c.querySelectorAll('input, textarea, select').forEach(n => n.remove());
                return (c.textContent || '').trim();
              }
            }
            if (el.tagName === 'BUTTON' || el.tagName === 'A')
              return (el.textContent || '').trim();
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
              return attr('placeholder') || attr('title') || '';
            return attr('title') || '';
          }
          const all = document.getElementsByTagName('*');
          const matches = [];
          for (let i = 0; i < all.length; i++) {
            const el = all[i];
            if (getRole(el) === role && getName(el) === wantName) {
              matches.push(el);
            }
          }
          return matches;
        }
        """
        frames = [page.main_frame]
        try:
            frames += [f for f in page.frames if f != page.main_frame]
        except Exception:
            pass

        for fr in frames:
            try:
                handles = fr.evaluate_handle(js, [role, name])
                # evaluate_handle on an array returns a JSHandle; get its
                # properties to access individual element handles.
                props = handles.get_properties()
                elements = []
                for key, val in props.items():
                    el = val.as_element()
                    if el is not None:
                        elements.append(el)
                if not elements:
                    continue
                # Choose nth if available, else first.
                idx = nth if nth < len(elements) else 0
                chosen = elements[idx]
                try:
                    if chosen.is_visible():
                        self._dlog(f"  resolve: name-source match in "
                                   f"{fr.url!r} ({len(elements)} found, used "
                                   f"idx={idx})")
                        return chosen
                except Exception:
                    return chosen
            except Exception as e:
                self._dlog(f"  resolve: name-source search error in "
                           f"{getattr(fr, 'url', '?')!r}: {e}")
                continue
        return None

    def _fingerprint(self, cdp):
        tree = cdp.send("Accessibility.getFullAXTree")
        parts = []
        for node in tree.get("nodes", []):
            if node.get("ignored"): continue
            role = (node.get("role") or {}).get("value", "")
            if role not in INTERACTIVE_ROLES: continue
            name = ((node.get("name") or {}).get("value") or "").strip()
            parts.append(f"{role}|{name}")
        return "\n".join(parts)

    def _lookup_locators(self, page, role, name, nth):
        """Find the element matching role+name+nth across frames and return
        its stable DOM locators (id, id_suffix, name attr). Mirrors the
        recorder's getRole/getName/getLocators so results are consistent with
        live recording. Returns a dict (possibly empty)."""
        js = r"""
        (args) => {
          const [wantRole, wantName, wantNth] = args;
          function getRole(el) {
            if (!el || !el.tagName) return null;
            const explicit = el.getAttribute && el.getAttribute('role');
            if (explicit) return explicit;
            const tag = el.tagName.toLowerCase();
            if (tag === 'a' && el.hasAttribute('href')) return 'link';
            if (tag === 'button') return 'button';
            if (tag === 'select') return 'combobox';
            if (tag === 'textarea') return 'textbox';
            if (tag === 'option') return 'option';
            if (tag === 'input') {
              const type = (el.getAttribute('type') || 'text').toLowerCase();
              if (['text','email','password','tel','url'].includes(type)) return 'textbox';
              if (type === 'search') return 'searchbox';
              if (type === 'checkbox') return 'checkbox';
              if (type === 'radio') return 'radio';
              if (type === 'number') return 'spinbutton';
              if (type === 'range') return 'slider';
              if (['submit','button','reset','image'].includes(type)) return 'button';
              return 'textbox';
            }
            return null;
          }
          function getName(el) {
            if (!el) return '';
            const attr = (n) => el.getAttribute && el.getAttribute(n);
            const al = attr('aria-label');
            if (al) return al.trim();
            if (el.id) {
              try {
                const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (lbl) return (lbl.textContent || '').trim();
              } catch(_) {}
            }
            if (el.closest) {
              const wrap = el.closest('label');
              if (wrap) {
                const c = wrap.cloneNode(true);
                c.querySelectorAll('input, textarea, select').forEach(n => n.remove());
                return (c.textContent || '').trim();
              }
            }
            if (el.tagName === 'BUTTON' || el.tagName === 'A')
              return (el.textContent || '').trim();
            if (el.tagName === 'OPTION') return (el.textContent || '').trim();
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
              return attr('placeholder') || attr('title') || '';
            return attr('title') || '';
          }
          function getLocators(target) {
            const loc = {};
            let el = target;
            for (let d = 0; el && d < 4; d++, el = el.parentElement) {
              const id = el.getAttribute && el.getAttribute('id');
              const nm = el.getAttribute && el.getAttribute('name');
              const testid = el.getAttribute && (el.getAttribute('data-testid')
                || el.getAttribute('data-test') || el.getAttribute('data-qa'));
              if (testid && !loc.testid) loc.testid = testid;
              if (id && !loc.id) {
                loc.id = id;
                const parts = id.split(/[_$]/);
                if (parts.length > 1) loc.id_suffix = parts[parts.length - 1];
              }
              if (nm && !loc.name_attr) {
                loc.name_attr = nm;
                const parts = nm.split(/[_$]/);
                if (parts.length > 1) loc.name_suffix = parts[parts.length - 1];
              }
              if (loc.id) break;
            }
            return loc;
          }
          const all = document.getElementsByTagName('*');
          let count = 0;
          for (let i = 0; i < all.length; i++) {
            const el = all[i];
            if (getRole(el) === wantRole && getName(el) === wantName) {
              if (count === wantNth) return getLocators(el);
              count++;
            }
          }
          return {};
        }
        """
        try:
            frames = [page.main_frame] + [
                f for f in page.frames if f != page.main_frame]
        except Exception:
            frames = [page.main_frame]
        for fr in frames:
            try:
                loc = fr.evaluate(js, [role, name, nth])
            except Exception:
                continue
            if loc:
                return loc
        return {}

    def _snapshot(self, page, cdp):
        try:
            tree = cdp.send("Accessibility.getFullAXTree")
        except Exception:
            cdp.send("Accessibility.enable")
            tree = cdp.send("Accessibility.getFullAXTree")

        flat = []
        for node in tree.get("nodes", []):
            if node.get("ignored"): continue
            role = (node.get("role") or {}).get("value", "")
            if role not in INTERACTIVE_ROLES: continue
            name = ((node.get("name") or {}).get("value") or "").strip()
            value = (node.get("value") or {}).get("value")
            disabled = False
            for prop in node.get("properties", []) or []:
                if prop.get("name") == "disabled":
                    disabled = bool((prop.get("value") or {}).get("value"))
                    break
            flat.append({
                "role": role, "name": name, "value": value, "disabled": disabled
            })

        counts = {}
        for el in flat:
            key = (el["role"], el["name"])
            el["nth"] = counts.get(key, 0)
            counts[key] = el["nth"] + 1

        return {"url": page.url, "elements": flat}


# ----- Helpers -------------------------------------------------------------

def format_step(step):
    """Human-readable one-line description of a step. Single source of truth
    lives in navigator_core (unit-tested); this thin wrapper keeps the existing
    call sites working."""
    return core.format_step(step)


# ----- Edit dialog ---------------------------------------------------------

class ResolveDialog(tk.Toplevel):
    """Collect a list of emails to resolve into student IDs. Optionally chain
    straight into the batch processor for the cleanly-resolved students."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Resolve emails to students")
        self.transient(parent)
        self.grab_set()
        self.emails = []
        self.auto_batch = False

        ttk.Label(self,
            text="Paste email addresses below (one per line), or load a file.\n"
                 "Each is searched; one match = resolved, none = not found,\n"
                 "multiple = set aside for manual review.",
            justify="left").pack(anchor="w", padx=10, pady=(10, 4))

        self.text = tk.Text(self, width=46, height=12, wrap="word")
        self.text.pack(fill="both", expand=True, padx=10)

        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self,
            text="After resolving, auto-run transcripts for resolved students "
                 "(uses the loaded template)",
            variable=self.auto_var).pack(anchor="w", padx=10, pady=(6, 0))

        btnrow = ttk.Frame(self)
        btnrow.pack(fill="x", padx=10, pady=8)
        ttk.Button(btnrow, text="Load from file...",
                   command=self._load_file).pack(side="left")
        self.count_var = tk.StringVar(value="")
        ttk.Label(btnrow, textvariable=self.count_var,
                  foreground="#0a5ed8").pack(side="left", padx=10)
        ttk.Button(btnrow, text="Cancel",
                   command=self.destroy).pack(side="right")
        ttk.Button(btnrow, text="Run",
                   command=self._on_run).pack(side="right", padx=6)

        self.text.bind("<KeyRelease>", lambda e: self._update_count())

    def _parse(self):
        raw = self.text.get("1.0", tk.END)
        out = []
        seen = set()
        for chunk in raw.replace(",", "\n").splitlines():
            s = chunk.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _update_count(self):
        n = len(self._parse())
        self.count_var.set(f"{n} email(s)" if n else "")

    def _load_file(self):
        path = filedialog.askopenfilename(
            title="Load emails",
            filetypes=[("Text/CSV", "*.txt *.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.text.insert(tk.END, ("\n" if self.text.get("1.0", tk.END).strip()
                                      else "") + content)
            self._update_count()
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    def _on_run(self):
        emails = self._parse()
        if not emails:
            messagebox.showinfo("No emails", "Enter at least one email.")
            return
        self.emails = emails
        self.auto_batch = self.auto_var.get()
        self.destroy()


class BatchDialog(tk.Toplevel):
    """Collect a list of student IDs to process. Accepts pasted text or a
    loaded file; parses one ID per line (also splits on commas)."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Batch process students")
        self.transient(parent)
        self.grab_set()
        self.student_ids = []

        ttk.Label(self,
            text="Paste student IDs below (one per line), or load from a file.\n"
                 "Commas and extra spaces are fine.",
            justify="left").pack(anchor="w", padx=10, pady=(10, 4))

        self.text = tk.Text(self, width=44, height=12, wrap="word")
        self.text.pack(fill="both", expand=True, padx=10)

        btnrow = ttk.Frame(self)
        btnrow.pack(fill="x", padx=10, pady=8)
        ttk.Button(btnrow, text="Load resolved IDs from roster",
                   command=self._load_roster).pack(side="left")
        ttk.Button(btnrow, text="Load from file...",
                   command=self._load_file).pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="")
        ttk.Label(btnrow, textvariable=self.count_var,
                  foreground="#0a5ed8").pack(side="left", padx=10)
        ttk.Button(btnrow, text="Cancel",
                   command=self.destroy).pack(side="right")
        ttk.Button(btnrow, text="Run batch",
                   command=self._on_run).pack(side="right", padx=6)

        self.text.bind("<KeyRelease>", lambda e: self._update_count())

    def _parse(self):
        raw = self.text.get("1.0", tk.END)
        ids = []
        for chunk in raw.replace(",", "\n").splitlines():
            s = chunk.strip()
            if s:
                ids.append(s)
        # De-dupe preserving order.
        seen = set()
        out = []
        for s in ids:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _update_count(self):
        n = len(self._parse())
        self.count_var.set(f"{n} student(s)" if n else "")

    def _load_roster(self):
        """Pull the resolved student IDs straight from a roster CSV (the one
        produced by 'Resolve emails...'). Defaults to email_roster.csv in the
        output folder; only rows with status 'resolved' contribute an ID, so
        not-found and multiple-match rows are skipped. This lets the user
        resolve now and batch later without copy-pasting from the CSV."""
        import csv as _csv
        default = os.path.join(DOWNLOADS_DIR, "email_roster.csv")
        path = default if os.path.exists(default) else filedialog.askopenfilename(
            title="Open roster CSV", initialdir=DOWNLOADS_DIR,
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            ids = []
            skipped = 0
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    status = (row.get("status") or "").strip().lower()
                    sid = (row.get("student_id") or "").strip()
                    if status == "resolved" and sid:
                        ids.append(sid)
                    elif status in ("not_found", "multiple_matches"):
                        skipped += 1
            if not ids:
                messagebox.showinfo(
                    "No resolved IDs",
                    f"No 'resolved' rows with a student ID were found in:\n"
                    f"{os.path.basename(path)}")
                return
            # De-dupe against whatever is already in the box, append the rest.
            existing = set(self._parse())
            new_ids = [i for i in ids if i not in existing]
            prefix = "\n" if self.text.get("1.0", tk.END).strip() else ""
            if new_ids:
                self.text.insert(tk.END, prefix + "\n".join(new_ids))
            self._update_count()
            note = f"Loaded {len(new_ids)} resolved ID(s)"
            if skipped:
                note += f"; {skipped} not-found/multiple skipped"
            self.count_var.set(note)
        except Exception as e:
            messagebox.showerror("Load failed",
                                 f"Couldn't read roster CSV:\n{e}")

    def _load_file(self):
        path = filedialog.askopenfilename(
            title="Load student IDs",
            filetypes=[("Text/CSV", "*.txt *.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            # If it's a CSV with a header containing 'id', try to pull that
            # column; otherwise just dump everything in and let _parse split.
            self.text.insert(tk.END, ("\n" if self.text.get("1.0", tk.END).strip()
                                      else "") + content)
            self._update_count()
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    def _on_run(self):
        ids = self._parse()
        if not ids:
            messagebox.showinfo("No IDs", "Enter at least one student ID.")
            return
        self.student_ids = ids
        self.destroy()


class StepEditDialog(tk.Toplevel):
    def __init__(self, parent, step):
        super().__init__(parent)
        self.title("Edit step")
        self.transient(parent)
        self.grab_set()
        self.result = None
        self._orig = dict(step) if step else {}

        # Fixed value sets for the fields that have a known vocabulary. Using
        # strict (readonly) dropdowns for these prevents typos that would
        # silently break replay (e.g. "clik" instead of "click").
        TYPE_CHOICES = ["", "click", "fill", "select_option", "select_kendo",
                        "check", "uncheck", "press", "navigate", "swap_dates",
                        "close_page", "copy", "paste", "cut", "download"]
        ROLE_CHOICES = ["", "link", "button", "textbox", "searchbox",
                        "combobox", "checkbox", "radio", "switch", "option",
                        "tab", "menuitem", "spinbutton", "slider", "link"]
        # de-dupe ROLE_CHOICES preserving order
        ROLE_CHOICES = list(dict.fromkeys(ROLE_CHOICES))
        SECRET_CHOICES = ["", "password", "otp", "cc", "secret"]
        dropdowns = {"type": TYPE_CHOICES, "role": ROLE_CHOICES,
                     "secret_kind": SECRET_CHOICES}

        self.vars = {}
        fields = [("Type", "type"), ("Role", "role"), ("Name", "name"),
                  ("Value", "value"), ("Key", "key"), ("Nth (0-based)", "nth"),
                  ("URL", "url"), ("Element ID", "_id"), ("Secret kind", "secret_kind")]
        for row, (label, key) in enumerate(fields):
            ttk.Label(self, text=label + ":").grid(
                row=row, column=0, sticky="w", padx=8, pady=4)
            if key == "_id":
                initial = (step.get("locators") or {}).get("id")
            else:
                initial = step.get(key)
            init_str = "" if initial is None else str(initial)
            v = tk.StringVar(value=init_str)
            self.vars[key] = v
            if key in dropdowns:
                choices = list(dropdowns[key])
                # If the existing value isn't a known choice, include it so
                # editing an unusual step doesn't silently change it.
                if init_str and init_str not in choices:
                    choices.append(init_str)
                cb = ttk.Combobox(self, textvariable=v, values=choices,
                                  width=43, state="readonly")
                cb.grid(row=row, column=1, padx=8, pady=4)
            else:
                ttk.Entry(self, textvariable=v, width=45).grid(
                    row=row, column=1, padx=8, pady=4)

        # Secret checkbox
        self.secret_var = tk.BooleanVar(value=bool(step.get("secret")))
        ttk.Checkbutton(self, text="Mark this step as containing a secret value",
                        variable=self.secret_var).grid(
            row=len(fields), column=0, columnspan=2, sticky="w", padx=8, pady=4)

        # Hint
        ttk.Label(self,
            text="Tip: leave Value empty for the replay engine to prompt you,\n"
                 "or use a placeholder like {{PASSWORD}} for variable substitution.\n"
                 "For a Kendo dropdown (data-role=\"dropdownlist\"): Type=select_kendo,\n"
                 "Element ID = the input id (e.g. cmbProgram), Value = the visible\n"
                 "option text (e.g. High School Diploma).",
            foreground="#555", justify="left").grid(
            row=len(fields) + 1, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))

        btnf = ttk.Frame(self)
        btnf.grid(row=len(fields) + 2, column=0, columnspan=2, pady=10)
        ttk.Button(btnf, text="OK", command=self.on_ok).pack(side="left", padx=6)
        ttk.Button(btnf, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    def on_ok(self):
        out = {}
        element_id = ""
        for key, var in self.vars.items():
            v = var.get().strip()
            if key == "_id":
                element_id = v
                continue
            if v == "":
                continue
            if key == "nth":
                try: out[key] = int(v)
                except ValueError: out[key] = 0
            else:
                out[key] = v
        if element_id:
            loc = dict(self._orig.get("locators") or {})
            loc["id"] = element_id
            # ASP.NET ids like ctl00_..._ddlBar: record the trailing segment
            # for suffix matching, mirroring what the recorder captures.
            parts = element_id.replace("$", "_").split("_")
            if len(parts) > 1:
                loc["id_suffix"] = parts[-1]
            out["locators"] = loc
        elif self._orig.get("locators"):
            # User cleared the ID field but the step had other locators
            # (name_attr, testid). Keep those rather than silently dropping
            # all locator info.
            kept = {k: v for k, v in self._orig["locators"].items()
                    if k not in ("id", "id_suffix")}
            if kept:
                out["locators"] = kept
        if self.secret_var.get():
            out["secret"] = True
            if "secret_kind" not in out:
                out["secret_kind"] = "password"
        self.result = out
        # Preserve keys that the dialog doesn't expose but that matter to
        # replay (e.g. select_option's stable 'label', swap_dates suffixes,
        # download path). Editing a step shouldn't silently drop them.
        for k in ("label", "diploma_suffix", "graduation_suffix",
                  "filename", "path", "id_suffix"):
            if k not in self.result and k in self._orig:
                self.result[k] = self._orig[k]
        self.destroy()


# ----- Main app ------------------------------------------------------------

class AboutDialog(tk.Toplevel):
    """A little credit screen. Dark panel, big name, accent color — the rad
    'CJ made this' moment."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("About ASAP Powertools")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.configure(bg="#0d1117")

        BG = "#0d1117"      # near-black panel
        ACCENT = "#39d3ff"  # cyan
        GOLD = "#ffd23f"    # star/name highlight
        FG = "#e6edf3"      # soft white
        DIM = "#8b949e"     # muted gray

        pad = tk.Frame(self, bg=BG)
        pad.pack(fill="both", expand=True, padx=28, pady=22)

        tk.Label(pad, text="ASAP  POWERTOOLS", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 18, "bold")).pack()
        tk.Label(pad, text="record · replay · batch ASAP transcripts",
                 bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(pady=(0, 16))

        tk.Label(pad, text="★", bg=BG, fg=GOLD,
                 font=("Segoe UI", 22)).pack()
        tk.Label(pad, text="created by", bg=BG, fg=DIM,
                 font=("Segoe UI", 9)).pack()
        tk.Label(pad, text="CJ", bg=BG, fg=GOLD,
                 font=("Segoe UI", 40, "bold")).pack()
        tk.Label(pad, text="builder of unreasonably helpful things",
                 bg=BG, fg=FG, font=("Segoe UI", 10, "italic")).pack(
                     pady=(2, 18))

        line = tk.Frame(pad, bg=ACCENT, height=2)
        line.pack(fill="x", pady=(0, 14))

        tk.Label(pad,
                 text="Automates ASAP Connected transcript exports end to end:\n"
                      "record once, then resolve emails and batch-download\n"
                      "transcripts for an entire roster — unattended.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9),
                 justify="center").pack(pady=(0, 16))

        btn = tk.Button(pad, text="Nice.", command=self.destroy,
                        bg=ACCENT, fg="#06222b", activebackground=GOLD,
                        activeforeground="#06222b", relief="flat",
                        font=("Segoe UI", 10, "bold"), padx=18, pady=4,
                        cursor="hand2")
        btn.pack()

        # Center over the parent.
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + parent.winfo_width() // 2
            py = parent.winfo_rooty() + parent.winfo_height() // 2
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px - w // 2}+{py - h // 2}")
        except Exception:
            pass

        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.destroy())
        btn.focus_set()


class App:
    def __init__(self, root):
        self.root = root
        root.title("ASAP Powertools — by CJ")
        root.geometry("1100x650")

        self.cmd_q = queue.Queue()
        self.result_q = queue.Queue()
        self.worker = BrowserWorker(self.cmd_q, self.result_q)
        self.worker.start()

        self.elements = []
        self.steps = []
        self.highlighted_idx = None
        self.highlight_after = None
        self.capture_secrets = False  # default: redact passwords etc.
        self.last_download_path = None  # for click-to-open in status bar
        self._batch_monitor = None  # live batch progress window (Phase 3)
        self._dark = False          # dark theme toggle state (Phase 3)

        # Replay state.
        self.credentials = CredentialsStore()
        self.replaying = False
        self.replay_steps_in_flight = []  # the steps being executed
        self._pending_insert = None  # element-insert awaiting locator lookup

        self._build_ui()
        self._bind_shortcuts()
        self._poll_results()

        # Auto-navigate to whatever URL is pre-filled in the URL bar. Small
        # delay so the worker thread has time to launch Chromium first.
        self.root.after(500, self.on_go)

    def _build_ui(self):
        # Top: URL bar + nav buttons
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="URL:").pack(side="left")
        self.url_var = tk.StringVar(value="https://admin.asapconnected.com/home")
        self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.url_entry.bind("<Return>", lambda e: self.on_go())

        ttk.Button(top, text="Go", command=self.on_go).pack(side="left")
        ttk.Button(top, text="Back",
                   command=lambda: self._send({"action": "back"})).pack(side="left", padx=4)
        ttk.Button(top, text="Reload",
                   command=lambda: self._send({"action": "reload"})).pack(side="left")
        ttk.Button(top, text="Re-scan",
                   command=lambda: self._send({"action": "rescan"})).pack(side="left", padx=4)
        ttk.Button(top, text="Downloads",
                   command=self._open_downloads_folder).pack(side="left")
        ttk.Button(top, text="Output folder...",
                   command=self._change_output_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Merge PDFs",
                   command=self._on_merge_pdfs).pack(side="left")

        # Capture-secrets toggle (off by default)
        self.capture_secrets_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top,
            text="Capture secret values (UNSAFE)",
            variable=self.capture_secrets_var,
            command=self._on_toggle_capture_secrets,
        ).pack(side="left", padx=12)

        # Middle: two panes
        middle = ttk.PanedWindow(self.root, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=8, pady=4)

        # Left: live elements
        left = ttk.Frame(middle)
        middle.add(left, weight=1)
        ttk.Label(left, text="Elements on this page (highlighted when used):").pack(anchor="w")
        lwrap = ttk.Frame(left)
        lwrap.pack(fill="both", expand=True)
        self.el_list = tk.Listbox(lwrap, activestyle="dotbox",
                                  exportselection=False)
        self.el_list.pack(side="left", fill="both", expand=True)
        sb1 = ttk.Scrollbar(lwrap, orient="vertical", command=self.el_list.yview)
        sb1.pack(side="right", fill="y")
        self.el_list.config(yscrollcommand=sb1.set)
        # Insert the selected element as a recorded step. Double-click does
        # the same thing for convenience.
        ttk.Button(left, text="→ Insert selected element as step",
                   command=self.insert_element_as_step).pack(anchor="w", pady=(4, 0))
        self.el_list.bind("<Double-Button-1>",
                          lambda e: self.insert_element_as_step())

        # Right: recorded steps
        right = ttk.Frame(middle)
        middle.add(right, weight=1)
        ttk.Label(right, text="Recorded steps (double-click to edit):").pack(anchor="w")
        rwrap = ttk.Frame(right)
        rwrap.pack(fill="both", expand=True)
        self.step_list = tk.Listbox(
            rwrap, activestyle="dotbox",
            # Don't surrender the selection when another widget (the element
            # list) is clicked, and keep the highlight a visible color instead
            # of letting Tk grey it out when the list loses focus.
            exportselection=False,
            selectbackground="#0a5ed8", selectforeground="white")
        self.step_list.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(rwrap, orient="vertical", command=self.step_list.yview)
        sb2.pack(side="right", fill="y")
        self.step_list.config(yscrollcommand=sb2.set)
        self.step_list.bind("<Double-Button-1>", self.on_edit_step)
        # Remember the most recently selected step so "Insert selected element
        # as step" can place the new step right after it — even if focus later
        # moves to the element list and the visible highlight clears.
        self._last_step_sel = None
        self.step_list.bind("<<ListboxSelect>>", self._remember_step_sel)

        btn = ttk.Frame(right)
        btn.pack(fill="x", pady=4)
        ttk.Button(btn, text="↑", width=3,
                   command=lambda: self.move_step(-1)).pack(side="left")
        ttk.Button(btn, text="↓", width=3,
                   command=lambda: self.move_step(1)).pack(side="left")
        ttk.Button(btn, text="Edit", command=self.on_edit_step).pack(side="left", padx=4)
        ttk.Button(btn, text="Insert...", command=self.insert_step).pack(side="left")
        ttk.Button(btn, text="Delete", command=self.delete_step).pack(side="left", padx=4)
        ttk.Button(btn, text="Clear all", command=self.clear_steps).pack(side="left", padx=4)
        ttk.Button(btn, text="Save...", command=self.save_steps).pack(side="right")
        ttk.Button(btn, text="Load...", command=self.load_steps).pack(side="right", padx=4)

        # Second row of controls dedicated to replay.
        btn2 = ttk.Frame(right)
        btn2.pack(fill="x", pady=(0, 4))
        self.replay_btn = ttk.Button(btn2, text="▶ Replay",
                                     command=self.on_replay)
        self.replay_btn.pack(side="left")
        ttk.Button(btn2, text="Preview",
                   command=self.on_preview).pack(side="left", padx=4)
        ttk.Button(btn2, text="Credentials...",
                   command=self.on_manage_credentials).pack(side="left", padx=4)
        ttk.Button(btn2, text="Insert date-swap",
                   command=self.insert_swap_dates).pack(side="left", padx=4)
        ttk.Button(btn2, text="Batch...",
                   command=self.on_batch).pack(side="left", padx=4)
        ttk.Button(btn2, text="Resolve emails...",
                   command=self.on_resolve).pack(side="left", padx=4)
        # Stop an in-progress batch/resolve. Disabled until a run is running.
        self.stop_btn = ttk.Button(btn2, text="■ Stop",
                                   command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        # Progress indicator: shown during replay.
        self.replay_progress_var = tk.StringVar(value="")
        ttk.Label(btn2, textvariable=self.replay_progress_var,
                  foreground="#0a5ed8").pack(side="left", padx=8)
        # Credit / About — far right of the replay row.
        ttk.Button(btn2, text="★ About",
                   command=self.on_about).pack(side="right", padx=4)
        ttk.Button(btn2, text="🌙 Theme",
                   command=self.toggle_theme).pack(side="right", padx=4)

        # Status bar — doubles as a clickable shortcut to the last download.
        self.status_var = tk.StringVar(
            value="Ready. Enter a URL and press Go.   ·   ASAP Powertools by CJ ★")
        self.status_label = ttk.Label(
            self.root, textvariable=self.status_var, anchor="w",
            padding=6, relief="sunken")
        self.status_label.pack(side="bottom", fill="x")
        self.status_label.bind("<Button-1>", self._on_status_click)

    # ----- UI events -----

    def on_go(self):
        url = self.url_var.get().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_var.set(url)
        self._send({"action": "goto", "url": url})

    def _send(self, cmd):
        self.cmd_q.put(cmd)
        self._set_status(f"{cmd['action']}...")

    def _set_status(self, text, clickable=False):
        """Update the status bar and switch styling.

        clickable=True means the message refers to a download the user can
        open by clicking the bar. Any other status update resets the
        styling so the bar doesn't keep looking like a link.
        """
        self.status_var.set(text)
        try:
            if clickable:
                self.status_label.config(foreground="#0a5ed8", cursor="hand2")
            else:
                self.status_label.config(foreground="", cursor="")
        except Exception:
            pass

    def _dlog(self, msg):
        """App-side diagnostic log. Mirrors BrowserWorker._dlog so UI-thread
        code (e.g. the postback-suppression branch in _on_navigation) can log
        too — App previously had no _dlog, so that path raised AttributeError."""
        core.dlog(msg)

    # ----- Worker results -----

    def _poll_results(self):
        try:
            while True:
                kind, action, payload = self.result_q.get_nowait()
                if kind == "snapshot":
                    self._on_snapshot(action, payload)
                elif kind == "event":
                    self._on_event(payload)
                elif kind == "navigation":
                    self._on_navigation(payload)
                elif kind == "download":
                    self._on_download(payload)
                elif kind == "replay_start":
                    self._on_replay_start(payload)
                elif kind == "replay_step":
                    self._on_replay_step(payload)
                elif kind == "replay_paused":
                    self._on_replay_paused(payload)
                elif kind == "replay_done":
                    self._on_replay_done(payload)
                elif kind == "batch_start":
                    self._on_batch_start(payload)
                elif kind == "batch_progress":
                    self._on_batch_progress(payload)
                elif kind == "batch_done":
                    self._on_batch_done(payload)
                elif kind == "resolve_start":
                    self._on_resolve_start(payload)
                elif kind == "resolve_progress":
                    self._on_resolve_progress(payload)
                elif kind == "resolve_done":
                    self._on_resolve_done(payload)
                elif kind == "locators":
                    self._on_locators_result(action, payload)
                elif kind == "err":
                    self._set_status(f"{action} failed: {payload}")
        except queue.Empty:
            pass
        self.root.after(60, self._poll_results)

    def _on_download(self, payload):
        filename = payload.get("filename", "")
        path = payload.get("path", "")
        url = payload.get("url", "")
        self._add_step({
            "type": "download",
            "filename": filename,
            "path": path,
            "url": url,
        })
        self.last_download_path = path
        self._set_status(
            f"Downloaded: {filename}  —  click here to open  ({path})",
            clickable=True,
        )

    def _on_status_click(self, _evt=None):
        if self.last_download_path and os.path.exists(self.last_download_path):
            open_path(self.last_download_path)

    def _open_downloads_folder(self):
        # Reveal the most recent download in the folder if we have one,
        # otherwise just open the folder.
        if self.last_download_path and os.path.exists(self.last_download_path):
            reveal_in_folder(self.last_download_path)
        else:
            open_path(DOWNLOADS_DIR)

    def _change_output_folder(self):
        """Let the user pick where downloads and the date-swap CSV are saved.
        The choice persists between runs."""
        global DOWNLOADS_DIR
        chosen = filedialog.askdirectory(
            title="Choose output folder (downloads + date-swap log)",
            initialdir=DOWNLOADS_DIR if os.path.isdir(DOWNLOADS_DIR)
                       else os.path.expanduser("~"))
        if not chosen:
            return
        DOWNLOADS_DIR = chosen
        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        except Exception:
            pass
        _save_output_dir(chosen)
        # Tell the worker thread so its download/CSV code uses the new path.
        self._send({"action": "set_output_dir", "path": chosen})
        self._set_status(f"Output folder set to: {chosen}")

    def _on_merge_pdfs(self):
        """Merge the per-student transcript PDFs in the output folder into a
        single combined.pdf. Runs the merge on a background thread so a large
        roster doesn't freeze the UI, then updates the status bar (clickable to
        open the result). Requires the optional 'pypdf' package."""
        folder = DOWNLOADS_DIR
        out = os.path.join(folder, "transcripts_combined.pdf")
        pdfs = [p for p in core.find_pdfs(folder)
                if os.path.abspath(p) != os.path.abspath(out)]
        if not pdfs:
            messagebox.showinfo(
                "Merge PDFs",
                f"No *_transcript*.pdf files found in:\n{folder}")
            return
        if not messagebox.askyesno(
                "Merge PDFs",
                f"Merge {len(pdfs)} transcript PDF(s) into:\n{out}?"):
            return
        self._set_status(f"Merging {len(pdfs)} PDFs…")

        def work():
            try:
                n = core.merge_pdfs(pdfs, out)
                self.root.after(0, lambda: self._on_merge_done(out, n))
            except Exception as e:
                self.root.after(0, lambda e=e: self._on_merge_failed(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_merge_done(self, out, n):
        self.last_download_path = out
        self._set_status(
            f"Merged {n} PDF(s) → {out}  —  click here to open",
            clickable=True)

    def _on_merge_failed(self, err):
        messagebox.showerror("Merge PDFs", str(err))
        self._set_status("PDF merge failed (see message).")

    def _on_snapshot(self, action, payload):
        # Element list refresh only. Navigation step recording is owned by
        # _on_navigation, fed by both framenavigated (real loads) and the
        # injected JS (SPA route changes).
        self.elements = payload["elements"]
        self.url_var.set(payload["url"])
        self._refresh_elements()
        self._set_status(f"{len(self.elements)} elements on {payload['url']}")

    def _on_event(self, payload):
        # Suppress event recording during replay — otherwise the listener
        # captures our own replayed actions and adds duplicate steps.
        if self.replaying:
            return
        step = dict(payload)
        # JS may emit a 'navigate' event for SPA route changes. Route it
        # through the same dedup path as real navigations.
        if step.get("type") == "navigate":
            self._on_navigation(step)
            return
        # Redact secret values unless the user explicitly opted in.
        if step.get("secret") and not self.capture_secrets:
            step["value"] = None
        self._add_step(step)
        self._highlight_match(step)

    def _on_navigation(self, payload):
        # Same reasoning as _on_event: don't double-record during replay.
        if self.replaying:
            # Still update the URL bar for visibility, just don't add a step.
            url = payload.get("url", "")
            if url:
                self.url_var.set(url)
            return
        url = payload.get("url", "")
        if not url:
            return
        # Dedupe: if the previous step is already a navigate to this URL,
        # skip. Catches the framenavigated+JS double-fire on the same load.
        if (self.steps
                and self.steps[-1].get("type") == "navigate"
                and self.steps[-1].get("url") == url):
            self.url_var.set(url)
            return
        # Suppress ASP.NET postback "navigations". When you select a dropdown,
        # save, or click certain buttons, the page does a __doPostBack that
        # reloads the SAME url. The browser fires framenavigated, but it isn't
        # a navigation the user performed — and replaying it as a goto() forces
        # a full reload that destroys the state just set (e.g. wiping a swapped
        # date). If the most recent recorded step already acted on this exact
        # url, treat this as a postback reload and don't record it.
        last_action_url = None
        for s in reversed(self.steps):
            su = s.get("page_url") or (s.get("url") if s.get("type") == "navigate" else None)
            if su:
                last_action_url = su
                break
        if last_action_url == url:
            self.url_var.set(url)
            self._dlog(f"  navigation to {url!r} suppressed (postback reload of "
                       f"current page)")
            return
        self._add_step({"type": "navigate", "url": url})
        self.url_var.set(url)

    def _on_toggle_capture_secrets(self):
        new = self.capture_secrets_var.get()
        if new:
            ok = messagebox.askyesno(
                "Capture secret values?",
                "If you turn this on, passwords and other sensitive values you "
                "type will be saved to the recording in plaintext.\n\n"
                "Only do this on a machine you trust, for recordings you will "
                "not share.\n\nContinue?",
                icon="warning",
            )
            if not ok:
                self.capture_secrets_var.set(False)
                return
        self.capture_secrets = new
        self._set_status(
            "Capturing secret values (UNSAFE)." if new
            else "Secret values will be redacted."
        )

    def _add_step(self, step):
        self.steps.append(step)
        self.step_list.insert(tk.END, format_step(step))
        self.step_list.see(tk.END)

    def _highlight_match(self, step):
        role = step.get("role")
        name = step.get("name", "") or ""
        nth = step.get("nth", 0) or 0
        if not role:
            return
        match_idx = None
        for i, el in enumerate(self.elements):
            if el["role"] == role and el["name"] == name and el["nth"] == nth:
                match_idx = i
                break
        if match_idx is None:
            for i, el in enumerate(self.elements):
                if el["role"] == role and el["name"] == name:
                    match_idx = i
                    break
        if match_idx is None:
            return
        if self.highlight_after:
            try: self.root.after_cancel(self.highlight_after)
            except Exception: pass
        if self.highlighted_idx is not None:
            try: self.el_list.itemconfig(self.highlighted_idx, background="")
            except Exception: pass
        try:
            self.el_list.itemconfig(match_idx, background="#fff2a8")
            self.el_list.see(match_idx)
            self.el_list.selection_clear(0, tk.END)
            self.el_list.selection_set(match_idx)
        except Exception:
            return
        self.highlighted_idx = match_idx
        self.highlight_after = self.root.after(2000, self._clear_highlight)

    def _clear_highlight(self):
        if self.highlighted_idx is not None:
            try: self.el_list.itemconfig(self.highlighted_idx, background="")
            except Exception: pass
        self.highlighted_idx = None
        self.highlight_after = None

    def _refresh_elements(self):
        self.el_list.delete(0, tk.END)
        for el in self.elements:
            label = f"[{el['role']}] {el['name'] or '(no name)'}"
            if el["nth"] > 0: label += f"  #{el['nth']+1}"
            if el["disabled"]: label += "  (disabled)"
            self.el_list.insert(tk.END, label)
        self.highlighted_idx = None
        self.highlight_after = None

    # ----- Step list management -----

    def on_edit_step(self, _evt=None):
        sel = self.step_list.curselection()
        if not sel: return
        idx = sel[0]
        dlg = StepEditDialog(self.root, self.steps[idx])
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.steps[idx] = dlg.result
            self.step_list.delete(idx)
            self.step_list.insert(idx, format_step(dlg.result))
            self.step_list.selection_set(idx)

    def _remember_step_sel(self, event=None):
        """Record the currently selected step index. Called on every selection
        change in the steps list so we can anchor an insert to it later even
        after the highlight is gone (e.g. once focus moves to the element
        list). Clearing the list (no selection) leaves the last value intact;
        it gets reset elsewhere when steps are cleared/loaded."""
        sel = self.step_list.curselection()
        if sel:
            self._last_step_sel = sel[0]

    def insert_element_as_step(self):
        """Insert the element selected in the left list as a recorded step,
        placed after the selected step on the right (or at the end). The
        action type is chosen from the element's role. We first ask the worker
        for the element's stable DOM locators (id/name) so the inserted step
        targets robustly rather than by fragile position."""
        sel = self.el_list.curselection()
        if not sel:
            self._set_status("Select an element in the left list first.")
            return
        el = self.elements[sel[0]]
        role = el.get("role")
        name = el.get("name", "") or ""
        nth = el.get("nth", 0) or 0

        # Map role -> sensible default action.
        if role in ("checkbox", "radio", "switch"):
            action_type = "check"
        elif role == "combobox":
            action_type = "select_option"
        else:
            action_type = "click"  # link, button, option, tab, menuitem, etc.

        # Remember where to insert and what we're inserting; the worker will
        # call back with locators.
        # Decide where to insert: right after the step selected on the right.
        # Prefer the live selection; if the highlight has cleared (focus moved
        # to the element list), fall back to the last step that was selected;
        # if neither, append to the end.
        rsel = self.step_list.curselection()
        if rsel:
            anchor = rsel[0]
        elif self._last_step_sel is not None \
                and self._last_step_sel < len(self.steps):
            anchor = self._last_step_sel
        else:
            anchor = None
        insert_idx = (anchor + 1) if anchor is not None else len(self.steps)
        self._pending_insert = {
            "type": action_type, "role": role, "name": name, "nth": nth,
            "insert_idx": insert_idx,
        }
        token = "insert_el"
        self._set_status(f"Looking up locators for [{role}] {name!r}…")
        self._send({"action": "lookup_locators", "role": role, "name": name,
                    "nth": nth, "token": token})

    def _on_locators_result(self, token, locators):
        pending = getattr(self, "_pending_insert", None)
        if not pending:
            return
        self._pending_insert = None

        step = {"type": pending["type"], "role": pending["role"],
                "name": pending["name"], "nth": pending["nth"]}
        if locators:
            step["locators"] = locators

        # For select_option we need a value/label, which the left list doesn't
        # carry. Open the editor so the user can fill in which option to pick.
        if pending["type"] == "select_option":
            dlg = StepEditDialog(self.root, step)
            dlg.title("Insert dropdown selection — set Value or label")
            self.root.wait_window(dlg)
            if dlg.result is None:
                self._set_status("Insert cancelled.")
                return
            step = dlg.result

        idx = pending["insert_idx"]
        if idx > len(self.steps):
            idx = len(self.steps)
        self.steps.insert(idx, step)
        self.step_list.insert(idx, format_step(step))
        self.step_list.selection_clear(0, tk.END)
        self.step_list.selection_set(idx)
        self.step_list.see(idx)
        # Anchor the next insert after this newly added step (programmatic
        # selection_set doesn't reliably fire <<ListboxSelect>>).
        self._last_step_sel = idx
        loc_note = (f"by id {locators['id']!r}" if locators.get("id")
                    else "by role+name (no stable id found)")
        self._set_status(f"Inserted {step['type']} [{step.get('role')}] "
                         f"{step.get('name')!r} at position {idx} ({loc_note}).")

    def insert_step(self):
        """Insert a brand-new step of any type. Opens the same editor used for
        editing, starting blank. The new step is placed AFTER the currently
        selected step (or at the end if nothing is selected). Element-targeting
        steps built by hand have no stable locators, so we warn that recording
        them in place is more reliable."""
        # Decide where it will go.
        sel = self.step_list.curselection()
        idx = (sel[0] + 1) if sel else len(self.steps)

        dlg = StepEditDialog(self.root, {})  # blank starting point
        dlg.title("Insert step")
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        step = dlg.result

        if not step.get("type"):
            messagebox.showwarning(
                "No type",
                "A step needs a Type (e.g. click, fill, navigate, "
                "select_option, check, swap_dates). Nothing was inserted.")
            return

        # Warn about hand-built element steps with no stable locator. These
        # fall back to positional (nth) targeting, which is the fragile kind
        # we work hard to avoid — recording the action in place captures the
        # element's id/name automatically and is far more reliable.
        element_types = {"click", "fill", "select_option", "check", "uncheck",
                         "press"}
        if (step.get("type") in element_types
                and not step.get("locators")
                and step.get("type") != "press"):
            proceed = messagebox.askyesno(
                "Hand-built element step",
                f"This '{step.get('type')}' step has no stable locator (id/name), "
                "so replay will fall back to position-based matching, which "
                "often breaks across pages or different data.\n\n"
                "More reliable: cancel, navigate to the right page, and simply "
                "perform the action — the recorder captures the element's id "
                "automatically.\n\n"
                "Insert this hand-built step anyway?",
                icon="warning")
            if not proceed:
                return

        self.steps.insert(idx, step)
        self.step_list.insert(idx, format_step(step))
        self.step_list.selection_clear(0, tk.END)
        self.step_list.selection_set(idx)
        self.step_list.see(idx)
        self._set_status(f"Inserted {step.get('type')} step at position {idx}.")

    def insert_swap_dates(self):
        """Insert a 'swap dates if needed' step. It goes AFTER the currently
        selected step (or at the end if nothing is selected). This is the
        conditional step that ensures the diploma date is later than the
        graduation date, swapping them if not."""
        step = {"type": "swap_dates",
                "diploma_suffix": "txtDiplomaDate",
                "graduation_suffix": "txtGraduationDate"}
        sel = self.step_list.curselection()
        if sel:
            idx = sel[0] + 1
        else:
            idx = len(self.steps)
        self.steps.insert(idx, step)
        self.step_list.insert(idx, format_step(step))
        self.step_list.selection_clear(0, tk.END)
        self.step_list.selection_set(idx)
        self.step_list.see(idx)
        self._set_status("Inserted date-swap step. It will ensure diploma date "
                         "is later than graduation date (swapping if needed).")

    def delete_step(self):
        sel = self.step_list.curselection()
        if not sel: return
        idx = sel[0]
        del self.steps[idx]
        self.step_list.delete(idx)

    def move_step(self, direction):
        sel = self.step_list.curselection()
        if not sel: return
        idx = sel[0]
        new = idx + direction
        if new < 0 or new >= len(self.steps): return
        self.steps[idx], self.steps[new] = self.steps[new], self.steps[idx]
        self.step_list.delete(idx)
        self.step_list.insert(idx, format_step(self.steps[idx]))
        self.step_list.delete(new)
        self.step_list.insert(new, format_step(self.steps[new]))
        self.step_list.selection_set(new)

    def clear_steps(self):
        if not self.steps: return
        if not messagebox.askyesno("Clear all",
                f"Delete all {len(self.steps)} recorded steps?"):
            return
        self.steps = []
        self.step_list.delete(0, tk.END)
        self._last_step_sel = None

    def save_steps(self):
        if not self.steps:
            messagebox.showinfo("Nothing to save", "No steps recorded yet.")
            return

        # Warn if the recording still has plaintext secret values in it.
        plaintext_secrets = sum(
            1 for s in self.steps
            if s.get("secret") and s.get("value") not in (None, "")
        )
        if plaintext_secrets > 0:
            ok = messagebox.askyesno(
                "Plaintext secrets in recording",
                f"{plaintext_secrets} step(s) contain secret values in plaintext.\n\n"
                "Saving this file will write those values to disk in clear text. "
                "Anyone with access to the file will be able to read them.\n\n"
                "Save anyway?",
                icon="warning",
            )
            if not ok:
                return

        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile="recording.json",
        )
        if not path: return
        try:
            # Strip internal-only metadata (page_url) that's used during
            # recording to detect postback reloads but isn't a replay
            # instruction. Keeps saved files clean.
            clean = [{k: v for k, v in s.items() if k != "page_url"}
                     for s in self.steps]
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"steps": clean}, f, indent=2, ensure_ascii=False)
            self._set_status(f"Saved {len(self.steps)} steps to {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # ----- Load recording -------------------------------------------------

    def load_steps(self):
        if self.steps and not messagebox.askyesno(
                "Replace current steps?",
                f"Loading will replace the {len(self.steps)} current step(s). Continue?"):
            return
        path = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = data.get("steps", [])
            if not isinstance(loaded, list):
                raise ValueError("File doesn't contain a 'steps' list.")
            self.steps = loaded
            self.step_list.delete(0, tk.END)
            self._last_step_sel = None
            for step in self.steps:
                self.step_list.insert(tk.END, format_step(step))
            self._set_status(f"Loaded {len(self.steps)} steps from {path}")
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    # ----- Replay --------------------------------------------------------

    def on_resolve(self):
        if self.replaying:
            messagebox.showinfo("Busy", "A run is already in progress.")
            return
        dlg = ResolveDialog(self.root)
        self.root.wait_window(dlg)
        if not dlg.emails:
            return

        # If the user wants to auto-run the batch, we need a template with
        # {{studentid}} loaded. Offer to chain only if one is present.
        auto_batch = dlg.auto_batch
        template = None
        if auto_batch:
            has_var = any(
                isinstance(s.get(k), str) and "{{studentid}}" in s.get(k, "")
                for s in self.steps for k in ("url", "value", "label"))
            if not (self.steps and has_var):
                # No usable template loaded. Don't silently fall back to
                # resolve-only — let the user choose. Yes = go ahead and just
                # build the roster; No/Cancel = back out so they can load the
                # batch template and start over.
                proceed = messagebox.askyesno(
                    "No batch template loaded",
                    "You asked to auto-run the batch, but no batch template "
                    "with {{studentid}} is loaded (one that navigates to "
                    "StudentDetail.aspx?Id={{studentid}}).\n\n"
                    "Yes — just resolve the emails to a roster (no downloads).\n"
                    "No — cancel so you can load the template first.")
                if not proceed:
                    self._set_status(
                        "Resolve cancelled — load a batch template, then retry.")
                    return
                auto_batch = False
            else:
                template = [dict(s) for s in self.steps]

        # Check the result CSVs aren't open in Excel. Resolve always writes
        # email_roster.csv; if it will auto-batch it also writes the batch
        # files, so include those too.
        needed = ["email_roster.csv"]
        if auto_batch:
            needed += ["batch_results.csv", "date_swaps.csv"]
        if not self._check_csvs_writable(needed):
            self._set_status("Resolve cancelled — close the open file and retry.")
            return

        self.replaying = True
        self.replay_btn.config(state="disabled")
        self.worker._stop_event.clear()
        self.stop_btn.config(state="normal")
        self._set_status(f"Resolving {len(dlg.emails)} emails...")
        self.replay_progress_var.set(f"Resolving 0/{len(dlg.emails)}")
        self.cmd_q.put({"action": "resolve_run", "emails": dlg.emails,
                        "template": template, "auto_batch": auto_batch})

    def _on_resolve_start(self, payload):
        self._set_status(f"Resolving {payload.get('total')} emails...")

    def _on_resolve_progress(self, payload):
        cur = payload.get("current"); total = payload.get("total")
        email = payload.get("email", "")
        self.replay_progress_var.set(f"Resolving {cur}/{total}")
        self._set_status(f"Looking up {email}...")

    def _on_resolve_done(self, payload):
        resolved = payload.get("resolved", 0)
        not_found = payload.get("not_found", 0)
        multiple = payload.get("multiple", 0)
        roster_path = payload.get("roster_path", "")
        will_batch = payload.get("will_batch", False)
        stopped = payload.get("stopped", False)
        prefix = "Resolve stopped early.\n\n" if stopped else ""
        msg = (f"{prefix}Resolved: {resolved}\n"
               f"Not found: {not_found}\n"
               f"Multiple matches (set aside): {multiple}\n\n"
               f"Roster saved to:\n{roster_path}")
        if will_batch:
            msg += (f"\n\nNow auto-running transcripts for the {resolved} "
                    f"cleanly-resolved students...")
            # The batch_* events will drive the rest of the UI; don't release
            # the replaying flag yet. Stop stays enabled for the batch phase.
            self._set_status(f"Resolved {resolved}; starting batch...")
            messagebox.showinfo("Resolve complete", msg)
        else:
            self.replaying = False
            self.replay_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.replay_progress_var.set("")
            done_word = "stopped" if stopped else "complete"
            self._set_status(f"Resolve {done_word}. Roster: {roster_path}")
            messagebox.showinfo("Resolve complete", msg)

    def on_batch(self):
        if self.replaying:
            messagebox.showinfo("Busy", "A replay or batch is already running.")
            return
        if not self.steps:
            messagebox.showinfo("No template",
                "Load a batch template recording first (one that navigates to "
                "StudentDetail.aspx?Id={{studentid}}).")
            return
        # The template must contain the {{studentid}} placeholder somewhere.
        has_var = any(
            isinstance(s.get(k), str) and "{{studentid}}" in s.get(k, "")
            for s in self.steps for k in ("url", "value", "label"))
        if not has_var:
            if not messagebox.askyesno(
                "No {{studentid}} found",
                "None of the loaded steps contain the {{studentid}} "
                "placeholder, so every student would run identically.\n\n"
                "The batch template should navigate to "
                "StudentDetail.aspx?Id={{studentid}}.\n\nRun anyway?"):
                return

        dlg = BatchDialog(self.root)
        self.root.wait_window(dlg)
        if not dlg.student_ids:
            return

        # Make sure the result CSVs aren't open in Excel before we start;
        # otherwise the run completes but can't save its results.
        if not self._check_csvs_writable(
                ["batch_results.csv", "date_swaps.csv"]):
            self._set_status("Batch cancelled — close the open file and retry.")
            return

        template = [dict(s) for s in self.steps]
        self.replaying = True
        self.replay_btn.config(state="disabled")
        self.worker._stop_event.clear()
        self.stop_btn.config(state="normal")
        self._set_status(f"Batch: processing {len(dlg.student_ids)} students...")
        self.replay_progress_var.set(f"Batch 0/{len(dlg.student_ids)}")
        self.cmd_q.put({"action": "batch_run", "template": template,
                        "student_ids": dlg.student_ids})

    def on_about(self):
        AboutDialog(self.root)

    # ----- Keyboard shortcuts (Phase 3) ---------------------------------

    def _bind_shortcuts(self):
        """Wire convenience keys. Modal dialogs grab input, so these only fire
        on the main window — they won't interfere with typing in a dialog."""
        r = self.root
        r.bind("<F5>", lambda e: self.on_replay())
        r.bind("<Control-s>", lambda e: self.save_steps())
        r.bind("<Control-o>", lambda e: self.load_steps())
        r.bind("<Control-p>", lambda e: self.on_preview())
        r.bind("<Control-r>", lambda e: self._send({"action": "reload"}))
        # Step-list editing: Delete removes; Alt+↑/↓ reorder (a keyboard
        # alternative to drag — robust and unambiguous).
        self.step_list.bind("<Delete>", self._key_delete)
        self.step_list.bind("<Alt-Up>", self._key_move_up)
        self.step_list.bind("<Alt-Down>", self._key_move_down)

    def _key_delete(self, _evt=None):
        self.delete_step()
        return "break"

    def _key_move_up(self, _evt=None):
        self.move_step(-1)
        return "break"

    def _key_move_down(self, _evt=None):
        self.move_step(1)
        return "break"

    # ----- Dry-run preview (Phase 3) ------------------------------------

    def on_preview(self):
        """Show the steps as they'd run, without executing — handy before a
        big batch. If the template uses {{variables}}, offer to substitute a
        sample value so the preview reads like a real run."""
        if not self.steps:
            messagebox.showinfo("Preview", "No steps loaded.")
            return
        vars_used = core.template_vars(self.steps)
        values = {}
        if vars_used:
            from tkinter import simpledialog
            sample = simpledialog.askstring(
                "Preview",
                "This template uses: " + ", ".join(sorted(vars_used)) + ".\n\n"
                "Enter a sample student id to substitute (or leave blank to "
                "preview with the {{placeholders}} intact):",
                parent=self.root)
            if sample:
                values = {v: sample for v in vars_used}
        preview = core.substitute_vars(self.steps, values) if values else self.steps
        PreviewDialog(self.root, preview, sorted(vars_used))

    # ----- Theme toggle (Phase 3) ---------------------------------------

    def toggle_theme(self):
        self._dark = not self._dark
        self._apply_theme(self._dark)
        self._set_status("Dark theme on." if self._dark else "Light theme on.")

    def _apply_theme(self, dark):
        """Best-effort dark/light theme. Wrapped so a theming hiccup on an
        exotic platform can never take the app down. The palette mirrors the
        About screen (near-black panel, cyan accent)."""
        try:
            style = ttk.Style()
            try:
                style.theme_use("clam")  # clam honours custom colours best
            except Exception:
                pass
            if dark:
                bg, fg, field, sel, accent = (
                    "#0d1117", "#e6edf3", "#161b22", "#1f6feb", "#39d3ff")
            else:
                bg, fg, field, sel, accent = (
                    "#f0f0f0", "#000000", "#ffffff", "#0a5ed8", "#0a5ed8")
            self.root.configure(bg=bg)
            for klass in ("TFrame", "TLabel", "TPanedwindow"):
                style.configure(klass, background=bg, foreground=fg)
            style.configure("TButton", background=field, foreground=fg)
            style.map("TButton", background=[("active", sel)])
            style.configure("TCheckbutton", background=bg, foreground=fg)
            style.configure("TEntry", fieldbackground=field, foreground=fg)
            # Plain tk widgets don't follow ttk styles — colour them directly.
            for lb in (getattr(self, "el_list", None),
                       getattr(self, "step_list", None)):
                if lb is not None:
                    lb.configure(bg=field, fg=fg,
                                 selectbackground=sel, selectforeground="#ffffff")
            if getattr(self, "status_label", None) is not None:
                self.status_label.configure(background=field, foreground=fg)
        except Exception as e:
            core.dlog(f"theme toggle failed (non-fatal): {e}")

    def _check_csvs_writable(self, filenames):
        """Before a run, check that the result CSVs we'll need to write aren't
        locked by another program (typically open in Excel on Windows, which
        takes an exclusive lock). Returns True if it's safe to proceed.

        For each existing file we try to open it in append mode; if that
        raises PermissionError the file is locked. We never truncate or write
        anything — append-open only tests the lock. Files that don't exist yet
        are fine (they'll be created at write time)."""
        locked = []
        for name in filenames:
            path = os.path.join(DOWNLOADS_DIR, name)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "a", encoding="utf-8"):
                    pass
            except PermissionError:
                locked.append(name)
            except Exception:
                # Any other error (missing dir, etc.) isn't a lock; let the
                # run proceed and surface real problems at write time.
                pass
        if not locked:
            return True
        names = "\n  ".join(locked)
        return messagebox.askyesno(
            "Close these files first?",
            f"These result files look like they're open in another program "
            f"(e.g. Excel):\n\n  {names}\n\n"
            f"If they stay open, the run will finish the browser work but "
            f"won't be able to save its results to them.\n\n"
            f"Close them, then click Yes to continue. Click No to cancel.")

    def on_stop(self):
        """Request that an in-progress batch or resolve stop at the next safe
        point (after the student/email currently being processed). We set the
        worker's stop flag directly from the Tk thread — the worker is busy in
        its loop and won't drain the command queue until idle, so a queued
        command wouldn't be seen in time. threading.Event is safe to set
        cross-thread."""
        self.worker._stop_event.set()
        self.stop_btn.config(state="disabled")
        self._set_status("Stop requested — finishing the current student, "
                         "then halting...")
        self.replay_progress_var.set("Stopping...")

    def _on_batch_start(self, payload):
        total = payload.get("total", 0)
        self._set_status(f"Batch started: {total} students.")
        # Open (or reuse) the live progress window — a table that fills in as
        # each student is processed, so a long unattended run is glanceable.
        try:
            if self._batch_monitor is None or not self._batch_monitor.alive():
                self._batch_monitor = BatchMonitor(self.root, total)
            else:
                self._batch_monitor.reset(total)
        except Exception:
            self._batch_monitor = None

    def _on_batch_progress(self, payload):
        cur = payload.get("current"); total = payload.get("total")
        sid = payload.get("student_id", "")
        status = payload.get("status", "")
        self.replay_progress_var.set(f"Batch {cur}/{total} (id {sid})")
        if status in ("success", "failed"):
            self._set_status(f"Student {sid}: {status}")
        if self._batch_monitor is not None:
            try:
                self._batch_monitor.update_row(
                    cur, sid, payload.get("name", ""), status)
            except Exception:
                pass

    def _on_batch_done(self, payload):
        self.replaying = False
        self.replay_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.replay_progress_var.set("")
        ok = payload.get("succeeded", 0)
        failed = payload.get("failed", 0)
        skipped = payload.get("skipped", 0)
        stopped = payload.get("stopped", False)
        csv_path = payload.get("csv_path", "")
        failed_ids = payload.get("failed_ids", [])
        head = "Batch stopped" if stopped else "Batch complete"
        msg = f"{head}: {ok} succeeded, {failed} failed"
        if skipped:
            msg += f", {skipped} skipped (already downloaded)"
        msg += "."
        if stopped:
            msg += ("\n\nStopped before finishing. Re-run the same list to "
                    "pick up the remaining students (already-downloaded ones "
                    "will be skipped).")
        if failed_ids:
            shown = ", ".join(failed_ids[:10])
            if len(failed_ids) > 10:
                shown += ", ..."
            msg += f"\n\nFailed student IDs: {shown}"
        msg += f"\n\nResults saved to:\n{csv_path}"
        status = f"{head}: {ok} ok, {failed} failed"
        if skipped:
            status += f", {skipped} skipped"
        self._set_status(f"{status}. Results: {csv_path}", clickable=False)
        if self._batch_monitor is not None:
            try:
                self._batch_monitor.finalize(head)
            except Exception:
                pass
        messagebox.showinfo(head, msg)

    def on_replay(self):
        if self.replaying:
            messagebox.showinfo("Replay in progress",
                                "A replay is already running.")
            return
        if not self.steps:
            messagebox.showinfo("Nothing to replay", "No steps loaded.")
            return

        # If any step contains a redacted secret, we need the credentials
        # store unlocked to supply the actual value.
        has_secrets = any(
            s.get("secret") and s.get("value") in (None, "")
            for s in self.steps
        )
        steps_to_run = [dict(s) for s in self.steps]  # shallow copy each

        if has_secrets:
            if not self._unlock_credentials():
                return
            # Fill in the secret values from the store.
            missing = []
            for s in steps_to_run:
                if not (s.get("secret") and s.get("value") in (None, "")):
                    continue
                # Use the URL nearest to (or just preceding) this step
                # as the host context. Find the nearest navigate-before step.
                host_source = self._find_host_for(steps_to_run, s)
                name = s.get("name", "")
                val = self.credentials.get(host_source, name)
                if val is None:
                    missing.append((host_source, name))
                else:
                    s["value"] = val

            if missing:
                # Prompt user to fill in the missing ones.
                if not self._prompt_for_missing_credentials(missing):
                    return
                # Re-fill from the (now updated) store.
                for s in steps_to_run:
                    if not (s.get("secret") and s.get("value") in (None, "")):
                        continue
                    host_source = self._find_host_for(steps_to_run, s)
                    val = self.credentials.get(host_source, s.get("name", ""))
                    if val is not None:
                        s["value"] = val

        self.replaying = True
        self.replay_steps_in_flight = steps_to_run
        self.replay_btn.config(state="disabled")
        self._set_status(f"Replaying {len(steps_to_run)} steps...")
        self.replay_progress_var.set(f"Replaying 0/{len(steps_to_run)}")
        self.cmd_q.put({"action": "replay", "steps": steps_to_run})

    def _find_host_for(self, steps, target_step):
        """Find the most recent navigate URL at-or-before target_step.
        Used to scope credentials to a specific host."""
        # Walk backward from target's position to find a navigate URL.
        try:
            idx = steps.index(target_step)
        except ValueError:
            idx = len(steps) - 1
        for i in range(idx, -1, -1):
            if steps[i].get("type") == "navigate" and steps[i].get("url"):
                return steps[i]["url"]
        return ""

    # ----- Credentials manager dialog -----------------------------------

    def _unlock_credentials(self):
        """Prompt for the passphrase and unlock self.credentials.
        Returns True on success, False if user cancelled or wrong pw."""
        try:
            CredentialsStore._check_crypto()
        except RuntimeError as e:
            messagebox.showerror("Encryption not available", str(e))
            return False

        # Skip if already unlocked.
        if self.credentials._fernet is not None:
            return True

        first_time = not self.credentials.exists()
        prompt = ("Set a passphrase for the credentials store.\n"
                  "You'll need this every time you start a session that\n"
                  "uses encrypted credentials. Don't lose it — there's no\n"
                  "recovery." if first_time else
                  "Enter the passphrase to unlock the credentials store:")
        from tkinter import simpledialog
        passphrase = simpledialog.askstring(
            "Credentials passphrase", prompt, show="*", parent=self.root)
        if not passphrase:
            return False
        try:
            ok = self.credentials.unlock(passphrase)
        except Exception as e:
            messagebox.showerror("Unlock failed", str(e))
            return False
        if not ok:
            messagebox.showerror("Wrong passphrase",
                                 "Could not decrypt the store.")
            return False
        return True

    def _prompt_for_missing_credentials(self, missing):
        """Ask the user to provide each missing credential.
        Returns True if all were provided; False if user cancelled."""
        from tkinter import simpledialog
        for host, name in missing:
            val = simpledialog.askstring(
                "Credential needed",
                f"Enter the value for:\n  Host: {host}\n  Field: {name}",
                show="*", parent=self.root,
            )
            if val is None:  # cancelled
                return False
            self.credentials.set(host, name, val)
        return True

    def on_manage_credentials(self):
        if not self._unlock_credentials():
            return
        CredentialsDialog(self.root, self.credentials)

    # ----- Replay events from worker ------------------------------------

    def _on_replay_start(self, payload):
        total = payload.get("total", 0)
        self.replay_progress_var.set(f"Replaying 0/{total}")

    def _on_replay_step(self, payload):
        idx = payload.get("index", 0)
        total = payload.get("total", 0)
        status = payload.get("status", "")
        self.replay_progress_var.set(f"Step {idx+1}/{total}: {status}")
        # Highlight the current step in the listbox.
        try:
            self.step_list.selection_clear(0, tk.END)
            self.step_list.selection_set(idx)
            self.step_list.see(idx)
            # Optional: tint the row while running.
            color = "#fff2a8" if status == "running" else "#d4f4d4"
            self.step_list.itemconfig(idx, background=color)
        except Exception:
            pass

    def _on_replay_paused(self, payload):
        idx = payload.get("index", 0)
        step = payload.get("step", {})
        reason = payload.get("reason", "")
        choice = ReplayPauseDialog(
            self.root, idx, step, reason
        ).result
        if choice == "stop":
            self._end_replay(message=f"Replay stopped at step {idx+1}.")
        elif choice == "skip":
            # Resume from the NEXT step.
            self.cmd_q.put({
                "action": "replay_resume",
                "steps": self.replay_steps_in_flight,
                "starting_index": idx + 1,
            })
        elif choice == "retry":
            self.cmd_q.put({
                "action": "replay_resume",
                "steps": self.replay_steps_in_flight,
                "starting_index": idx,
            })
        else:  # closed dialog without choosing
            self._end_replay(message="Replay cancelled.")

    def _on_replay_done(self, payload):
        total = payload.get("total", 0)
        self._end_replay(message=f"Replay finished. {total} step(s) executed.")

    def _end_replay(self, message=""):
        self.replaying = False
        self.replay_steps_in_flight = []
        self.replay_btn.config(state="normal")
        self.replay_progress_var.set("")
        if message:
            self._set_status(message)
        # Clear the row highlights from the listbox.
        try:
            for i in range(self.step_list.size()):
                self.step_list.itemconfig(i, background="")
        except Exception:
            pass


# ----- Dialogs --------------------------------------------------------------

class CredentialsDialog(tk.Toplevel):
    """List/add/edit/delete entries in the credentials store."""
    def __init__(self, parent, store):
        super().__init__(parent)
        self.title("Credentials")
        self.transient(parent)
        self.geometry("550x340")
        self.store = store

        ttk.Label(self,
            text="Saved credentials are encrypted at rest with your passphrase.",
            foreground="#555").pack(anchor="w", padx=8, pady=(8, 4))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8, pady=4)

        cols = ("host", "field", "value")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=10)
        self.tree.heading("host", text="Host")
        self.tree.heading("field", text="Field")
        self.tree.heading("value", text="Value")
        self.tree.column("host", width=200)
        self.tree.column("field", width=140)
        self.tree.column("value", width=160)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.config(yscrollcommand=sb.set)

        btn = ttk.Frame(self)
        btn.pack(fill="x", padx=8, pady=8)
        ttk.Button(btn, text="Add", command=self.on_add).pack(side="left")
        ttk.Button(btn, text="Edit", command=self.on_edit).pack(side="left", padx=4)
        ttk.Button(btn, text="Delete", command=self.on_delete).pack(side="left")
        ttk.Button(btn, text="Close", command=self.destroy).pack(side="right")

        self._refresh()

    def _refresh(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for host, field, value in self.store.list_entries():
            # Show value as ••••• in the list — it's sensitive.
            masked = "•" * min(len(value or ""), 12)
            self.tree.insert("", "end", values=(host, field, masked),
                             tags=(host, field))

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")

    def on_add(self):
        d = CredentialEditDialog(self, "", "", "")
        self.wait_window(d)
        if d.result:
            host, field, value = d.result
            self.store.set(host, field, value)
            self._refresh()

    def on_edit(self):
        row = self._selected()
        if not row:
            return
        host, field, _ = row
        actual = self.store.get(host, field) or ""
        d = CredentialEditDialog(self, host, field, actual)
        self.wait_window(d)
        if d.result:
            new_host, new_field, value = d.result
            if (new_host, new_field) != (host, field):
                self.store.delete(host, field)
            self.store.set(new_host, new_field, value)
            self._refresh()

    def on_delete(self):
        row = self._selected()
        if not row:
            return
        host, field, _ = row
        if not messagebox.askyesno("Delete credential",
                f"Delete the credential for {host} / {field}?", parent=self):
            return
        self.store.delete(host, field)
        self._refresh()


class CredentialEditDialog(tk.Toplevel):
    def __init__(self, parent, host, field, value):
        super().__init__(parent)
        self.title("Credential")
        self.transient(parent)
        self.grab_set()
        self.result = None

        rows = [("Host", host, False),
                ("Field name", field, False),
                ("Value", value, True)]
        self.vars = []
        for r, (label, initial, secret) in enumerate(rows):
            ttk.Label(self, text=label + ":").grid(
                row=r, column=0, sticky="w", padx=8, pady=4)
            v = tk.StringVar(value=initial or "")
            entry = ttk.Entry(self, textvariable=v, width=40,
                              show="*" if secret else "")
            entry.grid(row=r, column=1, padx=8, pady=4)
            self.vars.append(v)

        ttk.Label(self,
            text="Host: e.g. id.vancoplatform.com  (or full URL — host is extracted)\n"
                 "Field: the form field name as recorded (case-insensitive)",
            foreground="#555", justify="left").grid(
            row=len(rows), column=0, columnspan=2, padx=8, pady=(4, 0), sticky="w")

        btnf = ttk.Frame(self)
        btnf.grid(row=len(rows) + 1, column=0, columnspan=2, pady=10)
        ttk.Button(btnf, text="OK", command=self.on_ok).pack(side="left", padx=6)
        ttk.Button(btnf, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    def on_ok(self):
        host = self.vars[0].get().strip()
        field = self.vars[1].get().strip()
        value = self.vars[2].get()
        if not host or not field:
            messagebox.showerror("Required", "Host and field are required.",
                                 parent=self)
            return
        # Normalize host: accept full URL, extract host.
        try:
            from urllib.parse import urlparse as _up
            parsed = _up(host)
            if parsed.hostname:
                host = parsed.hostname
        except Exception:
            pass
        self.result = (host.lower(), field, value)
        self.destroy()


class PreviewDialog(tk.Toplevel):
    """Read-only dry-run view: lists each step as it would run, with any
    {{variables}} already substituted. Steps that still contain an unresolved
    placeholder are flagged in red so problems are obvious before a real run."""

    def __init__(self, parent, steps, vars_used):
        super().__init__(parent)
        self.title("Preview — dry run")
        self.transient(parent)
        self.geometry("620x460")

        ttk.Label(self, text=f"Replay would run these {len(steps)} step(s):",
                  font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 4))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=10)
        lb = tk.Listbox(wrap, activestyle="none")
        lb.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=lb.yview)
        sb.pack(side="right", fill="y")
        lb.config(yscrollcommand=sb.set)

        unresolved = 0
        for i, step in enumerate(steps, start=1):
            lb.insert(tk.END, f"{i:>3}. {format_step(step)}")
            if any(isinstance(step.get(k), str) and "{{" in step.get(k, "")
                   for k in ("url", "value", "label")):
                unresolved += 1
                lb.itemconfig(tk.END, foreground="#a00")

        if vars_used:
            note = "Variables: " + ", ".join(vars_used) + ".  "
            note += (f"{unresolved} step(s) still have unresolved "
                     "{{placeholders}} (red)." if unresolved
                     else "All placeholders resolved.")
        else:
            note = "No variables in this template."
        ttk.Label(self, text=note, foreground="#555",
                  wraplength=580, justify="left").pack(
            anchor="w", padx=10, pady=(6, 4))

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=(0, 10))
        self.bind("<Escape>", lambda e: self.destroy())


class BatchMonitor(tk.Toplevel):
    """Live, glanceable table of batch progress. Rows fill in as students are
    processed and recolour by outcome. Independent of the modal completion
    dialog so a long unattended run can be watched without blocking."""

    STATUS_LABEL = {"running": "running…", "success": "done",
                    "failed": "FAILED", "skipped": "skipped"}

    def __init__(self, parent, total):
        super().__init__(parent)
        self.title("Batch progress")
        self.geometry("680x420")
        self.total = total
        self._rows = {}  # current-index -> tree iid

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        self.summary = tk.StringVar()
        ttk.Label(top, textvariable=self.summary,
                  font=("TkDefaultFont", 10, "bold")).pack(side="left")

        cols = ("n", "id", "name", "status", "note")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, w, t, anchor in [("n", 44, "#", "e"), ("id", 90, "ID", "w"),
                                ("name", 200, "Name", "w"),
                                ("status", 90, "Status", "w"),
                                ("note", 230, "Note", "w")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anchor)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree.tag_configure("success", background="#e7f7e7")
        self.tree.tag_configure("failed", background="#fde8e8")
        self.tree.tag_configure("skipped", background="#fff7e0")
        self.tree.tag_configure("running", background="#eef3ff")
        self._refresh_summary()

    def alive(self):
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def reset(self, total):
        self.total = total
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._rows = {}
        self._refresh_summary()

    def update_row(self, current, student_id, name, status, note=""):
        vals = (current, student_id, name or "",
                self.STATUS_LABEL.get(status, status), note or "")
        iid = self._rows.get(current)
        if iid is None:
            iid = self.tree.insert("", "end", values=vals, tags=(status,))
            self._rows[current] = iid
        else:
            self.tree.item(iid, values=vals, tags=(status,))
        try:
            self.tree.see(iid)
        except Exception:
            pass
        self._refresh_summary()

    def _refresh_summary(self):
        counts = {"success": 0, "failed": 0, "skipped": 0}
        for iid in self._rows.values():
            tags = self.tree.item(iid, "tags")
            if tags and tags[0] in counts:
                counts[tags[0]] += 1
        processed = sum(counts.values())
        self.summary.set(
            f"{processed} / {self.total} processed   "
            f"({counts['success']} ok, {counts['failed']} failed, "
            f"{counts['skipped']} skipped)")

    def finalize(self, head):
        self.title(f"Batch progress — {head}")
        self._refresh_summary()


class ReplayPauseDialog(tk.Toplevel):
    """Shown when a replay step fails. Offers Stop/Skip/Retry."""
    def __init__(self, parent, index, step, reason):
        super().__init__(parent)
        self.title("Replay paused")
        self.transient(parent)
        self.grab_set()
        self.result = None

        ttk.Label(self, text=f"Step {index + 1} could not be executed.",
                  font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=12, pady=(12, 4))
        ttk.Label(self, text=format_step(step), wraplength=480,
                  justify="left").pack(anchor="w", padx=12, pady=(0, 4))
        if reason:
            ttk.Label(self, text=reason, foreground="#a00",
                      wraplength=480, justify="left").pack(
                anchor="w", padx=12, pady=(0, 8))

        btn = ttk.Frame(self)
        btn.pack(padx=12, pady=10)
        ttk.Button(btn, text="Retry",
                   command=lambda: self._choose("retry")).pack(side="left", padx=4)
        ttk.Button(btn, text="Skip",
                   command=lambda: self._choose("skip")).pack(side="left", padx=4)
        ttk.Button(btn, text="Stop",
                   command=lambda: self._choose("stop")).pack(side="left", padx=4)

        self.protocol("WM_DELETE_WINDOW", lambda: self._choose("stop"))
        self.wait_window()  # block caller until dismissed

    def _choose(self, choice):
        self.result = choice
        self.destroy()


# ----- Headless CLI ---------------------------------------------------------
#
# `python navigator.py` with no subcommand launches the GUI exactly as before.
# Subcommands (batch / resolve / merge-pdf) run without the GUI so the same
# recordings can be driven from Task Scheduler or cron. They share the
# BrowserWorker engine; only the driver (here) differs from the Tk app.

def _print_cli_event(kind, action, payload):
    """Render a worker result-queue event as a console progress line."""
    p = payload if isinstance(payload, dict) else {}
    if kind == "resolve_start":
        print(f"  resolving {p.get('total')} email(s)…")
    elif kind == "resolve_progress":
        print(f"  [{p.get('current')}/{p.get('total')}] {p.get('email','')}")
    elif kind == "resolve_done":
        print(f"  resolved={p.get('resolved')} not_found={p.get('not_found')} "
              f"multiple={p.get('multiple')}  →  {p.get('roster_path')}")
    elif kind == "batch_start":
        print(f"  batch: {p.get('total')} student(s)…")
    elif kind == "batch_progress":
        st = p.get("status", "")
        if st in ("success", "failed", "skipped"):
            tail = f" — {p.get('name')}" if p.get("name") else ""
            print(f"  [{p.get('current')}/{p.get('total')}] "
                  f"id={p.get('student_id')} {st}{tail}")
    elif kind == "batch_done":
        print(f"  batch done: {p.get('succeeded')} ok, {p.get('failed')} "
              f"failed, {p.get('skipped')} skipped  →  {p.get('csv_path')}")
        if p.get("failed_ids"):
            print(f"  failed ids: {', '.join(p['failed_ids'][:20])}"
                  + (" …" if len(p['failed_ids']) > 20 else ""))
    elif kind == "download":
        print(f"    ⤓ {p.get('filename')}")
    elif kind == "err":
        print(f"  ! error ({action}): {payload}", file=sys.stderr)


def _drive(command, *, output=None, headless=False):
    """Start a headless BrowserWorker, send one command, and stream progress
    to the console until the run finishes. Returns a process exit code."""
    global DOWNLOADS_DIR
    if output:
        DOWNLOADS_DIR = os.path.abspath(output)
    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    except Exception:
        pass
    core.setup_logging(log_dir=DOWNLOADS_DIR)
    core.log.info("CLI %s starting (output=%s, headless=%s)",
                  command.get("action"), DOWNLOADS_DIR, headless)

    cmd_q, result_q = queue.Queue(), queue.Queue()
    worker = BrowserWorker(cmd_q, result_q, headless=headless)
    worker.start()
    cmd_q.put(command)

    rc = 0
    try:
        while True:
            try:
                kind, action, payload = result_q.get(timeout=1.0)
            except queue.Empty:
                if not worker.is_alive():
                    print("Browser worker exited unexpectedly — check that "
                          "Playwright/Chromium is installed and see "
                          "the log file (asap-powertools.log).", file=sys.stderr)
                    rc = 1
                    break
                continue
            _print_cli_event(kind, action, payload)
            if kind == "resolve_done":
                # An auto-batch run continues into batch_* events; only stop
                # here when no batch will follow.
                if not (isinstance(payload, dict) and payload.get("will_batch")):
                    rc = 0 if (payload or {}).get("resolved", 0) else rc
                    break
            elif kind == "batch_done":
                if (payload or {}).get("failed"):
                    rc = 2  # completed, but some students failed
                break
    finally:
        cmd_q.put(None)
        worker.join(timeout=15)
    return rc


def _cli_merge(folder, out, pattern):
    """merge-pdf subcommand: combine matching transcript PDFs into one file."""
    folder = os.path.abspath(folder or DOWNLOADS_DIR)
    core.setup_logging(log_dir=folder)
    out = os.path.abspath(out or os.path.join(folder, "transcripts_combined.pdf"))
    pdfs = [p for p in core.find_pdfs(folder, contains=pattern)
            if os.path.abspath(p) != out]
    if not pdfs:
        print(f"No PDFs matching {pattern!r} in {folder}.", file=sys.stderr)
        return 1
    try:
        n = core.merge_pdfs(pdfs, out)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Merged {n} PDF(s) → {out}")
    return 0


def _run_gui():
    # Configure logging before anything else so worker/app diagnostics land in
    # the rotating file log (asap-powertools.log) in the output folder. The console
    # stays quiet (INFO) unless ASAP_POWERTOOLS_DEBUG is set.
    core.setup_logging(log_dir=DOWNLOADS_DIR)
    core.log.info("ASAP Powertools GUI starting (output folder: %s)", DOWNLOADS_DIR)
    root = tk.Tk()
    app = App(root)
    try:
        root.mainloop()
    finally:
        app.cmd_q.put(None)
    return 0


def main(argv=None):
    # The CLI surface (arg parser + input parsing) lives in navigator_core so
    # it can be unit-tested without importing this browser/GUI module.
    args = core.build_cli_parser().parse_args(argv)

    if args.command is None:
        return _run_gui()

    if args.command == "merge-pdf":
        return _cli_merge(args.dir, args.out, args.pattern)

    if args.command == "batch":
        template = core.load_template_steps(args.template)
        if args.csv:
            items = core.read_csv_rows(args.csv, args.id_column)
            if not items:
                print("No usable rows in CSV (need a non-empty id column "
                      f"{args.id_column!r}).", file=sys.stderr)
                return 1
            print(f"Batch: {len(items)} student(s) from {args.csv} "
                  f"(headless={args.headless}).")
        else:
            items = core.read_tokens(args.ids)
            if not items:
                print("No student IDs provided.", file=sys.stderr)
                return 1
            print(f"Batch: {len(items)} student(s) (headless={args.headless}).")
        return _drive({"action": "batch_run", "template": template,
                       "student_ids": items},
                      output=args.output, headless=args.headless)

    if args.command == "resolve":
        emails = core.read_tokens(args.emails)
        if not emails:
            print("No emails provided.", file=sys.stderr)
            return 1
        template = None
        if args.auto_batch:
            if not args.template:
                print("--auto-batch requires --template.", file=sys.stderr)
                return 1
            template = core.load_template_steps(args.template)
        print(f"Resolve: {len(emails)} email(s) "
              f"(auto_batch={args.auto_batch}, headless={args.headless}).")
        return _drive({"action": "resolve_run", "emails": emails,
                       "template": template, "auto_batch": args.auto_batch},
                      output=args.output, headless=args.headless)

    return 0


if __name__ == "__main__":
    sys.exit(main())
