// ASAP Powertools — side panel controller

// ── State ─────────────────────────────────────────────────────────────────────

let selectedProcess = null;
let running = false;
let recordedRawSteps = [];
let cleanedSteps = [];

// ── DOM refs ──────────────────────────────────────────────────────────────────

const screens = {
  main:      document.getElementById('screen-main'),
  recording: document.getElementById('screen-recording'),
  review:    document.getElementById('screen-review'),
  settings:  document.getElementById('screen-settings'),
};

const procList        = document.getElementById('proc-list');
const feed            = document.getElementById('feed');
const statusBar       = document.getElementById('status-bar');
const headerSub       = document.getElementById('header-sub');
const btnOne          = document.getElementById('btn-one');
const btnAll          = document.getElementById('btn-all');
const btnStop         = document.getElementById('btn-stop');
const btnConfirm      = document.getElementById('btn-confirm');
const btnCancel       = document.getElementById('btn-cancel');
const batchSection    = document.getElementById('batch-input-section');
const studentIdsInput = document.getElementById('student-ids-input');
const newProcBtn      = document.getElementById('new-proc-btn');
const modalOverlay    = document.getElementById('modal-overlay');
const modalInput      = document.getElementById('modal-input');
const modalOk         = document.getElementById('modal-ok');
const modalCancel     = document.getElementById('modal-cancel');
const settingsBtn     = document.getElementById('settings-btn');
const recFeed         = document.getElementById('rec-feed');
const btnStopRec      = document.getElementById('btn-stop-rec');
const reviewNameInput = document.getElementById('review-name-input');
const reviewSteps     = document.getElementById('review-steps');
const btnSaveProc     = document.getElementById('btn-save-proc');
const btnReClean      = document.getElementById('btn-re-clean');
const btnDiscard      = document.getElementById('btn-discard');
const apiKeyInput     = document.getElementById('api-key-input');
const btnSaveSettings = document.getElementById('btn-save-settings');
const btnBackSettings = document.getElementById('btn-back-settings');
const settingsStatus  = document.getElementById('settings-status');

// ── Screen navigation ─────────────────────────────────────────────────────────

function showScreen(name) {
  Object.entries(screens).forEach(([k, el]) => {
    el.classList.toggle('active', k === name);
  });
}

// ── Activity feed (main screen) ───────────────────────────────────────────────

let feedHasContent = false;

function log(text, tag = 'info') {
  if (!feedHasContent) { feed.innerHTML = ''; feedHasContent = true; }
  const span = document.createElement('span');
  span.className = `feed-line ${tag}`;
  span.textContent = text;
  feed.appendChild(span);
  feed.scrollTop = feed.scrollHeight;
}

function clearFeed() { feed.innerHTML = ''; feedHasContent = false; }
function setStatus(text) { statusBar.textContent = text; }

// ── Recording feed ────────────────────────────────────────────────────────────

let recFeedHasContent = false;

function logRec(text) {
  if (!recFeedHasContent) { recFeed.innerHTML = ''; recFeedHasContent = true; }
  const span = document.createElement('span');
  span.className = 'feed-line info';
  span.textContent = text;
  recFeed.appendChild(span);
  recFeed.scrollTop = recFeed.scrollHeight;
}

function stepToEnglish(step) {
  const t = step.type || '';
  const name = step.name || '';
  const label = step.label || step.value || '';
  switch (t) {
    case 'navigate':
      try { return `Opened ${new URL(step.url).hostname}`; }
      catch (_) { return `Navigated to ${step.url}`; }
    case 'click':        return name ? `Clicked "${name}"` : 'Clicked an element';
    case 'select_option':return `Selected "${label}" from dropdown`;
    case 'select_kendo': return `Set program to "${step.value}"`;
    case 'check':        return `Checked "${name}"`;
    case 'fill':         return `Filled in "${name}"`;
    case 'swap_dates':   return 'Checked and fixed diploma/graduation dates';
    case 'download':     return `Downloaded ${step.filename || 'file'}`;
    case 'close_page':   return 'Closed popup window';
    default:             return t;
  }
}

// ── Process cards ─────────────────────────────────────────────────────────────

function buildProcCards(templates) {
  procList.innerHTML = '';
  if (!templates || templates.length === 0) {
    procList.innerHTML = '<p style="color:var(--muted);font-size:11px;padding:4px">No processes yet. Click below to set one up.</p>';
    return;
  }
  for (const tpl of templates) {
    addProcCard(tpl);
  }
}

function addProcCard(tpl) {
  const card = document.createElement('div');
  card.className = 'proc-card';
  card.dataset.id = tpl.id || tpl.name;
  card.innerHTML = `
    <div class="proc-name">${tpl.name}</div>
    ${tpl.description ? `<div class="proc-desc">${tpl.description}</div>` : ''}
    ${tpl.userDefined ? `<button class="delete-btn" title="Delete this process">✕</button>` : ''}
  `;
  card.addEventListener('click', (e) => {
    if (e.target.classList.contains('delete-btn')) {
      deleteProc(tpl, card);
    } else {
      selectProcess(tpl, card);
    }
  });
  procList.appendChild(card);
}

function deleteProc(tpl, card) {
  if (!confirm(`Delete "${tpl.name}"? This can't be undone.`)) return;
  chrome.runtime.sendMessage({ type: 'delete_process', id: tpl.id }, () => {
    card.remove();
    if (selectedProcess && selectedProcess.id === tpl.id) {
      selectedProcess = null;
      btnOne.disabled = true;
      btnAll.disabled = true;
    }
  });
}

function selectProcess(tpl, card) {
  if (running) return;
  selectedProcess = tpl;
  document.querySelectorAll('.proc-card').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  btnOne.disabled = false;
  btnAll.disabled = false;
  clearFeed();
  log(`— ${tpl.name} —`, 'accent');
  log(`${tpl.steps.length} steps ready to run.`, 'muted');
  setStatus(`${tpl.name} selected.`);
}

function reloadProcessCards() {
  chrome.runtime.sendMessage({ type: 'get_templates' }, (resp) => {
    if (resp && resp.templates) buildProcCards(resp.templates);
  });
}

// ── UI state ──────────────────────────────────────────────────────────────────

function setRunning(isRunning) {
  running = isRunning;
  btnOne.disabled  = isRunning || !selectedProcess;
  btnAll.disabled  = isRunning || !selectedProcess;
  btnStop.style.display    = isRunning ? '' : 'none';
  btnConfirm.style.display = 'none';
  btnCancel.style.display  = 'none';
  batchSection.style.display = 'none';
  btnAll.style.display = '';
  btnOne.style.display = '';
}

function showBatchInput() {
  batchSection.style.display = 'block';
  btnAll.style.display = 'none';
  btnOne.style.display = 'none';
  btnConfirm.style.display = '';
  btnCancel.style.display  = '';
  studentIdsInput.value = '';
  studentIdsInput.focus();
}

function hideBatchInput() {
  batchSection.style.display = 'none';
  btnAll.style.display = '';
  btnOne.style.display = '';
  btnConfirm.style.display = 'none';
  btnCancel.style.display  = 'none';
}

// ── Run buttons ───────────────────────────────────────────────────────────────

btnOne.addEventListener('click', () => {
  if (!selectedProcess) return;
  modalInput.value = '';
  modalOverlay.classList.add('open');
  modalInput.focus();
});

btnAll.addEventListener('click', () => {
  if (!selectedProcess) return;
  showBatchInput();
});

btnConfirm.addEventListener('click', () => {
  const ids = studentIdsInput.value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!ids.length) { log('Please enter at least one student ID.', 'warn'); return; }
  hideBatchInput();
  startBatch(ids);
});

btnCancel.addEventListener('click', hideBatchInput);

btnStop.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'stop' });
  btnStop.disabled = true;
  setStatus('Stopping…');
});

function startBatch(studentIds) {
  if (!selectedProcess) return;
  clearFeed();
  setRunning(true);
  setStatus(`Running for ${studentIds.length} student${studentIds.length !== 1 ? 's' : ''}…`);
  chrome.runtime.sendMessage({
    type: 'run_batch',
    template: selectedProcess.steps,
    studentIds,
  });
}

// ── Modal ─────────────────────────────────────────────────────────────────────

function closeModal() { modalOverlay.classList.remove('open'); }

modalOk.addEventListener('click', () => {
  const sid = modalInput.value.trim();
  if (!sid) return;
  closeModal();
  startBatch([sid]);
});
modalCancel.addEventListener('click', closeModal);
modalInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') modalOk.click();
  if (e.key === 'Escape') closeModal();
});
modalOverlay.addEventListener('click', e => { if (e.target === modalOverlay) closeModal(); });

// ── New process (recording) ───────────────────────────────────────────────────

newProcBtn.addEventListener('click', () => {
  recFeed.innerHTML = '<span style="color:var(--muted);font-size:11px">Waiting for your first action…</span>';
  recFeedHasContent = false;
  recordedRawSteps = [];
  showScreen('recording');
  headerSub.textContent = 'Recording…';

  chrome.runtime.sendMessage({ type: 'start_recording' }, (resp) => {
    if (!resp || !resp.ok) {
      alert('Could not start recording: ' + (resp && resp.err || 'unknown error'));
      showScreen('main');
    }
  });
});

btnStopRec.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'stop_recording' }, (resp) => {
    const steps = (resp && resp.steps) || recordedRawSteps;
    if (!steps.length) {
      alert('No steps were recorded. Try again and make sure you perform some actions in the ASAP tab.');
      showScreen('main');
      headerSub.textContent = 'Select a process or set up a new one.';
      return;
    }
    recordedRawSteps = steps;
    runClaudeCleanup(steps);
  });
});

// ── Claude cleanup ────────────────────────────────────────────────────────────

function runClaudeCleanup(steps) {
  showScreen('review');
  headerSub.textContent = 'Reviewing recording…';
  reviewSteps.innerHTML = '<span style="color:var(--muted)">Cleaning up with AI…</span>';
  reviewNameInput.value = '';
  btnSaveProc.disabled  = true;

  const name = 'New Process';
  chrome.runtime.sendMessage({ type: 'cleanup_with_claude', steps, name }, (resp) => {
    if (!resp || !resp.ok) {
      const errMsg = resp && resp.err || 'Unknown error';
      reviewSteps.innerHTML = `<span style="color:var(--red)">AI cleanup failed: ${errMsg}</span>`;
      btnSaveProc.disabled = true;
      return;
    }
    cleanedSteps = resp.steps;
    showReviewSteps(cleanedSteps);
    btnSaveProc.disabled = false;
  });
}

function showReviewSteps(steps) {
  reviewSteps.innerHTML = '';
  for (const step of steps) {
    const span = document.createElement('span');
    span.className = 'step-line';
    span.textContent = stepToEnglish(step);
    reviewSteps.appendChild(span);
  }
}

btnSaveProc.addEventListener('click', () => {
  const name = reviewNameInput.value.trim() || 'Unnamed Process';
  const proc = {
    id: 'user_' + Date.now(),
    name,
    description: `Recorded process — ${cleanedSteps.length} steps`,
    steps: cleanedSteps,
    userDefined: true,
    createdAt: new Date().toISOString(),
  };
  chrome.runtime.sendMessage({ type: 'save_process', process: proc }, (resp) => {
    if (resp && resp.ok) {
      reloadProcessCards();
      showScreen('main');
      headerSub.textContent = 'Select a process or set up a new one.';
      log(`✓ "${name}" saved and ready to run.`, 'ok');
      setStatus(`"${name}" saved.`);
    } else {
      alert('Save failed: ' + (resp && resp.err || 'Unknown error'));
    }
  });
});

btnReClean.addEventListener('click', () => {
  runClaudeCleanup(recordedRawSteps);
});

btnDiscard.addEventListener('click', () => {
  if (confirm('Discard this recording?')) {
    showScreen('main');
    headerSub.textContent = 'Select a process or set up a new one.';
  }
});

// ── Settings ──────────────────────────────────────────────────────────────────

settingsBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'get_settings' }, (resp) => {
    apiKeyInput.value = resp && resp.apiKey ? '••••••••' : '';
    apiKeyInput.dataset.loaded = resp && resp.apiKey ? 'yes' : '';
    settingsStatus.textContent = '';
    showScreen('settings');
    headerSub.textContent = 'Settings';
  });
});

apiKeyInput.addEventListener('focus', () => {
  if (apiKeyInput.dataset.loaded) {
    apiKeyInput.value = '';
    apiKeyInput.dataset.loaded = '';
  }
});

btnSaveSettings.addEventListener('click', () => {
  const key = apiKeyInput.value.trim();
  if (!key || key === '••••••••') {
    settingsStatus.style.color = 'var(--yellow)';
    settingsStatus.textContent = 'Enter a new key to update.';
    return;
  }
  chrome.runtime.sendMessage({ type: 'save_settings', apiKey: key }, (resp) => {
    if (resp && resp.ok) {
      settingsStatus.style.color = 'var(--green)';
      settingsStatus.textContent = 'Saved.';
      apiKeyInput.value = '••••••••';
      apiKeyInput.dataset.loaded = 'yes';
    } else {
      settingsStatus.style.color = 'var(--red)';
      settingsStatus.textContent = 'Save failed.';
    }
  });
});

btnBackSettings.addEventListener('click', () => {
  showScreen('main');
  headerSub.textContent = 'Select a process or set up a new one.';
});

// ── Messages from background ──────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg) => {
  if (msg._source !== 'background') return;

  if (msg.type === 'log') {
    log(msg.text, msg.tag || 'info');
  }

  if (msg.type === 'progress') {
    setStatus(`${msg.current} of ${msg.total} students processed…`);
  }

  if (msg.type === 'batch_done') {
    setRunning(false);
    btnStop.disabled = false;
    const { succeeded, failed, stopped } = msg;
    setStatus(`${stopped ? 'Stopped. ' : 'Done. '}${succeeded} completed, ${failed} needed attention.`);
  }

  if (msg.type === 'recorded_step') {
    recordedRawSteps.push(msg.step);
    logRec(stepToEnglish(msg.step));
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

reloadProcessCards();
