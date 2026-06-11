// ASAP Powertools — background service worker
// Orchestrates batch runs, recording mode, and Claude cleanup.

// ── Side panel ────────────────────────────────────────────────────────────────

chrome.action.onClicked.addListener((tab) => {
  chrome.sidePanel.open({ windowId: tab.windowId });
});

// ── Recording state ───────────────────────────────────────────────────────────

let rec = {
  active: false,
  tabId: null,
  steps: [],
};

function startRecording(tabId) {
  rec = { active: true, tabId: tabId, steps: [] };
  injectRecorder(tabId);
  // Re-inject whenever the tab navigates to a new page.
  chrome.tabs.onUpdated.addListener(onTabUpdatedForRecording);
}

function stopRecording() {
  rec.active = false;
  chrome.tabs.onUpdated.removeListener(onTabUpdatedForRecording);
  return rec.steps;
}

function onTabUpdatedForRecording(tabId, changeInfo) {
  if (tabId === rec.tabId && changeInfo.status === 'complete') {
    injectRecorder(tabId);
  }
}

async function injectRecorder(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['recorder.js'],
    });
  } catch (e) {
    console.warn('Recorder injection failed:', e.message);
  }
}

// ── Claude cleanup ────────────────────────────────────────────────────────────

async function cleanupWithClaude(rawSteps, processName, apiKey) {
  const systemPrompt = `You are cleaning up a browser automation recording for ASAP Connected (a student management system built on ASP.NET WebForms with Telerik/Kendo UI).

The recording was captured automatically and may contain noise. Return a clean, reliable automation template as a JSON array of steps.

Rules:
1. Remove duplicate consecutive navigate steps to the same URL.
2. Remove navigate steps that are postback reloads (same URL as the previous navigate, or same as the page_url of the previous action step).
3. For locators, prefer id_suffix over full id (more stable across ASP.NET viewstate). Drop locators that are empty.
4. All fill step values are already [REDACTED] — keep them as-is.
5. If you see fill or click steps touching fields whose id_suffix matches "txtDiplomaDate" or "txtGraduationDate", replace them with a single step: {"type":"swap_dates","diploma_suffix":"txtDiplomaDate","graduation_suffix":"txtGraduationDate"}
6. Remove any steps where name or locators are completely empty and the step can't be identified.
7. Return ONLY a valid JSON array — no explanation, no markdown, no code fences.

Step schema (only include fields that are present):
{"type":"navigate"|"click"|"select_option"|"select_kendo"|"check"|"fill"|"swap_dates"|"download"|"close_page",
 "role":"button"|"link"|"combobox"|"checkbox"|"textbox",
 "name":"visible label",
 "nth":0,
 "locators":{"id":"...","id_suffix":"...","name_attr":"...","name_suffix":"..."},
 "value":"option value or [REDACTED]",
 "label":"visible option text"}`;

  const userPrompt = `Process name: "${processName}"

Raw recorded steps:
${JSON.stringify(rawSteps, null, 2)}`;

  const resp = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'anthropic-dangerous-direct-browser-access': 'true',
    },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 4096,
      system: systemPrompt,
      messages: [{ role: 'user', content: userPrompt }],
    }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Claude API error ${resp.status}: ${err}`);
  }

  const data = await resp.json();
  const text = data.content[0].text.trim();

  // Strip any accidental markdown fences.
  const json = text.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '');
  return JSON.parse(json);
}

// ── Process storage ───────────────────────────────────────────────────────────

async function saveProcess(proc) {
  const { processes = [] } = await chrome.storage.local.get('processes');
  // Replace if name already exists, otherwise append.
  const idx = processes.findIndex(p => p.id === proc.id);
  if (idx >= 0) processes[idx] = proc;
  else processes.push(proc);
  await chrome.storage.local.set({ processes });
}

async function deleteProcess(id) {
  const { processes = [] } = await chrome.storage.local.get('processes');
  await chrome.storage.local.set({ processes: processes.filter(p => p.id !== id) });
}

async function getSavedProcesses() {
  const { processes = [] } = await chrome.storage.local.get('processes');
  return processes;
}

// ── Batch state ───────────────────────────────────────────────────────────────

let state = {
  running: false,
  stopRequested: false,
  mainTabId: null,
  popupTabId: null,
  activeTabId: null,
  succeeded: 0,
  failed: 0,
  skipped: 0,
  pendingDownloadName: null,
  pendingDownloadUrl: null,
  lastSuggestedFilename: null,
  retryDownloadName: null,
  primaryDownloadId: null,      // ID of the page-triggered download being canceled
  currentStudentId: null,
  currentStudentName: null,
  lastDownloadId: null,
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
    pendingDownloadName: null,
    pendingDownloadUrl: null,
    lastSuggestedFilename: null,
    retryDownloadName: null,
    primaryDownloadId: null,
    currentStudentId: null,
    currentStudentName: null,
    lastDownloadId: null,
  };
}

// ── Download folder preference ────────────────────────────────────────────────
// Stored as a subfolder path relative to the user's Downloads directory.

const DEFAULT_DOWNLOAD_SUBFOLDER = 'ASAP Transcripts';

// Cached in memory so onDeterminingFilename can call suggest() synchronously.
let _cachedSubfolder = DEFAULT_DOWNLOAD_SUBFOLDER;
chrome.storage.local.get('downloadSubfolder', ({ downloadSubfolder }) => {
  if (downloadSubfolder) _cachedSubfolder = downloadSubfolder.replace(/\\/g, '/').replace(/\/$/, '');
});

async function getDownloadSubfolder() {
  return _cachedSubfolder;
}

// ── Download filename interception ────────────────────────────────────────────
// onDeterminingFilename fires before Chrome writes the file or shows any dialog.
// Calling suggest() redirects the file to our subfolder with our custom name.

// Chrome's download filename rules (cross-platform safe):
//   - forward slashes only as separators
//   - no leading slash, no ".." components
//   - no Windows-illegal chars: < > : " \ | ? *  (/ is a separator, not a char)
function safeDownloadPath(subfolder, filename) {
  const cleanSeg = s => s
    .replace(/[<>:"|?*\x00-\x1f\\]+/g, '_')  // illegal chars
    .replace(/\.{2,}/g, '.')                   // no ".."
    .replace(/^[./\s]+|[./\s]+$/g, '')         // no leading/trailing dots or slashes
    .slice(0, 120) || 'download';

  const parts = subfolder.split('/').map(cleanSeg).filter(Boolean);
  const file  = cleanSeg(filename.replace(/\//g, '_'));
  return [...parts, file].join('/');
}

// ── Download interception ─────────────────────────────────────────────────────
// onCreated fires BEFORE onDeterminingFilename (and thus before Chrome shows
// any dialog). We cancel the page-triggered download here — at this point
// Chrome hasn't reached the "Ask where to save" check yet — then re-initiate
// it ourselves with saveAs:false via the USER_CANCELED retry path.

chrome.downloads.onCreated.addListener((item) => {
  if (!state.pendingDownloadName) return;

  const name = state.pendingDownloadName;
  state.pendingDownloadName = null;
  const url = item.url || null;

  let filename = name;
  try { filename = safeDownloadPath(_cachedSubfolder, name); } catch (_) {}

  state.pendingDownloadUrl = url;
  state.lastSuggestedFilename = filename;
  state.retryDownloadName = filename; // consumed by onDeterminingFilename for retry
  state.primaryDownloadId = item.id;

  // Cancel before onDeterminingFilename runs → Chrome never shows any dialog.
  chrome.downloads.cancel(item.id);
});

// onDeterminingFilename: for the primary download (being canceled above),
// return true (async mode) so Chrome keeps waiting and never shows a dialog.
// For the silent retry, call suggest() with the correct filename.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  if (item.id === state.primaryDownloadId) {
    // Primary is already canceled. Stay in async mode so Chrome doesn't show
    // a dialog while waiting — the cancel will abort this state.
    return true;
  }
  if (state.retryDownloadName) {
    const filename = state.retryDownloadName;
    state.retryDownloadName = null;
    suggest({ filename, conflictAction: 'uniquify' });
  }
});

// Reveal the preferred download folder in the OS file manager.
// If a transcript was downloaded this session, reveal that file.
// Otherwise write a tiny marker file to the subfolder, wait for it to
// complete, reveal it, then erase it — so the folder is always correct.
async function openDownloadFolder() {
  if (state.lastDownloadId != null) {
    chrome.downloads.show(state.lastDownloadId);
    return;
  }
  // Drop a marker file in the preferred subfolder so we can reveal that folder.
  // Must use safeDownloadPath — leading dots and illegal chars cause "Invalid filename".
  const subfolder = await getDownloadSubfolder();
  const markerPath = safeDownloadPath(subfolder, 'asap-open-folder.tmp');
  chrome.downloads.download({
    url: 'data:text/plain,',
    filename: markerPath,
    saveAs: false,
    conflictAction: 'overwrite',
  }, id => {
    if (id == null) { chrome.downloads.showDefaultFolder(); return; }
    function onChange(delta) {
      if (delta.id !== id) return;
      const st = delta.state?.current;
      if (st === 'complete') {
        chrome.downloads.onChanged.removeListener(onChange);
        chrome.downloads.show(id);
        setTimeout(() => {
          chrome.downloads.removeFile(id, () => chrome.downloads.erase({ id }));
        }, 1500);
      } else if (st === 'interrupted') {
        chrome.downloads.onChanged.removeListener(onChange);
        chrome.downloads.showDefaultFolder();
      }
    }
    chrome.downloads.onChanged.addListener(onChange);
  });
}

// ── Debug log ─────────────────────────────────────────────────────────────────

let debugLog = [];
let _debugSensitive = []; // runtime list of values to scrub before export

function debugClear() {
  debugLog = [];
  _debugSensitive = [];
}

function debugRedact(text) {
  if (typeof text !== 'string') text = JSON.stringify(text);
  // Scrub known sensitive runtime values (student IDs, emails).
  for (const val of _debugSensitive) {
    if (val) text = text.replaceAll(val, '[REDACTED]');
  }
  // Scrub URL query params that carry IDs or search terms.
  text = text.replace(/([?&](?:Id|s)=)[^&\s"']*/gi, '$1[REDACTED]');
  // Scrub any remaining email-shaped or digit-run patterns in URLs.
  text = text.replace(/[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/g, '[EMAIL]');
  return text;
}

function debugPush(entry) {
  debugLog.push({ t: Date.now(), ...entry });
}

function getDebugLog() {
  const redacted = debugLog.map(e => {
    const out = { ...e };
    if (out.err)  out.err  = debugRedact(out.err);
    if (out.url)  out.url  = debugRedact(out.url);
    if (out.note) out.note = debugRedact(out.note);
    return out;
  });
  return {
    version: chrome.runtime.getManifest().version,
    exported: new Date().toISOString(),
    entries: redacted,
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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg._source === 'background') return;

  // ── Batch run ──
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

  // ── Templates (bundled + saved) ──
  if (msg.type === 'get_templates') {
    Promise.all([loadTemplates(), getSavedProcesses()]).then(([bundled, saved]) => {
      sendResponse({ templates: [...bundled, ...saved] });
    }).catch(() => sendResponse({ templates: [] }));
    return true;
  }

  // ── Recording ──
  if (msg.type === 'start_recording') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        startRecording(tabs[0].id);
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false, err: 'No active tab found' });
      }
    });
    return true;
  }

  if (msg.type === 'stop_recording') {
    const steps = stopRecording();
    sendResponse({ ok: true, steps });
    return true;
  }

  if (msg.type === 'recorded_step') {
    // From recorder.js content script — forward to panel.
    if (rec.active) {
      rec.steps.push(msg.step);
      toPanel({ type: 'recorded_step', step: msg.step });
    }
    return false;
  }

  // ── Claude cleanup ──
  if (msg.type === 'cleanup_with_claude') {
    chrome.storage.local.get('apiKey', ({ apiKey }) => {
      if (!apiKey) {
        sendResponse({ ok: false, err: 'No API key set. Open Settings to add your Anthropic API key.' });
        return;
      }
      cleanupWithClaude(msg.steps, msg.name, apiKey)
        .then(cleanSteps => sendResponse({ ok: true, steps: cleanSteps }))
        .catch(err => sendResponse({ ok: false, err: err.message }));
    });
    return true;
  }

  // ── Process save / delete ──
  if (msg.type === 'save_process') {
    saveProcess(msg.process)
      .then(() => sendResponse({ ok: true }))
      .catch(err => sendResponse({ ok: false, err: err.message }));
    return true;
  }

  if (msg.type === 'delete_process') {
    deleteProcess(msg.id)
      .then(() => sendResponse({ ok: true }))
      .catch(err => sendResponse({ ok: false, err: err.message }));
    return true;
  }

  // ── Debug log ──
  if (msg.type === 'get_debug_log') {
    sendResponse({ log: getDebugLog() });
    return true;
  }

  // ── Settings ──
  if (msg.type === 'save_settings') {
    chrome.storage.local.set({ apiKey: msg.apiKey })
      .then(() => sendResponse({ ok: true }))
      .catch(err => sendResponse({ ok: false, err: err.message }));
    return true;
  }

  if (msg.type === 'get_settings') {
    chrome.storage.local.get(['apiKey', 'downloadSubfolder'], data => {
      sendResponse({
        apiKey: data.apiKey || '',
        downloadSubfolder: data.downloadSubfolder || DEFAULT_DOWNLOAD_SUBFOLDER,
      });
    });
    return true;
  }

  if (msg.type === 'save_download_folder') {
    const folder = (msg.folder || DEFAULT_DOWNLOAD_SUBFOLDER).replace(/\\/g, '/').replace(/\/$/, '');
    _cachedSubfolder = folder;
    chrome.storage.local.set({ downloadSubfolder: folder })
      .then(() => sendResponse({ ok: true }))
      .catch(err => sendResponse({ ok: false, err: err.message }));
    return true;
  }

  if (msg.type === 'open_download_folder') {
    openDownloadFolder();
    sendResponse({ ok: true });
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
        setTimeout(resolve, 200);
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

function waitForDownload(timeoutMs = 45000) {
  let onCreated;
  let timer;
  const promise = new Promise((resolve, reject) => {
    timer = setTimeout(() => {
      chrome.downloads.onCreated.removeListener(onCreated);
      reject(new Error('Download did not start within 45 seconds'));
    }, timeoutMs);

    onCreated = function(item) {
      clearTimeout(timer);
      chrome.downloads.onCreated.removeListener(onCreated);
      state.lastDownloadId = item.id;
      waitForDownloadComplete(item.id, 120000).then(resolve).catch(reject);
    };
    chrome.downloads.onCreated.addListener(onCreated);
  });
  // Allow caller to cancel the listener (e.g. when pdfBase64 path is taken).
  promise.abort = () => {
    clearTimeout(timer);
    if (onCreated) chrome.downloads.onCreated.removeListener(onCreated);
  };
  return promise;
}

function waitForDownloadComplete(downloadId, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.downloads.onChanged.removeListener(onChange);
      reject(new Error('Download did not complete within 2 minutes'));
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
        const reason = delta.error ? delta.error.current : 'unknown';
        reject(new Error(`Download was interrupted (${reason})`));
      }
    }
    chrome.downloads.onChanged.addListener(onChange);
  });
}

// ── Step executor (injected into page) ────────────────────────────────────────
// Async so chrome.scripting awaits the returned Promise.
// Must be entirely self-contained — no closures over outer variables.

async function executeStepInPage(step) {
  // Waits for an ASP.NET UpdatePanel postback to complete.
  // Uses beginRequest to detect whether a postback actually started (fires
  // client-side within ms of the action). If nothing starts within 300ms the
  // click didn't trigger a postback and we move on immediately. If one starts,
  // we wait for endRequest — however long the server actually takes (up to 30s).
  function waitForPostback() {
    return new Promise((resolve) => {
      try {
        const mgr = window.Sys &&
                    window.Sys.WebForms &&
                    window.Sys.WebForms.PageRequestManager &&
                    window.Sys.WebForms.PageRequestManager.getInstance();
        if (!mgr) return resolve('no-updatepanel');

        // Window to detect if a postback started at all.
        const detectTid = setTimeout(() => {
          mgr.remove_beginRequest(onBegin);
          resolve('no-postback');
        }, 300);

        function onBegin() {
          clearTimeout(detectTid);
          mgr.remove_beginRequest(onBegin);
          // Postback started — now wait for it to finish.
          const endTid = setTimeout(() => { mgr.remove_endRequest(onEnd); resolve('timeout'); }, 30000);
          function onEnd() { clearTimeout(endTid); mgr.remove_endRequest(onEnd); resolve('done'); }
          mgr.add_endRequest(onEnd);
        }
        mgr.add_beginRequest(onBegin);
      } catch (_) { resolve('error'); }
    });
  }

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

      // btnPrint: the button calls window.open(pdfUrl) to trigger the download.
      // Intercept that URL and fetch the PDF directly from the page context
      // (same-origin, session cookies included) — bypasses Chrome's download
      // pipeline entirely: no onCreated, no dialog, no Save As prompt.
      const isPrint = loc.id === 'btnPrint' || (loc.id_suffix || '') === 'btnPrint';
      if (isPrint) {
        let capturedPdfUrl = null;
        const origOpenPrint = window.open;
        window.open = (url) => { capturedPdfUrl = String(url); return null; };
        const postbackPrint = waitForPostback();
        el.click();
        window.open = origOpenPrint;
        await postbackPrint;

        if (capturedPdfUrl) {
          try {
            const pdfUrl = new URL(capturedPdfUrl, location.href).href;
            const resp = await fetch(pdfUrl, { credentials: 'same-origin' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const ct = (resp.headers.get('content-type') || '').toLowerCase();
            if (!ct.includes('pdf') && !ct.includes('octet-stream')) {
              const snippet = await resp.clone().text().then(t => t.slice(0, 300)).catch(() => '');
              throw new Error(`Expected PDF, got ${ct.slice(0, 60)} | ${snippet}`);
            }
            const buf = await resp.arrayBuffer();
            const u8 = new Uint8Array(buf);
            let bin = '';
            for (let i = 0; i < u8.length; i += 0x8000) {
              bin += String.fromCharCode(...u8.subarray(i, Math.min(i + 0x8000, u8.length)));
            }
            return { ok: true, pdfBase64: btoa(bin) };
          } catch (fetchErr) {
            return { ok: false, err: 'PDF fetch failed: ' + fetchErr.message,
                     diag: { capturedPdfUrl } };
          }
        }

        // window.open was NOT called — fall through to normal download handling.
        return { ok: true, postbackReason: 'no-postback', waitForNav: false };
      }

      // Intercept window.open — injected clicks aren't trusted gestures so
      // window.open gets blocked. Capture the URL; background opens it instead.
      let capturedUrl = null;
      const origOpen = window.open;
      window.open = (url) => { capturedUrl = String(url); return null; };
      // Register UpdatePanel listener BEFORE the click so we don't miss the event.
      const postback = waitForPostback();
      el.click();
      window.open = origOpen;
      if (capturedUrl) {
        return { ok: true, openHref: new URL(capturedUrl, location.href).href };
      }
      const reason = await postback;
      // 'done'        → postback finished, DOM updated, no further wait needed
      // 'no-postback' → click didn't trigger AJAX (e.g. opening a dropdown) — move on
      // 'no-updatepanel' / 'error' / 'timeout' → fall back to tab-load detection
      return { ok: true, postbackReason: reason, waitForNav: reason !== 'done' && reason !== 'no-postback' };
    }

    // ── select_option ────────────────────────────────────────────
    if (t === 'select_option') {
      const el = findEl(loc, 'combobox', step.name, step.nth);
      if (!el) return { ok: false, err: `Select not found: ${step.name}` };
      let found = false;
      for (const opt of el.options) {
        if (opt.value === step.value || opt.text === step.label) {
          el.value = opt.value;
          found = true;
          break;
        }
      }
      if (!found) return { ok: false, err: `Option not found: ${step.label || step.value}` };
      const postback = waitForPostback();
      dispatch(el, ['change', 'input']);
      const reason = await postback;
      return { ok: true, postbackReason: reason, waitForNav: reason !== 'done' && reason !== 'no-postback' };
    }

    // ── select_kendo ─────────────────────────────────────────────
    if (t === 'select_kendo') {
      const wantText = (step.label || step.value || '').trim();
      const exactId  = loc.id || null;
      const idSuffix = loc.id_suffix || loc.id || null;

      // Mirror navigator.py: find the Kendo INPUT (not a <select>).
      // Kendo builds a visible <input> for its ComboBox/DropDownList;
      // getElementById may or may not return it depending on how the
      // widget was initialised, so also search input[id$=suffix].
      function findKendoInput() {
        if (exactId) {
          const byId = document.getElementById(exactId);
          if (byId) return byId;
        }
        if (idSuffix) {
          for (const el of document.querySelectorAll('input[id]')) {
            if (el.id === idSuffix || el.id.endsWith(idSuffix)) return el;
          }
        }
        return null;
      }

      function getWidget(el) {
        if (!el) return null;
        const jq = window.jQuery || window.$;
        if (jq) {
          const w = jq(el).data('kendoDropDownList') || jq(el).data('kendoComboBox');
          if (w) return w;
        }
        if (window.kendo && kendo.widgetInstance) {
          return kendo.widgetInstance(el) || null;
        }
        return null;
      }

      // Poll up to 5s for jQuery+Kendo widget. Exit early if we detect the
      // DOM-only Kendo pattern (listbox present but no JS API) — no need to
      // wait the full timeout just to discover jQuery never loads.
      const deadline = Date.now() + 5000;
      let input, widget;
      while (Date.now() < deadline) {
        input  = findKendoInput();
        widget = getWidget(input);
        if (widget) break;
        // DOM-only Kendo: listbox already rendered, JS API won't arrive.
        const listboxId = `${exactId || idSuffix}_listbox`;
        if (input && document.getElementById(listboxId)) break;
        await new Promise(r => setTimeout(r, 200));
      }

      if (widget) {
        // Use widget.select(fn) like navigator.py — matches by display text.
        const want = wantText.toLowerCase();
        widget.select(function(dataItem) {
          const tf = widget.options && widget.options.dataTextField;
          const label = tf ? dataItem[tf] : (dataItem.text || String(dataItem));
          return String(label).trim().toLowerCase() === want;
        });
        widget.trigger('change');
        const got = (widget.text() || '').trim();
        if (got.toLowerCase() !== wantText.toLowerCase()) {
          return { ok: false, err: `Kendo selected "${got}" but wanted "${wantText}"`,
                   diag: { got, wantText } };
        }
        return { ok: true };
      }

      // Kendo not found — fall back to plain <select> (covers pages without Kendo).
      let sel = null;
      if (exactId) sel = document.querySelector(`select[id="${exactId}"]`);
      if (!sel && idSuffix) sel = document.querySelector(`select[id$="${idSuffix}"]`);
      if (!sel) sel = findEl(loc, 'combobox', null, 0);
      if (sel && sel.tagName === 'SELECT') {
        for (const opt of sel.options) {
          if (opt.value === wantText || opt.text.trim().toLowerCase() === wantText.toLowerCase()) {
            sel.value = opt.value;
            dispatch(sel, ['change', 'input']);
            return { ok: true, waitForNav: true };
          }
        }
        return { ok: false, err: `Option "${wantText}" not found in <select>`,
                 diag: { options: Array.from(sel.options).map(o => o.text.trim()) } };
      }

      // Kendo DOM-only fallback: widget rendered its HTML but exposed no JS API.
      // Pattern: <input id="X"> drives a <ul id="X_listbox"> with <li> items.
      // Click the input to open the dropdown, then click the matching <li>.
      const listbox = document.getElementById(`${exactId}_listbox`)
                   || document.getElementById(`${idSuffix}_listbox`);
      if (input && listbox) {
        const want = wantText.toLowerCase();
        // Open the dropdown by clicking the input.
        input.click();
        input.focus();
        await new Promise(r => setTimeout(r, 300));
        // Find matching <li> — list may have been populated after click.
        const li = Array.from(listbox.querySelectorAll('li')).find(
          li => li.textContent.trim().toLowerCase() === want
        );
        if (li) {
          li.click();
          await new Promise(r => setTimeout(r, 150));
          // Verify: input value should now match.
          const got = (input.value || '').trim();
          return { ok: true, note: `DOM-click selected "${got}"` };
        }
        const available = Array.from(listbox.querySelectorAll('li')).map(l => l.textContent.trim());
        return { ok: false, err: `Option "${wantText}" not found in Kendo listbox`,
                 diag: { available } };
      }

      // Nothing found — emit diagnostics so the debug log is self-explaining.
      const diag = {
        hasJQuery:  !!(window.jQuery || window.$),
        hasKendo:   !!(window.kendo),
        inputFound: !!input,
        inputTag:   input ? input.tagName : null,
        inputId:    input ? input.id : null,
        selectsWithId: Array.from(document.querySelectorAll(`[id*="${idSuffix}"]`))
                          .map(e => ({ tag: e.tagName, id: e.id })),
      };
      return { ok: false, err: `Kendo widget not found and no <select> fallback for "${exactId}"`, diag };
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
      // Diploma date must be later than (or equal to) graduation date.
      // If diploma < graduation, the dates were entered backwards — swap them.
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

// ── Student name reader ───────────────────────────────────────────────────────

async function readStudentName(tabId) {
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const tryText = sel => {
          const el = document.querySelector(sel);
          return el ? (el.textContent || '').trim() : '';
        };
        const n = tryText('#ContentPlaceHolder1_txtStudentName')
               || tryText('[id$="txtStudentName"]')
               || tryText('[id$="lblStudentName"]')
               || tryText('[id$="lblName"]');
        return (n || '').replace(/\s+/g, ' ').trim();
      },
    });
    return result || '';
  } catch (_) {
    return '';
  }
}

function sanitizeFilename(s) {
  return s.replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_')
          .replace(/\s+/g, '_')
          .replace(/^[._]+|[._]+$/g, '')
          .slice(0, 120) || 'download';
}

// ── Silent download retry ─────────────────────────────────────────────────────
// USER_CANCELED usually means Chrome showed a "Save As" dialog (e.g. because
// the user has "Ask where to save each file" enabled in Chrome settings).
// Fix: re-initiate the download via chrome.downloads.download with saveAs:false,
// which bypasses Chrome's dialog entirely regardless of that setting.

function retryDownloadSilent(url, filename) {
  // Ensure onDeterminingFilename has the filename ready for the retry download.
  // (It's usually pre-set in onDeterminingFilename already; this is a fallback.)
  if (!state.retryDownloadName) state.retryDownloadName = filename;
  return new Promise((resolve, reject) => {
    // No 'filename' here — onDeterminingFilename intercepts this download
    // and calls suggest() with the correct path. saveAs:false prevents any dialog.
    chrome.downloads.download({
      url,
      saveAs: false,
      conflictAction: 'uniquify',
    }, (id) => {
      if (chrome.runtime.lastError || id == null) {
        state.retryDownloadName = null;
        reject(new Error(chrome.runtime.lastError?.message || 'Could not initiate retry download'));
        return;
      }
      state.lastDownloadId = id;
      waitForDownloadComplete(id, 120000).then(resolve).catch(reject);
    });
  });
}

// ── DOM-settle helper ─────────────────────────────────────────────────────────
// Mirrors navigator.py's _wait_for_change: polls the page DOM fingerprint
// every 200ms and resolves once it's been stable for `stableMs`, or after
// `timeoutMs` regardless. Never rejects — a timeout just means we proceed.

function waitForDomSettle(tabId, stableMs = 600, timeoutMs = 25000) {
  return new Promise(resolve => {
    const pollInterval = 200;
    let lastFp = null;
    let stableSince = null;
    const deadline = Date.now() + timeoutMs;

    async function fingerprint() {
      try {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => {
            const all = document.querySelectorAll('*');
            // Combine node count + total text length as a cheap fingerprint.
            let textLen = 0;
            for (const el of all) {
              if (el.childNodes) {
                for (const n of el.childNodes) {
                  if (n.nodeType === 3) textLen += (n.nodeValue || '').length;
                }
              }
            }
            return `${all.length}:${textLen}`;
          },
        });
        return result || '';
      } catch (_) {
        return null;
      }
    }

    async function poll() {
      const fp = await fingerprint();
      const now = Date.now();

      if (fp !== null && fp === lastFp) {
        // Fingerprint unchanged — check if stable long enough.
        if (stableSince === null) stableSince = now;
        if (now - stableSince >= stableMs) {
          resolve('stable');
          return;
        }
      } else {
        // Changed (or first read) — reset stable timer.
        lastFp = fp;
        stableSince = null;
      }

      if (now >= deadline) {
        resolve('timeout');
        return;
      }
      setTimeout(poll, pollInterval);
    }

    poll();
  });
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

  // Start watchers BEFORE executing the step to avoid race conditions where
  // a fast postback completes before we start listening.
  const locId = (step.locators || {}).id || '';
  const locSuffix = (step.locators || {}).id_suffix || '';
  const isBtnPrint = locId === 'btnPrint' || locSuffix === 'btnPrint';

  const navPromise   = (t === 'click' || t === 'select_option')
    ? waitForTabLoad(tabId, 15000).catch(() => null) : null;
  const newTabPromise = (t === 'click')
    ? waitForNewTab(3000) : null;
  const downloadPromise = (t === 'click' && isBtnPrint)
    ? waitForDownload(45000) : null;

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

  // Case 1: window.open captured — open as a new popup tab directly.
  if (result.openHref) {
    const newTab = await chrome.tabs.create({ url: result.openHref, active: false });
    state.popupTabId = newTab.id;
    state.activeTabId = newTab.id;
    await waitForTabLoad(newTab.id, 15000);
    return result;
  }

  // Case 2+3: check if a same-tab navigation or new-tab popup happened.
  // Give the browser 60ms to start any navigation before we inspect tab status.
  await new Promise(r => setTimeout(r, 60));

  const tabInfo = await chrome.tabs.get(tabId).catch(() => null);
  if (tabInfo && tabInfo.status === 'loading') {
    // Same-tab navigation started — wait for it (navPromise already listening).
    await navPromise;
  } else {
    // No same-tab navigation. Check if a popup tab opened (e.g. target=_blank).
    const newTabId = await Promise.race([
      newTabPromise || Promise.resolve(null),
      new Promise(r => setTimeout(() => r(null), 400)),
    ]);
    if (newTabId) {
      state.popupTabId = newTabId;
      state.activeTabId = newTabId;
      await waitForTabLoad(newTabId, 15000);
    } else {
      // Nothing navigated — AJAX, dropdown open, or static click. Small settle.
      await new Promise(r => setTimeout(r, 150));
    }
  }

  // DOM-settle wait: poll page fingerprint until stable (like navigator.py's
  // _wait_for_change). Resolves when DOM hasn't changed for 600ms, or 25s max.
  if (step.waitForDomSettle) {
    log('Waiting for report to finish rendering…', 'muted');
    await waitForDomSettle(state.activeTabId, 600, 25000);
    log('Report ready — saving PDF…', 'muted');
  }

  // pdfBase64 path: executeStepInPage fetched the PDF directly via fetch().
  // No Chrome download pipeline was involved — just save it via a data URL.
  if (result.pdfBase64) {
    if (downloadPromise) downloadPromise.abort?.();
    const name = state.pendingDownloadName || 'transcript.pdf';
    state.pendingDownloadName = null;
    const filename = safeDownloadPath(_cachedSubfolder, name);
    log('Saving transcript…', 'muted');
    const dataUrl = `data:application/pdf;base64,${result.pdfBase64}`;
    try {
      const dlItem = await new Promise((resolve, reject) => {
        chrome.downloads.download(
          { url: dataUrl, filename, saveAs: false, conflictAction: 'uniquify' },
          (id) => {
            if (chrome.runtime.lastError || id == null) {
              reject(new Error(chrome.runtime.lastError?.message || 'download() failed'));
              return;
            }
            state.lastDownloadId = id;
            waitForDownloadComplete(id, 120000).then(resolve).catch(reject);
          }
        );
      });
      result.downloadedFile = dlItem.filename ? dlItem.filename.split(/[/\\]/).pop() : name;
    } catch (e) {
      return { ok: false, err: 'PDF save failed: ' + e.message };
    }
    return result;
  }

  // Download handling (btnPrint triggers a download after its click).
  if (downloadPromise) {
    log('Downloading transcript…', 'muted');
    try {
      const dlItem = await downloadPromise;
      const filename = dlItem.filename ? dlItem.filename.split(/[/\\]/).pop() : 'file';
      result.downloadedFile = filename;
    } catch (e) {
      // USER_CANCELED means Chrome tried to show a Save As dialog.
      // We pre-canceled the download in onDeterminingFilename; the retry
      // re-initiates it silently via chrome.downloads.download({ saveAs:false }).
      if ((e.message || '').includes('USER_CANCELED')
          && state.pendingDownloadUrl
          && state.lastSuggestedFilename) {
        try {
          const dlItem = await retryDownloadSilent(state.pendingDownloadUrl, state.lastSuggestedFilename);
          const filename = dlItem.filename ? dlItem.filename.split(/[/\\]/).pop() : 'file';
          result.downloadedFile = filename;
          result.retried = true;
        } catch (e2) {
          return { ok: false, err: 'Download failed after retry: ' + e2.message };
        }
      } else {
        return { ok: false, err: 'Download failed: ' + e.message };
      }
    }
  }

  return result;
}

// ── Email → student ID resolver ───────────────────────────────────────────────

async function resolveEmail(email) {
  const searchUrl = 'https://app.asapconnected.com/Students.aspx?s=' + encodeURIComponent(email);
  await chrome.tabs.update(state.activeTabId, { url: searchUrl });
  await waitForTabLoad(state.activeTabId);

  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: state.activeTabId },
    func: () => {
      const links = document.querySelectorAll('a[id*="rptStudents_ctrl"][id$="_btnView"]');
      const out = [];
      links.forEach(a => {
        const m = (a.getAttribute('href') || '').match(/[?&]Id=(\d+)/i);
        if (m) out.push(m[1]);
      });
      return out;
    },
  });

  if (!result || result.length === 0) throw new Error('No student found with that email');
  if (result.length > 1) throw new Error(`Multiple students matched (${result.length}) — use a student ID instead`);
  return result[0];
}

// ── Run one student ───────────────────────────────────────────────────────────

async function runStudent(template, studentId, vars, idx, total) {
  log(`Starting student ${idx + 1} of ${total} (ID: ${studentId})…`, 'accent');
  debugPush({ event: 'student_start', studentIndex: idx, total });

  state.currentStudentId = studentId;
  state.currentStudentName = null;
  state.pendingDownloadName = null;

  const steps = substituteVars(template, vars);

  for (let i = 0; i < steps.length; i++) {
    if (state.stopRequested) return 'stopped';
    const step = steps[i];

    // After landing on a StudentDetail page, read the name and prepare
    // the download filename: LastName_FirstName_ID_transcript.pdf
    if (step.type === 'navigate' && (step.url || '').includes('StudentDetail')) {
      // Name read happens AFTER the navigate completes — handled below.
    }

    log(stepToEnglish(step));
    const t0 = Date.now();

    try {
      const result = await runStep(step);
      const ms = Date.now() - t0;

      debugPush({
        event: 'step',
        stepIndex: i,
        type: step.type,
        name: step.name || undefined,
        locator: (step.locators || {}).id_suffix || (step.locators || {}).id || undefined,
        tabId: state.activeTabId,
        ok: result.ok,
        ms,
        err: result.ok ? undefined : result.err,
        diag: result.ok ? undefined : result.diag,
        note: result.note || undefined,
        swapped: result.swapped,
        retried: result.retried || undefined,
        downloadedFile: result.downloadedFile,
        openHref: result.openHref ? debugRedact(result.openHref) : undefined,
        postback: result.postbackReason,
      });

      if (!result.ok) {
        if (step.optional) {
          log(`Skipped optional step "${step.name || step.type}": ${result.err}`, 'muted');
          debugPush({ event: 'step_skipped', stepIndex: i, reason: result.err });
          continue;
        }
        log(`Could not complete: ${result.err}`, 'err');
        debugPush({ event: 'student_fail', stepIndex: i });
        return 'failed';
      }

      // After StudentDetail navigation lands, read the student name.
      if (step.type === 'navigate' && (step.url || '').includes('StudentDetail')
          && !state.currentStudentName) {
        const name = await readStudentName(state.activeTabId);
        if (name) {
          state.currentStudentName = name;
          debugPush({ event: 'student_name', name: '[REDACTED]' });
        }
        // Set the download filename now so it's ready when btnPrint fires.
        const namePart = name ? sanitizeFilename(name) + '_' : '';
        state.pendingDownloadName = `${namePart}${studentId}_transcript.pdf`;
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
      const ms = Date.now() - t0;
      debugPush({ event: 'step', stepIndex: i, type: step.type, ok: false, ms, err: e.message });
      log(`Error on step ${i + 1}: ${e.message}`, 'err');
      debugPush({ event: 'student_fail', stepIndex: i });
      return 'failed';
    }
  }

  debugPush({ event: 'student_done' });
  return 'succeeded';
}

// ── Batch runner ──────────────────────────────────────────────────────────────

async function runBatch(template, studentIds) {
  if (state.running) {
    log('A run is already in progress.', 'warn');
    return;
  }
  resetState();
  debugClear();
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

    const isEmail = sid.includes('@');
    let resolvedId = sid;

    if (isEmail) {
      log(`Looking up student ID for ${sid}…`, 'info');
      debugPush({ event: 'email_lookup', input: '[EMAIL]' });
      try {
        resolvedId = await resolveEmail(sid);
        log(`Found student ID: ${resolvedId}`, 'info');
        debugPush({ event: 'email_lookup_ok' });
      } catch (e) {
        log(`Could not find student for email "${sid}": ${e.message}`, 'err');
        debugPush({ event: 'email_lookup_fail', err: e.message });
        state.failed++;
        toPanel({ type: 'progress', current: i + 1, total, studentId: sid, status: 'failed' });
        continue;
      }
    }

    // Register resolved ID as sensitive so it gets scrubbed from debug output.
    _debugSensitive.push(resolvedId);
    if (isEmail) _debugSensitive.push(sid);

    const vars = { studentid: resolvedId, email: isEmail ? sid : '' };
    state.activeTabId = state.mainTabId; // reset to main tab for each student
    state.popupTabId = null;

    const outcome = await runStudent(template, resolvedId, vars, i, total);

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
