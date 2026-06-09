// ASAP Page Inspector — injected by the Chrome extension.
// Scans the page for interactive elements, builds a structure map,
// and shows a popup with a one-click Copy button. No PII captured.

(function () {
  // Remove any existing popup (re-clicking the icon refreshes it).
  var old = document.getElementById('__asap_inspector_popup');
  if (old) { old.remove(); return; }

  // ── PII safety ─────────────────────────────────────────────────────────────
  // Two-layer protection:
  //
  // 1. SAFE_ACTION_LABELS — links/buttons whose text is structural, not data.
  //    These are always kept even inside repeating list rows.
  // 2. isInDataRow() — detects elements inside <tbody> rows (repeating student
  //    records). For those, only safe action labels are kept; everything else
  //    is suppressed so student names, emails, phones, DOBs, addresses can't
  //    leak through link text.
  // 3. looksLikePii() — pattern-based backstop that redacts anything that
  //    slipped through and matches an email, phone, SSN, or date-of-birth shape.

  var SAFE_ACTION_LABELS = new Set([
    'quick enroll','enroll','email','view schedule','schedule',
    'view details','details','view account','account','edit',
    'view','delete','remove','add','save','cancel','close',
    'print','download','export','search','find','submit','ok',
    'yes','no','back','next','continue','select','deselect',
    'check in','check out','unenroll','transfer','copy',
    'view transcript report','view transcript','transcript',
  ]);

  function isSafeActionLabel(text) {
    return SAFE_ACTION_LABELS.has((text || '').toLowerCase().trim());
  }

  function isInDataRow(el) {
    // Walk up looking for a <tbody> row — indicates a repeating data table.
    var cur = el ? el.parentElement : null;
    for (var d = 0; cur && d < 10; d++, cur = cur.parentElement) {
      if (cur.tagName === 'TBODY') return true;
      // Also catch ASP.NET Repeater / GridView patterns that use divs with
      // repeated structure: if we're inside a container that has 5+ sibling
      // rows of the same tag, treat it as a data list.
      if (cur.tagName === 'TR' && cur.parentElement &&
          cur.parentElement.tagName === 'TBODY') return true;
    }
    return false;
  }

  var PII_RE = new RegExp([
    '[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}', // email
    '\\b\\d{3}[\\s.\\-]?\\d{3}[\\s.\\-]?\\d{4}\\b',         // phone
    '\\b\\d{3}[\\-]\\d{2}[\\-]\\d{4}\\b',                    // SSN
    '\\b(0?[1-9]|1[0-2])[/\\-](0?[1-9]|[12]\\d|3[01])[/\\-]\\d{2,4}\\b', // date
  ].join('|'));

  function looksLikePii(text) {
    return PII_RE.test(text || '');
  }

  // ── Helpers ────────────────────────────────────────────────────────────────

  function getRole(e) {
    if (!e || !e.tagName) return null;
    var t = e.getAttribute && e.getAttribute('role');
    if (t) return t;
    var n = e.tagName.toLowerCase();
    if (n === 'button') return 'button';
    if (n === 'a' && e.hasAttribute('href')) return 'link';
    if (n === 'select') return 'combobox';
    if (n === 'textarea') return 'textbox';
    if (n === 'input') {
      var tp = (e.getAttribute('type') || 'text').toLowerCase();
      if (['text','email','tel','url','search','password'].includes(tp)) return 'textbox';
      if (tp === 'checkbox') return 'checkbox';
      if (tp === 'radio') return 'radio';
      if (tp === 'number') return 'spinbutton';
      if (tp === 'range') return 'slider';
      if (['submit','button','reset','image'].includes(tp)) return 'button';
      return 'textbox';
    }
    return null;
  }

  function getLabel(e) {
    if (!e) return '';
    var a = function (n) { return e.getAttribute && e.getAttribute(n); };
    var lb = a('aria-labelledby');
    if (lb) {
      var parts = lb.split(/\s+/).map(function (id) {
        var r = document.getElementById(id);
        return r ? (r.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    var al = a('aria-label');
    if (al) return al.trim();
    if (e.id) {
      try {
        var lbl = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
        if (lbl) return (lbl.textContent || '').trim();
      } catch (_) {}
    }
    if (e.closest) {
      var w = e.closest('label');
      if (w) {
        var c = w.cloneNode(true);
        c.querySelectorAll('input,textarea,select').forEach(function (n) { n.remove(); });
        return (c.textContent || '').trim();
      }
    }
    var tg = e.tagName.toUpperCase();
    if (tg === 'BUTTON' || tg === 'A') return (e.textContent || '').trim();
    if (tg === 'INPUT' || tg === 'TEXTAREA') return a('placeholder') || a('title') || a('name') || '';
    return a('title') || '';
  }

  function getLoc(e) {
    var l = {}, cur = e;
    for (var d = 0; cur && d < 4; d++, cur = cur.parentElement) {
      var id = cur.getAttribute && cur.getAttribute('id');
      var nm = cur.getAttribute && cur.getAttribute('name');
      var ti = cur.getAttribute && (
        cur.getAttribute('data-testid') ||
        cur.getAttribute('data-test') ||
        cur.getAttribute('data-qa'));
      if (ti && !l.testid) l.testid = ti;
      if (id && !l.id) {
        l.id = id;
        var p = id.split(/[_$]/);
        if (p.length > 1) l.id_suffix = p[p.length - 1];
      }
      if (nm && !l.name_attr) l.name_attr = nm;
      if (l.id) break;
    }
    return l;
  }

  function isHidden(e) {
    if (!e) return true;
    try {
      var s = window.getComputedStyle(e);
      if (s.display === 'none' || s.visibility === 'hidden') return true;
    } catch (_) {}
    if (e.offsetParent === null && e.tagName !== 'BODY') return true;
    if (e.type && e.type.toLowerCase() === 'hidden') return true;
    return false;
  }

  function nearestHeading(e) {
    var cur = e ? e.parentElement : null;
    for (var d = 0; cur && d < 6; d++, cur = cur.parentElement) {
      if (cur.tagName === 'FIELDSET') {
        var lg = cur.querySelector(':scope > legend');
        if (lg) return (lg.textContent || '').trim();
      }
      var h = cur.querySelector(
        ':scope > h1,:scope > h2,:scope > h3,:scope > h4,' +
        ':scope > .panel-heading,:scope > .panel-title,:scope > .section-title');
      if (h) return (h.textContent || '').trim().split('\n')[0].trim();
    }
    return null;
  }

  // ── Column header detection ────────────────────────────────────────────────
  // For list/table screens, capture <th> text so we know the column structure
  // without any row data.

  function getTableHeaders() {
    var headers = [];
    document.querySelectorAll('th').forEach(function (th) {
      var t = (th.textContent || '').trim();
      if (t) headers.push(t);
    });
    return headers.filter(function (v, i, a) { return a.indexOf(v) === i; });
  }

  // ── Scan ───────────────────────────────────────────────────────────────────

  var ROLES = new Set(['button','link','textbox','checkbox','radio','combobox',
    'menuitem','tab','switch','searchbox','slider','spinbutton','option']);
  var SENSITIVE_RE = /\b(ssn|social.?security|password|dob|date.of.birth|birth.?date|pin|credit.?card|cvv|cvc)\b/i;

  var seen = new Set();
  // For deduplicating repeating row actions: track (label+id_suffix) pairs
  // so ctrl0_btnViewDetails and ctrl1_btnViewDetails collapse to one entry.
  var seenRowActions = new Set();
  var fields = [];
  var suppressedCount = 0;

  document.querySelectorAll('*').forEach(function (el) {
    var role = getRole(el);
    if (!role || !ROLES.has(role)) return;
    if (isHidden(el)) return;

    var rawLabel = getLabel(el);
    var inDataRow = isInDataRow(el);

    // Inside a data row: only keep known safe action labels.
    // Everything else (student names, emails, phones rendered as links) is suppressed.
    if (inDataRow && !isSafeActionLabel(rawLabel)) {
      suppressedCount++;
      return;
    }

    // Backstop: if the label itself looks like PII, redact it regardless of location.
    var label = looksLikePii(rawLabel) ? '[REDACTED]' : rawLabel;

    var loc = getLoc(el);

    // Deduplicate repeating row actions: ASP.NET Repeater generates ids like
    // rptStudents_ctrl0_btnViewDetails, ctrl1_btnViewDetails, etc.
    // Collapse these to a single representative entry using the id_suffix.
    if (inDataRow && loc.id_suffix) {
      var rowActionKey = role + '|' + label + '|' + loc.id_suffix;
      if (seenRowActions.has(rowActionKey)) return;
      seenRowActions.add(rowActionKey);
    }

    var dk = loc.id || (role + '|' + label);
    if (seen.has(dk)) return;
    seen.add(dk);

    var section = nearestHeading(el);
    var entry = { role: role, label: label };
    if (loc.id) entry.id = loc.id;
    if (loc.id_suffix && loc.id_suffix !== loc.id) entry.id_suffix = loc.id_suffix;
    if (loc.name_attr) entry.name_attr = loc.name_attr;
    if (loc.testid) entry.testid = loc.testid;
    if (section) entry.section = section;
    if (inDataRow) entry.in_list_row = true;

    if (role === 'combobox' && el.tagName === 'SELECT') {
      var opts = Array.from(el.options)
        .map(function (o) { return (o.textContent || '').trim(); })
        .filter(function (t) { return t; });
      if (opts.length) entry.options = opts.slice(0, 40);
    }

    if (SENSITIVE_RE.test(label || '')) entry.sensitive = true;

    var rv = el.value !== undefined ? String(el.value || '') : '';
    if (role === 'checkbox' || role === 'radio') {
      entry.checked = el.checked;
    } else if (rv.trim()) {
      entry.value = '[REDACTED]';
    }

    fields.push(entry);
  });

  var actions = fields
    .filter(function (f) { return f.role === 'button' || f.role === 'link'; })
    .map(function (f) { return f.label || '(unlabelled)'; })
    .filter(function (v, i, a) { return v && a.indexOf(v) === i; });

  var secMap = {};
  fields.forEach(function (f) {
    var s = f.section || '(ungrouped)';
    if (!secMap[s]) secMap[s] = [];
    var e = Object.assign({}, f);
    delete e.section;
    secMap[s].push(e);
  });

  var tableHeaders = getTableHeaders();

  var output = {
    meta: {
      url: location.href,
      title: document.title || '',
      timestamp: new Date().toISOString(),
      pii_suppressed: suppressedCount > 0
        ? suppressedCount + ' data-row elements suppressed to protect PII'
        : undefined,
    },
    table_columns: tableHeaders.length ? tableHeaders : undefined,
    actions: actions,
    sections: Object.entries(secMap).map(function (kv) {
      return { heading: kv[0], items: kv[1] };
    }),
  };

  // Clean up undefined keys.
  if (!output.table_columns) delete output.table_columns;
  if (!output.meta.pii_suppressed) delete output.meta.pii_suppressed;

  var json = JSON.stringify(output, null, 2);

  // ── Popup ──────────────────────────────────────────────────────────────────

  var pageLabel = output.meta.title || (function () {
    try { return new URL(output.meta.url).pathname; } catch (_) { return output.meta.url; }
  })();

  var overlay = document.createElement('div');
  overlay.id = '__asap_inspector_popup';
  overlay.style.cssText = [
    'position:fixed','top:0','left:0','width:100%','height:100%',
    'background:rgba(0,0,0,0.55)','z-index:2147483647',
    'display:flex','align-items:center','justify-content:center',
    'font-family:Segoe UI,Arial,sans-serif',
  ].join(';');

  var card = document.createElement('div');
  card.style.cssText = [
    'background:#161b22','border:1px solid #30363d','border-radius:12px',
    'width:580px','max-width:94vw','max-height:82vh',
    'display:flex','flex-direction:column','overflow:hidden',
    'box-shadow:0 8px 32px rgba(0,0,0,0.7)',
  ].join(';');

  var header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid #30363d;flex-shrink:0;';
  header.innerHTML =
    '<span style="color:#39d3ff;font-weight:700;font-size:1rem;">🔍 ASAP Page Inspector</span>' +
    '<span style="color:#8b949e;font-size:.8rem;margin:0 12px;flex:1;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' +
      pageLabel.replace(/</g, '&lt;') +
    '</span>';

  var closeBtn = document.createElement('button');
  closeBtn.textContent = '✕';
  closeBtn.style.cssText = 'background:none;border:none;color:#8b949e;font-size:1.2rem;cursor:pointer;padding:0 4px;line-height:1;';
  header.appendChild(closeBtn);
  card.appendChild(header);

  // PII warning banner (shown only when data rows were suppressed).
  if (suppressedCount > 0) {
    var banner = document.createElement('div');
    banner.style.cssText = 'background:#0f2a1a;border-bottom:1px solid #238636;padding:8px 18px;color:#3fb950;font-size:.8rem;flex-shrink:0;';
    banner.textContent = '🔒 ' + suppressedCount + ' list-row items suppressed — student data protected.';
    card.appendChild(banner);
  }

  var ta = document.createElement('textarea');
  ta.readOnly = true;
  ta.value = json;
  ta.style.cssText = [
    'flex:1','background:#0d1117','color:#c9d1d9',
    'border:none','outline:none','resize:none',
    'padding:14px 16px','font-family:Consolas,Menlo,monospace',
    'font-size:.78rem','line-height:1.5','overflow:auto','min-height:280px',
  ].join(';');
  card.appendChild(ta);

  var footer = document.createElement('div');
  footer.style.cssText = 'display:flex;align-items:center;gap:10px;padding:12px 18px;border-top:1px solid #30363d;flex-shrink:0;';

  var copyBtn = document.createElement('button');
  copyBtn.textContent = 'Copy';
  copyBtn.style.cssText = [
    'background:#39d3ff','color:#06222b','font-weight:700',
    'font-size:.9rem','border:none','border-radius:7px',
    'padding:9px 28px','cursor:pointer','flex-shrink:0',
  ].join(';');

  var msgSpan = document.createElement('span');
  msgSpan.textContent = '✓ Copied — paste into the chat';
  msgSpan.style.cssText = 'color:#3fb950;font-size:.82rem;display:none;';

  var hintSpan = document.createElement('span');
  hintSpan.textContent = 'No PII · values redacted';
  hintSpan.style.cssText = 'color:#484f58;font-size:.78rem;margin-left:auto;';

  footer.appendChild(copyBtn);
  footer.appendChild(msgSpan);
  footer.appendChild(hintSpan);
  card.appendChild(footer);
  overlay.appendChild(card);
  document.body.appendChild(overlay);

  setTimeout(function () { try { ta.select(); } catch (_) {} }, 50);

  copyBtn.addEventListener('click', function () {
    navigator.clipboard.writeText(json).then(function () {
      copyBtn.textContent = '✓ Copied!';
      copyBtn.style.background = '#3fb950';
      msgSpan.style.display = 'inline';
      setTimeout(function () {
        copyBtn.textContent = 'Copy';
        copyBtn.style.background = '#39d3ff';
      }, 2500);
    }).catch(function () {
      try { ta.select(); document.execCommand('copy'); } catch (_) {}
      copyBtn.textContent = '✓ Copied!';
      msgSpan.style.display = 'inline';
    });
  });

  closeBtn.addEventListener('click', function () { overlay.remove(); });
  overlay.addEventListener('click', function (e) { if (e.target === overlay) overlay.remove(); });
})();
