// ASAP Powertools — background service worker
// Orchestrates batch runs: navigates tabs, injects step executors,
// monitors downloads, and streams plain-English progress to the side panel.

// ── Side panel ────────────────────────────────────────────────────────────────

chrome.action.onClicked.addListener((tab) => {
  chrome.sidePanel.open({ windowId: tab.windowId });
});

// ── Batch state ───────────────────────────────────────────────────────────────

let state = {
  running: false,
  stopRequested: false,
  mainTabId: null,   // the ASAP student detail tab
  popupTabId: null,  // transcript popup tab (if open)
  activeTabId: null, // whichever tab we're currently acting on
  succeeded: 0,
  failed: 0,
  skipped: 0,
};

function resetState() {
  state = {
    running: false,
    stopRequested: false,
    mainTabId: null,
    popupTabId: null,
    activeTabId: null,
    succeeded: 0,
    failed: 0,
    skipped: 0,
  };
}

// ── Messaging ─────────────────────────────────────────────────────────────────

// Send a message to the side panel (fire-and-forget; panel may not be open).
function toPanel(msg) {
  chrome.runtime.sendMessage({ ...msg, _source: 'background' }).catch(() => {});
}

function log(text, tag = 'info') {
  toPanel({ type: 'log', text, tag });
}

// ── Message router ────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg._source === 'background') return; // ignore our own broadcasts

  if (msg.type === 'run_batch') {
    runBatch(msg.template, msg.studentIds).catch(err => {
      log('Unexpected error: ' + err.message, 'err');
      resetState();
      toPanel({ type: 'batch_done', succeeded: state.succeeded, failed: state.failed });
    });
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === 'stop') {
    state.stopRequested = true;
    log('Stop requested — finishing current student then halting.', 'warn');
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === 'get_templates') {
    loadTemplates().then(t => sendResponse({ templates: t })).catch(() => sendResponse({ templates: [] }));
    return true;
  }
});

// ── Template loader ───────────────────────────────────────────────────────────

async function loadTemplates() {
  // Templates are bundled in the extension under templates/*.json.
  // We maintain a manifest list because service workers can't list directories.
  const names = ['print_hsd_transcript'];
  const templates = [];
  for (const name of names) {
    try {
      const url = chrome.runtime.getURL(`templates/${name}.json`);
      const resp = await fetch(url);
      const data = await resp.json();
      const meta = data.meta || {};
      templates.push({
        id: name,
        name: meta.name || name,
        description: meta.description || '',
        variable: meta.variable || 'studentid',
        steps: data.steps || [],
      });
    } catch (e) {
      console.warn('Failed to load template:', name, e);
    }
  }
  return templates;
}

// ── Variable substitution ─────────────────────────────────────────────────────

function substituteVars(steps, vars) {
  return steps.map(step => {
    const s = { ...step };
    for (const [k, v] of Object.entries(vars)) {
      const ph = `{{${k}}}`;
      if (typeof s.url   === 'string') s.url   = s.url.replaceAll(ph, v);
      if (typeof s.value === 'string') s.value = s.value.replaceAll(ph, v);
      if (typeof s.label === 'string') s.label = s.label.replaceAll(ph, v);
    }
    return s;
  });
}

// ── Step → plain English ──────────────────────────────────────────────────────

function stepToEnglish(step) {
  const t = step.type || '';
  const name = step.name || '';
  const label = step.label || step.value || '';
  switch (t) {
    case 'navigate':     return `Opening ${new URL(step.url).hostname}…`;
    case 'click':        return name ? `Clicking "${name}"…` : 'Clicking…';
    case 'select_option':return `Selecting "${label}"…`;
    case 'select_kendo': return `Setting program to "${step.value}"…`;
    case 'check':        return `Checking "${name}"…`;
    case 'swap_dates':   return 'Checking diploma and graduation dates…';
    case 'fill':         return `Filling in "${name}"…`;
    case 'download':     return `Downloading ${step.filename || 'file'}…`;
    case 'close_page':   return 'Closing transcript window…';
    default:             return t;
  }
}

// ── Tab helpers ───────────────────────────────────────────────────────────────

function waitForTabLoad(tabId, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error(`Tab ${tabId} did not finish loading within ${timeoutMs}ms`));
    }, timeoutMs);

    function listener(id, changeInfo) {
      if (id === tabId && changeInfo.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        // Small extra delay for JS frameworks to settle (ASP.NET postbacks).
        setTimeout(resolve, 600);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function waitForNewTab(timeoutMs = 5000) {
  return new Promise(resolve => {
    const timer = setTimeout(() => {
      chrome.tabs.onCreated.removeListener(listener);
      resolve(null); // no popup opened
    }, timeoutMs);

    function listener(tab) {
      clearTimeout(timer);
      chrome.tabs.onCreated.removeListener(listener);
      resolve(tab.id);
    }
    chrome.tabs.onCreated.addListener(listener);
  });
}

function waitForDownload(timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.downloads.onCreated.removeListener(onCreated);
      reject(new Error('Download did not start within 30 seconds'));
    }, timeoutMs);

    function onCreated(item) {
      clearTimeout(timer);
      chrome.downloads.onCreated.removeListener(onCreated);
      // Wait for the download to finish.
      waitForDownloadComplete(item.id, 60000).then(resolve).catch(reject);
    }
    chrome.downloads.onCreated.addListener(onCreated);
  });
}

function waitForDownloadComplete(downloadId, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.downloads.onChanged.removeListener(onChange);
      reject(new Error('Download did not complete within 60 seconds'));
    }, timeoutMs);

    function onChange(delta) {
      if (delta.id !== downloadId) return;
      if (delta.state && delta.state.current === 'complete') {
        clearTimeout(timer);
        chrome.downloads.onChanged.removeListener(onChange);
        chrome.downloads.search({ id: downloadId }, items => {
          resolve(items[0] || { filename: 'unknown' });
        });
      } else if (delta.state && delta.state.current === 'interrupted') {
        clearTimeout(timer);
        chrome.downloads.onChanged.removeListener(onChange);
        reject(new Error('Download was interrupted'));
      }
    }
    chrome.downloads.onChanged.addListener(onChange);
  });
}

// ── Step executor (injected into page) ────────────────────────────────────────
// This function is serialised and sent to the tab via chrome.scripting.
// It must be entirely self-contained — no closures over outer variables.

function executeStepInPage(step) {
  function findEl(locators, role, name, nth) {
    if (!locators) locators = {};
    // 1. Exact ID
    if (locators.id) {
      const el = document.getElementById(locators.id);
      if (el) return el;
    }
    // 2. ID suffix
    if (locators.id_suffix) {
      const el = document.querySelector(`[id$="${locators.id_suffix}"]`);
      if (el) return el;
    }
    // 3. name attribute suffix
    if (locators.name_suffix || locators.name_attr) {
      const suffix = locators.name_suffix || locators.name_attr.split('$').pop();
      const el = document.querySelector(`[name$="${suffix}"]`);
      if (el) return el;
    }
    // 4. Role + name + nth scan
    if (role) {
      const candidates = [];
      const all = document.querySelectorAll('*');
      for (const el of all) {
        const r = el.getAttribute('role') ||
          (el.tagName === 'BUTTON' ? 'button' :
           el.tagName === 'A' && el.href ? 'link' :
           el.tagName === 'SELECT' ? 'combobox' :
           el.tagName === 'INPUT' && el.type === 'checkbox' ? 'checkbox' :
           el.tagName === 'INPUT' ? 'textbox' : null);
        if (r !== role) continue;
        const elName = (el.getAttribute('aria-label') ||
                        el.getAttribute('title') ||
                        el.textContent || '').trim();
        if (name && elName.toLowerCase() !== name.toLowerCase()) continue;
        candidates.push(el);
      }
      if (candidates[nth || 0]) return candidates[nth || 0];
    }
    return null;
  }

  function dispatch(el, events) {
    for (const ev of events) {
      el.dispatchEvent(new Event(ev, { bubbles: true }));
    }
  }

  const t = step.type;
  const loc = step.locators || {};

  try {
    // ── navigate / download / close_page handled in background ──
    if (['navigate', 'download', 'close_page'].includes(t)) {
      return { ok: true, waitForNav: false };
    }

    // ── click ────────────────────────────────────────────────────
    if (t === 'click') {
      const el = findEl(loc, step.role, step.name, step.nth);
      if (!el) return { ok: false, err: `Element not found: ${step.name || step.role}` };
      el.click();
      return { ok: true, waitForNav: true };
    }

    // ── select_option ────────────────────────────────────────────
    if (t === 'select_option') {
      const el = findEl(loc, 'combobox', step.name, step.nth);
      if (!el) return { ok: false, err: `Select not found: ${step.name}` };
      // Try by value first, then by label text.
      let found = false;
      for (const opt of el.options) {
        if (opt.value === step.value || opt.text === step.label) {
          el.value = opt.value;
          found = true;
          break;
        }
      }
      if (!found) return { ok: false, err: `Option not found: ${step.label || step.value}` };
      dispatch(el, ['change', 'input']);
      return { ok: true, waitForNav: true };
    }

    // ── select_kendo ─────────────────────────────────────────────
    if (t === 'select_kendo') {
      const el = findEl(loc, null, null, 0);
      if (!el) return { ok: false, err: 'Kendo element not found' };
      const jq = window.jQuery || window.$;
      if (!jq) return { ok: false, err: 'jQuery not available on this page' };
      const widget = jq(el).data('kendoDropDownList') ||
                     jq(el).data('kendoComboBox');
      if (!widget) return { ok: false, err: 'Kendo widget not found on element' };
      widget.value(step.value);
      widget.trigger('change');
      return { ok: true, waitForNav: false };
    }

    // ── check (checkbox) ─────────────────────────────────────────
    if (t === 'check') {
      const el = findEl(loc, 'checkbox', step.name, step.nth);
      if (!el) return { ok: false, err: `Checkbox not found: ${step.name}` };
      if (!el.checked) {
        el.click();
        dispatch(el, ['change']);
      }
      return { ok: true, waitForNav: false };
    }

    // ── fill ─────────────────────────────────────────────────────
    if (t === 'fill') {
      const el = findEl(loc, 'textbox', step.name, step.nth);
      if (!el) return { ok: false, err: `Field not found: ${step.name}` };
      el.focus();
      el.value = step.value || '';
      dispatch(el, ['input', 'change', 'blur']);
      return { ok: true, waitForNav: false };
    }

    // ── swap_dates ───────────────────────────────────────────────
    if (t === 'swap_dates') {
      const diplEl = document.querySelector(`[id$="${step.diploma_suffix}"]`);
      const gradEl = document.querySelector(`[id$="${step.graduation_suffix}"]`);
      if (!diplEl || !gradEl) {
        return { ok: true, swapped: false, note: 'Date fields not found — skipping' };
      }
      const d1 = new Date(diplEl.value);
      const d2 = new Date(gradEl.value);
      if (isNaN(d1) || isNaN(d2) || !diplEl.value || !gradEl.value) {
        return { ok: true, swapped: false, note: 'One or both dates are empty' };
      }
      // Swap if diploma date is before graduation date (they were entered backwards).
      if (d1 < d2) {
        const tmp = diplEl.value;
        diplEl.value = gradEl.value;
        gradEl.value = tmp;
        dispatch(diplEl, ['change', 'input', 'blur']);
        dispatch(gradEl, ['change', 'input', 'blur']);
        return { ok: true, swapped: true };
      }
      return { ok: true, swapped: false, note: 'Dates are in correct order' };
    }

    return { ok: false, err: `Unknown step type: ${t}` };

  } catch (e) {
    return { ok: false, err: e.message };
  }
}

// ── Run one step ──────────────────────────────────────────────────────────────

async function runStep(step) {
  const tabId = state.activeTabId;
  const t = step.type;

  if (t === 'navigate') {
    await chrome.tabs.update(tabId, { url: step.url });
    await waitForTabLoad(tabId);
    return { ok: true };
  }

  if (t === 'close_page') {
    if (state.popupTabId) {
      try { await chrome.tabs.remove(state.popupTabId); } catch (_) {}
      state.popupTabId = null;
    }
    state.activeTabId = state.mainTabId;
    return { ok: true };
  }

  if (t === 'download') {
    // The previous 'click' on btnPrint already triggered the download.
    // waitForDownload was set up before that click (see runStep for 'click').
    // Here we just resolve — the result was already stored.
    return { ok: true };
  }

  // For click steps, watch for a potential popup tab opening.
  let popupPromise = null;
  if (t === 'click') {
    popupPromise = waitForNewTab(3000);
  }

  // For the step just before a download (btnPrint), start watching for downloads.
  let downloadPromise = null;
  // We'll start a download watcher if this click targets btnPrint.
  const locId = (step.locators || {}).id || '';
  const locSuffix = (step.locators || {}).id_suffix || '';
  const isBtnPrint = locId === 'btnPrint' || locSuffix === 'btnPrint';
  if (t === 'click' && isBtnPrint) {
    downloadPromise = waitForDownload(45000);
  }

  // Inject and execute the step.
  let result;
  try {
    const [{ result: r }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: executeStepInPage,
      args: [step],
    });
    result = r;
  } catch (e) {
    return { ok: false, err: e.message };
  }

  if (!result.ok) return result;

  // If a postback/navigation was triggered, wait for the page to settle.
  if (result.waitForNav) {
    try {
      await waitForTabLoad(tabId, 10000);
    } catch (_) {
      // Tab may not have navigated at all — that's fine.
    }
  }

  // Check if a popup tab opened (e.g. transcript report popup).
  if (popupPromise) {
    const newTabId = await popupPromise;
    if (newTabId) {
      state.popupTabId = newTabId;
      state.activeTabId = newTabId;
      await waitForTabLoad(newTabId, 15000);
    }
  }

  // If we were waiting for a download, record the result.
  if (downloadPromise) {
    try {
      const dlItem = await downloadPromise;
      const filename = dlItem.filename ? dlItem.filename.split(/[/\\]/).pop() : 'file';
      result.downloadedFile = filename;
    } catch (e) {
      return { ok: false, err: 'Download failed: ' + e.message };
    }
  }

  return result;
}

// ── Run one student ───────────────────────────────────────────────────────────

async function runStudent(template, studentId, vars, idx, total) {
  log(`Starting student ${idx + 1} of ${total} (ID: ${studentId})…`, 'accent');

  const steps = substituteVars(template, vars);

  for (let i = 0; i < steps.length; i++) {
    if (state.stopRequested) return 'stopped';
    const step = steps[i];

    log(stepToEnglish(step));

    try {
      const result = await runStep(step);
      if (!result.ok) {
        log(`Could not complete: ${result.err}`, 'err');
        return 'failed';
      }
      if (step.type === 'swap_dates') {
        if (result.swapped) {
          log('Dates were in the wrong order — fixed automatically.', 'warn');
        } else {
          log(result.note || 'Dates are already correct.', 'muted');
        }
      }
      if (result.downloadedFile) {
        log(`✓ Downloaded: ${result.downloadedFile}`, 'ok');
      }
    } catch (e) {
      log(`Error on step ${i + 1}: ${e.message}`, 'err');
      return 'failed';
    }
  }

  return 'succeeded';
}

// ── Batch runner ──────────────────────────────────────────────────────────────

async function runBatch(template, studentIds) {
  if (state.running) {
    log('A run is already in progress.', 'warn');
    return;
  }
  resetState();
  state.running = true;

  // Find or create an ASAP tab to use as our workspace.
  const tabs = await chrome.tabs.query({ url: '*://*.asapconnected.com/*' });
  if (tabs.length > 0) {
    state.mainTabId = tabs[0].id;
    state.activeTabId = tabs[0].id;
    await chrome.tabs.update(state.mainTabId, { active: true });
  } else {
    // Open a new tab.
    const tab = await chrome.tabs.create({ url: 'https://admin.asapconnected.com/home' });
    state.mainTabId = tab.id;
    state.activeTabId = tab.id;
    await waitForTabLoad(tab.id);
  }

  const total = studentIds.length;
  log(`Batch started: ${total} student${total !== 1 ? 's' : ''}.`, 'accent');

  for (let i = 0; i < studentIds.length; i++) {
    if (state.stopRequested) {
      log('Batch stopped early.', 'warn');
      break;
    }

    const sid = studentIds[i].trim();
    if (!sid) continue;

    const vars = { studentid: sid };
    state.activeTabId = state.mainTabId; // reset to main tab for each student
    state.popupTabId = null;

    const outcome = await runStudent(template, sid, vars, i, total);

    if (outcome === 'succeeded') {
      state.succeeded++;
      log(`✓ Student ${sid} — done.`, 'ok');
    } else if (outcome === 'stopped') {
      log(`Stopped at student ${sid}.`, 'warn');
      break;
    } else {
      state.failed++;
      log(`Student ${sid} — could not complete.`, 'err');
    }

    toPanel({
      type: 'progress',
      current: i + 1,
      total,
      studentId: sid,
      status: outcome,
    });
  }

  const stopped = state.stopRequested;
  state.running = false;

  const summary = `${stopped ? 'Stopped. ' : ''}${state.succeeded} completed, ${state.failed} needed attention.`;
  log(summary, state.failed > 0 ? 'warn' : 'ok');
  toPanel({
    type: 'batch_done',
    succeeded: state.succeeded,
    failed: state.failed,
    stopped,
  });
}
