/**
 * page_inspector.js — ASAP Powertools screen mapper
 *
 * HOW TO USE
 * ----------
 * 1. Navigate to any ASAP Connected screen in your browser.
 * 2. Right-click anywhere → Inspect → Console tab.
 * 3. Paste this entire script and press Enter.
 * 4. Copy the JSON block that appears in the console.
 * 5. Paste it into the chat with CJ / Claude to map the screen.
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
      if (['text','email','tel','url','search'].includes(type)) return 'textbox';
      if (type === 'password') return 'textbox';
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

    // aria-labelledby
    const lby = attr('aria-labelledby');
    if (lby) {
      const parts = lby.split(/\s+/).map(id => {
        const r = document.getElementById(id);
        return r ? (r.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }

    // aria-label
    const al = attr('aria-label');
    if (al) return al.trim();

    // <label for="id">
    if (el.id) {
      try {
        const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lbl) return (lbl.textContent || '').trim();
      } catch (_) {}
    }

    // wrapping <label>
    if (el.closest) {
      const wrap = el.closest('label');
      if (wrap) {
        const c = wrap.cloneNode(true);
        c.querySelectorAll('input,textarea,select').forEach(n => n.remove());
        return (c.textContent || '').trim();
      }
    }

    // button / link text
    const tag = el.tagName.toUpperCase();
    if (tag === 'BUTTON' || tag === 'A') return (el.textContent || '').trim();

    // input placeholder / title
    if (tag === 'INPUT' || tag === 'TEXTAREA')
      return attr('placeholder') || attr('title') || attr('name') || '';

    return attr('title') || '';
  }

  function getLocators(el) {
    const loc = {};
    let cur = el;
    for (let d = 0; cur && d < 4; d++, cur = cur.parentElement) {
      const id = cur.getAttribute && cur.getAttribute('id');
      const nm = cur.getAttribute && cur.getAttribute('name');
      const testid = cur.getAttribute && (
        cur.getAttribute('data-testid') ||
        cur.getAttribute('data-test') ||
        cur.getAttribute('data-qa'));
      if (testid && !loc.testid) loc.testid = testid;
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

  // Identify inputs that carry sensitive STRUCTURAL labels (e.g. "SSN",
  // "Password"). The label itself is safe to include — it's just the name of
  // the field. We flag these so you know they exist, but still redact values.
  const SENSITIVE_LABEL_RE =
    /\b(ssn|social.?security|password|dob|date.of.birth|birth.?date|pin|credit.?card|cvv|cvc)\b/i;

  function isSensitiveLabel(label) {
    return SENSITIVE_LABEL_RE.test(label || '');
  }

  // ── Section detection ─────────────────────────────────────────────────────
  // Walk up from an element looking for a meaningful section heading.

  function nearestHeading(el) {
    let cur = el ? el.parentElement : null;
    const limit = 6;
    let depth = 0;
    while (cur && depth < limit) {
      // fieldset > legend
      if (cur.tagName === 'FIELDSET') {
        const legend = cur.querySelector(':scope > legend');
        if (legend) return (legend.textContent || '').trim();
      }
      // panel / group heading — look for a sibling or child heading element
      const headingEl = cur.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4,:scope > .panel-heading,:scope > .panel-title,:scope > .section-title');
      if (headingEl) return (headingEl.textContent || '').trim().split('\n')[0].trim();
      cur = cur.parentElement;
      depth++;
    }
    return null;
  }

  // ── Interactive element scan ──────────────────────────────────────────────

  const INTERACTIVE_ROLES = new Set([
    'button','link','textbox','checkbox','radio','combobox',
    'menuitem','tab','switch','searchbox','slider','spinbutton','option',
  ]);

  const seen = new Set();
  const fields = [];

  document.querySelectorAll('*').forEach(el => {
    const role = getRole(el);
    if (!role || !INTERACTIVE_ROLES.has(role)) return;
    if (isHidden(el)) return;

    // De-duplicate by id, then by (role+label) pair.
    const loc = getLocators(el);
    const dedupeKey = loc.id || (role + '|' + getLabel(el));
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);

    const label = getLabel(el);
    const section = nearestHeading(el);

    const entry = { role, label };

    // Structural identifiers (help future automation target this element).
    if (loc.id)        entry.id        = loc.id;
    if (loc.id_suffix && loc.id_suffix !== loc.id)
                       entry.id_suffix = loc.id_suffix;
    if (loc.name_attr) entry.name_attr = loc.name_attr;
    if (loc.testid)    entry.testid    = loc.testid;
    if (section)       entry.section   = section;

    // For select/combobox: list the option labels (not values) so we know
    // what choices exist — still no student data.
    if (role === 'combobox' && el.tagName === 'SELECT') {
      const opts = Array.from(el.options)
        .map(o => (o.textContent || '').trim())
        .filter(t => t);
      if (opts.length) entry.options = opts.slice(0, 40); // cap at 40
    }

    // Mark sensitive fields (label tells us the field name; value is always
    // redacted so there's no PII exposure).
    if (isSensitiveLabel(label)) entry.sensitive = true;

    // Value: always redacted. We include the shape ("filled" vs "empty") so
    // you can tell whether the screen arrived pre-populated.
    const rawVal = el.value !== undefined ? String(el.value || '') : '';
    if (role === 'checkbox' || role === 'radio') {
      entry.checked = el.checked;
    } else if (rawVal.trim()) {
      entry.value = '[REDACTED — field has a value]';
    }

    fields.push(entry);
  });

  // ── Button / link summary (top-level actions on the page) ─────────────────
  // Separate list so the output is easy to skim for "what can you DO here."

  const actions = fields
    .filter(f => f.role === 'button' || f.role === 'link')
    .map(f => f.label || '(unlabelled)')
    .filter((v, i, a) => v && a.indexOf(v) === i); // unique, preserve order

  // ── Page metadata ─────────────────────────────────────────────────────────

  const meta = {
    url:    location.href,
    title:  document.title || '',
    timestamp: new Date().toISOString(),
  };

  // ── Assemble output ───────────────────────────────────────────────────────
  // Group fields by section for readability. Fields with no detected section
  // go into an "(ungrouped)" bucket.

  const sectionMap = {};
  fields.forEach(f => {
    const sec = f.section || '(ungrouped)';
    if (!sectionMap[sec]) sectionMap[sec] = [];
    const entry = Object.assign({}, f);
    delete entry.section;  // already encoded in the key
    sectionMap[sec].push(entry);
  });

  const sections = Object.entries(sectionMap).map(([heading, items]) => ({
    heading,
    items,
  }));

  const output = {
    meta,
    actions,
    sections,
  };

  // ── Print ─────────────────────────────────────────────────────────────────

  const json = JSON.stringify(output, null, 2);

  // Print in a visually distinct block so it's easy to copy.
  console.log('%c── ASAP Powertools: Page Inspector Output ──', 'color:#39d3ff;font-weight:bold');
  console.log('%cCopy everything between the lines below and paste it into the chat.', 'color:#8b949e');
  console.log('---BEGIN---');
  console.log(json);
  console.log('---END---');

  // Also return the object so DevTools shows it as an inspectable tree.
  return output;
})();
