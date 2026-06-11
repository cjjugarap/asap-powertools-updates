// ASAP Powertools — side panel controller

let selectedProcess = null;
let running = false;

// ── DOM refs ──────────────────────────────────────────────────────────────────
const procList       = document.getElementById('proc-list');
const feed           = document.getElementById('feed');
const statusBar      = document.getElementById('status-bar');
const btnOne         = document.getElementById('btn-one');
const btnAll         = document.getElementById('btn-all');
const btnStop        = document.getElementById('btn-stop');
const btnConfirm     = document.getElementById('btn-confirm');
const btnCancel      = document.getElementById('btn-cancel');
const batchSection   = document.getElementById('batch-input-section');
const studentIdsInput = document.getElementById('student-ids-input');
const modalOverlay   = document.getElementById('modal-overlay');
const modalInput     = document.getElementById('modal-input');
const modalOk        = document.getElementById('modal-ok');
const modalCancel    = document.getElementById('modal-cancel');

// ── Activity feed ─────────────────────────────────────────────────────────────

let feedHasContent = false;

function log(text, tag = 'info') {
  if (!feedHasContent) {
    feed.innerHTML = '';
    feedHasContent = true;
  }
  const span = document.createElement('span');
  span.className = `feed-line ${tag}`;
  span.textContent = text;
  feed.appendChild(span);
  feed.scrollTop = feed.scrollHeight;
}

function clearFeed() {
  feed.innerHTML = '';
  feedHasContent = false;
}

function setStatus(text) {
  statusBar.textContent = text;
}

// ── Process cards ─────────────────────────────────────────────────────────────

function buildProcCards(templates) {
  procList.innerHTML = '';
  if (!templates || templates.length === 0) {
    procList.innerHTML = '<p style="color:var(--muted);font-size:11px;padding:4px">No processes found.</p>';
    return;
  }
  for (const tpl of templates) {
    const card = document.createElement('div');
    card.className = 'proc-card';
    card.innerHTML = `
      <div class="proc-name">${tpl.name}</div>
      ${tpl.description ? `<div class="proc-desc">${tpl.description}</div>` : ''}
    `;
    card.addEventListener('click', () => selectProcess(tpl, card));
    procList.appendChild(card);
  }
}

function selectProcess(tpl, card) {
  if (running) return;
  selectedProcess = tpl;
  // Highlight selected card.
  document.querySelectorAll('.proc-card').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  // Enable action buttons.
  btnOne.disabled = false;
  btnAll.disabled = false;
  // Feed update.
  clearFeed();
  log(`— ${tpl.name} —`, 'accent');
  log(`${tpl.steps.length} steps ready to run.`, 'muted');
  setStatus(`${tpl.name} selected.`);
}

// ── UI state transitions ──────────────────────────────────────────────────────

function setRunning(isRunning) {
  running = isRunning;
  btnOne.disabled  = isRunning || !selectedProcess;
  btnAll.disabled  = isRunning || !selectedProcess;
  btnStop.style.display    = isRunning ? 'block' : 'none';
  btnConfirm.style.display = 'none';
  btnCancel.style.display  = 'none';
  batchSection.style.display = 'none';
  if (!isRunning && selectedProcess) {
    btnOne.disabled = false;
    btnAll.disabled = false;
  }
}

function showBatchInput() {
  batchSection.style.display = 'block';
  btnAll.style.display     = 'none';
  btnOne.style.display     = 'none';
  btnConfirm.style.display = 'flex';
  btnCancel.style.display  = 'flex';
  studentIdsInput.value = '';
  studentIdsInput.focus();
}

function hideBatchInput() {
  batchSection.style.display = 'none';
  btnAll.style.display     = '';
  btnOne.style.display     = '';
  btnConfirm.style.display = 'none';
  btnCancel.style.display  = 'none';
}

// ── Button handlers ───────────────────────────────────────────────────────────

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
  const ids = studentIdsInput.value
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean);
  if (ids.length === 0) {
    log('Please enter at least one student ID.', 'warn');
    return;
  }
  hideBatchInput();
  startBatch(ids);
});

btnCancel.addEventListener('click', () => {
  hideBatchInput();
});

btnStop.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'stop' });
  btnStop.disabled = true;
  setStatus('Stopping…');
});

// ── Modal ─────────────────────────────────────────────────────────────────────

function closeModal() {
  modalOverlay.classList.remove('open');
}

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

modalOverlay.addEventListener('click', e => {
  if (e.target === modalOverlay) closeModal();
});

// ── Start batch ───────────────────────────────────────────────────────────────

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
    const { succeeded, failed, stopped } = msg;
    const label = stopped ? 'Stopped.' : 'Done.';
    setStatus(`${label} ${succeeded} completed, ${failed} needed attention.`);
    btnStop.disabled = false;
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

chrome.runtime.sendMessage({ type: 'get_templates' }, (resp) => {
  if (resp && resp.templates) {
    buildProcCards(resp.templates);
  }
});
