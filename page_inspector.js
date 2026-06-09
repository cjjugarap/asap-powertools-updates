/**
 * page_inspector.js — ASAP Powertools screen mapper
 *
 * HOW TO USE
 * ----------
 * 1. Navigate to any ASAP Connected screen in your browser.
 * 2. Click the "🔍 Inspect Page" bookmark in your bookmarks bar.
 * 3. A popup appears with the JSON already selected.
 * 4. Click "Copy" in the popup, then paste into the chat.
 *
 * PRIVACY GUARANTEE
 * -----------------
 * Field VALUES are never captured — only labels, IDs, roles, and structure.
 * Any filled input appears as "[REDACTED]" in the output.
 */

(() => {
  // ── Helpers ──────────────────────────────────────────────────────────────

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
      if (['text','email','tel','url','search','password'].includes(type)) return 'textbox';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'number') return 'spinbutton';
      if (type === 'range') return 'slider';
      if (['submit','button','reset','image'].includes(type)) return 'button';
      return 'textbox';
    }
    return null;
  }

  function getLabel(el) {
    if (!el) return '';
    const attr = n => el.getAttribute && el.getAttribute(n);
    const lby = attr('aria-labelledby');
    if (lby) {
      const parts = lby.split(/\s+/).map(id => {
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
      } catch (_) {}
    }
    if (el.closest) {
      const wrap = el.closest('label');
      if (wrap) {
        const c = wrap.cloneNode(true);
        c.querySelectorAll('input,textarea,select').forEach(n => n.remove());
        return (c.textContent || '').trim();
      }
    }
    const tag = el.tagName.toUpperCase();
    if (tag === 'BUTTON' || tag === 'A') return (el.textContent || '').trim();
    if (tag === 'INPUT' || tag === 'TEXTAREA')
      return attr('placeholder') || attr('title') || attr('name') || '';
    return attr('title') || '';
  }

  function getLocators(el) {
    const loc = {};
    let cur = el;
    for (let d = 0; cur && d < 4; d++, cur = cur.parentElement) {
      const id  = cur.getAttribute && cur.getAttribute('id');
      const nm  = cur.getAttribute && cur.getAttribute('name');
      const ti  = cur.getAttribute && (
        cur.getAttribute('data-testid') ||
        cur.getAttribute('data-test')   ||
        cur.getAttribute('data-qa'));
      if (ti && !loc.testid) loc.testid = ti;
      if (id && !loc.id) {
        loc.id = id;
        const parts = id.split(/[_$]/);
        if (parts.length > 1) loc.id_suffix = parts[parts.length - 1];
      }
      if (nm && !loc.name_attr) loc.name_attr = nm;
      if (loc.id) break;
    }
    return loc;
  }

  function isHidden(el) {
    if (!el) return true;
    try {
      const s = window.getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') return true;
    } catch (_) {}
    if (el.offsetParent === null && el.tagName !== 'BODY') return true;
    if (el.type && el.type.toLowerCase() === 'hidden') return true;
    return false;
  }

  function nearestHeading(el) {
    let cur = el ? el.parentElement : null;
    for (let depth = 0; cur && depth < 6; depth++, cur = cur.parentElement) {
      if (cur.tagName === 'FIELDSET') {
        const legend = cur.querySelector(':scope > legend');
        if (legend) return (legend.textContent || '').trim();
      }
      const h = cur.querySelector(
        ':scope > h1,:scope > h2,:scope > h3,:scope > h4,' +
        ':scope > .panel-heading,:scope > .panel-title,:scope > .section-title');
      if (h) return (h.textContent || '').trim().split('\n')[0].trim();
    }
    return null;
  }

  // ── Scan ──────────────────────────────────────────────────────────────────

  const INTERACTIVE_ROLES = new Set([
    'button','link','textbox','checkbox','radio','combobox',
    'menuitem','tab','switch','searchbox','slider','spinbutton','option',
  ]);
  const SENSITIVE_RE = /\b(ssn|social.?security|password|dob|date.of.birth|birth.?date|pin|credit.?card|cvv|cvc)\b/i;

  const seen   = new Set();
  const fields = [];

  document.querySelectorAll('*').forEach(el => {
    const role = getRole(el);
    if (!role || !INTERACTIVE_ROLES.has(role)) return;
    if (isHidden(el)) return;

    const loc      = getLocators(el);
    const dedupeKey = loc.id || (role + '|' + getLabel(el));
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);

    const label   = getLabel(el);
    const section = nearestHeading(el);
    const entry   = { role, label };

    if (loc.id)                              entry.id        = loc.id;
    if (loc.id_suffix && loc.id_suffix !== loc.id) entry.id_suffix = loc.id_suffix;
    if (loc.name_attr)                       entry.name_attr = loc.name_attr;
    if (loc.testid)                          entry.testid    = loc.testid;
    if (section)                             entry.section   = section;

    if (role === 'combobox' && el.tagName === 'SELECT') {
      const opts = Array.from(el.options)
        .map(o => (o.textContent || '').trim()).filter(t => t);
      if (opts.length) entry.options = opts.slice(0, 40);
    }

    if (SENSITIVE_RE.test(label || '')) entry.sensitive = true;

    const rawVal = el.value !== undefined ? String(el.value || '') : '';
    if (role === 'checkbox' || role === 'radio') {
      entry.checked = el.checked;
    } else if (rawVal.trim()) {
      entry.value = '[REDACTED]';
    }

    fields.push(entry);
  });

  const actions = fields
    .filter(f => f.role === 'button' || f.role === 'link')
    .map(f => f.label || '(unlabelled)')
    .filter((v, i, a) => v && a.indexOf(v) === i);

  const sectionMap = {};
  fields.forEach(f => {
    const sec = f.section || '(ungrouped)';
    if (!sectionMap[sec]) sectionMap[sec] = [];
    const e = Object.assign({}, f);
    delete e.section;
    sectionMap[sec].push(e);
  });

  const output = {
    meta: { url: location.href, title: document.title || '', timestamp: new Date().toISOString() },
    actions,
    sections: Object.entries(sectionMap).map(([heading, items]) => ({ heading, items })),
  };

  const json = JSON.stringify(output, null, 2);

  // ── Popup ─────────────────────────────────────────────────────────────────
  // Show a floating dialog with the JSON pre-selected and a one-click Copy
  // button. No DevTools needed — just Copy → paste into the chat.

  // Remove any existing inspector popup (re-running the bookmarklet refreshes it).
  const existing = document.getElementById('__asap_inspector_popup');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = '__asap_inspector_popup';
  overlay.style.cssText = [
    'position:fixed','top:0','left:0','width:100%','height:100%',
    'background:rgba(0,0,0,0.55)','z-index:2147483647',
    'display:flex','align-items:center','justify-content:center',
    'font-family:Segoe UI,sans-serif',
  ].join(';');

  overlay.innerHTML = `
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;
                width:560px;max-width:94vw;max-height:80vh;
                display:flex;flex-direction:column;overflow:hidden;
                box-shadow:0 8px 32px rgba(0,0,0,0.6);">

      <!-- Header -->
      <div style="display:flex;align-items:center;justify-content:space-between;
                  padding:14px 18px;border-bottom:1px solid #30363d;flex-shrink:0;">
        <span style="color:#39d3ff;font-weight:700;font-size:1rem;">
          🔍 ASAP Page Inspector
        </span>
        <span style="color:#8b949e;font-size:.8rem;margin:0 12px;flex:1;text-align:center;
                     white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
          ${output.meta.title || new URL(output.meta.url).pathname}
        </span>
        <button id="__asap_close"
          style="background:none;border:none;color:#8b949e;font-size:1.2rem;
                 cursor:pointer;padding:0 4px;line-height:1;">✕</button>
      </div>

      <!-- JSON area -->
      <textarea id="__asap_json" readonly
        style="flex:1;background:#0d1117;color:#c9d1d9;border:none;outline:none;
               resize:none;padding:14px 16px;font-family:Consolas,monospace;
               font-size:.78rem;line-height:1.5;overflow:auto;"
      >${json.replace(/</g,'&lt;')}</textarea>

      <!-- Footer -->
      <div style="display:flex;align-items:center;gap:10px;
                  padding:12px 18px;border-top:1px solid #30363d;flex-shrink:0;">
        <button id="__asap_copy"
          style="background:#39d3ff;color:#06222b;font-weight:700;font-size:.9rem;
                 border:none;border-radius:7px;padding:9px 24px;cursor:pointer;
                 flex-shrink:0;">
          Copy
        </button>
        <span id="__asap_msg"
          style="color:#3fb950;font-size:.82rem;display:none;">
          ✓ Copied — paste into the chat
        </span>
        <span style="color:#484f58;font-size:.78rem;margin-left:auto;">
          No PII · values redacted
        </span>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  // Auto-select the textarea content on open.
  const ta  = document.getElementById('__asap_json');
  const btn = document.getElementById('__asap_copy');
  const msg = document.getElementById('__asap_msg');

  setTimeout(() => { try { ta.select(); } catch(_) {} }, 50);

  btn.addEventListener('click', () => {
    try {
      navigator.clipboard.writeText(json).then(() => {
        btn.textContent = '✓ Copied!';
        btn.style.background = '#3fb950';
        msg.style.display = 'inline';
        setTimeout(() => {
          btn.textContent = 'Copy';
          btn.style.background = '#39d3ff';
        }, 2500);
      }).catch(() => {
        // Fallback for browsers that block clipboard in cross-origin frames.
        ta.select();
        document.execCommand('copy');
        btn.textContent = '✓ Copied!';
        msg.style.display = 'inline';
      });
    } catch(_) {
      ta.select();
    }
  });

  document.getElementById('__asap_close').addEventListener('click', () => overlay.remove());

  // Click outside the card to dismiss.
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

  // Also log to console as before (useful for debugging).
  console.log('%c── ASAP Powertools: Page Inspector ──', 'color:#39d3ff;font-weight:bold');
  console.log(json);

  return output;
})();
