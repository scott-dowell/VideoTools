// ============================================================
// State
// ============================================================
let _files        = [];   // file objects loaded by scan
let _fileIndexByPath = {}; // full_path → _files index for O(1) probe/remove lookup
let _scanEs       = null; // active EventSource for /api/scan
let _probeTotal   = 0;    // files queued for phase-2 probe
let _probeDone    = 0;    // probe events received so far
let _hashTotal    = 0;    // files queued for hash-check phase
let _hashDone     = 0;    // hash events received so far
let _hashRemoved  = 0;    // files removed during hash-check (won't reach probe)
let _probePhaseTotal = 0; // files expected in probe phase only
let _probePhaseDone  = 0; // probe progress counter (resets after hashing)
const _ALL_STATUSES = ['pending', 'done_session', 'done', 'failed', 'no_saving', 'skipped', 'low_savings'];
const _DEFAULT_ACTIVE_STATUSES = ['pending', 'done_session'];
let _activeStatuses = new Set(_DEFAULT_ACTIVE_STATUSES);
let _searchQuery  = '';
let _filterSizeMin = ''; let _filterSizeMax = '';
let _filterBrMin   = ''; let _filterBrMax   = '';
let _filterDurMin  = ''; let _filterDurMax  = '';
let _filterCodec   = '';
let _appState     = 'idle';  // idle | scanning | ready | running | done | stopped
let _dragSrcIndex = null;
let _sortBy       = 'bitrate'; // 'bitrate' | 'size' | 'name' | 'duration' | 'est_saving' | 'est_saving_mb'
let _sortDir      = 'desc';    // 'desc' | 'asc'
let _currentScanPath = null;  // last successfully scanned folder path
let _lastAutoScrollPath = null; // path of row last auto-scrolled to; prevents re-scroll on every poll
let _sessionSavedMB = null;   // null = no run this session, number = realized MB saved this run
let _sessionEstimatedMB = 0;  // projected extra MB from currently executing file
let _sessionProcessed = 0;    // files completed (done/failed/no_saving) this run
let _sessionStartedAt = 0;        // unix epoch (s) when current session started; 0 = not started
let _sessionElapsedTimer = null;  // setInterval ID for the live session elapsed clock
let _fileElapsedTimer    = null;  // setInterval ID for the live per-file elapsed clock
let _detailsFileIndex = null;     // index currently shown in Video Details modal
let _detailsPreviewPath = '';      // preview copy path for details modal file
let _detailsEngStereoPreviewPath = ''; // english-stereo preview path for details modal file
let _detailsBusy = false;          // prevent duplicate stream-edit actions
let _streamReplaceConfirmModal = null;
let _engStereoReplaceConfirmModal = null;
let _subtitleLikelyCache = {};     // key: "<full_path>|<stream_index>" -> detection payload
let _subtitleLikelyPending = new Set();
const _QUEUE_COL_COUNT = 18;

// Estimation background task state
let _estUserPaused  = false;  // user clicked the strip
let _estAutoPaused  = false;  // conversion is running
let _estCancelled   = false;  // new scan started
let _estRunning     = false;  // chain is live (not dormant)
let _estDone        = 0;
let _estTotal       = 0;
let _estLastFolder  = null;

// Build status badge HTML (includes SW/FC indicators when enabled)
function _badgeHtml(status, force_sw, force_convert) {
  const cls = status === 'done'         ? 'badge-done'
            : status === 'failed'       ? 'badge-failed'
            : status === 'no_saving'    ? 'badge-no-saving'
            : status === 'low_savings'  ? 'badge-low-savings'
            : status === 'skipped'      ? 'badge-skipped'
            : status === 'converting'   ? 'badge-converting'
            : status === 'ocr'          ? 'badge-ocr'
            : 'badge-pending';
  const lbl = status === 'no_saving'   ? 'Skipped \u2013 Larger'
            : status === 'low_savings' ? 'Low Savings'
            : status === 'skipped'     ? 'Skipped \u2013 Manual'
            : status === 'ocr'         ? 'OCR'
            : (status || 'pending');
  const sw  = force_sw ? ' <small style="font-size:.7em;opacity:.85" class="text-warning">SW</small>' : '';
  const fc  = force_convert ? ' <small style="font-size:.7em;opacity:.85" class="text-info">FC</small>' : '';
  return '<span class="badge ' + cls + '">' + lbl + sw + fc + '</span>';
}

// Returns a small secondary badge showing OCR outcome, or '' if not yet known
function _ocrBadgeHtml(f) {
  if (f.ocr_status === 'done')
    return ' <span class="badge badge-ocr-done ms-1" title="OCR complete \u2014 bitmap subtitles extracted to SRT">OCR \u2713</span>';
  if (f.ocr_status === 'skipped')
    return ' <span class="badge badge-ocr-skip ms-1" title="No PGS bitmap subtitle tracks \u2014 OCR not required">No PGS</span>';
  return '';
}

// Returns a small badge when the file has dropped streams, otherwise ''
function _droppedBadgeHtml(f) {
  const n = f.dropped_streams ? f.dropped_streams.length : 0;
  if (!n) return '';
  return ' <span class="badge bg-warning text-dark ms-1" style="font-size:.65rem" title="' + n + ' stream(s) excluded from conversion"><i class="bi bi-slash-circle"></i>\u202f' + n + '</span>';
}

function _estimateHtml(f) {
  if (f.est_pct == null) return '';
  let html =
    '<span class="text-success fw-semibold">' + f.est_pct + '%</span>' +
    '<br><small class="text-secondary">' + (f.est_mb || 0) + '\u202fMB</small>';
  if (f.est_high_variance) {
    const cvText = f.est_cv != null ? ('cv ' + Number(f.est_cv).toFixed(1) + '%') : 'high variance';
    const aggText = f.est_aggregation ? (' · ' + String(f.est_aggregation).replaceAll('_', ' ')) : '';
    html += '<br><small class="text-warning" title="Estimate samples varied sharply across the file; low-savings auto-skip is bypassed for this estimate."><i class="bi bi-exclamation-triangle me-1"></i>' + cvText + aggText + '</small>';
  }
  return html;
}

// Parse "H:MM:SS" or "MM:SS" → seconds
function _parseDuration(d) {
  if (!d) return 0;
  const parts = d.split(':').map(Number);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return 0;
}

function _parseFpsVal(v) {
  if (v == null) return 0;
  if (typeof v === 'number') return v > 0 ? v : 0;
  const s = String(v).trim();
  if (!s) return 0;
  if (s.includes('/')) {
    const [n, d] = s.split('/').map(Number);
    if (d && Number.isFinite(n) && Number.isFinite(d)) {
      const r = n / d;
      return r > 0 ? r : 0;
    }
  }
  const f = Number(s);
  return Number.isFinite(f) && f > 0 ? f : 0;
}

function _extractFrameFromStatus(statusText) {
  if (!statusText) return 0;
  const m = String(statusText).match(/frame=\s*(\d+)/i);
  return m ? (parseInt(m[1], 10) || 0) : 0;
}

function _fmtInt(n) {
  return Math.max(0, Math.floor(Number(n) || 0)).toLocaleString();
}

// Format elapsed seconds as "Xm Ys" or "Xh MMm SSs"
function _fmtDuration(secs) {
  secs = Math.max(0, Math.floor(secs));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return h + 'h ' + String(m).padStart(2, '0') + 'm ' + String(s).padStart(2, '0') + 's';
  if (m > 0) return m + 'm ' + String(s).padStart(2, '0') + 's';
  return s + 's';
}

function _fmtMbVal(mb) {
  const n = Number(mb || 0);
  return n.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function _fmtSavedShort(mb) {
  const n = Number(mb || 0);
  return n / 1024 > 1 ? (n / 1024).toFixed(1) + ' GB' : n.toFixed(0) + ' MB';
}

function _savedMbFromFile(f) {
  if (!f || !f.saved) return 0;
  return parseFloat(String(f.saved).replace(/,/g, '')) || 0;
}

function _conversionSpeedX(durationSecs, convSecs) {
  const d = Number(durationSecs || 0);
  const c = Number(convSecs || 0);
  if (d <= 0 || c <= 0) return 0;
  return d / c;
}

function _formatSpeedX(x) {
  const n = Number(x || 0);
  if (n <= 0) return '—';
  if (n >= 10) return n.toFixed(0) + 'x';
  return n.toFixed(1) + 'x';
}

// Compute bitrate in kbps from file object
function _fileBitrate(f) {
  const mb   = parseFloat((f.size || '0').replace(/,/g, '')) || 0;
  const secs = _parseDuration(f.duration);
  return secs > 0 ? Math.round(mb * 8192 / secs) : 0;
}

function _trackCountText(n) {
  return (n === 0 || n) ? String(n) : '—';
}

function _syncTrackCountsFromStreams(f) {
  if (!f || !f.streams) return;
  f.video_track_count = f.streams.video ? 1 : 0;
  f.audio_track_count = Array.isArray(f.streams.audio) ? f.streams.audio.length : 0;
  f.subtitle_track_count = Array.isArray(f.streams.subs) ? f.streams.subs.length : 0;
}

function _sortFiles(files) {
  const arr = [...files];
  const mul = _sortDir === 'asc' ? -1 : 1;
  if (_sortBy === 'bitrate') arr.sort((a, b) => (_fileBitrate(b) - _fileBitrate(a)) * mul);
  else if (_sortBy === 'size') arr.sort((a, b) => ((parseFloat((b.size||'0').replace(/,/g,''))||0) - (parseFloat((a.size||'0').replace(/,/g,''))||0)) * mul);
  else if (_sortBy === 'name') arr.sort((a, b) => a.name.localeCompare(b.name) * mul);
  else if (_sortBy === 'path') arr.sort((a, b) => (a.full_path || '').localeCompare(b.full_path || '') * mul);
  else if (_sortBy === 'duration') arr.sort((a, b) => (_parseDuration(b.duration) - _parseDuration(a.duration)) * mul);
  else if (_sortBy === 'est_saving')    arr.sort((a, b) => ((b.est_pct || 0) - (a.est_pct || 0)) * mul);
  else if (_sortBy === 'est_saving_mb') arr.sort((a, b) => ((b.est_mb  || 0) - (a.est_mb  || 0)) * mul);
  return arr;
}

function _updateSortDirBtn() {
  const btn = document.getElementById('sortDirBtn');
  if (!btn) return;
  if (_sortDir === 'desc') {
    btn.innerHTML = '<i class="bi bi-sort-down"></i>';
    btn.title = 'Descending — click for ascending';
  } else {
    btn.innerHTML = '<i class="bi bi-sort-up"></i>';
    btn.title = 'Ascending — click for descending';
  }
}

function toggleSortDir() {
  _sortDir = _sortDir === 'desc' ? 'asc' : 'desc';
  _updateSortDirBtn();
  setSortBy(_sortBy);
}

function jumpToActive() {
  const row = document.querySelector('#queueBody tr.tr-converting');
  if (row) row.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

function setSortBy(val) {
  _sortBy = val;
  _updateSortDirBtn();
  // Re-sort and re-render, preserving current _files order in _files
  const sorted = _sortFiles(_files);
  // Push sort order back into _files so drag picks up from here
  _files.length = 0;
  sorted.forEach(f => _files.push(f));
  populateTable(_files);
  if (_appState === 'ready') {
    document.querySelectorAll('#queueBody tr[id^="row-"]').forEach(row => {
      row.draggable = true;
      const h = row.querySelector('.drag-handle');
      if (h) h.style.opacity = '1';
    });
  }
}

// ============================================================
// UI helpers
// ============================================================
function addLog(msg, cls) {
  const box = document.getElementById('logBox');
  const div = document.createElement('div');
  div.className = 'log-' + cls;
  div.textContent = '\u25b8 ' + msg;
  box.appendChild(div);
  // Trim oldest entries so the log doesn't grow unbounded
  while (box.children.length > 200) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

// state: 'idle' | 'scanning' | 'ready' | 'running' | 'done'
function setButtonStates(state) {
  const prev = _appState;
  _appState = state;
  document.getElementById('startBtn').disabled  = state !== 'ready';
  const estimateBtn = document.getElementById('estimateBtn');
  if (estimateBtn) estimateBtn.disabled = state !== 'ready';
  document.getElementById('pauseBtn').disabled  = state !== 'running';
  document.getElementById('stopBtn').disabled   = !(state === 'running' || state === 'scanning');
  const hstopBtn = document.getElementById('hstopBtn');
  if (hstopBtn) hstopBtn.disabled = state !== 'running';
  const rescanBtn = document.getElementById('rescanBtn');
  if (rescanBtn) rescanBtn.disabled = !_currentScanPath || state === 'running';
  const cleanupBtn = document.getElementById('cleanupBtn');
  if (cleanupBtn) cleanupBtn.disabled = !_currentScanPath || state === 'running';
  const jumpBtn = document.getElementById('jumpToActiveBtn');
  if (jumpBtn) jumpBtn.classList.toggle('d-none', state !== 'running');
  if (state !== 'running') _lastAutoScrollPath = null;
  // Modal action buttons — disable Prep and Cleanup when a job is running
  const modalPrepBtn    = document.getElementById('modalPrepBtn');
  const modalCleanupBtn = document.getElementById('modalCleanupBtn');
  if (modalPrepBtn    && _selectedPath) modalPrepBtn.disabled    = state === 'running';
  if (modalCleanupBtn && _selectedPath) modalCleanupBtn.disabled = state === 'running';
  const detailsCreateBtn = document.getElementById('detailsPreviewCreateBtn');
  const detailsPlayBtn = document.getElementById('detailsPreviewPlayBtn');
  const detailsDiscardBtn = document.getElementById('detailsPreviewDiscardBtn');
  const detailsCommitBtn = document.getElementById('detailsPreviewCommitBtn');
  const detailsEngCreateBtn = document.getElementById('detailsEngStereoCreateBtn');
  const detailsEngPlayBtn = document.getElementById('detailsEngStereoPlayBtn');
  const detailsEngDiscardBtn = document.getElementById('detailsEngStereoDiscardBtn');
  const detailsEngCommitBtn = document.getElementById('detailsEngStereoCommitBtn');
  if (detailsCreateBtn) detailsCreateBtn.disabled = state === 'running' || _detailsBusy;
  if (detailsPlayBtn) detailsPlayBtn.disabled = state === 'running' || _detailsBusy || !_detailsPreviewPath;
  if (detailsDiscardBtn) detailsDiscardBtn.disabled = state === 'running' || _detailsBusy || !_detailsPreviewPath;
  if (detailsCommitBtn) detailsCommitBtn.disabled = state === 'running' || _detailsBusy || !_detailsPreviewPath;
  if (detailsEngCreateBtn) detailsEngCreateBtn.disabled = state === 'running' || _detailsBusy;
  if (detailsEngPlayBtn) detailsEngPlayBtn.disabled = state === 'running' || _detailsBusy || !_detailsEngStereoPreviewPath;
  if (detailsEngDiscardBtn) detailsEngDiscardBtn.disabled = state === 'running' || _detailsBusy || !_detailsEngStereoPreviewPath;
  if (detailsEngCommitBtn) detailsEngCommitBtn.disabled = state === 'running' || _detailsBusy || !_detailsEngStereoPreviewPath;
  // Enable drag handles only when queue is ready and not running
  const canDrag = (state === 'ready');
  document.querySelectorAll('#queueBody tr[id^="row-"]').forEach(tr => {
    tr.draggable = canDrag;
    const handle = tr.querySelector('.drag-handle');
    if (handle) handle.style.opacity = canDrag ? '1' : '0.15';
  });
  // Auto-pause estimation during conversion; resume when done/paused/ready
  if (state === 'running' && !_estAutoPaused) {
    _estAutoPaused = true;
    _updateEstStrip();
  } else if (state !== 'running' && prev === 'running' && _estAutoPaused) {
    _estAutoPaused = false;
    if (!_estRunning && !_estUserPaused && _estTotal > 0 && _estDone < _estTotal) {
      _estTick(_estPendingFiles, _estPendingIndex);
    }
    _updateEstStrip();
  }
}

// ============================================================
// Queue table
// ============================================================
function buildRow(f, index) {
  const tr = document.createElement('tr');
  tr.id = 'row-' + index;

  // Apply pre-baked state class
  if (f.status === 'done')    tr.classList.add('tr-done');
  if (f.status === 'failed')  tr.classList.add('tr-failed');
  if (f.status === 'converting') tr.classList.add('tr-converting');

  // Drag events
  tr.addEventListener('dragstart', e => {
    _dragSrcIndex = index;
    tr.classList.add('drag-dragging');
    e.dataTransfer.effectAllowed = 'move';
  });
  tr.addEventListener('dragend', () => {
    tr.classList.remove('drag-dragging');
    document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
  });
  tr.addEventListener('dragover', e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
    tr.classList.add('drag-over');
  });
  tr.addEventListener('dragleave', () => tr.classList.remove('drag-over'));
  tr.addEventListener('drop', e => {
    e.preventDefault();
    tr.classList.remove('drag-over');
    if (_dragSrcIndex === null || _dragSrcIndex === index) return;
    // Reorder _files array
    const moved = _files.splice(_dragSrcIndex, 1)[0];
    _files.splice(index, 0, moved);
    _dragSrcIndex = null;
    populateTable(_files);
    // Re-apply drag state
    document.querySelectorAll('#queueBody tr[id^="row-"]').forEach(row => {
      row.draggable = true;
      const h = row.querySelector('.drag-handle');
      if (h) h.style.opacity = '1';
    });
  });

  // col 0 — Drag handle
  const tdHandle = document.createElement('td');
  tdHandle.style.width = '20px';
  tdHandle.innerHTML = '<i class="bi bi-grip-vertical drag-handle"></i>';
  tr.appendChild(tdHandle);

  // col 1 — Folder
  const tdFolder = document.createElement('td');
  tdFolder.className = 'text-secondary text-truncate';
  tdFolder.style.maxWidth = '130px';
  tdFolder.title = f.folder || '';
  tdFolder.textContent = f.folder || '\u2014';
  tr.appendChild(tdFolder);

  // col 1 — Filename
  const tdName = document.createElement('td');
  tdName.className = 'text-truncate';
  tdName.style.maxWidth = '240px';
  tdName.title = f.name;
  tdName.textContent = f.name;
  tr.appendChild(tdName);

  // col 2 — Size
  const tdSize = document.createElement('td');
  tdSize.className = 'text-end text-secondary';
  tdSize.textContent = f.size + ' MB';
  tr.appendChild(tdSize);

  // col 3 — Bitrate
  const tdBr = document.createElement('td');
  tdBr.className = 'text-end text-secondary';
  const brVal = f.bitrate_kbps || _fileBitrate(f);
  tdBr.textContent = brVal > 0 ? (brVal >= 1000 ? (brVal/1000).toFixed(1)+' Mbps' : brVal+' kbps') : '—';
  tr.appendChild(tdBr);

  // col 4 — Codec
  const tdCodec = document.createElement('td');
  const span = document.createElement('span');
  span.className = 'codec-' + (f.codec || '').toLowerCase();
  span.textContent = f.codec || '\u2014';
  tdCodec.appendChild(span);
  tr.appendChild(tdCodec);

  // col 5 — Duration
  const tdDur = document.createElement('td');
  tdDur.className = 'text-secondary';
  tdDur.textContent = f.duration || '\u2014';
  tr.appendChild(tdDur);

  // col 5 — Status badge
  const tdStatus = document.createElement('td');
  // Suppress the primary "OCR" badge once an OCR outcome badge is available
  const _ocrDone = f.status === 'ocr' && (f.ocr_status === 'done' || f.ocr_status === 'skipped');
  tdStatus.innerHTML = (_ocrDone ? '' : _badgeHtml(f.status, f.force_sw, f.force_convert)) + _droppedBadgeHtml(f) + _ocrBadgeHtml(f);
  tr.appendChild(tdStatus);

  // col 6 — Video tracks
  const tdVTracks = document.createElement('td');
  tdVTracks.className = 'text-end text-secondary';
  tdVTracks.textContent = _trackCountText(f.video_track_count);
  tr.appendChild(tdVTracks);

  // col 7 — Audio tracks
  const tdATracks = document.createElement('td');
  tdATracks.className = 'text-end text-secondary';
  tdATracks.textContent = _trackCountText(f.audio_track_count);
  tr.appendChild(tdATracks);

  // col 8 — Subtitle tracks
  const tdSTracks = document.createElement('td');
  tdSTracks.className = 'text-end text-secondary';
  tdSTracks.textContent = _trackCountText(f.subtitle_track_count);
  tr.appendChild(tdSTracks);

  // col 9 — Est. saving
  const tdEst = document.createElement('td');
  tdEst.className = 'text-end';
  tdEst.id = 'est-' + index;
  const estHtml = _estimateHtml(f);
  if (estHtml) {
    tdEst.innerHTML = estHtml;
  } else if (f.status === 'done' || f.status === 'failed' || f.status === 'no_saving' || f.status === 'low_savings' || f.status === 'skipped') {
    tdEst.textContent = '\u2014';
  } else {
    tdEst.innerHTML = '<span class="text-secondary" style="font-size:.75rem">\u2026</span>';
  }
  tr.appendChild(tdEst);

  // col 10 — Output
  const tdOut = document.createElement('td');
  tdOut.className = 'text-end';
  tdOut.textContent = f.output ? f.output + ' MB' : '';
  tr.appendChild(tdOut);

  // col 11 — Saved
  const tdSaved = document.createElement('td');
  tdSaved.className = 'text-end text-success';
  tdSaved.textContent = f.saved ? f.saved + ' MB' : '';
  tr.appendChild(tdSaved);

  // col 12 — %
  const tdPct = document.createElement('td');
  tdPct.className = 'text-end';
  if (f.pct) tdPct.innerHTML = '<strong>' + f.pct + '%</strong>';
  tr.appendChild(tdPct);

  // col 13 — Conversion speed
  const tdSpeed = document.createElement('td');
  tdSpeed.className = 'text-end text-secondary';
  const speedX = _conversionSpeedX(_parseDuration(f.duration), f.conv_secs || 0);
  tdSpeed.textContent = speedX > 0 ? _formatSpeedX(speedX) : '';
  tr.appendChild(tdSpeed);

  // col 14 — Conversion time
  const tdTime = document.createElement('td');
  tdTime.className = 'text-end text-secondary';
  tdTime.style.whiteSpace = 'nowrap';
  tdTime.textContent = f.conv_secs ? _fmtDuration(f.conv_secs) : '';
  tr.appendChild(tdTime);

  // col 15 — Actions
  const tdAct = document.createElement('td');
  tdAct.style.width = '32px';
  const btn = document.createElement('button');
  btn.className = 'row-menu-btn';
  btn.title = 'Actions';
  btn.innerHTML = '<i class="bi bi-three-dots-vertical"></i>';
  btn.addEventListener('click', e => { e.stopPropagation(); showRowMenu(index, btn); });
  tdAct.appendChild(btn);
  tr.appendChild(tdAct);

  // Disable drag until state is ready
  tr.draggable = (_appState === 'ready');

  return tr;
}

// Append only the new rows (without touching existing ones).
// Used by the SSE scan path for incremental rendering.
function _appendRows(newFiles, startIndex) {
  const tbody = document.getElementById('queueBody');
  // Remove the placeholder row if it is still there
  const placeholder = tbody.querySelector('td[colspan]');
  if (placeholder) tbody.innerHTML = '';
  newFiles.forEach((f, i) => {
    if (!f.bitrate_kbps) f.bitrate_kbps = _fileBitrate(f);
    tbody.appendChild(buildRow(f, startIndex + i));
  });
  applyFilter();
}

// Fill in codec / bitrate / duration / track-count cells once a probe result arrives.
function _updateRowProbe(idx, f) {
  const tr = document.getElementById('row-' + idx);
  if (!tr) return;
  const cells = tr.querySelectorAll('td');
  // Column order matches buildRow:
  // 0=handle 1=folder 2=name 3=size 4=bitrate 5=codec 6=duration 7=status 8=vtracks 9=atracks 10=stracks 11=est …
  const tdBr    = cells[4];
  const tdCodec = cells[5];
  const tdDur   = cells[6];
  const tdVTracks = cells[8];
  const tdATracks = cells[9];
  const tdSTracks = cells[10];
  if (tdBr) {
    const br = f.bitrate_kbps || 0;
    tdBr.textContent = br > 0 ? (br >= 1000 ? (br/1000).toFixed(1)+' Mbps' : br+' kbps') : '—';
  }
  if (tdCodec) {
    const span = document.createElement('span');
    span.className = 'codec-' + (f.codec || '').toLowerCase();
    span.textContent = f.codec || '—';
    tdCodec.textContent = '';
    tdCodec.appendChild(span);
  }
  if (tdDur) {
    tdDur.textContent = f.duration || '—';
  }
  if (tdVTracks) {
    tdVTracks.textContent = _trackCountText(f.video_track_count);
  }
  if (tdATracks) {
    tdATracks.textContent = _trackCountText(f.audio_track_count);
  }
  if (tdSTracks) {
    tdSTracks.textContent = _trackCountText(f.subtitle_track_count);
  }
  // Scroll to keep the probed row visible (instant — no smooth-scroll jitter)
  if (_probeDone < _probeTotal) {
    tr.scrollIntoView({ block: 'nearest', behavior: 'instant' });
  }
}

function populateTable(files) {
  const tbody = document.getElementById('queueBody');
  tbody.innerHTML = '';
  if (files.length === 0) {
    tbody.innerHTML = '<tr><td colspan="' + _QUEUE_COL_COUNT + '" class="text-center text-secondary py-4">No video files found in the selected folder.</td></tr>';
    return;
  }
  // Attach computed bitrate
  files.forEach(f => { if (!f.bitrate_kbps) f.bitrate_kbps = _fileBitrate(f); });
  files.forEach((f, i) => tbody.appendChild(buildRow(f, i)));
  applyFilter();
}

function updateStats(files) {
  const totalMB  = files.reduce((s, f) => s + (parseFloat(f.size.replace(/,/g, '')) || 0), 0);
  const doneAll   = files.filter(f => f.status === 'done').length;
  const doneSession = files.filter(f => f.status === 'done' && f.session_done).length;
  const done      = doneAll - doneSession;
  const failed    = files.filter(f => f.status === 'failed').length;
  const noSaving  = files.filter(f => f.status === 'no_saving').length;
  const skipped    = files.filter(f => f.status === 'skipped').length;
  const pending   = files.filter(f => f.status === 'pending' || f.status === 'ocr').length;
  const savedMB  = files.reduce((s, f) => s + (f.saved  ? parseFloat(f.saved.replace(/,/g, ''))  || 0 : 0), 0);
  const origMB   = files.filter(f => f.status === 'done')
                        .reduce((s, f) => s + (parseFloat(f.size.replace(/,/g, '')) || 0), 0);
  const donePct  = files.length ? Math.round(doneAll / files.length * 100) : 0;
  const failPct  = files.length ? Math.round(failed / files.length * 100) : 0;
  const origTotalMB = origMB + savedMB; // output size + saved = original size
  const avgRatio = origTotalMB > 0 ? Math.round(savedMB / origTotalMB * 100) : 0;

  // Values
  document.getElementById('statTotal').textContent  = files.length;
  document.getElementById('statDone').textContent   = doneAll;
  document.getElementById('statFailed').textContent = failed;
  document.getElementById('statSaved').textContent  = savedMB > 0 ? (savedMB / 1024).toFixed(1) + ' GB' : '—';

  // Sub-labels
  document.getElementById('statTotalSub').textContent  = totalMB > 0 ? (totalMB / 1024).toFixed(1) + ' GB · ' + pending + ' remaining' : '—';
  document.getElementById('statDoneSub').textContent   = donePct  > 0 ? donePct  + '% of queue complete' : '—';
  document.getElementById('statSavedSub').textContent  = avgRatio > 0 ? 'avg ' + avgRatio + '% savings' : '—';
  document.getElementById('statFailedSub').textContent = failed   > 0 ? failPct + '% failure rate' : 'No failures';

  // Progress strips
  document.getElementById('statTotalBar').style.width  = files.length ? '100%'       : '0%';
  document.getElementById('statDoneBar').style.width   = donePct + '%';
  document.getElementById('statSavedBar').style.width  = avgRatio + '%';
  document.getElementById('statFailedBar').style.width = failPct  + '%';

  // Right panel
  const overallPct = files.length ? Math.round((doneAll + failed) / files.length * 100) : 0;
  const overallPctEl = document.getElementById('overallPct');
  const overallBarEl = document.getElementById('overallBar');
  if (overallPctEl) overallPctEl.textContent = overallPct + '%';
  if (overallBarEl) overallBarEl.style.width = overallPct + '%';
  document.getElementById('totalSizeLabel').textContent = (totalMB / 1024).toFixed(1) + ' GB total';
  const lowSavings = files.filter(f => f.status === 'low_savings').length;
  // Filter chip counts
  document.getElementById('chipCount-pending').textContent      = pending;
  const dseEl = document.getElementById('chipCount-done_session');
  if (dseEl) dseEl.textContent = doneSession;
  document.getElementById('chipCount-done').textContent         = done;
  document.getElementById('chipCount-failed').textContent       = failed;
  const nsEl = document.getElementById('chipCount-no-saving');
  if (nsEl) nsEl.textContent = noSaving;
  const skEl = document.getElementById('chipCount-skipped');
  if (skEl) skEl.textContent = skipped;
  const lsEl = document.getElementById('chipCount-low_savings');
  if (lsEl) lsEl.textContent = lowSavings;
}

function _refreshQueueStateFromFiles() {
  const hasRunnable = _files.some(f => f.status === 'pending' || f.status === 'failed');
  if (_files.length === 0) {
    setButtonStates('idle');
  } else if (hasRunnable) {
    setButtonStates('ready');
  } else {
    setButtonStates('done');
  }
}

// ============================================================
// Filter + Search
// ============================================================
function toggleStatus(s) {
  if (_activeStatuses.has(s)) {
    _activeStatuses.delete(s);
  } else {
    _activeStatuses.add(s);
  }
  document.getElementById('chip-' + s).classList.toggle('active', _activeStatuses.has(s));
  applyFilter();
}

function resetStatusFilter() {
  _activeStatuses = new Set(_DEFAULT_ACTIVE_STATUSES);
  _ALL_STATUSES.forEach(s => {
    document.getElementById('chip-' + s).classList.toggle('active', _activeStatuses.has(s));
  });
  applyFilter();
}

function onSearchInput(val) {
  _searchQuery = val.toLowerCase().trim();
  applyFilter();
}

function _fileMatchesFilter(f) {
  const _eff = (f.status === 'ocr' || f.status === 'converting') ? 'pending' : f.status;
  let matchStatus;
  if (_eff === 'done' && f.session_done) {
    matchStatus = _activeStatuses.has('done') || _activeStatuses.has('done_session');
  } else {
    matchStatus = _activeStatuses.has(_eff);
  }
  const matchSearch = !_searchQuery ||
    f.name.toLowerCase().includes(_searchQuery) ||
    (f.folder || '').toLowerCase().includes(_searchQuery);
  const sizeMB = parseFloat((f.size || '0').replace(/,/g, '')) || 0;
  const kbps   = f.bitrate_kbps || _fileBitrate(f);
  const durMin = _parseDuration(f.duration) / 60;
  const matchSize  = (!_filterSizeMin || sizeMB >= +_filterSizeMin) &&
                     (!_filterSizeMax || sizeMB <= +_filterSizeMax);
  const matchBr    = (!_filterBrMin   || kbps   >= +_filterBrMin)   &&
                     (!_filterBrMax   || kbps   <= +_filterBrMax);
  const matchDur   = (!_filterDurMin  || durMin >= +_filterDurMin)  &&
                     (!_filterDurMax  || durMin <= +_filterDurMax);
  const matchCodec = !_filterCodec ||
                     (f.codec || '').toLowerCase().includes(_filterCodec);
  return matchStatus && matchSearch && matchSize && matchBr && matchDur && matchCodec;
}

function _updateSessionCard() {
  const el = document.getElementById('statSession');
  const sub = document.getElementById('statSessionSub');
  const est = document.getElementById('statSessionEst');
  const bar = document.getElementById('statSessionBar');
  if (!el) return;
  if (_sessionSavedMB === null) {
    el.textContent  = '—';
    if (sub) sub.textContent = 'No conversions yet';
    if (est) est.textContent = '';
    if (bar) bar.style.width = '0%';
  } else {
    el.textContent = _fmtSavedShort(_sessionSavedMB);
    if (sub) {
      const fileLabel = _sessionProcessed === 1 ? '1 file' : _sessionProcessed + ' files';
      let _avgSpeed = '';
      const sessionDone = _files.filter(f => !!f.session_done);
      const totalDur = sessionDone.reduce((sum, f) => sum + _parseDuration(f.duration), 0);
      const totalConv = sessionDone.reduce((sum, f) => sum + (Number(f.conv_secs || 0)), 0);
      const avgX = _conversionSpeedX(totalDur, totalConv);
      if (avgX > 0) _avgSpeed = ' · avg ' + _formatSpeedX(avgX);
      sub.textContent = (_sessionProcessed > 0 ? fileLabel + ' processed · realized' : 'this run · realized') + _avgSpeed;
    }
    if (est) {
      est.textContent = _sessionEstimatedMB > 0
        ? ('In-progress est: +' + _fmtSavedShort(_sessionEstimatedMB))
        : 'In-progress est: —';
    }
    // Bar: proportional to total saved card's bar (cap at 100%)
    // Use ratio vs total saved for relative sense, or just show fill capped at bar width
    if (bar) {
      const totalEl  = document.getElementById('statSaved');
      const totalTxt = totalEl ? totalEl.textContent : '';
      const totalMB  = totalTxt.endsWith('GB')
        ? parseFloat(totalTxt) * 1024
        : parseFloat(totalTxt) || 0;
      bar.style.width = (totalMB > 0 ? Math.min(100, Math.round(_sessionSavedMB / totalMB * 100)) : 0) + '%';
    }
  }
}

function _startElapsedTimer(startTs) {
  _sessionStartedAt = startTs;
  if (_sessionElapsedTimer) clearInterval(_sessionElapsedTimer);
  const _tick = () => {
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - _sessionStartedAt));
    const formatted = _fmtDuration(secs);
    const el2 = document.getElementById('statSessionElapsed');
    if (el2) el2.textContent = formatted;
    const lbl = document.getElementById('statSessionElapsedLabel');
    if (lbl) lbl.textContent = 'elapsed';
  };
  _tick();
  _sessionElapsedTimer = setInterval(_tick, 1000);
}

function _stopElapsedTimer() {
  if (_sessionElapsedTimer) { clearInterval(_sessionElapsedTimer); _sessionElapsedTimer = null; }
  const jel = document.getElementById('jobElapsedVal');
  if (jel) jel.textContent = '—';
}

function _startFileElapsedTimer(fileStartTs) {
  if (_fileElapsedTimer) clearInterval(_fileElapsedTimer);
  const _tick = () => {
    const el = document.getElementById('elapsedVal');
    if (el) el.textContent = _fmtDuration(Math.max(0, Date.now() / 1000 - fileStartTs));
  };
  _tick();
  _fileElapsedTimer = setInterval(_tick, 1000);
}

function _stopFileElapsedTimer() {
  if (_fileElapsedTimer) { clearInterval(_fileElapsedTimer); _fileElapsedTimer = null; }
  const el = document.getElementById('elapsedVal');
  if (el) el.textContent = '—';
  const sp = document.getElementById('speedVal');
  if (sp) sp.textContent = '—';
}

function applyFilter() {
  const tbody = document.getElementById('queueBody');
  // Separate matching vs non-matching, preserving current _files order (which
  // reflects the last sort applied by setSortBy / populateTable).
  const matching    = [];
  const nonMatching = [];
  _files.forEach((f, i) => {
    const row = document.getElementById('row-' + i);
    if (!row) return;
    if (_fileMatchesFilter(f)) {
      row.style.display = '';
      matching.push(row);
    } else {
      row.style.display = 'none';
      nonMatching.push(row);
    }
  });
  // Re-append matching rows first (in sort order), then hidden ones at the end
  // so the DOM order matches the active sort when a filter is applied.
  matching.forEach(r => tbody.appendChild(r));
  nonMatching.forEach(r => tbody.appendChild(r));
  const countEl = document.getElementById('queueCountLabel');
  if (countEl) {
    const total = matching.length + nonMatching.length;
    countEl.textContent = (nonMatching.length > 0 || _searchQuery || _activeStatuses.size < 7)
      ? `· ${matching.length} / ${total}`
      : '';
  }
}

function toggleFilterBar() {
  const bar = document.getElementById('filterBar');
  if (!bar) return;
  bar.classList.toggle('d-none');
}

function onFilterChange() {
  _filterSizeMin = document.getElementById('fSizeMin').value;
  _filterSizeMax = document.getElementById('fSizeMax').value;
  _filterBrMin   = document.getElementById('fBrMin').value;
  _filterBrMax   = document.getElementById('fBrMax').value;
  _filterDurMin  = document.getElementById('fDurMin').value;
  _filterDurMax  = document.getElementById('fDurMax').value;
  _filterCodec   = (document.getElementById('fCodec').value || '').toLowerCase().trim();
  const hasFilter = _filterSizeMin || _filterSizeMax || _filterBrMin || _filterBrMax || _filterDurMin || _filterDurMax || _filterCodec;
  const btn = document.getElementById('filterToggleBtn');
  if (btn) btn.classList.toggle('active', !!hasFilter);
  applyFilter();
}

function clearRangeFilters() {
  ['fSizeMin','fSizeMax','fBrMin','fBrMax','fDurMin','fDurMax','fCodec'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  onFilterChange();
}

// ============================================================
// Scan  (live /api/scan SSE)
// ============================================================
const DEMO_STREAMS = {
  h264_typical: {
    video: { codec: 'h264', profile: 'High', level: '4.1', resolution: '1920×1080', fps: '23.976', bitrate: '5,800 kbps', color: 'yuv420p', hdr: false },
    audio: [
      { track: 1, codec: 'AAC', channels: '2.0 Stereo', language: 'jpn', bitrate: '160 kbps', title: 'Japanese' },
      { track: 2, codec: 'AAC', channels: '2.0 Stereo', language: 'eng', bitrate: '160 kbps', title: 'English Dub' },
    ],
    subs: [
      { track: 1, codec: 'ASS', language: 'eng', title: 'English (Full)' },
      { track: 2, codec: 'ASS', language: 'eng', title: 'English (Signs)' },
    ],
  },
  hevc_done: {
    video: { codec: 'hevc', profile: 'Main', level: '4.1', resolution: '1920×1080', fps: '23.976', bitrate: '2,400 kbps', color: 'yuv420p', hdr: false },
    audio: [
      { track: 1, codec: 'AAC', channels: '2.0 Stereo', language: 'jpn', bitrate: '160 kbps', title: 'Japanese' },
    ],
    subs: [
      { track: 1, codec: 'ASS', language: 'eng', title: 'English (Full)' },
    ],
  },
  movie_mkv: {
    video: { codec: 'h264', profile: 'High', level: '5.1', resolution: '1920×1080', fps: '23.976', bitrate: '12,400 kbps', color: 'yuv420p', hdr: false },
    audio: [
      { track: 1, codec: 'DTS-HD MA', channels: '5.1 Surround', language: 'jpn', bitrate: '3,072 kbps', title: 'Japanese' },
      { track: 2, codec: 'AC3',       channels: '5.1 Surround', language: 'eng', bitrate: '640 kbps',   title: 'English Dub' },
    ],
    subs: [
      { track: 1, codec: 'PGS', language: 'eng', title: 'English' },
      { track: 2, codec: 'PGS', language: 'spa', title: 'Spanish' },
    ],
  },
};

const DEMO_FILES = [
  { folder: '',         name: 'One.Piece.E1050.mp4',    size: '1,105', codec: 'H264', duration: '24:12', status: 'done',    output: '298',   saved: '807',   pct: '73', full_path: 'D:/Anime/One.Piece.E1050.mp4',    output_path: 'D:/Anime/One.Piece.E1050_hevc.mp4', streams: DEMO_STREAMS.h264_typical },
  { folder: '',         name: 'One.Piece.E1051.mp4',    size: '1,098', codec: 'H264', duration: '24:05', status: 'done',    output: '312',   saved: '786',   pct: '72', full_path: 'D:/Anime/One.Piece.E1051.mp4',    output_path: 'D:/Anime/One.Piece.E1051_hevc.mp4', streams: DEMO_STREAMS.h264_typical },
  { folder: 'Season 2', name: 'Blue.Lock.E24.mp4',      size: '1,876', codec: 'H264', duration: '23:45', status: 'done',    output: '521',   saved: '1,355', pct: '72', full_path: 'D:/Anime/Season 2/Blue.Lock.E24.mp4', output_path: 'D:/Anime/Season 2/Blue.Lock.E24_hevc.mp4', streams: DEMO_STREAMS.h264_typical },
  { folder: 'Season 2', name: 'Blue.Lock.E25.mp4',      size: '1,743', codec: 'HEVC', duration: '22:58', status: 'done',    output: '501',   saved: '1,242', pct: '71', full_path: 'D:/Anime/Season 2/Blue.Lock.E25.mp4', output_path: 'D:/Anime/Season 2/Blue.Lock.E25_hevc.mp4', streams: DEMO_STREAMS.hevc_done },
  { folder: 'Season 2', name: 'Blue.Lock.E26.mp4',      size: '1,811', codec: 'H264', duration: '24:01', status: 'failed',  output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Season 2/Blue.Lock.E26.mp4', output_path: null, ffmpeg_cmd: 'ffmpeg -y -i "D:/Anime/Season 2/Blue.Lock.E26.mp4" -c:v hevc_qsv ...', error_tail: 'Error initializing output stream 0:0 -- Error while opening encoder\nConversion failed!', streams: DEMO_STREAMS.h264_typical },
  { folder: 'Movies',  name: 'Vinland.Saga.Movie.mkv', size: '8,231', codec: 'H264', duration: '1:52:04', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Movies/Vinland.Saga.Movie.mkv', output_path: null, streams: DEMO_STREAMS.movie_mkv },
  { folder: 'Movies',  name: 'Steins.Gate.Movie.mkv',  size: '6,504', codec: 'H264', duration: '1:32:44', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Movies/Steins.Gate.Movie.mkv',  output_path: null, streams: DEMO_STREAMS.movie_mkv },
  { folder: 'Extras',  name: 'Steins.Gate.E01.mkv',    size: '2,203', codec: 'H264', duration: '23:23', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Extras/Steins.Gate.E01.mkv', output_path: null, streams: DEMO_STREAMS.h264_typical },
  { folder: 'Extras',  name: 'Steins.Gate.E02.mkv',    size: '2,187', codec: 'H264', duration: '23:15', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Extras/Steins.Gate.E02.mkv', output_path: null, streams: DEMO_STREAMS.h264_typical },
  { folder: '',         name: 'Jujutsu.Kaisen.E01.mkv', size: '1,456', codec: 'H264', duration: '24:30', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Jujutsu.Kaisen.E01.mkv',   output_path: null, streams: DEMO_STREAMS.h264_typical },
];

function scanFolder(path) {
  _currentScanPath = path;
  localStorage.setItem('vc_last_folder', path);  // remember for re-scan and cleanup
  const rescanBtn = document.getElementById('rescanBtn');
  if (rescanBtn) rescanBtn.disabled = false;
  const cleanupBtn2 = document.getElementById('cleanupBtn');
  if (cleanupBtn2) cleanupBtn2.disabled = false;
  _estCancelled  = true;   // stop any in-flight estimation from previous scan
  _estAutoPaused = false;
  _estRunning    = false;
  const estStrip = document.getElementById('estStrip');
  if (estStrip) estStrip.classList.add('d-none');
  _files = [];
  _fileIndexByPath = {};
  _probeTotal = 0;
  _probeDone  = 0;
  _hashTotal  = 0;
  _hashDone   = 0;
  _hashRemoved = 0;
  _probePhaseTotal = 0;
  _probePhaseDone  = 0;
  _sessionSavedMB = null;
  _sessionEstimatedMB = 0;
  _sessionProcessed = 0;
  _updateSessionCard();
  _scanStripPhase1();
  _searchQuery   = '';
  _filterSizeMin = ''; _filterSizeMax = '';
  _filterBrMin   = ''; _filterBrMax   = '';
  _filterDurMin  = ''; _filterDurMax  = '';
  _filterCodec   = '';
  const sb = document.getElementById('searchBox');
  if (sb) sb.value = '';
  ['fSizeMin','fSizeMax','fBrMin','fBrMax','fDurMin','fDurMax','fCodec'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const filterBtn = document.getElementById('filterToggleBtn');
  if (filterBtn) filterBtn.classList.remove('active');
  const ss = document.getElementById('sortSelect');
  if (ss) ss.value = _sortBy;
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  resetStatusFilter();
  setButtonStates('scanning');
  document.getElementById('queueBody').innerHTML =
    '<tr><td colspan="' + _QUEUE_COL_COUNT + '" class="text-center text-secondary py-4">' +
    '<div class="spinner-border spinner-border-sm me-2"></div>Scanning for video files\u2026</td></tr>';
  document.getElementById('totalSizeLabel').textContent = 'Scanning\u2026';
  addLog('Scanning: ' + path, 'info');

  if (_scanEs) { _scanEs.close(); _scanEs = null; }
  _scanEs = new EventSource('/api/scan?path=' + encodeURIComponent(path));

  _scanEs.onmessage = function(e) {
    const msg = JSON.parse(e.data);
    if (msg.type === 'folder') {
      const startIdx = _files.length;
      msg.files.forEach((f, i) => {
        _fileIndexByPath[f.full_path] = startIdx + i;
        _files.push(f);
      });
      _appendRows(msg.files, startIdx);
      updateStats(_files);
      _scanStripPhase1(_files.length);
    } else if (msg.type === 'probe') {
      if (_probePhaseTotal === 0) {
        _scanStripPhase2();
        // Probe denominator excludes the hashing stage and files removed there.
        _probePhaseTotal = Math.max(0, _probeTotal - _hashTotal - _hashRemoved);
      }
      const idx = _fileIndexByPath[msg.full_path];
      if (idx === undefined) {
        _probeDone++;
        _probePhaseDone++;
        _scanStripProbeProgress('Probing');
        return;
      }
      const f = _files[idx];
      f.codec        = msg.codec;
      f.duration     = msg.duration;
      f.is_hi10      = msg.is_hi10;
      f.streams      = msg.streams;
      f.video_track_count = msg.video_track_count;
      f.audio_track_count = msg.audio_track_count;
      f.subtitle_track_count = (msg.subtitle_track_count != null)
        ? msg.subtitle_track_count
        : ((msg.streams && Array.isArray(msg.streams.subs)) ? msg.streams.subs.length : f.subtitle_track_count);
      f.bitrate_kbps = msg.bitrate_kbps || (msg.streams && msg.streams.video
        ? Math.round((msg.streams.video.bitrate || 0) / 1000) : 0);
      _updateRowProbe(idx, f);
      _probeDone++;
      _probePhaseDone++;
      _scanStripProbeProgress('Probing');
    } else if (msg.type === 'hash_progress') {
      _probeDone = msg.done || 0;
      _hashDone = msg.done || 0;
      _scanStripHashProgress();
    } else if (msg.type === 'remove') {
      const _inHashPhase = _hashTotal > 0 && _hashDone < _hashTotal;
      const idx = _fileIndexByPath[msg.full_path];
      if (idx === undefined) return;
      _files.splice(idx, 1);
      // Rebuild index map after removal
      _fileIndexByPath = {};
      _files.forEach((f, i) => { _fileIndexByPath[f.full_path] = i; });
      const tr = document.getElementById('row-' + idx);
      if (tr) tr.remove();
      // Re-index remaining row id attributes
      document.querySelectorAll('#queueBody tr[id^="row-"]').forEach((row, i) => {
        row.id = 'row-' + i;
      });
      if (_inHashPhase) {
        _hashRemoved++;
        _scanStripHashProgress();
      } else {
        if (_probePhaseTotal > 0) _probePhaseTotal--;
        _scanStripProbeProgress('Probing');
      }
      updateStats(_files);
    } else if (msg.type === 'scan_done') {
      // Phase 1 complete — grid may have buffered files still awaiting probe.
      // Enable actions immediately; Phase 2 (ffprobe) streams the rest.
      const totalGB = (msg.total_mb / 1024).toFixed(1);
      document.getElementById('totalSizeLabel').textContent = totalGB + ' GB total';
      _probeTotal = msg.total_files;  // only files actually needing Phase 2/3 probe
      _hashTotal  = msg.hash_files || 0;
      _hashDone = 0;
      _hashRemoved = 0;
      _probePhaseTotal = 0;
      _probePhaseDone = 0;
      const pendingCount = _files.filter(f => f.status === 'pending' || f.status === 'failed' || !f.status).length;
      if (_files.length === 0) {
        document.getElementById('queueBody').innerHTML =
          '<tr><td colspan="' + _QUEUE_COL_COUNT + '" class="text-center text-secondary py-4">' +
          'No video files found.</td></tr>';
        addLog('Scan complete \u2014 no video files found.', 'ok');
        setButtonStates('idle');
        _scanEs.close(); _scanEs = null;
        _scanStripHide();
      } else if (pendingCount === 0) {
        addLog('Found ' + _files.length + ' files \u2014 all already converted.', 'ok');
        setButtonStates('idle');
        if (_probeTotal === 0) { _scanEs.close(); _scanEs = null; _scanStripHide(); setSortBy(_sortBy); }
        else { _scanStripPhase2(); }
      } else {
        const cached = _files.length - _probeTotal;
        const cacheNote = cached > 0 ? ' (\u202f' + cached + ' cached)' : '';
        if (_probeTotal > 0) {
          addLog('Found ' + _files.length + ' files \u2014 ' + totalGB + ' GB' + cacheNote + ' \u2014 hashing/probing\u2026', 'ok');
          _scanStripPhase2();
        } else {
          addLog('Found ' + _files.length + ' files \u2014 ' + totalGB + ' GB \u2014 all probe data cached.', 'ok');
          setSortBy(_sortBy);
        }
        setButtonStates('ready');
      }
    } else if (msg.type === 'done') {
      // Phase 3 complete — re-render with sort applied now that bitrates are known
      _scanEs.close(); _scanEs = null;
      _scanStripHide();
      const totalGB = (msg.total_mb / 1024).toFixed(1);
      document.getElementById('totalSizeLabel').textContent = totalGB + ' GB total';
      setSortBy(_sortBy);
    } else if (msg.type === 'warning') {
      addLog('Skipped: ' + msg.message, 'warn');
    } else if (msg.type === 'error') {
      _scanEs.close(); _scanEs = null;
      _scanStripHide();
      addLog('Scan error: ' + msg.message, 'err');
      setButtonStates('idle');
    }
  };

  _scanEs.onerror = function() {
    if (_scanEs) { _scanEs.close(); _scanEs = null; }
    _scanStripHide();
    addLog('Scan connection lost.', 'err');
    setButtonStates('idle');
  };
}

// ============================================================
// Scan strip helpers
// ============================================================
function _scanStripPhase1(count) {
  const strip = document.getElementById('scanStrip');
  const label = document.getElementById('scanStripLabel');
  const bar   = document.getElementById('scanBar');
  const icon  = document.getElementById('scanStripIcon');
  if (!strip) return;
  strip.classList.remove('d-none', 'scan-probing');
  icon.className = 'bi bi-folder2-open est-strip-icon';
  label.textContent = count > 0 ? 'Scanning\u2026 ' + count + ' files found' : 'Scanning\u2026';
  // Indeterminate: animate bar between 5% and 25% to show activity
  bar.style.transition = 'none';
  bar.style.width = '5%';
  requestAnimationFrame(() => {
    bar.style.transition = 'width 1.8s ease-in-out';
    bar.style.width = '25%';
  });
}
function _scanStripPhase2() {
  const strip = document.getElementById('scanStrip');
  const icon  = document.getElementById('scanStripIcon');
  if (!strip) return;
  strip.classList.add('scan-probing');
  icon.className = 'bi bi-cpu est-strip-icon';
  // Looping indeterminate bar until the first hash/probe progress result arrives
  const label = document.getElementById('scanStripLabel');
  const bar   = document.getElementById('scanBar');
  if (label) label.textContent = _hashTotal > 0 ? 'Hashing\u2026' : 'Probing\u2026';
  if (bar) bar.classList.add('scan-bar-indeterminate');
}
function _scanStripHashProgress() {
  const label = document.getElementById('scanStripLabel');
  const bar   = document.getElementById('scanBar');
  if (!label || !bar) return;
  if (_hashDone === 0 || _hashTotal === 0) return;
  bar.classList.remove('scan-bar-indeterminate');
  const pct = Math.round((_hashDone / _hashTotal) * 100);
  bar.style.transition = 'width .3s ease';
  bar.style.width = pct + '%';
  label.textContent = 'Hashing ' + _hashDone + '\u202f/\u202f' + _hashTotal + '\u2026';
}
function _scanStripProbeProgress(phaseLabel) {
  const label = document.getElementById('scanStripLabel');
  const bar   = document.getElementById('scanBar');
  if (!label || !bar) return;
  if (_probePhaseDone === 0) return;  // no granular probe progress yet
  // First real progress event — stop the looping animation
  bar.classList.remove('scan-bar-indeterminate');
  const total = _probePhaseTotal > 0 ? _probePhaseTotal : _probeTotal;
  const pct = total > 0 ? Math.round(_probePhaseDone / total * 100) : 0;
  bar.style.transition = 'width .3s ease';
  bar.style.width = pct + '%';
  const _phase = phaseLabel || 'Probing';
  label.textContent = _phase + ' ' + _probePhaseDone + '\u202f/\u202f' + total + '\u2026';
}
function _scanStripHide() {
  const strip = document.getElementById('scanStrip');
  if (strip) strip.classList.add('d-none');
}

// ============================================================
// Estimation background task
// ============================================================
function _shouldEstimate() {
  return !_estUserPaused && !_estAutoPaused && !_estCancelled;
}

function _updateEstStrip() {
  const strip = document.getElementById('estStrip');
  if (!strip) return;
  const label = document.getElementById('estStripLabel');
  const bar   = document.getElementById('estBar');
  const btn   = document.getElementById('estStripBtn');
  const icon  = document.getElementById('estStripIcon');
  if (_estDone >= _estTotal && _estTotal > 0) {
    strip.classList.add('d-none');
    return;
  }
  strip.classList.remove('d-none');
  const pct = _estTotal > 0 ? Math.round(_estDone / _estTotal * 100) : 0;
  bar.style.width = pct + '%';
  const isPaused = _estUserPaused || _estAutoPaused;
  if (isPaused) {
    const reason = _estAutoPaused ? ' (converting)' : '';
    label.textContent = 'Estimation paused' + reason + ' — ' + _estDone + ' / ' + _estTotal;
    btn.className  = 'bi bi-play-fill est-strip-btn';
    icon.className = 'bi bi-pause-circle est-strip-icon';
    strip.classList.add('est-paused');
  } else {
    label.textContent = 'Estimating ' + _estDone + ' / ' + _estTotal + '\u2026';
    btn.className  = 'bi bi-pause-fill est-strip-btn';
    icon.className = 'bi bi-hourglass-split est-strip-icon';
    strip.classList.remove('est-paused');
  }
}

function toggleEstimation() {
  _estUserPaused = !_estUserPaused;
  _updateEstStrip();
  if (!_estUserPaused && !_estRunning) _estTick(_estPendingFiles, _estPendingIndex);
}

// These are set by runEstimation so toggleEstimation can restart the chain
let _estPendingFiles = [];
let _estPendingIndex = 0;

function runEstimation(files) {
  const pending = files.filter(f => (f.status === 'pending' || f.status === 'failed') && f.full_path);
  _estCancelled  = false;
  _estDone       = 0;
  _estTotal      = pending.length;
  _estLastFolder = null;
  _estPendingFiles = pending;
  _estPendingIndex = 0;
  _updateEstStrip();
  if (_estTotal === 0) return;
  _estTick(pending, 0);
}

function _estTick(pending, i) {
  if (_estCancelled || i >= pending.length) {
    _estRunning = false;
    _updateEstStrip();
    return;
  }
  if (!_shouldEstimate()) {
    _estRunning = false;          // chain goes dormant; toggleEstimation or setButtonStates will restart
    _estPendingIndex = i;         // save position
    _updateEstStrip();
    return;
  }
  _estRunning = true;
  _estPendingIndex = i;
  const f   = pending[i];
  const idx = _files.indexOf(f);
  const cell = document.getElementById('est-' + idx);
  fetch('/api/estimate?path=' + encodeURIComponent(f.full_path))
    .then(r => r.json())
    .then(data => {
      // Store back into _files so sort-by-estimated-savings can use it
      if (idx !== undefined && _files[idx]) {
        _files[idx].est_pct = data.error ? 0 : (data.estimated_saving_pct || 0);
        _files[idx].est_mb  = data.error ? 0 : (data.estimated_saving_mb  || 0);
        _files[idx].est_cv  = data.error ? null : (data.sample_cv_pct ?? null);
        _files[idx].est_high_variance = data.error ? false : !!data.high_variance;
        _files[idx].est_aggregation = data.error ? null : (data.aggregation || null);
      }
      if (cell) {
        if (data.error) {
          cell.innerHTML = '<span class="text-secondary">—</span>';
        } else if (idx !== undefined && _files[idx]) {
          cell.innerHTML = _estimateHtml(_files[idx]);
        }
      }
      // If currently sorted by est_saving, re-apply the sort live so the table
      // reorders itself as each result arrives.
      if (_sortBy === 'est_saving' || _sortBy === 'est_saving_mb') setSortBy(_sortBy);
      _estDone++;
      _updateEstStrip();
      // Delay before next: longer when crossing folder boundary
      const nextF  = pending[i + 1];
      const delay  = nextF && nextF.folder !== f.folder ? 6000 : 2500;
      setTimeout(() => _estTick(pending, i + 1), delay);
    })
    .catch(() => {
      if (cell) cell.textContent = '\u2014';
      _estDone++;
      _updateEstStrip();
      setTimeout(() => _estTick(pending, i + 1), 2500);
    });
}

// ============================================================
// Conversion  (live /api/start + /api/status polling)
// ============================================================
let _pollTimer  = null;
let _isPaused   = false;
let _logCursor  = 0;

// Returns true for verbose ffmpeg internals that clutter the log box
function _isVerboseLogLine(msg) {
  if (msg.startsWith('Running: ')) return true;
  if (/^\[[\w_]+ @ /.test(msg)) return true;   // [hevc_qsv @ 0x...] codec context
  if (/^(ffmpeg |ffprobe |  |Input #|Output #|Stream mapping|Stream #\d|Press \[|video:|audio:|subtitle:|global headers:)/.test(msg)) return true;
  return false;
}

function _startPolling() {
  if (_pollTimer) return;
  _pollTimer = setInterval(_pollStatus, 500);
}

function _stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

function _stepIcon(state) {
  if (state === 'done')    return '<i class="bi bi-check-circle-fill"></i>';
  if (state === 'running') return '<i class="bi bi-arrow-repeat"></i>';
  if (state === 'retry')   return '<i class="bi bi-arrow-clockwise"></i>';
  if (state === 'failed')  return '<i class="bi bi-x-circle-fill"></i>';
  if (state === 'skipped') return '<i class="bi bi-dash-circle"></i>';
  return '<i class="bi bi-circle"></i>'; // waiting
}

function _esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _renderCurrentJob(s) {
  const idle       = document.getElementById('jobIdle');
  const ocrDiv     = document.getElementById('jobOcrBatch');
  const convDiv    = document.getElementById('jobConverting');
  const counter    = document.getElementById('jobFileCounter');
  const phasePill  = document.getElementById('jobPhasePill');
  const pausedPill = document.getElementById('jobPausedPill');
  const modeChip   = document.getElementById('jobModeChip');
  if (!idle) return; // card not in DOM yet

  const phase     = s.phase || '';
  const isRunning = s.state === 'running';

  if (counter) {
    counter.textContent = isRunning && s.total
      ? (s.current_index + 1) + ' of ' + s.total
      : '';
  }

  const curFile = s.current_file ? (_files.find(f => f.full_path === s.current_file) || null) : null;

  if (phasePill) {
    let phaseText = '';
    let phaseClass = 'badge-pending';
    if (isRunning) {
      if (phase === 'ocr_batch') {
        phaseText = 'OCR pre-pass';
        phaseClass = 'badge-ocr';
      } else if (phase === 'converting') {
        const _steps = Array.isArray(s.steps) ? s.steps : [];
        const _active = _steps.find(st => st && (st.state === 'running' || st.state === 'retry'));
        const _postIds = new Set(['audio', 'remux', 'verify']);
        const _isPostStep = !!(_active && _postIds.has(String(_active.id || '')));
        const _frameVisible = /frame=\s*\d+/i.test(String(s.ffmpeg_status || ''));
        const _nearEnd = (Number(s.progress_pct) || 0) >= 99;
        if (_isPostStep || (_nearEnd && !_frameVisible)) {
          phaseText = 'Finalizing';
          phaseClass = 'badge-pending';
        } else {
          phaseText = 'Converting';
          phaseClass = 'badge-converting';
        }
      } else {
        phaseText = 'Starting';
        phaseClass = 'badge-pending';
      }
    }
    if (phaseText) {
      phasePill.textContent = phaseText;
      phasePill.classList.remove('d-none', 'badge-pending', 'badge-converting', 'badge-ocr');
      phasePill.classList.add(phaseClass);
    } else {
      phasePill.classList.add('d-none');
    }
  }

  if (pausedPill) {
    pausedPill.classList.toggle('d-none', !(isRunning && !!s.paused));
  }

  if (modeChip) {
    let modeText = '';
    let modeClass = 'badge-pending';
    if (s.encoder) {
      modeText = String(s.encoder).toUpperCase();
      modeClass = 'badge-converting';
    } else if (curFile) {
      const sw = !!curFile.force_sw;
      const fc = !!curFile.force_convert;
      modeText = sw ? 'SW path' : 'QSV path';
      if (fc) modeText += ' · FC';
      modeClass = sw ? 'badge-pending' : 'badge-converting';
    }
    if (modeText && isRunning) {
      modeChip.textContent = modeText;
      modeChip.classList.remove('d-none', 'badge-pending', 'badge-converting', 'badge-ocr');
      modeChip.classList.add(modeClass);
    } else {
      modeChip.classList.add('d-none');
    }
  }

  idle.classList.toggle('d-none',       phase !== '');
  ocrDiv.classList.toggle('d-none',     phase !== 'ocr_batch');
  convDiv.classList.toggle('d-none',    phase !== 'converting');

  if (phase === 'ocr_batch' && s.ocr_batch) {
    const b   = s.ocr_batch;
    const pct = b.total > 0 ? Math.round(b.done / b.total * 100) : 0;
    const bar = document.getElementById('ocrBatchBar');
    if (bar) bar.style.width = pct + '%';
    const listEl = document.getElementById('ocrBatchFileList');
    if (listEl) {
      listEl.innerHTML = (b.files || []).map(f => {
        const cls  = 'ocr-file-' + (f.state || 'waiting');
        const icon = f.state === 'done'    ? '<i class="bi bi-check-circle-fill"></i>'
                   : f.state === 'running' ? '<i class="bi bi-cpu-fill" style="animation:step-pulse 1.2s ease infinite"></i>'
                   : f.state === 'failed'  ? '<i class="bi bi-x-circle-fill"></i>'
                   :                         '<i class="bi bi-circle"></i>';
        return '<div class="ocr-batch-file ' + cls + '">' + icon +
               '<span class="text-truncate">' + _esc(f.name) + '</span></div>';
      }).join('');
      // Scroll the running item into view
      const running = listEl.querySelector('.ocr-file-running');
      if (running) running.scrollIntoView({ block: 'nearest' });
    }
  }

  if (phase === 'converting') {
    const filename = s.current_file ? s.current_file.split(/[\\/]/).pop() : '\u2014';
    const fnEl = document.getElementById('jobFilename');
    if (fnEl) { fnEl.textContent = filename; fnEl.title = s.current_file || ''; }

    const listEl = document.getElementById('stepList');
    if (listEl && s.steps) {
      listEl.innerHTML = s.steps.map(st => {
        return '<li class="step-' + (st.state || 'waiting') + '">' +
          '<span class="step-icon">' + _stepIcon(st.state) + '</span>' +
          '<span class="step-label">' + _esc(st.label) + '</span>' +
          (st.detail ? '<span class="step-detail">' + _esc(st.detail) + '</span>' : '') +
          '</li>';
      }).join('');
    }
  }
}

function _pollStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(s => {
      _renderCurrentJob(s);
      const statusEl = document.getElementById('ffmpegStatus');
      const statusInlineEl = document.getElementById('ffmpegStatusInline');
      const statusText = s.ffmpeg_status || (s.state === 'running' ? 'waiting for ffmpeg telemetry...' : '');
      if (statusEl) {
        if (statusText) {
          statusEl.textContent = statusText;
          statusEl.classList.remove('d-none');
        } else {
          statusEl.textContent = '';
          statusEl.classList.add('d-none');
        }
      }
      if (statusInlineEl) statusInlineEl.textContent = statusText || '\u2014';
      // Update progress widgets
      const pct = s.progress_pct || 0;
      document.getElementById('fileBar').style.width = pct + '%';
      document.getElementById('filePct').textContent  = Math.round(pct) + '%';
      document.getElementById('fpsVal').textContent   = s.fps ? s.fps.toFixed(0) : '\u2014';
      const framesEl = document.getElementById('framesVal');
      const framesRemainingEl = document.getElementById('framesRemainingVal');
      if (framesEl) {
        if (s.state === 'running' && s.current_file) {
          const curFile = _files.find(f => f.full_path === s.current_file);
          const x = _extractFrameFromStatus(s.ffmpeg_status || '');
          const durSecs = curFile ? _parseDuration(curFile.duration) : 0;
          const srcFps = curFile && curFile.streams && curFile.streams.video
            ? _parseFpsVal(curFile.streams.video.fps)
            : 0;
          const yFromMeta = (durSecs > 0 && srcFps > 0) ? Math.round(durSecs * srcFps) : 0;
          const pctNum = Number(s.progress_pct) || 0;
          // Fallback total-frame estimate from live frame + progress when source FPS is unavailable.
          const yFromPct = (x > 0 && pctNum > 0.5) ? Math.round(x / (pctNum / 100)) : 0;
          const y = yFromMeta > 0 ? yFromMeta : yFromPct;
          if (x > 0 && y > 0) {
            framesEl.textContent = _fmtInt(x) + ' / ~' + _fmtInt(y);
            if (framesRemainingEl) framesRemainingEl.textContent = '~' + _fmtInt(Math.max(0, y - x));
          } else if (x > 0) {
            framesEl.textContent = _fmtInt(x);
            if (framesRemainingEl) framesRemainingEl.textContent = '\u2014';
          } else {
            framesEl.textContent = '\u2014';
            if (framesRemainingEl) framesRemainingEl.textContent = '\u2014';
          }
        } else {
          framesEl.textContent = '\u2014';
          if (framesRemainingEl) framesRemainingEl.textContent = '\u2014';
        }
      }
      let liveSpeedX = 0;
      if (s.file_started_at && s.current_file && pct > 0) {
        const curFile = _files.find(f => f.full_path === s.current_file);
        const durSecs = curFile ? _parseDuration(curFile.duration) : 0;
        const elapsedSecs = Math.max(0, (Date.now() / 1000) - s.file_started_at);
        if (durSecs > 0 && elapsedSecs > 1) {
          liveSpeedX = _conversionSpeedX(durSecs * (pct / 100), elapsedSecs);
        }
      }
      const speedEl = document.getElementById('speedVal');
      if (speedEl) speedEl.textContent = _formatSpeedX(liveSpeedX);
      document.getElementById('etaVal').textContent   = s.eta_secs > 0
        ? Math.floor(s.eta_secs / 60) + 'm ' + (s.eta_secs % 60) + 's'
        : '\u2014';
      const jobElapsedEl = document.getElementById('jobElapsedVal');
      if (jobElapsedEl) {
        const _jobStart = s.session_started_at || _sessionStartedAt;
        if (s.state === 'running' && _jobStart) {
          jobElapsedEl.textContent = _fmtDuration(Math.max(0, Date.now() / 1000 - _jobStart));
        } else {
          jobElapsedEl.textContent = '—';
        }
      }
      if (s.files) {
        _sessionProcessed = s.files.filter(f =>
          f.status === 'done' || f.status === 'failed' || f.status === 'no_saving' || f.status === 'low_savings'
        ).length;
      }
      if (s.session_started_at && _sessionStartedAt !== s.session_started_at) _startElapsedTimer(s.session_started_at);
      if (s.file_started_at && s.state === 'running') _startFileElapsedTimer(s.file_started_at);
      // Update pause button label
      const pauseBtn = document.getElementById('pauseBtn');
      if (pauseBtn) {
        pauseBtn.innerHTML = s.paused
          ? '<i class="bi bi-play-fill me-1"></i>Resume'
          : '<i class="bi bi-pause-fill me-1"></i>Pause';
      }

      // Sync row badges + output cells from status.files.
      // s.files only contains the SUBMITTED (pending/failed) subset — match by
      // full_path so we never clobber done/skipped rows that weren't submitted.
      if (s.files) {
        let _statusChanged = false;
        const statusByPath = {};
        s.files.forEach(sf => { if (sf.full_path) statusByPath[sf.full_path] = sf; });

        _files.forEach((f, idx) => {
          const sf = statusByPath[f.full_path];
          if (!sf) return;
          const _prevStatus = f.status || '';
          if ((f.status || '') !== (sf.status || '')) _statusChanged = true;
          // Sync into _files so local state matches
          f.status = sf.status;
          if (_prevStatus !== 'done' && sf.status === 'done') f.session_done = true;
          if (sf.force_sw !== undefined) f.force_sw = sf.force_sw;
          if (sf.force_convert !== undefined) f.force_convert = sf.force_convert;
          if (sf.output)      f.output      = sf.output;
          if (sf.saved)       f.saved       = sf.saved;
          if (sf.pct)         f.pct         = sf.pct;
          if (sf.output_path) f.output_path = sf.output_path;
          if (sf.conv_secs  !== undefined) f.conv_secs  = sf.conv_secs;
          if (sf.ffmpeg_cmd !== undefined) f.ffmpeg_cmd = sf.ffmpeg_cmd;
          if (sf.error_tail !== undefined) f.error_tail = sf.error_tail;
          if (sf.log_dir    !== undefined) f.log_dir    = sf.log_dir;
          if (sf.ocr_status !== undefined) f.ocr_status = sf.ocr_status;
          if (sf.est_pct    != null) f.est_pct    = sf.est_pct;
          if (sf.est_mb     != null) f.est_mb     = sf.est_mb;
          if (sf.est_cv     !== undefined) f.est_cv = sf.est_cv;
          if (sf.est_high_variance !== undefined) f.est_high_variance = sf.est_high_variance;
          if (sf.est_aggregation !== undefined) f.est_aggregation = sf.est_aggregation;

          const row = document.getElementById('row-' + idx);
          if (!row) return;
          // Status badge (col index 7)
          const badgeCell = row.cells[7];
          if (badgeCell) {
            const _ocrDone = sf.status === 'ocr' && (f.ocr_status === 'done' || f.ocr_status === 'skipped');
            badgeCell.innerHTML = (_ocrDone ? '' : _badgeHtml(sf.status, sf.force_sw, sf.force_convert)) + _droppedBadgeHtml(f) + _ocrBadgeHtml(f);
            row.classList.toggle('tr-done',        sf.status === 'done');
            row.classList.toggle('tr-failed',      sf.status === 'failed');
            row.classList.toggle('tr-low-savings', sf.status === 'low_savings');
            row.classList.toggle('tr-skipped',     sf.status === 'skipped');
            row.classList.toggle('tr-converting',  sf.status === 'converting');
            // Auto-scroll the active row into view — only when the converting file changes
            if (sf.status === 'converting' && f.full_path !== _lastAutoScrollPath) {
              row.scrollIntoView({ block: 'center', behavior: 'smooth' });
              _lastAutoScrollPath = f.full_path;
            }
          }
          // Output/Saved/% cells (cols 12, 13, 14)
          if (sf.status === 'done') {
            if (row.cells[12]) row.cells[12].textContent  = sf.output ? sf.output + ' MB' : '';
            if (row.cells[13]) row.cells[13].textContent = sf.saved  ? sf.saved  + ' MB' : '';
            if (row.cells[14]) row.cells[14].innerHTML   = sf.pct     ? '<strong>' + sf.pct + '%</strong>' : '';
          }
          if (sf.conv_secs !== undefined) {
            if (row.cells[16]) row.cells[16].textContent = sf.conv_secs ? _fmtDuration(sf.conv_secs) : '';
            if (row.cells[15]) {
              const sx = _conversionSpeedX(_parseDuration(f.duration), sf.conv_secs || 0);
              row.cells[15].textContent = sx > 0 ? _formatSpeedX(sx) : '';
            }
          }
          const estCell = document.getElementById('est-' + idx);
          if (estCell && sf.est_pct != null) {
            estCell.innerHTML = _estimateHtml(f);
          }
        });

        _sessionSavedMB = (s.session_realized_mb !== undefined && s.session_realized_mb !== null)
          ? (Number(s.session_realized_mb) || 0)
          : _sessionSavedMB;
        _sessionEstimatedMB = (s.session_in_progress_est_mb !== undefined && s.session_in_progress_est_mb !== null)
          ? Math.max(0, Number(s.session_in_progress_est_mb) || 0)
          : 0;
        _updateSessionCard();

        const _savedEl = document.getElementById('savedVal');
        const _estEl = document.getElementById('estVal');
        const _currentSavedMb = (s.current_file_saved_mb !== undefined && s.current_file_saved_mb !== null)
          ? Math.max(0, Number(s.current_file_saved_mb) || 0)
          : 0;
        const _currentEstMb = (s.current_file_est_mb !== undefined && s.current_file_est_mb !== null)
          ? Math.max(0, Number(s.current_file_est_mb) || 0)
          : 0;

        if (_savedEl) _savedEl.textContent = _currentSavedMb > 0 ? _fmtSavedShort(_currentSavedMb) : '—';
        if (_estEl) _estEl.textContent = _currentEstMb > 0 ? ('+' + _fmtSavedShort(_currentEstMb)) : '—';

        updateStats(_files);
        if (_statusChanged) applyFilter();
      }

      // Render new backend log lines (filter out verbose ffmpeg internals)
      if (s.log && s.log.length > _logCursor) {
        s.log.slice(_logCursor).forEach(msg => {
          if (_isVerboseLogLine(msg)) return;
          const cls = msg.startsWith('ERROR:') ? 'err'
                    : /^(Done\.|NOTE:|Skipped |Track \d+ pre-encoded)/.test(msg) ? 'ok'
                    : 'info';
          addLog(msg, cls);
        });
        _logCursor = s.log.length;
      }

      // Transition state
      if (s.state === 'done' || s.state === 'stopped') {
        // Hide the ffmpeg status bar once the queue finishes
        const statusEl2 = document.getElementById('ffmpegStatus');
        if (statusEl2) statusEl2.classList.add('d-none');
      }
      if (s.state === 'done') {
        _stopPolling();
        _stopElapsedTimer();
        _stopFileElapsedTimer();
        _isPaused = false;
        setButtonStates('ready');
        addLog('Queue complete. Saved ' + (s.saved_mb || 0).toFixed(1) + ' MB total.', 'ok');
        if (_prepEstimateRoot) {
          const _prepRoot = _prepEstimateRoot;
          _prepEstimateRoot = null;
          buildPrepQueue(_prepRoot);
        }
      } else if (s.state === 'stopped') {
        _stopPolling();
        _stopElapsedTimer();
        _stopFileElapsedTimer();
        _isPaused = false;
        setButtonStates('ready');
        addLog('Conversion stopped.', 'warn');
      }
    })
    .catch(() => {}); // ignore transient errors
}

function startConversion() {
  if (_files.length === 0) return;
  _cancelProbeStream();
  const anime = document.getElementById('animeMode').checked;
  const pendingFiles = _files.filter(f =>
    (f.status === 'pending' || f.status === 'failed') && _fileMatchesFilter(f)
  );
  if (pendingFiles.length === 0) {
    addLog('No pending files to convert.', 'warn');
    return;
  }
  _sessionSavedMB = 0;
  _sessionEstimatedMB = 0;
  _sessionProcessed = 0;
  _updateSessionCard();
  _stopElapsedTimer();
  _stopFileElapsedTimer();
  const _elEl2 = document.getElementById('statSessionElapsed');
  if (_elEl2) _elEl2.textContent = '';
  const _elLbl = document.getElementById('statSessionElapsedLabel');
  if (_elLbl) _elLbl.textContent = '';
  addLog('Starting conversion queue\u2026', 'info');
  addLog(anime
    ? 'Anime mode: ON \u2014 remux to MP4, AAC transcode, OCR subs'
    : 'Normal mode \u2014 compress only, copy all tracks', 'info');
  fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ files: pendingFiles, anime_mode: anime }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _isPaused = false;
      _logCursor = 0;
      // Clear log and hide ffmpeg status bar for fresh conversion
      const _lb = document.getElementById('logBox');
      if (_lb) _lb.innerHTML = '';
      const _fs = document.getElementById('ffmpegStatus');
      if (_fs) { _fs.textContent = ''; _fs.classList.add('d-none'); }
      setButtonStates('running');
      _startPolling();
    } else {
      addLog('Start failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(e => addLog('Could not reach server: ' + e, 'err'));
}

function estimateAll() {
  if (_files.length === 0) return;
  _cancelProbeStream();
  const pendingFiles = _files.filter(f =>
    (f.status === 'pending' || f.status === 'failed') &&
    !f.est_pct  // only files without a cached estimate
  );
  const alreadyEstimated = _files.filter(f =>
    (f.status === 'pending' || f.status === 'failed') && f.est_pct
  ).length;
  if (pendingFiles.length === 0) {
    addLog(alreadyEstimated > 0
      ? `All ${alreadyEstimated} pending files already have estimates \u2014 sort by Est. Savings and start conversion.`
      : 'No pending files to estimate.', 'warn');
    return;
  }
  addLog(`Estimating savings for ${pendingFiles.length} files\u2026` +
    (alreadyEstimated > 0 ? ` (${alreadyEstimated} already cached)` : ''), 'info');
  fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ files: pendingFiles, anime_mode: false, estimate_only: true }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _isPaused = false;
      _logCursor = 0;
      const _lb = document.getElementById('logBox');
      if (_lb) _lb.innerHTML = '';
      const _fs = document.getElementById('ffmpegStatus');
      if (_fs) { _fs.textContent = ''; _fs.classList.add('d-none'); }
      setButtonStates('running');
      _startPolling();
    } else {
      addLog('Estimate failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(e => addLog('Could not reach server: ' + e, 'err'));
}

function pauseConversion() {
  const endpoint = _isPaused ? '/api/resume' : '/api/pause';
  fetch(endpoint, { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        _isPaused = !_isPaused;
        const pauseBtn = document.getElementById('pauseBtn');
        if (pauseBtn) {
          pauseBtn.innerHTML = _isPaused
            ? '<i class="bi bi-play-fill me-1"></i>Resume'
            : '<i class="bi bi-pause-fill me-1"></i>Pause';
        }
        addLog(_isPaused ? 'Paused.' : 'Resumed.', 'info');
      }
    })
    .catch(e => addLog('Pause/resume error: ' + e, 'err'));
}

function stopConversion() {
  fetch('/api/stop', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        addLog('Stopping after current file completes…', 'warn');
        // Keep polling — _pollStatus will handle the stopped→ready transition
        // once the backend worker has actually terminated.
      }
    })
    .catch(e => addLog('Stop error: ' + e, 'err'));
}

function hardStopConversion() {
  fetch('/api/hardstop', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      if (d.ok) addLog('Hard stop — ffmpeg killed.', 'warn');
    })
    .catch(e => addLog('Hard stop error: ' + e, 'err'));
}

// ============================================================
// Row action menu
// ============================================================
let _rowMenuIndex = -1;

function _getRowMenu() { return document.getElementById('rowMenu'); }

// Close menu on any outside click
document.addEventListener('click', () => {
  const m = _getRowMenu();
  if (m) m.style.display = 'none';
});

function _menuItem(icon, label, handler, extraClass) {
  const a = document.createElement('a');
  a.className = 'dropdown-item' + (extraClass ? ' ' + extraClass : '');
  a.href = '#';
  a.innerHTML = '<i class="bi ' + icon + ' me-2"></i>' + label;
  a.addEventListener('click', e => { e.preventDefault(); const m = _getRowMenu(); if (m) m.style.display = 'none'; handler(); });
  return a;
}

function _menuDivider() {
  const d = document.createElement('hr');
  d.className = 'dropdown-divider';
  return d;
}

// Delete item with inline confirm step
function _menuDeleteItem(index, path) {
  const a = document.createElement('a');
  a.className = 'dropdown-item text-danger';
  a.href = '#';
  a.innerHTML = '<i class="bi bi-trash me-2"></i>Delete (Recycle Bin)';
  a.addEventListener('click', e => {
    e.preventDefault();
    e.stopPropagation();
    // Replace with confirm button inline
    a.innerHTML = '<i class="bi bi-exclamation-triangle-fill me-2"></i>Confirm delete?'
      + ' <span class="badge ms-1" style="background:rgba(248,81,73,.18);color:#f85149;border:1px solid #f85149;border-radius:2rem;padding:.15rem .55rem;font-size:.72rem;font-weight:600">Yes, trash it</span>';
    a.onclick = e2 => {
      e2.preventDefault();
      e2.stopPropagation();
      const m = _getRowMenu();
      if (m) m.style.display = 'none';
      trashFile(index, path);
    };
  });
  return a;
}

function trashFile(index, path) {
  fetch('/api/trash', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path})
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _files.splice(index, 1);
      populateTable(_files);
      updateStats(_files);
    } else {
      alert('Delete failed: ' + (d.error || 'unknown error'));
    }
  })
  .catch(err => alert('Delete failed: ' + err));
}

function showRowMenu(index, btn) {
  _rowMenuIndex = index;
  const _rowMenu = _getRowMenu();
  if (!_rowMenu) return;
  const f = _files[index];
  _rowMenu.innerHTML = '';

  const isDone       = f.status === 'done';
  const isFailed     = f.status === 'failed';
  const isPending    = f.status === 'pending';
  const isSkipped    = f.status === 'skipped';
  const isConverting = f.status === 'converting';

  // Target path: converted output if done, otherwise source
  const targetPath = isDone && f.output_path ? f.output_path : f.full_path;

  if (!isConverting) {
    _rowMenu.appendChild(_menuItem('bi-play-circle-fill text-success',
      isDone ? 'Play Converted' : 'Play Original',
      () => apiOpen(targetPath, 'play')));
    _rowMenu.appendChild(_menuDivider());
  }

  _rowMenu.appendChild(_menuItem('bi-folder2-open', 'Open Folder',
    () => apiOpen(isDone && f.output_path ? f.output_path : f.full_path, 'folder')));
  _rowMenu.appendChild(_menuItem('bi-clipboard', 'Copy Path',
    () => copyPath(targetPath)));

  if (isFailed) {
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-exclamation-triangle text-danger', 'View Error Log',
      () => viewErrorLog(index)));
    _rowMenu.appendChild(_menuItem('bi-arrow-clockwise', 'Retry',
      () => retryFile(index)));
  }

  _rowMenu.appendChild(_menuDivider());
  _rowMenu.appendChild(_menuItem('bi-info-circle', 'Video Details',
    () => viewDetails(index)));
  _rowMenu.appendChild(_menuItem('bi-stethoscope text-warning', 'Diagnose',
    () => diagnoseFile(index)));

  if (isPending || isFailed) {
    const hasDropped = f.dropped_streams && f.dropped_streams.length > 0;
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-slash-circle text-warning', 'Drop PGS tracks (this file)',
      () => dropPgsFile(index)));
    _rowMenu.appendChild(_menuItem('bi-slash-circle-fill text-warning', 'Drop PGS \u2014 all \u201c' + (f.folder || 'root') + '\u201d files',
      () => dropPgsFolder(f.folder || '')));
    if (hasDropped) {
      _rowMenu.appendChild(_menuItem('bi-arrow-counterclockwise text-success', 'Restore all dropped tracks',
        () => restoreStreams(index)));
    }
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-slash-circle text-warning', 'Skip this file',
      () => skipFile(index)));
  }

  if (isSkipped) {
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-arrow-clockwise', 'Un-skip (reset to pending)',
      () => unskipFile(index)));
  }

  if (isDone || isFailed || f.status === 'low_savings' || f.status === 'no_saving') {
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-arrow-counterclockwise text-warning', 'Reset to pending',
      () => resetToPending(index)));
  }

  if (isPending || isFailed || isSkipped || f.status === 'low_savings') {
    if (f.force_sw) {
      _rowMenu.appendChild(_menuDivider());
      _rowMenu.appendChild(_menuItem('bi-cpu text-secondary', 'Remove SW-only flag',
        () => unforceSw(index)));
    } else {
      _rowMenu.appendChild(_menuDivider());
      _rowMenu.appendChild(_menuItem('bi-cpu text-warning', 'Force SW encode (skip QSV)',
        () => forceSw(index)));
    }

    if (f.force_convert) {
      _rowMenu.appendChild(_menuDivider());
      _rowMenu.appendChild(_menuItem('bi-lightning-charge text-secondary', 'Remove Force Convert flag',
        () => unforceConvert(index)));
    } else {
      _rowMenu.appendChild(_menuDivider());
      _rowMenu.appendChild(_menuItem('bi-lightning-charge text-info', 'Force Convert (ignore low-savings estimate)',
        () => forceConvert(index)));
    }
  }

  if (isPending) {
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-x-circle text-danger', 'Remove from Queue',
      () => removeFromQueue(index)));
  }

  // Delete → Recycle Bin (available for all non-converting rows)
  if (!isConverting) {
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuDeleteItem(index, f.full_path));
  }

  // Position: align right of button, flip up if near bottom
  _rowMenu.style.display = 'block';
  const rect    = btn.getBoundingClientRect();
  const menuW   = _rowMenu.offsetWidth;
  const menuH   = _rowMenu.offsetHeight;
  let top  = rect.bottom + 2;
  let left = rect.right - menuW;
  if (top + menuH > window.innerHeight - 8) top = rect.top - menuH - 2;
  if (left < 4) left = rect.left;
  _rowMenu.style.top  = top + 'px';
  _rowMenu.style.left = left + 'px';
}

// ============================================================
// Row actions
// ============================================================
function apiOpen(path, action) {
  if (!path) { addLog('No path available.', 'warn'); return; }
  fetch('/api/open?path=' + encodeURIComponent(path) + '&action=' + action)
    .then(r => r.json())
    .then(d => { if (d.error) addLog('Error: ' + d.error, 'err'); })
    .catch(() => addLog('Could not reach server.', 'err'));
}

function copyPath(path) {
  if (!path) return;
  navigator.clipboard.writeText(path)
    .then(() => addLog('Copied: ' + path, 'info'))
    .catch(() => addLog('Clipboard write failed.', 'err'));
}

function viewErrorLog(index) {
  const f = _files[index];
  document.getElementById('errModalFilename').textContent = f.name;
  document.getElementById('errModalCmd').textContent  = f.ffmpeg_cmd  || '(no FFmpeg command recorded)';
  document.getElementById('errModalTail').textContent = f.error_tail  || '(no error output recorded)';
  const logPathEl = document.getElementById('errModalLogPath');
  if (logPathEl) logPathEl.textContent = f.log_dir ? 'Log folder: ' + f.log_dir : '';
  const openLogBtn = document.getElementById('errModalOpenLog');
  if (openLogBtn) {
    if (f.log_dir) {
      openLogBtn.style.display = '';
      openLogBtn.onclick = () => apiOpen(f.log_dir, 'folder');
    } else {
      openLogBtn.style.display = 'none';
    }
  }
  new bootstrap.Modal(document.getElementById('errorLogModal')).show();
}

function viewDetails(index) {
  const f = _files[index];
  _detailsFileIndex = index;
  const body = document.getElementById('detailsModalBody');
  const titleEl = document.getElementById('detailsModalTitle');
  if (titleEl) titleEl.textContent = f.name;

  const s = f.streams || null;
  const v = s ? s.video : null;
  const dropped = new Set(f.dropped_streams || []);

  // ---- Video section ----
  const codecBadge = v ? `<span class="details-codec-badge details-codec-${(v.codec||'').toLowerCase()}">${v.codec.toUpperCase()}</span>` : '—';
  const hdrBadge   = (v && v.hdr) ? '<span class="badge bg-warning text-dark ms-1 small">HDR</span>' : '';

  let html = `
    <h6 class="details-section-head"><i class="bi bi-camera-video me-2"></i>Video Stream</h6>
    <table class="table table-sm details-table mb-3">
      <tr><td>Codec</td><td>${v ? codecBadge + hdrBadge : '—'}</td></tr>
      <tr><td>Profile / Level</td><td>${v ? v.profile + ' / L' + v.level : '—'}</td></tr>
      <tr><td>Resolution</td><td>${v ? v.resolution : '—'}</td></tr>
      <tr><td>Frame rate</td><td>${v ? v.fps + ' fps' : '—'}</td></tr>
      <tr><td>Bitrate</td><td>${v ? v.bitrate : '—'}</td></tr>
      <tr><td>Pixel format</td><td>${v ? v.color : '—'}</td></tr>
    </table>`;

  // ---- File info ----
  html += `
    <h6 class="details-section-head"><i class="bi bi-file-earmark me-2"></i>File</h6>
    <table class="table table-sm details-table mb-3">
      <tr><td>Filename</td><td class="fw-semibold">${f.name}</td></tr>
      <tr><td>Folder</td><td>${f.folder || '<span class="text-secondary">(root)</span>'}</td></tr>
      <tr><td>File size</td><td>${f.size} MB</td></tr>
      <tr><td>Duration</td><td>${f.duration || '—'}</td></tr>
      ${f.est_pct != null ? `<tr><td>Estimate</td><td>${_estimateHtml(f)}</td></tr>` : ''}
      ${f.status === 'done' ? `<tr><td>Output size</td><td>${f.output} MB</td></tr>
      <tr><td>Space saved</td><td class="text-success fw-semibold">${f.saved} MB (${f.pct}%)</td></tr>` : ''}
    </table>`;

  // ---- Audio tracks ----
  const audioTracks = s ? s.audio : [];
  html += `<h6 class="details-section-head"><i class="bi bi-music-note-list me-2"></i>Audio Tracks <span class="badge bg-secondary ms-1">${audioTracks.length}</span></h6>`;
  if (!s) {
    html += `<p class="text-secondary small mb-3">Stream data not loaded. <button class="btn btn-sm btn-outline-secondary py-0" onclick="probeStreams(${index})"><i class="bi bi-search me-1"></i>Probe</button></p>`;
  } else if (audioTracks.length) {
    html += '<div class="details-track-table-wrap mb-3"><table class="table table-sm details-table mb-0"><thead><tr><th>#</th><th>Codec</th><th>Channels</th><th>Language</th><th>Bitrate</th><th>Title</th><th class="text-end"><button class="btn btn-link btn-sm p-0 me-2" onclick="setAllStreamsDropped(' + index + ',\'audio\',true)">Drop all</button><button class="btn btn-link btn-sm p-0" onclick="setAllStreamsDropped(' + index + ',\'audio\',false)">Restore all</button></th></tr></thead><tbody>';
    audioTracks.forEach(a => {
      const streamIdx = a.index != null ? a.index : null;
      const isDrop = streamIdx != null && dropped.has(streamIdx);
      const dropBtn = streamIdx != null
        ? `<button class="btn btn-xs details-drop-btn ${isDrop ? 'dropped' : ''}" title="${isDrop ? 'Click to restore track' : 'Click to drop track'}" onclick="toggleDropStream(${index},${streamIdx},this)"><i class="bi bi-${isDrop ? 'plus-circle' : 'dash-circle'}"></i></button>`
        : '';
      const rowClass = isDrop ? ' class="details-track-dropped"' : '';
      html += `<tr${rowClass}><td class="text-secondary">${a.track}</td><td>${a.codec}</td><td>${a.channels}</td><td><span class="badge bg-secondary">${a.language}</span></td><td class="text-secondary">${a.bitrate}</td><td>${a.title || '—'}</td><td>${dropBtn}</td></tr>`;
    });
    html += '</tbody></table></div>';
  } else if (s) {
    html += '<p class="text-secondary small mb-3">No audio tracks found.</p>';
  }

  // ---- Subtitle tracks ----
  const subTracks = s ? s.subs : [];
  html += `<h6 class="details-section-head"><i class="bi bi-badge-cc me-2"></i>Subtitle Tracks <span class="badge bg-secondary ms-1">${subTracks.length}</span></h6>`;
  if (!s) {
    html += '<p class="text-secondary small mb-0">Click Probe above to load track info.</p>';
  } else if (subTracks.length) {
    html += '<div class="details-track-table-wrap"><table class="table table-sm details-table mb-0"><thead><tr><th>#</th><th>Format</th><th>Language</th><th>Likely</th><th>Title</th><th class="text-end"><button class="btn btn-link btn-sm p-0 me-2" onclick="setAllStreamsDropped(' + index + ',\'subs\',true)">Drop all</button><button class="btn btn-link btn-sm p-0" onclick="setAllStreamsDropped(' + index + ',\'subs\',false)">Restore all</button></th></tr></thead><tbody>';
    subTracks.forEach(sub => {
      const streamIdx = sub.index != null ? sub.index : null;
      const isDrop = streamIdx != null && dropped.has(streamIdx);
      const isImage = ['PGS','VOBSUB','DVDSUB'].includes(sub.codec.toUpperCase());
      const fmtBadge = isImage
        ? `<span class="badge details-sub-image">${sub.codec}</span>`
        : `<span class="badge details-sub-text">${sub.codec}</span>`;
      const likelyId = streamIdx != null ? _likelyLangCellId(index, streamIdx) : '';
      const likelyCell = streamIdx != null
        ? `<span id="${likelyId}" class="text-secondary small">…</span>`
        : '<span class="text-secondary small">—</span>';
      const dropBtn = streamIdx != null
        ? `<button class="btn btn-xs details-drop-btn ${isDrop ? 'dropped' : ''}" title="${isDrop ? 'Click to restore track' : 'Click to drop track'}" onclick="toggleDropStream(${index},${streamIdx},this)"><i class="bi bi-${isDrop ? 'plus-circle' : 'dash-circle'}"></i></button>`
        : '';
      const previewBtn = streamIdx != null
        ? `<button class="btn btn-xs btn-outline-secondary ms-1" title="Preview subtitle text" onclick="previewSubtitleText(${index},${streamIdx})"><i class="bi bi-eye"></i></button>`
        : '';
      const rowClass = isDrop ? ' class="details-track-dropped"' : '';
      html += `<tr${rowClass}><td class="text-secondary">${sub.track}</td><td>${fmtBadge}</td><td><span class="badge bg-secondary">${sub.language}</span></td><td>${likelyCell}</td><td>${sub.title || '—'}</td><td>${previewBtn}${dropBtn}</td></tr>`;
    });
    html += '</tbody></table></div>';
    if (dropped.size > 0) {
      html += '<p class="text-warning small mt-2 mb-0"><i class="bi bi-exclamation-triangle me-1"></i>Dropped tracks will be excluded from conversion and OCR.</p>';
    }
    html += `
      <div class="card mt-3">
        <div class="card-header py-2 small"><i class="bi bi-card-text me-1"></i>Subtitle Text Preview</div>
        <div class="card-body p-2">
          <div id="detailsSubPreviewStatus" class="small text-secondary mb-2">Click <strong>Preview</strong> on a subtitle row to inspect text and detected language.</div>
          <pre id="detailsSubPreviewText" class="bg-body-secondary rounded p-2 mb-0 small" style="max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-word;"></pre>
        </div>
      </div>`;
  } else if (s) {
    html += '<p class="text-secondary small mb-0">No subtitle tracks found.</p>';
  }

  body.innerHTML = html;
  const modalEl = document.getElementById('detailsModal');
  let existing = bootstrap.Modal.getInstance(modalEl);
  if (!existing) {
    existing = new bootstrap.Modal(modalEl);
  }
  existing.show();
  refreshStreamEditStatus();
  refreshEngStereoStatus();
  refreshLikelySubtitleLanguages(index);
}

function _likelyLangCellId(fileIndex, streamIndex) {
  return 'detailsLikelyLang_' + fileIndex + '_' + streamIndex;
}

function _isImageSubtitleCodec(codec) {
  const c = String(codec || '').toUpperCase();
  return c === 'PGS' || c === 'HDMV_PGS_SUBTITLE' || c === 'PGSSUB' || c === 'DVDSUB' || c === 'DVD_SUBTITLE' || c === 'VOBSUB' || c === 'XSUB';
}

function _likelyCacheKey(filePath, streamIndex) {
  return String(filePath || '') + '|' + String(streamIndex);
}

function _setLikelyCell(fileIndex, streamIndex, text, title) {
  const el = document.getElementById(_likelyLangCellId(fileIndex, streamIndex));
  if (!el) return;
  el.textContent = text;
  if (title) el.title = title;
}

function _formatLikelyLanguage(data) {
  const det = (data && data.detected_language) || {};
  const label = det.label || 'Unknown';
  const conf = Number.isFinite(det.confidence) ? det.confidence : null;
  const base = conf != null ? `${label} (${conf}%)` : label;
  if (data && data.language_mismatch) return base + ' *';
  return base;
}

function refreshLikelySubtitleLanguages(fileIndex) {
  const f = _files[fileIndex];
  if (!f || !f.streams) return;
  const subTracks = (f.streams && f.streams.subs) || [];

  subTracks.forEach(sub => {
    const streamIndex = sub.index;
    if (streamIndex == null) return;

    if (_isImageSubtitleCodec(sub.codec)) {
      _setLikelyCell(fileIndex, streamIndex, 'Image (OCR)', 'Image-based subtitle track');
      return;
    }

    const key = _likelyCacheKey(f.full_path, streamIndex);
    const cached = _subtitleLikelyCache[key];
    if (cached) {
      _setLikelyCell(fileIndex, streamIndex, _formatLikelyLanguage(cached), cached.language_mismatch ? 'Metadata language differs from subtitle text' : 'Detected from subtitle text preview');
      return;
    }

    if (_subtitleLikelyPending.has(key)) {
      _setLikelyCell(fileIndex, streamIndex, 'Detecting...', 'Running text-based language detection');
      return;
    }

    _subtitleLikelyPending.add(key);
    _setLikelyCell(fileIndex, streamIndex, 'Detecting...', 'Running text-based language detection');
    const qs = new URLSearchParams({
      path: f.full_path,
      stream_index: String(streamIndex),
      max_lines: '80',
      metadata_language: String(sub.language || 'und'),
    });
    fetch('/api/subtitle_preview?' + qs.toString())
      .then(async r => {
        const data = await r.json().catch(() => ({ error: 'Invalid response' }));
        return { ok: r.ok, data };
      })
      .then(({ ok, data }) => {
        if (!ok || !data.ok) {
          _setLikelyCell(fileIndex, streamIndex, 'Unknown', data.error || 'Detection failed');
          return;
        }
        _subtitleLikelyCache[key] = data;
        _setLikelyCell(fileIndex, streamIndex, _formatLikelyLanguage(data), data.language_mismatch ? 'Metadata language differs from subtitle text' : 'Detected from subtitle text preview');
      })
      .catch(() => {
        _setLikelyCell(fileIndex, streamIndex, 'Unknown', 'Detection failed');
      })
      .finally(() => {
        _subtitleLikelyPending.delete(key);
      });
  });
}

function previewSubtitleText(fileIndex, streamIndex) {
  const f = _files[fileIndex];
  if (!f || !f.full_path) return;

  const statusEl = document.getElementById('detailsSubPreviewStatus');
  const textEl = document.getElementById('detailsSubPreviewText');
  if (!statusEl || !textEl) return;

  const subs = (f.streams && f.streams.subs) || [];
  const sub = subs.find(s => Number(s.index) === Number(streamIndex));
  const metadataLanguage = (sub && sub.language) ? String(sub.language) : 'und';

  statusEl.textContent = `Loading subtitle preview for stream #${streamIndex}...`;
  textEl.textContent = '';

  const qs = new URLSearchParams({
    path: f.full_path,
    stream_index: String(streamIndex),
    max_lines: '120',
    metadata_language: metadataLanguage,
  });

  fetch('/api/subtitle_preview?' + qs.toString())
    .then(async r => {
      const data = await r.json().catch(() => ({ error: 'Invalid response' }));
      return { ok: r.ok, data };
    })
    .then(({ ok, data }) => {
      if (!ok || !data.ok) {
        statusEl.textContent = 'Subtitle preview failed';
        textEl.textContent = data.error || 'Could not extract subtitle text.';
        addLog('Subtitle preview failed: ' + (data.error || 'unknown error'), 'err');
        return;
      }

      textEl.textContent = data.preview_text || '(No subtitle text extracted.)';

      const det = data.detected_language || {};
      const detLabel = det.label || 'Unknown';
      const conf = (det.confidence != null) ? ` (${det.confidence}% confidence)` : '';
      const tag = data.metadata_language || metadataLanguage || 'und';
      const mismatch = data.language_mismatch ? ' - tag/content mismatch' : '';
      statusEl.textContent = `Tag: ${tag} - Detected: ${detLabel}${conf}${mismatch}`;
    })
    .catch(err => {
      statusEl.textContent = 'Subtitle preview failed';
      textEl.textContent = String(err || 'Unknown error');
      addLog('Subtitle preview failed: ' + err, 'err');
    });
}

function _setDetailsButtonsDisabled(disabled) {
  const createBtn = document.getElementById('detailsPreviewCreateBtn');
  const playBtn = document.getElementById('detailsPreviewPlayBtn');
  const discardBtn = document.getElementById('detailsPreviewDiscardBtn');
  const commitBtn = document.getElementById('detailsPreviewCommitBtn');
  const engCreateBtn = document.getElementById('detailsEngStereoCreateBtn');
  const engPlayBtn = document.getElementById('detailsEngStereoPlayBtn');
  const engDiscardBtn = document.getElementById('detailsEngStereoDiscardBtn');
  const engCommitBtn = document.getElementById('detailsEngStereoCommitBtn');
  if (createBtn) createBtn.disabled = disabled;
  if (playBtn) playBtn.disabled = disabled || !_detailsPreviewPath;
  if (discardBtn) discardBtn.disabled = disabled || !_detailsPreviewPath;
  if (commitBtn) commitBtn.disabled = disabled || !_detailsPreviewPath;
  if (engCreateBtn) engCreateBtn.disabled = disabled;
  if (engPlayBtn) engPlayBtn.disabled = disabled || !_detailsEngStereoPreviewPath;
  if (engDiscardBtn) engDiscardBtn.disabled = disabled || !_detailsEngStereoPreviewPath;
  if (engCommitBtn) engCommitBtn.disabled = disabled || !_detailsEngStereoPreviewPath;
}

function _setDetailsStatusText(text) {
  const statusEl = document.getElementById('detailsStreamEditStatus');
  if (statusEl) statusEl.textContent = text || '';
}

function _setEngStereoStatusText(text) {
  const statusEl = document.getElementById('detailsEngStereoStatus');
  if (statusEl) statusEl.textContent = text || '';
}

function _renderRowStatusCell(fileIndex) {
  const f = _files[fileIndex];
  const row = document.getElementById('row-' + fileIndex);
  if (!row || !f) return;
  row.cells[7].innerHTML = _badgeHtml(f.status, f.force_sw, f.force_convert) + _droppedBadgeHtml(f) + _ocrBadgeHtml(f);
}

function refreshStreamEditStatus() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f) return;

  fetch('/api/stream_edit_status?path=' + encodeURIComponent(f.full_path))
    .then(r => r.json())
    .then(data => {
      _detailsPreviewPath = data.preview_exists ? (data.preview_path || '') : '';
      _setDetailsButtonsDisabled(_detailsBusy || _appState === 'running');
      if (data.preview_exists) {
        _setDetailsStatusText('Preview ready: ' + (data.preview_size_mb || 0).toFixed(1) + ' MB');
      } else {
        _setDetailsStatusText('No preview copy yet');
      }
    })
    .catch(() => {
      _detailsPreviewPath = '';
      _setDetailsButtonsDisabled(true);
      _setDetailsStatusText('Could not read preview status');
    });
}

function refreshEngStereoStatus() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f) return;

  fetch('/api/eng_stereo_status?path=' + encodeURIComponent(f.full_path))
    .then(r => r.json())
    .then(data => {
      _detailsEngStereoPreviewPath = data.preview_exists ? (data.preview_path || '') : '';
      _setDetailsButtonsDisabled(_detailsBusy || _appState === 'running');
      if (data.preview_exists) {
        _setEngStereoStatusText('Eng stereo test ready: ' + (data.preview_size_mb || 0).toFixed(1) + ' MB');
      } else {
        _setEngStereoStatusText('No Eng stereo test copy yet');
      }
    })
    .catch(() => {
      _detailsEngStereoPreviewPath = '';
      _setDetailsButtonsDisabled(true);
      _setEngStereoStatusText('Could not read Eng stereo status');
    });
}

function createStreamEditPreview() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f) return;
  const dropped = f.dropped_streams || [];
  if (!dropped.length) {
    addLog('No dropped streams selected for ' + f.name + '.', 'warn');
    return;
  }

  _detailsBusy = true;
  _setDetailsButtonsDisabled(true);
  _setDetailsStatusText('Building preview copy...');

  fetch('/api/stream_edit_preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: f.full_path, dropped}),
  }).then(r => r.json()).then(data => {
    _detailsBusy = false;
    if (data.ok) {
      _detailsPreviewPath = data.preview_path || '';
      _setDetailsButtonsDisabled(false);
      _setDetailsStatusText('Preview ready: ' + (data.preview_size_mb || 0).toFixed(1) + ' MB');
      addLog('Created stream-edit preview: ' + f.name, 'ok');
    } else {
      _detailsPreviewPath = '';
      _setDetailsButtonsDisabled(false);
      _setDetailsStatusText('Preview failed');
      addLog('Stream-edit preview failed: ' + (data.error || 'unknown error'), 'err');
    }
  }).catch(err => {
    _detailsBusy = false;
    _detailsPreviewPath = '';
    _setDetailsButtonsDisabled(false);
    _setDetailsStatusText('Preview failed');
    addLog('Stream-edit preview failed: ' + err, 'err');
  });
}

function playStreamEditPreview() {
  if (!_detailsPreviewPath) return;
  apiOpen(_detailsPreviewPath, 'play');
}

function _persistDroppedStreams(fileIndex, newDropped, opts) {
  const f = _files[fileIndex];
  if (!f) return;
  const options = opts || {};
  fetch('/api/drop_streams', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: f.full_path, dropped: newDropped}),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      f.dropped_streams = newDropped;
      _renderRowStatusCell(fileIndex);
      if (options.refreshDetails) viewDetails(fileIndex);
      if (options.logText) addLog(options.logText, 'info');
    } else {
      addLog('drop_streams error: ' + (data.error || 'unknown'), 'err');
    }
  }).catch(err => addLog('drop_streams fetch error: ' + err, 'err'));
}

function setAllStreamsDropped(fileIndex, kind, dropAll) {
  const f = _files[fileIndex];
  if (!f || !f.streams) return;
  const source = kind === 'audio' ? (f.streams.audio || []) : (f.streams.subs || []);
  const indices = source.map(t => t.index).filter(i => i != null);
  if (!indices.length) return;

  const dropped = new Set(f.dropped_streams || []);
  indices.forEach(i => {
    if (dropAll) dropped.add(i);
    else dropped.delete(i);
  });
  const newDropped = [...dropped].sort((a, b) => a - b);
  const label = kind === 'audio' ? 'audio' : 'subtitle';
  _persistDroppedStreams(fileIndex, newDropped, {
    refreshDetails: true,
    logText: (dropAll ? 'Dropped all ' : 'Restored all ') + label + ' streams for ' + f.name,
  });
}

function discardStreamEditPreview() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f || !_detailsPreviewPath) return;

  _detailsBusy = true;
  _setDetailsButtonsDisabled(true);
  fetch('/api/stream_edit_discard', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: f.full_path}),
  }).then(r => r.json()).then(data => {
    _detailsBusy = false;
    if (data.ok) {
      _detailsPreviewPath = '';
      _setDetailsButtonsDisabled(false);
      _setDetailsStatusText('Preview discarded');
      addLog('Discarded stream-edit preview for ' + f.name, 'info');
    } else {
      _setDetailsButtonsDisabled(false);
      addLog('Discard preview failed: ' + (data.error || 'unknown error'), 'err');
    }
  }).catch(err => {
    _detailsBusy = false;
    _setDetailsButtonsDisabled(false);
    addLog('Discard preview failed: ' + err, 'err');
  });
}

function commitStreamEditPreview() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f || !_detailsPreviewPath) return;

  _detailsBusy = true;
  _setDetailsButtonsDisabled(true);
  _setDetailsStatusText('Replacing original...');
  fetch('/api/stream_edit_commit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: f.full_path}),
  }).then(r => r.json()).then(data => {
    _detailsBusy = false;
    if (!data.ok) {
      _setDetailsButtonsDisabled(false);
      _setDetailsStatusText('Replace failed');
      addLog('Replace original failed: ' + (data.error || 'unknown error'), 'err');
      return;
    }

    _detailsPreviewPath = '';
    f.dropped_streams = [];
    f.status = data.status || 'pending';
    f.output = data.output_mb != null ? _fmtMbVal(data.output_mb) : null;
    f.saved = data.saved_mb != null ? _fmtMbVal(data.saved_mb) : null;
    f.pct = data.saved_pct != null ? String(data.saved_pct) : null;
    if (data.new_size_mb != null) {
      f.size = _fmtMbVal(data.new_size_mb);
    }
    if (data.bitrate_kbps != null) {
      f.bitrate_kbps = Number(data.bitrate_kbps) || 0;
    }
    if (data.duration) {
      f.duration = data.duration;
    }
    if (data.video_track_count != null) {
      f.video_track_count = Number(data.video_track_count) || 0;
    }
    if (data.audio_track_count != null) {
      f.audio_track_count = Number(data.audio_track_count) || 0;
    }
    if (data.subtitle_track_count != null) {
      f.subtitle_track_count = Number(data.subtitle_track_count) || 0;
    }
    if (data.codec) {
      f.codec = String(data.codec).toUpperCase();
    }
    if (data.streams) {
      f.streams = data.streams;
      _syncTrackCountsFromStreams(f);
      const v = data.streams.video || {};
      const codec = (v.codec || '').toUpperCase();
      if (codec) f.codec = codec;
    }
    _renderRowStatusCell(_detailsFileIndex);
    const row = document.getElementById('row-' + _detailsFileIndex);
    if (row) {
      row.classList.toggle('tr-done', f.status === 'done');
      row.classList.toggle('tr-failed', f.status === 'failed');
      row.classList.toggle('tr-low-savings', f.status === 'low_savings');
      row.cells[3].textContent = (f.size || '0') + ' MB';
      _updateRowProbe(_detailsFileIndex, f);
      if (row.cells[12]) row.cells[12].textContent = f.output ? f.output + ' MB' : '';
      if (row.cells[13]) row.cells[13].textContent = f.saved ? f.saved + ' MB' : '';
      if (row.cells[14]) row.cells[14].textContent = f.pct ? f.pct + ' %' : '';
    }
    updateStats(_files);
    _setDetailsButtonsDisabled(false);
    _setDetailsStatusText('Original replaced. Preview cleared.');
    addLog('Stream edit accepted for ' + f.name + ' - original replaced.', 'ok');
    viewDetails(_detailsFileIndex);
  }).catch(err => {
    _detailsBusy = false;
    _setDetailsButtonsDisabled(false);
    _setDetailsStatusText('Replace failed');
    addLog('Replace original failed: ' + err, 'err');
  });
}

function openCommitStreamEditModal() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f || !_detailsPreviewPath) return;
  const label = document.getElementById('streamReplaceFileLabel');
  if (label) label.textContent = f.name;
  if (!_streamReplaceConfirmModal) {
    _streamReplaceConfirmModal = new bootstrap.Modal(document.getElementById('streamReplaceConfirmModal'));
  }
  _streamReplaceConfirmModal.show();
}

function confirmCommitStreamEditPreview() {
  if (_streamReplaceConfirmModal) _streamReplaceConfirmModal.hide();
  commitStreamEditPreview();
}

function createEngStereoPreview() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f) return;

  _detailsBusy = true;
  _setDetailsButtonsDisabled(true);
  _setEngStereoStatusText('Building Eng stereo test copy...');

  fetch('/api/eng_stereo_preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: f.full_path}),
  }).then(r => r.json()).then(data => {
    _detailsBusy = false;
    if (data.ok) {
      _detailsEngStereoPreviewPath = data.preview_path || '';
      _setDetailsButtonsDisabled(false);
      _setEngStereoStatusText('Eng stereo test ready: ' + (data.preview_size_mb || 0).toFixed(1) + ' MB');
      addLog('Created English-stereo test preview: ' + f.name, 'ok');
    } else {
      _detailsEngStereoPreviewPath = '';
      _setDetailsButtonsDisabled(false);
      _setEngStereoStatusText('Eng stereo build failed');
      addLog('English-stereo preview failed: ' + (data.error || 'unknown error'), 'err');
    }
  }).catch(err => {
    _detailsBusy = false;
    _detailsEngStereoPreviewPath = '';
    _setDetailsButtonsDisabled(false);
    _setEngStereoStatusText('Eng stereo build failed');
    addLog('English-stereo preview failed: ' + err, 'err');
  });
}

function playEngStereoPreview() {
  if (!_detailsEngStereoPreviewPath) return;
  apiOpen(_detailsEngStereoPreviewPath, 'play');
}

function discardEngStereoPreview() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f || !_detailsEngStereoPreviewPath) return;

  _detailsBusy = true;
  _setDetailsButtonsDisabled(true);
  fetch('/api/eng_stereo_discard', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: f.full_path}),
  }).then(r => r.json()).then(data => {
    _detailsBusy = false;
    if (data.ok) {
      _detailsEngStereoPreviewPath = '';
      _setDetailsButtonsDisabled(false);
      _setEngStereoStatusText('Eng stereo test discarded');
      addLog('Discarded English-stereo test preview for ' + f.name, 'info');
    } else {
      _setDetailsButtonsDisabled(false);
      addLog('Discard Eng stereo preview failed: ' + (data.error || 'unknown error'), 'err');
    }
  }).catch(err => {
    _detailsBusy = false;
    _setDetailsButtonsDisabled(false);
    addLog('Discard Eng stereo preview failed: ' + err, 'err');
  });
}

function openCommitEngStereoModal() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f || !_detailsEngStereoPreviewPath) return;
  const label = document.getElementById('engStereoReplaceFileLabel');
  if (label) label.textContent = f.name;
  if (!_engStereoReplaceConfirmModal) {
    _engStereoReplaceConfirmModal = new bootstrap.Modal(document.getElementById('engStereoReplaceConfirmModal'));
  }
  _engStereoReplaceConfirmModal.show();
}

function confirmCommitEngStereoPreview() {
  if (_engStereoReplaceConfirmModal) _engStereoReplaceConfirmModal.hide();
  commitEngStereoPreview();
}

function commitEngStereoPreview() {
  if (_detailsFileIndex == null) return;
  const f = _files[_detailsFileIndex];
  if (!f || !_detailsEngStereoPreviewPath) return;

  _detailsBusy = true;
  _setDetailsButtonsDisabled(true);
  _setEngStereoStatusText('Replacing original with Eng stereo test...');
  fetch('/api/eng_stereo_commit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: f.full_path}),
  }).then(r => r.json()).then(data => {
    _detailsBusy = false;
    if (!data.ok) {
      _setDetailsButtonsDisabled(false);
      _setEngStereoStatusText('Eng stereo replace failed');
      addLog('Replace with English-stereo failed: ' + (data.error || 'unknown error'), 'err');
      return;
    }

    _detailsEngStereoPreviewPath = '';
    f.status = data.status || 'pending';
    f.output = data.output_mb != null ? _fmtMbVal(data.output_mb) : null;
    f.saved = data.saved_mb != null ? _fmtMbVal(data.saved_mb) : null;
    f.pct = data.saved_pct != null ? String(data.saved_pct) : null;
    if (data.new_size_mb != null) {
      f.size = _fmtMbVal(data.new_size_mb);
    }
    if (data.bitrate_kbps != null) {
      f.bitrate_kbps = Number(data.bitrate_kbps) || 0;
    }
    if (data.duration) {
      f.duration = data.duration;
    }
    if (data.video_track_count != null) {
      f.video_track_count = Number(data.video_track_count) || 0;
    }
    if (data.audio_track_count != null) {
      f.audio_track_count = Number(data.audio_track_count) || 0;
    }
    if (data.subtitle_track_count != null) {
      f.subtitle_track_count = Number(data.subtitle_track_count) || 0;
    }
    if (data.codec) {
      f.codec = String(data.codec).toUpperCase();
    }
    if (data.streams) {
      f.streams = data.streams;
      _syncTrackCountsFromStreams(f);
      const v = data.streams.video || {};
      const codec = (v.codec || '').toUpperCase();
      if (codec) f.codec = codec;
    }
    _renderRowStatusCell(_detailsFileIndex);
    const row = document.getElementById('row-' + _detailsFileIndex);
    if (row) {
      row.classList.toggle('tr-done', f.status === 'done');
      row.classList.toggle('tr-failed', f.status === 'failed');
      row.classList.toggle('tr-low-savings', f.status === 'low_savings');
      row.cells[3].textContent = (f.size || '0') + ' MB';
      _updateRowProbe(_detailsFileIndex, f);
      if (row.cells[12]) row.cells[12].textContent = f.output ? f.output + ' MB' : '';
      if (row.cells[13]) row.cells[13].textContent = f.saved ? f.saved + ' MB' : '';
      if (row.cells[14]) row.cells[14].textContent = f.pct ? f.pct + ' %' : '';
    }
    updateStats(_files);
    _setDetailsButtonsDisabled(false);
    _setEngStereoStatusText('Original replaced from Eng stereo test.');
    addLog('English-stereo test accepted for ' + f.name + '. Backup saved.', 'ok');
    viewDetails(_detailsFileIndex);
  }).catch(err => {
    _detailsBusy = false;
    _setDetailsButtonsDisabled(false);
    _setEngStereoStatusText('Eng stereo replace failed');
    addLog('Replace with English-stereo failed: ' + err, 'err');
  });
}

function toggleDropStream(fileIndex, streamIdx, btn) {
  const f = _files[fileIndex];
  if (!f) return;
  const dropped = new Set(f.dropped_streams || []);
  if (dropped.has(streamIdx)) {
    dropped.delete(streamIdx);
  } else {
    dropped.add(streamIdx);
  }
  const newDropped = [...dropped].sort((a, b) => a - b);
  _persistDroppedStreams(fileIndex, newDropped, {refreshDetails: true});
}

// Returns the ffprobe stream indices of PGS subtitle tracks for a file object
function _pgsIndices(f) {
  const PGS = new Set(['PGS', 'HDMV_PGS_SUBTITLE', 'PGSSUB']);
  const subs = (f.streams && f.streams.subs) || [];
  return subs
    .filter(s => PGS.has((s.codec || '').toUpperCase()))
    .map(s => s.index)
    .filter(i => i != null);
}

function dropPgsFile(index) {
  const f = _files[index];
  const pgsIdx = _pgsIndices(f);
  if (!pgsIdx.length) {
    addLog('No PGS streams found for ' + f.name + ' — load stream data via Video Details → Probe first.', 'warn');
    return;
  }
  const existing = new Set(f.dropped_streams || []);
  pgsIdx.forEach(i => existing.add(i));
  const newDropped = [...existing].sort((a, b) => a - b);
  _persistDroppedStreams(index, newDropped, {
    logText: 'Dropped ' + pgsIdx.length + ' PGS stream(s) for ' + f.name,
  });
}

function restoreStreams(index) {
  const f = _files[index];
  _persistDroppedStreams(index, [], {
    logText: 'Restored all dropped tracks for ' + f.name,
  });
}

async function dropPgsFolder(folder) {
  // Collect all files in this folder
  const folderFiles = _files.map((f, i) => ({f, i})).filter(({f}) => (f.folder || '') === (folder || ''));
  if (!folderFiles.length) {
    addLog('No files found in folder \u201c' + (folder || 'root') + '\u201d', 'warn');
    return;
  }

  // Probe any files that don't have stream data yet
  const unprobed = folderFiles.filter(({f}) => !f.streams);
  if (unprobed.length) {
    addLog('Probing ' + unprobed.length + ' file(s) in \u201c' + (folder || 'root') + '\u201d\u2026', 'info');
    for (const {f} of unprobed) {
      try {
        const r = await fetch('/api/probe_streams?' + new URLSearchParams({path: f.full_path}));
        const data = await r.json();
        if (data.ok) f.streams = data.streams;
      } catch (_) { /* ignore probe failures for individual files */ }
    }
  }

  // Now build drop list using stream data
  const updates = [];
  const targets = [];
  folderFiles.forEach(({f, i}) => {
    const pgsIdx = _pgsIndices(f);
    if (!pgsIdx.length) return;
    const existing = new Set(f.dropped_streams || []);
    pgsIdx.forEach(x => existing.add(x));
    const newDropped = [...existing];
    updates.push({path: f.full_path, dropped: newDropped});
    targets.push({f, i, newDropped});
  });

  if (!updates.length) {
    addLog('No PGS streams found in any file in \u201c' + (folder || 'root') + '\u201d', 'info');
    return;
  }

  fetch('/api/drop_pgs_bulk', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({updates}),
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      targets.forEach(({f, i, newDropped}) => {
        f.dropped_streams = newDropped;
        const row = document.getElementById('row-' + i);
        if (row) row.cells[7].innerHTML = _badgeHtml(f.status, f.force_sw, f.force_convert) + _droppedBadgeHtml(f);
      });
      addLog('Dropped PGS tracks for ' + updates.length + ' file(s) in \u201c' + (folder || 'root') + '\u201d', 'info');
    } else {
      addLog('drop_pgs_bulk error: ' + (d.error || 'unknown'), 'error');
    }
  }).catch(err => addLog('drop_pgs_bulk fetch error: ' + err, 'error'));
}

function probeStreams(index) {
  const f = _files[index];
  if (!f) return;
  fetch('/api/probe_streams?' + new URLSearchParams({path: f.full_path}))
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        f.streams = data.streams;
        viewDetails(index);
      } else {
        addLog('probe_streams error: ' + (data.error || 'unknown'), 'error');
      }
    })
    .catch(err => addLog('probe_streams fetch error: ' + err, 'error'));
}

function diagnoseFile(index) {
  const f = _files[index];
  const modal = new bootstrap.Modal(document.getElementById('diagnoseModal'));
  const titleEl = document.getElementById('diagnoseModalTitle');
  const spinner = document.getElementById('diagnoseSpinner');
  const output  = document.getElementById('diagnoseOutput');
  const copyBtn = document.getElementById('diagnoseCopyBtn');

  titleEl.textContent = f.name;
  spinner.style.display = '';
  output.style.display  = 'none';
  copyBtn.style.display = 'none';
  output.textContent    = '';
  modal.show();

  fetch('/api/diagnose?path=' + encodeURIComponent(f.full_path))
    .then(r => r.json())
    .then(data => {
      spinner.style.display = 'none';
      if (data.error) {
        output.textContent = 'ERROR: ' + data.error;
        output.style.display = '';
        return;
      }
      const lines = [];
      lines.push('=== Diagnostic Report ===');
      lines.push('File: ' + data.path);
      lines.push('');
      (data.sections || []).forEach(sec => {
        lines.push('── ' + sec.title + ' ──');
        (sec.lines || []).forEach(l => lines.push(l));
        lines.push('');
      });
      output.textContent = lines.join('\n');
      output.style.display  = '';
      copyBtn.style.display = '';
    })
    .catch(err => {
      spinner.style.display = 'none';
      output.textContent = 'Network error: ' + err;
      output.style.display = '';
    });
}

function copyDiagnostics() {
  const text = document.getElementById('diagnoseOutput').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('diagnoseCopyBtn');
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-check2 me-1"></i>Copied!';
    setTimeout(() => { btn.innerHTML = orig; }, 2000);
  });
}

function retryFile(index) {
  const f = _files[index];
  const row = document.getElementById('row-' + index);
  if (row) {
    row.classList.remove('tr-failed');
    row.cells[7].innerHTML = '<span class="badge badge-pending">pending</span>';
  }
  _files[index].status = 'pending';
  addLog('Queued for retry: ' + f.name, 'info');
}

function skipFile(index) {
  const f = _files[index];
  fetch('/api/update_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [f.full_path], status: 'skipped' }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _files[index].status = 'skipped';
      const row = document.getElementById('row-' + index);
      if (row) {
        row.classList.remove('tr-failed', 'tr-pending');
        row.classList.add('tr-skipped');
        row.cells[7].innerHTML = _badgeHtml('skipped', _files[index].force_sw, _files[index].force_convert);
      }
      updateStats(_files);
      addLog('Skipped: ' + f.name, 'warn');
    } else {
      addLog('Skip failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(() => addLog('Could not reach server.', 'err'));
}

function unskipFile(index) {
  const f = _files[index];
  fetch('/api/update_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [f.full_path], status: 'pending' }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _files[index].status = 'pending';
      const row = document.getElementById('row-' + index);
      if (row) {
        row.classList.remove('tr-skipped');
        row.cells[7].innerHTML = _badgeHtml('pending', _files[index].force_sw, _files[index].force_convert);
      }
      updateStats(_files);
      _refreshQueueStateFromFiles();
      addLog('Reset to pending: ' + f.name, 'info');
    } else {
      addLog('Un-skip failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(() => addLog('Could not reach server.', 'err'));
  _refreshQueueStateFromFiles();
}

function forceSw(index) {
  _refreshQueueStateFromFiles();
  const f = _files[index];
  fetch('/api/update_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [f.full_path], status: 'pending', force_sw: true }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _files[index].force_sw = true;
      _files[index].status = 'pending';
      const row = document.getElementById('row-' + index);
      if (row) {
        row.classList.remove('tr-skipped', 'tr-failed');
        row.cells[7].innerHTML = _badgeHtml('pending', true, !!_files[index].force_convert);
      }
      updateStats(_files);
      _refreshQueueStateFromFiles();
      addLog('SW-only mode enabled: ' + f.name, 'warn');
    } else {
      addLog('Force SW failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(() => addLog('Could not reach server.', 'err'));
}

function unforceSw(index) {
  const f = _files[index];
  fetch('/api/update_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [f.full_path], force_sw: false }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _files[index].force_sw = false;
      const row = document.getElementById('row-' + index);
      if (row) row.cells[7].innerHTML = _badgeHtml(_files[index].status, false, !!_files[index].force_convert);
      addLog('SW-only mode cleared: ' + f.name, 'info');
      _refreshQueueStateFromFiles();
    } else {
      addLog('Clear SW failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(() => addLog('Could not reach server.', 'err'));
}

function forceConvert(index) {
  _refreshQueueStateFromFiles();
  const f = _files[index];
  fetch('/api/update_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [f.full_path], status: 'pending', force_convert: true }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _files[index].force_convert = true;
      _files[index].status = 'pending';
      const row = document.getElementById('row-' + index);
      if (row) {
        row.classList.remove('tr-skipped', 'tr-failed');
        row.cells[7].innerHTML = _badgeHtml('pending', !!_files[index].force_sw, true);
      }
      updateStats(_files);
      _refreshQueueStateFromFiles();
      addLog('Force convert enabled: ' + f.name, 'warn');
    } else {
      addLog('Force convert failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(() => addLog('Could not reach server.', 'err'));
}

function unforceConvert(index) {
  const f = _files[index];
  fetch('/api/update_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [f.full_path], force_convert: false }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      _files[index].force_convert = false;
      const row = document.getElementById('row-' + index);
      if (row) row.cells[7].innerHTML = _badgeHtml(_files[index].status, !!_files[index].force_sw, false);
      addLog('Force convert cleared: ' + f.name, 'info');
      _refreshQueueStateFromFiles();
    } else {
      addLog('Clear force convert failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(() => addLog('Could not reach server.', 'err'));
}

function resetToPending(index) {
  const f = _files[index];
  if (!f) return;
  fetch('/api/update_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [f.full_path], status: 'pending' }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      f.status = 'pending';
      f.output = null;
      f.saved = null;
      f.pct = null;
      f.output_path = null;
      f.output_size_mb = null;
      f.output_hash = null;
      f.output_bitrate_kbps = null;
      const row = document.getElementById('row-' + index);
      if (row) {
        row.classList.remove('tr-done', 'tr-failed', 'tr-low-savings', 'tr-skipped', 'tr-converting');
        row.cells[7].innerHTML = _badgeHtml('pending', !!f.force_sw, !!f.force_convert) + _droppedBadgeHtml(f) + _ocrBadgeHtml(f);
        if (row.cells[12]) row.cells[12].textContent = '';
        if (row.cells[13]) row.cells[13].textContent = '';
        if (row.cells[14]) row.cells[14].innerHTML = '';
        if (row.cells[15]) row.cells[15].textContent = '';
        if (row.cells[16]) row.cells[16].textContent = '';
      }
      updateStats(_files);
      applyFilter();
      addLog('Reset to pending: ' + f.name, 'info');
    } else {
      addLog('Reset failed: ' + (d.error || 'unknown'), 'err');
    }
  })
  .catch(() => addLog('Could not reach server.', 'err'));
}

function removeFromQueue(index) {
  _files.splice(index, 1);
  populateTable(_files);
  updateStats(_files);
  addLog('Removed from queue.', 'info');
  if (_files.length === 0) setButtonStates('idle');
}

// ============================================================
// Theme toggle
// ============================================================
function _storedTheme() {
  try { return localStorage.getItem('vc-theme') || 'light'; }
  catch(e) { return 'light'; }
}
function _saveTheme(t) {
  try { localStorage.setItem('vc-theme', t); } catch(e) {}
}

function toggleTheme() {
  const html = document.documentElement;
  const icon = document.getElementById('themeIcon');
  const isDark = html.getAttribute('data-bs-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  html.setAttribute('data-bs-theme', next);
  icon.className = isDark ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
  _saveTheme(next);
}

function toggleTheme() {
  const html = document.documentElement;
  const icon = document.getElementById('themeIcon');
  const isDark = html.getAttribute('data-bs-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  html.setAttribute('data-bs-theme', next);
  icon.className = isDark ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
  _saveTheme(next);
}

// Sync icon to whatever theme was applied before paint
(function() {
  const saved = _storedTheme();
  const icon = document.getElementById('themeIcon');
  if (icon) icon.className = saved === 'dark' ? 'bi bi-moon-stars-fill' : 'bi bi-sun-fill';
})();

// ============================================================
// Right-panel vertical splitter
// ============================================================
(function() {
  const STORAGE_KEY = 'vc-job-panel-height';
  const DEFAULT_H   = 260;
  const MIN_H       = 80;

  function init() {
    const splitter = document.getElementById('rightSplitter');
    const jobCard  = document.getElementById('currentJobCard');
    if (!splitter || !jobCard) return;

    // Restore persisted height
    const saved = parseInt(localStorage.getItem(STORAGE_KEY), 10);
    if (saved >= MIN_H) jobCard.style.height = saved + 'px';

    splitter.addEventListener('mousedown', function(e) {
      e.preventDefault();
      const startY = e.clientY;
      const startH = jobCard.getBoundingClientRect().height;
      splitter.classList.add('dragging');
      document.body.style.cursor = 'ns-resize';
      document.body.style.userSelect = 'none';

      function onMove(e) {
        const maxH = window.innerHeight * 0.72;
        const newH = Math.max(MIN_H, Math.min(maxH, startH + (e.clientY - startY)));
        jobCard.style.height = newH + 'px';
      }
      function onUp() {
        splitter.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        localStorage.setItem(STORAGE_KEY, Math.round(jobCard.getBoundingClientRect().height));
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

// ============================================================
// Settings
// ============================================================
let _settingsModal = null;

function openSettings() {
  fetch('/api/settings')
    .then(r => r.json())
    .then(s => {
      document.getElementById('qsvQuality').value    = s.qsv_quality;
      document.getElementById('qsvQualityVal').textContent = s.qsv_quality;
      document.getElementById('swCrf').value         = s.sw_hevc_crf;
      document.getElementById('swCrfVal').textContent = s.sw_hevc_crf;
      document.getElementById('settingsTempDir').value      = s.local_temp_dir;
      document.getElementById('settingsKeepFailedIntermediates').checked = !!s.keep_failed_intermediates;
      document.getElementById('settingsDefaultSort').value  = s.default_sort || 'bitrate';
      const thr = s.low_savings_threshold_pct !== undefined ? s.low_savings_threshold_pct : 5;
      document.getElementById('settingsLowSavingsThreshold').value = thr;
      document.getElementById('lowSavingsThresholdVal').textContent = thr;
    })
    .catch(() => {}); // show modal even if fetch fails — defaults already in HTML
  _settingsModal = new bootstrap.Modal(document.getElementById('settingsModal'));
  _settingsModal.show();
}

function saveSettings() {
  const payload = {
    qsv_quality:               parseInt(document.getElementById('qsvQuality').value, 10),
    sw_hevc_crf:               parseInt(document.getElementById('swCrf').value, 10),
    local_temp_dir:            document.getElementById('settingsTempDir').value.trim(),
    keep_failed_intermediates: !!document.getElementById('settingsKeepFailedIntermediates').checked,
    default_sort:              document.getElementById('settingsDefaultSort').value,
    low_savings_threshold_pct: parseInt(document.getElementById('settingsLowSavingsThreshold').value, 10),
  };
  fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  .then(r => r.json())
  .then(() => {
    // Apply sort change immediately if queue is loaded
    if (_files.length > 0 && payload.default_sort !== _sortBy) {
      _sortBy = payload.default_sort;
      const ss = document.getElementById('sortSelect');
      if (ss) ss.value = _sortBy;
      setSortBy(_sortBy);
    }
    addLog('Settings saved.', 'ok');
    if (_settingsModal) _settingsModal.hide();
  })
  .catch(e => addLog('Failed to save settings: ' + e, 'err'));
}

// Load settings on startup and apply defaults
// Set the dropdown to the JS default immediately so it's correct before the fetch resolves.
document.addEventListener('DOMContentLoaded', function() {
  const ss = document.getElementById('sortSelect');
  if (ss) ss.value = _sortBy;
  _updateSortDirBtn();
});

(function _applyStartupSettings() {
  fetch('/api/settings')
    .then(r => r.json())
    .then(s => {
      if (s.default_sort) {
        _sortBy = s.default_sort;
        const ss = document.getElementById('sortSelect');
        if (ss) ss.value = _sortBy;
        _updateSortDirBtn();
      }
      if (s.anime_mode !== undefined) {
        const am = document.getElementById('animeMode');
        if (am) am.checked = s.anime_mode;
      }
    })
    .catch(() => {});
})();

// Auto-save anime_mode whenever the navbar checkbox changes
(function _wireAnimeModeAutoSave() {
  const am = document.getElementById('animeMode');
  if (am) am.addEventListener('change', function() {
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ anime_mode: this.checked }),
    });
  });
})();

// ============================================================
// Page-load recovery — restore queue if a job is active / completed
// ============================================================
(function _recoverJobState() {
  fetch('/api/status')
    .then(r => r.json())
    .then(s => {
      if (!s.files || s.files.length === 0 || s.state === 'idle') return;
      // Restore the file list from the persisted job state
      _files = s.files.map(f => Object.assign({}, f));
      // Guess the root folder from the first file path
      const firstPath = (_files[0] || {}).full_path || '';
      const folderGuess = firstPath.replace(/[\\/][^\\/]+$/, '');
      if (folderGuess) {
        _currentScanPath = folderGuess;
        document.getElementById('folderPath').textContent = folderGuess;
        const rb = document.getElementById('rescanBtn');
        if (rb) rb.disabled = false;
      }
      setSortBy(_sortBy);
      updateStats(_files);
      if (s.state === 'running') {
        setButtonStates('running');
        _startPolling();
        addLog('Reconnected to running conversion.', 'info');
      } else {
        setButtonStates('ready');
        addLog('Previous job ' + s.state + '. Queue restored.', 'info');
      }
    })
    .catch(() => {});
})();

// ============================================================
// Folder browser
// ============================================================
// Re-scan current folder
// ============================================================
function _cancelProbeStream() {
  if (_scanEs) { _scanEs.close(); _scanEs = null; _scanStripHide(); }
}

function rescanFolder() {
  if (_currentScanPath) scanFolder(_currentScanPath);
}

async function cleanupLegacyFolders(path = _currentScanPath) {
  if (!path) return;
  _cancelProbeStream();
  const btn = document.getElementById('cleanupBtn');
  if (btn) btn.disabled = true;

  // Stop any in-flight estimation so no sample encode holds a file open.
  if (_estRunning) {
    _estCancelled = true;
    _estRunning   = false;
    await new Promise(resolve => {
      const deadline = Date.now() + 12000;
      const poll = setInterval(() => {
        if (!_estRunning || Date.now() > deadline) { clearInterval(poll); resolve(); }
      }, 200);
    });
  }

  // Prepare and show the progress modal
  const modalEl = document.getElementById('cleanupModal');
  const modal   = bootstrap.Modal.getOrCreateInstance(modalEl);
  document.getElementById('cleanupBar').style.width      = '0%';
  document.getElementById('cleanupCountLabel').textContent = 'Scanning\u2026';
  document.getElementById('cleanupMovedLabel').textContent = '';
  document.getElementById('cleanupCurrentFile').textContent = '\u00a0';
  modal.show();

  let movedFinal = 0;
  await new Promise(resolve => {
    const es = new EventSource('/api/cleanup_stream?path=' + encodeURIComponent(path));
    es.onmessage = e => {
      const ev = JSON.parse(e.data);
      if (ev.type === 'scan_done') {
        const t = ev.total;
        document.getElementById('cleanupCountLabel').textContent = t === 0 ? 'Nothing to move' : '0 / ' + t;
        if (t === 0) document.getElementById('cleanupBar').style.width = '100%';
      } else if (ev.type === 'progress') {
        const pct = ev.total > 0 ? Math.round(ev.done / ev.total * 100) : 100;
        document.getElementById('cleanupBar').style.width      = pct + '%';
        document.getElementById('cleanupCountLabel').textContent = ev.done + ' / ' + ev.total;
        document.getElementById('cleanupCurrentFile').textContent = ev.name || '\u00a0';
      } else if (ev.type === 'done') {
        movedFinal = ev.moved;
        document.getElementById('cleanupBar').style.width      = '100%';
        document.getElementById('cleanupCountLabel').textContent = ev.total !== undefined ? ev.total + ' / ' + ev.total : 'Done';
        let lbl = ev.moved + ' moved';
        if (ev.renamed) lbl += ', ' + ev.renamed + ' renamed';
        if (ev.skipped) lbl += ', ' + ev.skipped + ' skipped';
        if (ev.errors)  lbl += ', ' + ev.errors  + ' errors';
        document.getElementById('cleanupMovedLabel').textContent  = lbl;
        document.getElementById('cleanupCurrentFile').textContent = '\u00a0';
        es.close();
        resolve();
      } else if (ev.type === 'error') {
        document.getElementById('cleanupCountLabel').textContent = 'Error: ' + (ev.message || 'unknown');
        es.close();
        resolve();
      }
    };
    es.onerror = () => { es.close(); resolve(); };
  });

  // Brief pause so the user can see the final state, then close
  setTimeout(() => {
    modal.hide();
    if (btn) btn.disabled = false;
    if (movedFinal > 0) {
      if (path === _currentScanPath) rescanFolder();
      else scanFolder(path);
    }
  }, 900);
}

// ============================================================
let _selectedPath = null;
let _modal = null;
let _prepEstimateRoot = null; // set during estimate-only pass; triggers buildPrepQueue on done

const _MODAL_ACTION_BTNS = ['confirmFolderBtn', 'modalLoadBtn', 'modalPrepBtn', 'modalAnalyseBtn', 'modalCleanupBtn'];

function _resetModalButtons() {
  document.getElementById('selectedPathDisplay').textContent = 'No folder selected';
  _MODAL_ACTION_BTNS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = true;
  });
}

function openBrowser() {
  _selectedPath = null;
  _resetModalButtons();
  _modal = new bootstrap.Modal(document.getElementById('folderModal'));
  _modal.show();
  browseTo(localStorage.getItem('vc_last_folder') || '');
}

// Aliases — all just open the unified folder browser
function openBrowserForPrep()     { openBrowser(); }
function openBrowserForAnalysis() { openBrowser(); }

function browseTo(path) {
  _selectedPath = null;
  _resetModalButtons();
  const listing = document.getElementById('dirListing');
  listing.innerHTML = '<div class="text-center text-secondary py-5"><div class="spinner-border spinner-border-sm"></div> Loading\u2026</div>';

  fetch('/api/browse?path=' + encodeURIComponent(path))
    .then(async r => {
      const contentType = (r.headers.get('content-type') || '').toLowerCase();
      if (!r.ok) {
        const body = await r.text();
        const details = body ? body.slice(0, 120).replace(/\s+/g, ' ').trim() : ('HTTP ' + r.status);
        throw new Error(details);
      }
      if (!contentType.includes('application/json')) {
        const body = await r.text();
        const details = body ? body.slice(0, 120).replace(/\s+/g, ' ').trim() : 'Non-JSON response';
        throw new Error(details);
      }
      return r.json();
    })
    .then(data => {
      if (data.error) {
        listing.innerHTML = '<div class="p-3 text-danger">' + data.error + '</div>';
        return;
      }
      renderBreadcrumb(data.path, data.parent);
      renderListing(data.dirs, data.path, data.parent);
    })
    .catch(e => {
      const message = (e && e.message) ? e.message : String(e);
      listing.innerHTML = '<div class="p-3 text-danger">Unable to load folders. ' + message + '</div>';
    });
}

function renderBreadcrumb(currentPath, parent) {
  const bc = document.getElementById('breadcrumb');
  bc.innerHTML = '';

  if (!currentPath) {
    const li = document.createElement('li');
    li.className = 'breadcrumb-item active';
    li.textContent = 'This PC';
    bc.appendChild(li);
    return;
  }

  const parts = currentPath.split('/').filter(p => p.length > 0);
  const crumbs = [{ label: 'This PC', path: '' }];
  let built = '';
  for (const p of parts) {
    built = built ? built + '/' + p : (p.endsWith(':') ? p + '/' : p);
    crumbs.push({ label: p, path: built });
  }

  crumbs.forEach((c, i) => {
    const li = document.createElement('li');
    const isLast = i === crumbs.length - 1;
    li.className = 'breadcrumb-item' + (isLast ? ' active' : '');
    if (!isLast) {
      const a = document.createElement('a');
      a.href = '#';
      a.textContent = c.label;
      a.addEventListener('click', e => { e.preventDefault(); browseTo(c.path); });
      li.appendChild(a);
    } else {
      li.textContent = c.label;
    }
    bc.appendChild(li);
  });
}

function renderListing(dirs, currentPath, parent) {
  const listing = document.getElementById('dirListing');
  listing.innerHTML = '';
  const ul = document.createElement('ul');
  ul.className = 'list-group list-group-flush';

  // Up one level row
  if (parent !== null && parent !== undefined) {
    const li = document.createElement('li');
    li.className = 'list-group-item list-group-item-action d-flex align-items-center gap-2 py-2';
    li.style.cursor = 'pointer';
    li.innerHTML = '<i class="bi bi-arrow-up text-secondary"></i><span class="text-secondary fst-italic">.. up one level</span>';
    li.addEventListener('click', () => browseTo(parent));
    ul.appendChild(li);
  }

  // "Use current folder" highlighted row
  if (currentPath) {
    const li = document.createElement('li');
    li.className = 'list-group-item d-flex align-items-center gap-2 py-2';
    li.style.background = 'var(--bs-primary-bg-subtle)';

    const icon = document.createElement('i');
    icon.className = 'bi bi-folder2-open text-warning';

    const label = document.createElement('span');
    label.className = 'flex-grow-1 fw-semibold';
    label.textContent = currentPath;

    const btn = document.createElement('button');
    btn.className = 'btn btn-sm btn-primary';
    btn.textContent = 'Use This Folder';
    btn.addEventListener('click', () => selectFolder(currentPath));

    li.appendChild(icon);
    li.appendChild(label);
    li.appendChild(btn);
    ul.appendChild(li);
  }

  // Subdirectory rows
  dirs.forEach(d => {
    const li = document.createElement('li');
    li.className = 'list-group-item list-group-item-action d-flex align-items-center gap-2 py-2';
    li.style.cursor = 'pointer';

    const icon = document.createElement('i');
    icon.className = 'bi bi-folder text-warning';

    const name = document.createElement('span');
    name.className = 'flex-grow-1';
    name.textContent = d.name;

    li.appendChild(icon);
    li.appendChild(name);

    if (d.has_children) {
      const chevron = document.createElement('i');
      chevron.className = 'bi bi-chevron-right text-secondary';
      li.appendChild(chevron);
    }

    li.addEventListener('click', () => browseTo(d.full_path));
    ul.appendChild(li);
  });

  if (ul.children.length === 0) {
    listing.innerHTML = '<div class="p-3 text-secondary">No subfolders found.</div>';
    return;
  }

  listing.appendChild(ul);
}

function selectFolder(path) {
  _selectedPath = path;
  document.getElementById('selectedPathDisplay').textContent = path;
  _MODAL_ACTION_BTNS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = false;
  });
}

// Modal action handlers
function confirmFolder() {
  if (!_selectedPath) return;
  document.getElementById('folderPath').textContent = _selectedPath;
  if (_modal) _modal.hide();
  scanFolder(_selectedPath);
}

function modalActionAnalyse() {
  if (!_selectedPath) return;
  if (_modal) _modal.hide();
  document.getElementById('faRoot').value = _selectedPath;
  const analysisModal = bootstrap.Modal.getOrCreateInstance(document.getElementById('folderAnalysisModal'));
  analysisModal.show();
  runFolderAnalysis();
}

function modalActionPrep() {
  if (!_selectedPath) return;
  if (_modal) _modal.hide();
  startPrepEstimate(_selectedPath);
}

function modalActionCleanup() {
  if (!_selectedPath) return;
  if (_modal) _modal.hide();
  cleanupLegacyFolders(_selectedPath);
}

async function modalActionLoad() {
  if (!_selectedPath) return;
  if (_modal) _modal.hide();
  await loadFromDb(_selectedPath);
}

// ============================================================
// Folder Analysis
// ============================================================
function runFolderAnalysis() {
  ['faSpinner','faTableWrap','faEmpty','faError'].forEach(id => {
    document.getElementById(id).style.display = id === 'faSpinner' ? '' : 'none';
  });
  document.getElementById('faSummary').textContent = '';

  const root       = (document.getElementById('faRoot').value || '').trim();
  const minDone    = document.getElementById('faMinDone').value;
  const minPending = document.getElementById('faMinPending').value;
  const top        = document.getElementById('faTop').value;
  const sort       = document.getElementById('faSort').value;

  const params = new URLSearchParams({ root, min_done: minDone, min_pending: minPending, top, sort });

  fetch('/api/analyse_folders?' + params)
    .then(r => r.json())
    .then(data => {
      document.getElementById('faSpinner').style.display = 'none';
      if (data.error) {
        const el = document.getElementById('faError');
        el.textContent = data.error;
        el.style.display = '';
        return;
      }
      if (!data.rows || data.rows.length === 0) {
        document.getElementById('faEmpty').style.display = '';
        return;
      }
      _renderFolderAnalysisTable(data);
      document.getElementById('faTableWrap').style.display = '';
      const staleNote = data.stale_removed > 0 ? ` · ${data.stale_removed} stale record${data.stale_removed === 1 ? '' : 's'} removed` : '';
      document.getElementById('faSummary').textContent =
        `${data.total_analysed} folders · ${data.opportunity_count} with opportunity · ` +
        `${data.total_pending} pending files · Est. ${_faMbStr(data.total_est_mb)} additional savings` + staleNote;
    })
    .catch(e => {
      document.getElementById('faSpinner').style.display = 'none';
      const el = document.getElementById('faError');
      el.textContent = 'Request failed: ' + e.message;
      el.style.display = '';
    });
}

function _faMbStr(mb) {
  if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
  return mb.toFixed(0) + ' MB';
}

function _renderFolderAnalysisTable(data) {
  const tbody = document.getElementById('faBody');
  tbody.innerHTML = '';

  data.rows.forEach((r, i) => {
    const tr = document.createElement('tr');

    const pct = r.avg_savings_pct;
    const pctCls = pct >= 40 ? 'text-success fw-semibold' : pct >= 20 ? 'text-warning' : 'text-secondary';
    const pendingHtml = r.pending_count > 50
      ? `<span class="text-success fw-semibold">${r.pending_count}</span>`
      : r.pending_count;
    const speedHtml = r.avg_speed != null ? r.avg_speed.toFixed(1) + '&times;' : '&mdash;';
    const allNoSize = r.pending_no_size === r.pending_count;
    const someNoSize = r.pending_no_size > 0 && !allNoSize;
    let estHtml;
    if (allNoSize) {
      estHtml = '<span class="text-secondary" title="No size data for pending files — re-scan this folder to populate">? (no size data)</span>';
    } else if (someNoSize) {
      const partial = _faMbStr(r.est_additional_mb);
      estHtml = `<span title="${r.pending_no_size} of ${r.pending_count} pending files have no size data — estimate is partial">~${i < 5 ? '<strong>' + partial + '</strong>' : partial}</span>`;
    } else {
      estHtml = i < 5
        ? `<strong>${_faMbStr(r.est_additional_mb)}</strong>`
        : _faMbStr(r.est_additional_mb);
    }

    tr.innerHTML =
      `<td class="font-monospace small text-break" style="max-width:420px" title="${r.folder}">${r.folder}</td>` +
      `<td class="text-end text-secondary">${r.done_count}</td>` +
      `<td class="text-end">${pendingHtml}</td>` +
      `<td class="text-end ${pctCls}">${Math.round(pct)}%</td>` +
      `<td class="text-end text-secondary">${speedHtml}</td>` +
      `<td class="text-end text-secondary">${_faMbStr(r.total_saved_mb)}</td>` +
      `<td class="text-end">${estHtml}</td>` +
      `<td class="text-end text-secondary">${r.priority_score.toFixed(1)}</td>`;
    tbody.appendChild(tr);
  });

  const tfoot = document.getElementById('faFoot');
  tfoot.innerHTML =
    `<tr class="table-secondary fw-semibold">` +
    `<td>Total (${data.rows.length} shown)</td>` +
    `<td></td>` +
    `<td class="text-end">${data.total_pending}</td>` +
    `<td></td><td></td><td></td>` +
    `<td class="text-end">${_faMbStr(data.total_est_mb)}</td>` +
    `<td></td></tr>`;
}

// ============================================================
// Prep for Analysis
// ============================================================
async function loadFromDb(path) {
  addLog('Loading records from database for ' + path + '\u2026', 'info');
  let data;
  try {
    const resp = await fetch('/api/load_from_db?root=' + encodeURIComponent(path));
    data = await resp.json();
  } catch (e) {
    addLog('Load from DB error: ' + e, 'err');
    return;
  }
  if (data.error) { addLog('Load from DB error: ' + data.error, 'err'); return; }
  if (!data.files || data.files.length === 0) {
    addLog('Load from DB: no records found under ' + path + '. Try Scan instead.', 'warn');
    return;
  }

  _currentScanPath = path;
  localStorage.setItem('vc_last_folder', path);
  document.getElementById('folderPath').textContent = path;

  _files = data.files.map(f => Object.assign({}, f));
  _fileIndexByPath = {};
  _files.forEach((f, i) => { _fileIndexByPath[f.full_path] = i; });
  populateTable(_files);
  updateStats(_files);
  setButtonStates('ready');

  const done    = _files.filter(f => f.status === 'done').length;
  const pending = _files.filter(f => f.status === 'pending' || f.status === 'failed').length;
  const totalGB = (_files.reduce((s, f) => s + (parseFloat((f.size || '0').replace(/,/g, '')) || 0), 0) / 1024).toFixed(1);
  addLog(`Loaded ${data.total} records from DB \u2014 ${totalGB}\u202fGB \u2014 ${done} done, ${pending} pending.`, 'ok');
}

async function startPrepEstimate(path) {
  addLog('Prep: gathering pending files under ' + path + '\u2026', 'info');
  let data;
  try {
    const resp = await fetch('/api/prep_scan?root=' + encodeURIComponent(path));
    data = await resp.json();
  } catch (e) {
    addLog('Prep scan error: ' + e, 'err');
    return;
  }
  if (data.error) { addLog('Prep error: ' + data.error, 'err'); return; }
  if (!data.files || data.files.length === 0) {
    addLog('Prep: no pending files found under ' + path, 'warn');
    return;
  }

  addLog('Prep: ' + data.total + ' pending files found \u2014 starting estimate pass\u2026', 'info');

  // Populate the table so the user sees the files being estimated
  _currentScanPath = path;
  document.getElementById('folderPath').textContent = path;
  _files = data.files.map(f => Object.assign({}, f));
  _fileIndexByPath = {};
  _files.forEach((f, i) => { _fileIndexByPath[f.full_path] = i; });
  populateTable(_files);
  updateStats(_files);

  _prepEstimateRoot = path;
  _logCursor = 0;
  _sessionSavedMB = 0;
  _sessionEstimatedMB = 0;
  _sessionProcessed = 0;
  _updateSessionCard();

  let startData;
  try {
    const startResp = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ files: data.files, estimate_only: true, anime_mode: false, force_reestimate: true }),
    });
    startData = await startResp.json();
  } catch (e) {
    addLog('Prep start error: ' + e, 'err');
    _prepEstimateRoot = null;
    return;
  }
  if (startData.error) {
    addLog('Prep start error: ' + startData.error, 'err');
    _prepEstimateRoot = null;
    return;
  }
  setButtonStates('running');
  _startPolling();
}

async function buildPrepQueue(root) {
  addLog('Prep: selecting one representative file per folder\u2026', 'info');
  let data;
  try {
    const resp = await fetch('/api/build_prep_queue?root=' + encodeURIComponent(root));
    data = await resp.json();
  } catch (e) {
    addLog('Prep build error: ' + e, 'err');
    return;
  }
  if (data.error) { addLog('Prep build error: ' + data.error, 'err'); return; }

  const n = (data.files || []).length;
  if (n === 0) {
    addLog('Prep: nothing to queue \u2014 all folders either already seeded or all low-savings.', 'warn');
    return;
  }

  // Replace the table with the selected representative files
  _files = data.files.map(f => Object.assign({}, f));
  _fileIndexByPath = {};
  _files.forEach((f, i) => { _fileIndexByPath[f.full_path] = i; });
  populateTable(_files);
  updateStats(_files);

  const parts = [n + ' file' + (n === 1 ? '' : 's') + ' queued across ' + data.folders_seeded + ' folder' + (data.folders_seeded === 1 ? '' : 's')];
  if (data.folders_already_seeded > 0) parts.push(data.folders_already_seeded + ' already seeded');
  if (data.folders_no_candidates  > 0) parts.push(data.folders_no_candidates  + ' all low-savings');
  addLog('Prep complete: ' + parts.join(', ') + '. Press Start to convert.', 'ok');
}
