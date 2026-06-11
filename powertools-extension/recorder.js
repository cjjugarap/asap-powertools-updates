// ASAP Powertools — step recorder
// Injected into ASAP tabs during recording mode.
// Captures user interactions as structured steps and sends them to the
// background service worker. Field VALUES are never captured (PII protection).

(function () {
  if (window.__asap_recorder_active) return;
  window.__asap_recorder_active = true;

  // ── PII protection (mirrors inspector.js) ────────────────────────────────

  var SAFE_ACTION_LABELS = new Set([
    'quick enroll','enroll','email','view schedule','schedule',
    'view details','details','view account','account','edit',
    'view','delete','remove','add','save','cancel','close',
    'print','download','export','search','find','submit','ok',
    'yes','no','back','next','continue','select','deselect',
    'check in','check out','unenroll','transfer','copy',
    'view transcript report','view transcript','transcript',
    'credits','transcripts','schedule','attendance','notes',
    'return to asap classic',
  ]);

  function isSafeActionLabel(text) {
    return SAFE_ACTION_LABELS.has((text || '').toLowerCase().trim());
  }

  function isInDataRow(el) {
    var cur = el ? el.parentElement : null;
    for (var d = 0; cur && d < 10; d++, cur = cur.parentElement) {
      if (cur.tagName === 'TBODY') return true;
      if (cur.tagName === 'TR' && cur.parentElement &&
          cur.parentElement.tagName === 'TBODY') return true;
    }
    return false;
  }

  var PII_RE = new RegExp([
    '[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}',
    '\\b\\d{3}[\\s.\\-]?\\d{3}[\\s.\\-]?\\d{4}\\b',
    '\\b\\d{3}[\\-]\\d{2}[\\-]\\d{4}\\b',
    '\\b(0?[1-9]|1[0-2])[/\\-](0?[1-9]|[12]\\d|3[01])[/\\-]\\d{2,4}\\b',
  ].join('|'));

  function looksLikePii(text) {
    return PII_RE.test(text || '');
  }

  function isSecretField(el) {
    var type = (el.getAttribute('type') || '').toLowerCase();
    var name = (el.name || el.id || '').toLowerCase();
    var autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
    return type === 'password' ||
           autocomplete === 'current-password' ||
           autocomplete === 'new-password' ||
           /passw|secret|otp|pin\b/.test(name);
  }

  // ── Element introspection ─────────────────────────────────────────────────

  function getRole(el) {
    var r = el.getAttribute && el.getAttribute('role');
    if (r) return r;
    var tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.href) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      var tp = (el.getAttribute('type') || 'text').toLowerCase();
      if (tp === 'checkbox') return 'checkbox';
      if (tp === 'radio') return 'radio';
      if (['submit','button','reset','image'].includes(tp)) return 'button';
      return 'textbox';
    }
    return null;
  }

  function getName(el) {
    // Prefer aria-label, then title, then associated label, then text content.
    var v = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    if (v.trim()) return v.trim();

    if (el.id) {
      var lbl = document.querySelector('label[for="' + el.id + '"]');
      if (lbl) return lbl.textContent.trim();
    }

    var text = (el.textContent || el.innerText || el.value || '').trim();
    // For links/buttons, use text content but strip excess whitespace.
    if (text && text.length < 80) return text.replace(/\s+/g, ' ').trim();

    return '';
  }

  function getLocators(el) {
    var loc = {};
    if (el.id) {
      loc.id = el.id;
      var parts = el.id.split('_');
      if (parts.length > 1) loc.id_suffix = parts[parts.length - 1];
    }
    if (el.name) {
      loc.name_attr = el.name;
      var dparts = el.name.split('$');
      if (dparts.length > 1) loc.name_suffix = dparts[dparts.length - 1];
    }
    return loc;
  }

  function getNth(el, role, name) {
    // Count how many matching elements appear before this one.
    var all = Array.from(document.querySelectorAll('*'));
    var idx = 0;
    for (var i = 0; i < all.length; i++) {
      if (all[i] === el) return idx;
      var r = getRole(all[i]);
      var n = getName(all[i]);
      if (r === role && n === name) idx++;
    }
    return 0;
  }

  // ── Step emission ─────────────────────────────────────────────────────────

  var pendingSteps = [];
  var flushTimer = null;

  function sendStep(step) {
    pendingSteps.push(step);
    if (!flushTimer) {
      flushTimer = setTimeout(flush, 100);
    }
  }

  function flush() {
    flushTimer = null;
    if (!pendingSteps.length) return;
    var toSend = pendingSteps.splice(0);
    for (var i = 0; i < toSend.length; i++) {
      try {
        chrome.runtime.sendMessage({ type: 'recorded_step', step: toSend[i] });
      } catch (e) {
        // Extension context may be invalidated after navigation — ignore.
      }
    }
  }

  // ── Click listener ────────────────────────────────────────────────────────

  document.addEventListener('click', function (e) {
    // Walk up to find the nearest actionable element.
    var el = e.target;
    while (el && el !== document.body) {
      var role = getRole(el);
      if (role && ['button','link','tab','menuitem','option'].includes(role)) break;
      el = el.parentElement;
    }
    if (!el || el === document.body) return;

    var role = getRole(el);
    if (!role) return;

    var name = getName(el);

    // PII guard: skip data-row items that aren't safe action labels.
    if (isInDataRow(el) && !isSafeActionLabel(name)) return;
    if (looksLikePii(name)) return;

    var locators = getLocators(el);
    var nth = getNth(el, role, name);

    // Record page URL for postback-suppression on replay.
    sendStep({
      type: 'click',
      role: role,
      name: name,
      nth: nth,
      locators: locators,
      page_url: location.href,
    });
  }, true);

  // ── Change listener (select, checkbox, text field) ────────────────────────

  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || !el.tagName) return;

    var tag = el.tagName.toLowerCase();

    if (tag === 'select') {
      var opt = el.options[el.selectedIndex];
      sendStep({
        type: 'select_option',
        role: 'combobox',
        name: getName(el),
        nth: getNth(el, 'combobox', getName(el)),
        locators: getLocators(el),
        value: opt ? opt.value : '',
        label: opt ? opt.text.trim() : '',
        page_url: location.href,
      });
      return;
    }

    if (el.type === 'checkbox' || el.type === 'radio') {
      sendStep({
        type: 'check',
        role: el.type === 'radio' ? 'radio' : 'checkbox',
        name: getName(el),
        nth: getNth(el, 'checkbox', getName(el)),
        locators: getLocators(el),
        checked: el.checked,
        page_url: location.href,
      });
      return;
    }

    // Text field — never capture the value (PII protection).
    if (!isSecretField(el)) {
      sendStep({
        type: 'fill',
        role: 'textbox',
        name: getName(el),
        nth: getNth(el, 'textbox', getName(el)),
        locators: getLocators(el),
        value: '[REDACTED]',
        page_url: location.href,
      });
    }
  }, true);

  // ── Kendo widget change detection ─────────────────────────────────────────
  // Kendo dropdowns don't fire native 'change' — bind to their 'change' event.

  function hookKendoWidgets() {
    if (!window.jQuery) return;
    var jq = window.jQuery;
    jq('[data-role="dropdownlist"], [data-role="combobox"]').each(function () {
      var el = this;
      var widget = jq(el).data('kendoDropDownList') || jq(el).data('kendoComboBox');
      if (!widget || el.__asap_kendo_hooked) return;
      el.__asap_kendo_hooked = true;
      widget.bind('change', function () {
        sendStep({
          type: 'select_kendo',
          locators: getLocators(el),
          value: widget.value(),
          label: widget.text(),
          page_url: location.href,
        });
      });
    });
  }

  // Try hooking Kendo widgets once the page is fully settled.
  setTimeout(hookKendoWidgets, 1500);

  // ── Navigation capture ────────────────────────────────────────────────────
  // Emit the current page URL as the starting navigate step.
  sendStep({ type: 'navigate', url: location.href });

  // Watch for SPA-style route changes (URL changes without full reload).
  var lastUrl = location.href;
  var urlPoll = setInterval(function () {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      sendStep({ type: 'navigate', url: location.href });
    }
  }, 500);

  // Clean up on unload.
  window.addEventListener('beforeunload', function () {
    clearInterval(urlPoll);
    flush();
    window.__asap_recorder_active = false;
  });

  console.log('[ASAP Powertools] Recorder active on', location.hostname);
})();
