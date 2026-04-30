// ============================================================
// State
// ============================================================
let _files        = [];   // file objects loaded by scan
let _fileIndexByPath = {}; // full_path → _files index for O(1) probe/remove lookup
let _scanEs       = null; // active EventSource for /api/scan
let _probeTotal   = 0;    // files queued for phase-2 probe
let _probeDone    = 0;    // probe events received so far
let _activeFilter = 'all';
let _searchQuery  = '';
let _filterSizeMin = ''; let _filterSizeMax = '';
let _filterBrMin   = ''; let _filterBrMax   = '';
let _filterDurMin  = ''; let _filterDurMax  = '';
let _appState     = 'idle';  // idle | scanning | ready | running | done | stopped
let _dragSrcIndex = null;
let _sortBy       = 'bitrate'; // 'bitrate' | 'size' | 'name'
let _currentScanPath = null;  // last successfully scanned folder path

// Estimation background task state
let _estUserPaused  = false;  // user clicked the strip
let _estAutoPaused  = false;  // conversion is running
let _estCancelled   = false;  // new scan started
let _estRunning     = false;  // chain is live (not dormant)
let _estDone        = 0;
let _estTotal       = 0;
let _estLastFolder  = null;

// Parse "H:MM:SS" or "MM:SS" → seconds
function _parseDuration(d) {
  if (!d) return 0;
  const parts = d.split(':').map(Number);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return 0;
}

// Compute bitrate in kbps from file object
function _fileBitrate(f) {
  const mb   = parseFloat((f.size || '0').replace(/,/g, '')) || 0;
  const secs = _parseDuration(f.duration);
  return secs > 0 ? Math.round(mb * 8192 / secs) : 0;
}

function _sortFiles(files) {
  const arr = [...files];
  if (_sortBy === 'bitrate') arr.sort((a, b) => _fileBitrate(b) - _fileBitrate(a));
  else if (_sortBy === 'size') arr.sort((a, b) => (parseFloat((b.size||'0').replace(/,/g,''))||0) - (parseFloat((a.size||'0').replace(/,/g,''))||0));
  else if (_sortBy === 'name') arr.sort((a, b) => a.name.localeCompare(b.name));
  return arr;
}

function setSortBy(val) {
  _sortBy = val;
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
  document.getElementById('pauseBtn').disabled  = state !== 'running';
  document.getElementById('stopBtn').disabled   = !(state === 'running' || state === 'scanning');
  const hstopBtn = document.getElementById('hstopBtn');
  if (hstopBtn) hstopBtn.disabled = state !== 'running';
  const rescanBtn = document.getElementById('rescanBtn');
  if (rescanBtn) rescanBtn.disabled = !_currentScanPath || state === 'running';
  const cleanupBtn = document.getElementById('cleanupBtn');
  if (cleanupBtn) cleanupBtn.disabled = !_currentScanPath || state === 'running';
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

  // col 4 — Duration
  const tdDur = document.createElement('td');
  tdDur.className = 'text-secondary';
  tdDur.textContent = f.duration || '\u2014';
  tr.appendChild(tdDur);

  // col 5 — Status badge
  const tdStatus = document.createElement('td');
  const badgeClass = f.status === 'done'       ? 'badge-done'
                   : f.status === 'failed'     ? 'badge-failed'
                   : f.status === 'no_saving'  ? 'badge-no-saving'
                   : f.status === 'converting' ? 'badge-converting'
                   : 'badge-pending';
  const badgeLabel  = f.status === 'no_saving' ? 'Skipped – Larger' : (f.status || 'pending');
  tdStatus.innerHTML = '<span class="badge ' + badgeClass + '">' + badgeLabel + '</span>';
  tr.appendChild(tdStatus);

  // col 5b — Est. saving
  const tdEst = document.createElement('td');
  tdEst.className = 'text-end';
  tdEst.id = 'est-' + index;
  if (f.status === 'done' || f.status === 'failed') {
    tdEst.textContent = '\u2014';
  } else {
    tdEst.innerHTML = '<span class="text-secondary" style="font-size:.75rem">…</span>';
  }
  tr.appendChild(tdEst);

  // col 6 — Output
  const tdOut = document.createElement('td');
  tdOut.className = 'text-end';
  tdOut.textContent = f.output ? f.output + ' MB' : '';
  tr.appendChild(tdOut);

  // col 7 — Saved
  const tdSaved = document.createElement('td');
  tdSaved.className = 'text-end text-success';
  tdSaved.textContent = f.saved ? f.saved + ' MB' : '';
  tr.appendChild(tdSaved);

  // col 8 — %
  const tdPct = document.createElement('td');
  tdPct.className = 'text-end';
  if (f.pct) tdPct.innerHTML = '<strong>' + f.pct + '%</strong>';
  tr.appendChild(tdPct);

  // col 9 — Actions
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

// Fill in codec / bitrate / duration cells once a probe result arrives.
function _updateRowProbe(idx, f) {
  const tr = document.getElementById('row-' + idx);
  if (!tr) return;
  const cells = tr.querySelectorAll('td');
  // Column order matches buildRow:
  // 0=handle 1=folder 2=name 3=size 4=bitrate 5=codec 6=duration 7=status 8=est …
  const tdBr    = cells[4];
  const tdCodec = cells[5];
  const tdDur   = cells[6];
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
}

function populateTable(files) {
  const tbody = document.getElementById('queueBody');
  tbody.innerHTML = '';
  if (files.length === 0) {
    tbody.innerHTML = '<tr><td colspan="13" class="text-center text-secondary py-4">No video files found in the selected folder.</td></tr>';
    return;
  }
  // Attach computed bitrate
  files.forEach(f => { if (!f.bitrate_kbps) f.bitrate_kbps = _fileBitrate(f); });
  files.forEach((f, i) => tbody.appendChild(buildRow(f, i)));
  applyFilter();
}

function updateStats(files) {
  const totalMB  = files.reduce((s, f) => s + (parseFloat(f.size.replace(/,/g, '')) || 0), 0);
  const done      = files.filter(f => f.status === 'done').length;
  const failed    = files.filter(f => f.status === 'failed').length;
  const noSaving  = files.filter(f => f.status === 'no_saving').length;
  const pending   = files.filter(f => f.status === 'pending').length;
  const savedMB  = files.reduce((s, f) => s + (f.saved  ? parseFloat(f.saved.replace(/,/g, ''))  || 0 : 0), 0);
  const origMB   = files.filter(f => f.status === 'done')
                        .reduce((s, f) => s + (parseFloat(f.size.replace(/,/g, '')) || 0), 0);
  const donePct  = files.length ? Math.round(done   / files.length * 100) : 0;
  const failPct  = files.length ? Math.round(failed / files.length * 100) : 0;
  const avgRatio = origMB > 0    ? Math.round(savedMB / origMB * 100) : 0;

  // Values
  document.getElementById('statTotal').textContent  = files.length;
  document.getElementById('statDone').textContent   = done;
  document.getElementById('statFailed').textContent = failed;
  document.getElementById('statSaved').textContent  = savedMB > 0 ? (savedMB / 1024).toFixed(1) + ' GB' : '—';

  // Sub-labels
  document.getElementById('statTotalSub').textContent  = totalMB > 0 ? (totalMB / 1024).toFixed(1) + ' GB · ' + pending + ' remaining' : '—';
  document.getElementById('statDoneSub').textContent   = donePct  > 0 ? donePct  + '% of queue complete' : '—';
  document.getElementById('statSavedSub').textContent  = avgRatio > 0 ? 'avg ' + avgRatio + '% compression ratio' : '—';
  document.getElementById('statFailedSub').textContent = failed   > 0 ? failPct + '% failure rate' : 'No failures';

  // Progress strips
  document.getElementById('statTotalBar').style.width  = files.length ? '100%'       : '0%';
  document.getElementById('statDoneBar').style.width   = donePct + '%';
  document.getElementById('statSavedBar').style.width  = avgRatio + '%';
  document.getElementById('statFailedBar').style.width = failPct  + '%';

  // Right panel
  document.getElementById('savedVal').textContent   = savedMB > 0 ? (savedMB / 1024).toFixed(1) + ' GB' : '—';
  const overallPct = files.length ? Math.round((done + failed) / files.length * 100) : 0;
  document.getElementById('overallPct').textContent = overallPct + '%';
  document.getElementById('overallBar').style.width = overallPct + '%';
  document.getElementById('totalSizeLabel').textContent = (totalMB / 1024).toFixed(1) + ' GB total';
  // Filter chip counts
  document.getElementById('chipCount-all').textContent     = files.length;
  document.getElementById('chipCount-pending').textContent = pending;
  document.getElementById('chipCount-done').textContent    = done;
  document.getElementById('chipCount-failed').textContent    = failed;
  const nsEl = document.getElementById('chipCount-no-saving');
  if (nsEl) nsEl.textContent = noSaving;
}

// ============================================================
// Filter + Search
// ============================================================
function setFilter(f) {
  _activeFilter = f;
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  document.getElementById('chip-' + f).classList.add('active');
  applyFilter();
}

function onSearchInput(val) {
  _searchQuery = val.toLowerCase().trim();
  applyFilter();
}

function _fileMatchesFilter(f) {
  const matchStatus = _activeFilter === 'all' || f.status === _activeFilter;
  const matchSearch = !_searchQuery ||
    f.name.toLowerCase().includes(_searchQuery) ||
    (f.folder || '').toLowerCase().includes(_searchQuery);
  const sizeMB = parseFloat((f.size || '0').replace(/,/g, '')) || 0;
  const kbps   = f.bitrate_kbps || _fileBitrate(f);
  const durMin = _parseDuration(f.duration) / 60;
  const matchSize = (!_filterSizeMin || sizeMB >= +_filterSizeMin) &&
                    (!_filterSizeMax || sizeMB <= +_filterSizeMax);
  const matchBr   = (!_filterBrMin   || kbps   >= +_filterBrMin)   &&
                    (!_filterBrMax   || kbps   <= +_filterBrMax);
  const matchDur  = (!_filterDurMin  || durMin >= +_filterDurMin)  &&
                    (!_filterDurMax  || durMin <= +_filterDurMax);
  return matchStatus && matchSearch && matchSize && matchBr && matchDur;
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
  const hasFilter = _filterSizeMin || _filterSizeMax || _filterBrMin || _filterBrMax || _filterDurMin || _filterDurMax;
  const btn = document.getElementById('filterToggleBtn');
  if (btn) btn.classList.toggle('active', !!hasFilter);
  applyFilter();
}

function clearRangeFilters() {
  ['fSizeMin','fSizeMax','fBrMin','fBrMax','fDurMin','fDurMax'].forEach(id => {
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
  // Restore estimation state from settings (don't override a deliberate user pause)
  fetch('/api/settings').then(r => r.json()).then(s => { _estUserPaused = !(s.estimation_enabled); }).catch(() => {});
  _estAutoPaused = false;
  _estRunning    = false;
  const estStrip = document.getElementById('estStrip');
  if (estStrip) estStrip.classList.add('d-none');
  _files = [];
  _fileIndexByPath = {};
  _probeTotal = 0;
  _probeDone  = 0;
  _scanStripPhase1();
  _activeFilter  = 'all';
  _searchQuery   = '';
  _filterSizeMin = ''; _filterSizeMax = '';
  _filterBrMin   = ''; _filterBrMax   = '';
  _filterDurMin  = ''; _filterDurMax  = '';
  const sb = document.getElementById('searchBox');
  if (sb) sb.value = '';
  ['fSizeMin','fSizeMax','fBrMin','fBrMax','fDurMin','fDurMax'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const filterBtn = document.getElementById('filterToggleBtn');
  if (filterBtn) filterBtn.classList.remove('active');
  const ss = document.getElementById('sortSelect');
  if (ss) ss.value = _sortBy;
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  const chipAll = document.getElementById('chip-all');
  if (chipAll) chipAll.classList.add('active');
  setButtonStates('scanning');
  document.getElementById('queueBody').innerHTML =
    '<tr><td colspan="13" class="text-center text-secondary py-4">' +
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
      if (_probeDone === 0) _scanStripPhase2();
      const idx = _fileIndexByPath[msg.full_path];
      if (idx === undefined) return;
      const f = _files[idx];
      f.codec        = msg.codec;
      f.duration     = msg.duration;
      f.is_hi10      = msg.is_hi10;
      f.streams      = msg.streams;
      f.bitrate_kbps = msg.bitrate_kbps || (msg.streams && msg.streams.video
        ? Math.round((msg.streams.video.bitrate || 0) / 1000) : 0);
      _updateRowProbe(idx, f);
      _probeDone++;
      _scanStripProbeProgress();
    } else if (msg.type === 'remove') {
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
      _probeTotal--;  // removed files don't count toward probe total
      _scanStripProbeProgress();
      updateStats(_files);
    } else if (msg.type === 'scan_done') {
      // Phase 1 complete — grid is fully populated, enable actions immediately.
      // Phase 2 (ffprobe) will continue streaming for files without cached probe data.
      const totalGB = (msg.total_mb / 1024).toFixed(1);
      document.getElementById('totalSizeLabel').textContent = totalGB + ' GB total';
      _probeTotal = msg.total_files;  // only files actually needing Phase 2/3 probe
      const pendingCount = _files.filter(f => f.status === 'pending' || f.status === 'failed' || !f.status).length;
      if (_files.length === 0) {
        document.getElementById('queueBody').innerHTML =
          '<tr><td colspan="13" class="text-center text-secondary py-4">' +
          'No video files found.</td></tr>';
        addLog('Scan complete \u2014 no video files found.', 'ok');
        setButtonStates('idle');
        _scanEs.close(); _scanEs = null;
        _scanStripHide();
      } else if (pendingCount === 0) {
        addLog('Found ' + _files.length + ' files \u2014 all already converted.', 'ok');
        setButtonStates('idle');
        if (_probeTotal === 0) { _scanEs.close(); _scanEs = null; _scanStripHide(); }
        else { _scanStripPhase2(); }
        runEstimation(_files);
      } else {
        const cached = _files.length - _probeTotal;
        const cacheNote = cached > 0 ? ' (\u202f' + cached + ' cached)' : '';
        if (_probeTotal > 0) {
          addLog('Found ' + _files.length + ' files \u2014 ' + totalGB + ' GB' + cacheNote + ' \u2014 probing\u2026', 'ok');
          _scanStripPhase2();
        } else {
          addLog('Found ' + _files.length + ' files \u2014 ' + totalGB + ' GB \u2014 all probe data cached.', 'ok');
        }
        setButtonStates('ready');
        runEstimation(_files);
      }
    } else if (msg.type === 'done') {
      // Phase 2 complete — re-render with sort applied now that bitrates are known
      _scanEs.close(); _scanEs = null;
      _scanStripHide();
      const totalGB = (msg.total_mb / 1024).toFixed(1);
      document.getElementById('totalSizeLabel').textContent = totalGB + ' GB total';
      populateTable(_files);
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
  _scanStripProbeProgress();
}
function _scanStripProbeProgress() {
  const label = document.getElementById('scanStripLabel');
  const bar   = document.getElementById('scanBar');
  if (!label || !bar) return;
  const pct = _probeTotal > 0 ? Math.round(_probeDone / _probeTotal * 100) : 0;
  bar.style.transition = 'width .3s ease';
  bar.style.width = pct + '%';
  label.textContent = 'Probing ' + _probeDone + '\u202f/\u202f' + _probeTotal + '\u2026';
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
  const pending = files.filter(f => (f.status === 'pending') && f.full_path);
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
      if (cell) {
        if (data.error) {
          cell.innerHTML = '<span class="text-secondary">\u2014</span>';
        } else {
          cell.innerHTML =
            '<span class="text-success fw-semibold">' + data.estimated_saving_pct + '%</span>' +
            '<br><small class="text-secondary">' + data.estimated_saving_mb + '\u202fMB</small>';
        }
      }
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

function _pollStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(s => {
      // Update progress widgets
      const pct = s.progress_pct || 0;
      document.getElementById('currentFilename').textContent = s.current_file
        ? s.current_file.split(/[\\/]/).pop()
        : (s.state === 'done' ? 'Complete' : '\u2014');
      document.getElementById('fileBar').style.width = pct + '%';
      document.getElementById('filePct').textContent  = Math.round(pct) + '%';
      document.getElementById('fpsVal').textContent   = s.fps ? s.fps.toFixed(0) : '\u2014';
      document.getElementById('etaVal').textContent   = s.eta_secs > 0
        ? Math.floor(s.eta_secs / 60) + 'm ' + (s.eta_secs % 60) + 's'
        : '\u2014';
      document.getElementById('savedVal').textContent = s.saved_mb
        ? (s.saved_mb / 1024 > 1
            ? (s.saved_mb / 1024).toFixed(1) + ' GB'
            : s.saved_mb.toFixed(0) + ' MB')
        : '\u2014';
      const overallDone  = s.files ? s.files.filter(f => f.status === 'done' || f.status === 'failed' || f.status === 'no_saving').length : 0;
      const overallTotal = s.total || 0;
      const overallPct   = overallTotal > 0 ? Math.round(overallDone / overallTotal * 100) : 0;
      document.getElementById('overallPct').textContent  = overallPct + '%';
      document.getElementById('overallBar').style.width  = overallPct + '%';

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
        const statusByPath = {};
        s.files.forEach(sf => { if (sf.full_path) statusByPath[sf.full_path] = sf; });

        _files.forEach((f, idx) => {
          const sf = statusByPath[f.full_path];
          if (!sf) return;
          // Sync into _files so local state matches
          f.status = sf.status;
          if (sf.output)      f.output      = sf.output;
          if (sf.saved)       f.saved       = sf.saved;
          if (sf.pct)         f.pct         = sf.pct;
          if (sf.output_path) f.output_path = sf.output_path;

          const row = document.getElementById('row-' + idx);
          if (!row) return;
          // Status badge (col index 7)
          const badgeCell = row.cells[7];
          if (badgeCell) {
            const cls = sf.status === 'done'       ? 'badge-done'
                      : sf.status === 'failed'     ? 'badge-failed'
                      : sf.status === 'no_saving'  ? 'badge-no-saving'
                      : sf.status === 'converting' ? 'badge-converting'
                      : 'badge-pending';
            const lbl = sf.status === 'no_saving' ? 'Skipped \u2013 Larger' : (sf.status || 'pending');
            badgeCell.innerHTML = '<span class="badge ' + cls + '">' + lbl + '</span>';
            row.classList.toggle('tr-done',       sf.status === 'done');
            row.classList.toggle('tr-failed',     sf.status === 'failed');
            row.classList.toggle('tr-no-saving',  sf.status === 'no_saving');
            row.classList.toggle('tr-converting', sf.status === 'converting');
          }
          // Output/Saved/% cells (cols 9, 10, 11)
          if (sf.status === 'done') {
            if (row.cells[9])  row.cells[9].textContent  = sf.output  ? sf.output  + ' MB' : '';
            if (row.cells[10]) row.cells[10].textContent = sf.saved   ? sf.saved   + ' MB' : '';
            if (row.cells[11]) row.cells[11].innerHTML   = sf.pct     ? '<strong>' + sf.pct + '%</strong>' : '';
          }
        });
        updateStats(_files);
      }

      // Render new backend log lines (filter out verbose ffmpeg internals)
      if (s.log && s.log.length > _logCursor) {
        const statusEl = document.getElementById('ffmpegStatus');
        s.log.slice(_logCursor).forEach(msg => {
          // FFmpeg progress lines → single-line status bar (replace, not append)
          if (/^frame=\s*\d+.*speed=/.test(msg)) {
            if (statusEl) { statusEl.textContent = msg.trim(); statusEl.classList.remove('d-none'); }
            return;
          }
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
        _isPaused = false;
        setButtonStates('ready');
        addLog('Queue complete. Saved ' + (s.saved_mb || 0).toFixed(1) + ' MB total.', 'ok');
      } else if (s.state === 'stopped') {
        _stopPolling();
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

function showRowMenu(index, btn) {
  _rowMenuIndex = index;
  const _rowMenu = _getRowMenu();
  if (!_rowMenu) return;
  const f = _files[index];
  _rowMenu.innerHTML = '';

  const isDone       = f.status === 'done';
  const isFailed     = f.status === 'failed';
  const isPending    = f.status === 'pending';
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

  if (isPending) {
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-x-circle text-danger', 'Remove from Queue',
      () => removeFromQueue(index)));
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
  document.getElementById('errModalCmd').textContent      = f.ffmpeg_cmd  || '(not yet recorded — backend not connected)';
  document.getElementById('errModalTail').textContent     = f.error_tail  || '(not yet recorded — backend not connected)';
  document.getElementById('errModalLogPath').textContent  = f.log_path    ? 'Full log: ' + f.log_path : '';
  new bootstrap.Modal(document.getElementById('errorLogModal')).show();
}

function viewDetails(index) {
  const f = _files[index];
  const body = document.getElementById('detailsModalBody');
  const titleEl = document.getElementById('detailsModalTitle');
  if (titleEl) titleEl.textContent = f.name;

  const s = f.streams || null;
  const v = s ? s.video : null;

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
      ${f.status === 'done' ? `<tr><td>Output size</td><td>${f.output} MB</td></tr>
      <tr><td>Space saved</td><td class="text-success fw-semibold">${f.saved} MB (${f.pct}%)</td></tr>` : ''}
    </table>`;

  // ---- Audio tracks ----
  const audioTracks = s ? s.audio : [];
  html += `<h6 class="details-section-head"><i class="bi bi-music-note-list me-2"></i>Audio Tracks <span class="badge bg-secondary ms-1">${audioTracks.length}</span></h6>`;
  if (audioTracks.length) {
    html += '<table class="table table-sm details-table mb-3"><thead><tr><th>#</th><th>Codec</th><th>Channels</th><th>Language</th><th>Bitrate</th><th>Title</th></tr></thead><tbody>';
    audioTracks.forEach(a => {
      html += `<tr><td class="text-secondary">${a.track}</td><td>${a.codec}</td><td>${a.channels}</td><td><span class="badge bg-secondary">${a.language}</span></td><td class="text-secondary">${a.bitrate}</td><td>${a.title || '—'}</td></tr>`;
    });
    html += '</tbody></table>';
  } else {
    html += '<p class="text-secondary small mb-3">No audio tracks found.</p>';
  }

  // ---- Subtitle tracks ----
  const subTracks = s ? s.subs : [];
  html += `<h6 class="details-section-head"><i class="bi bi-badge-cc me-2"></i>Subtitle Tracks <span class="badge bg-secondary ms-1">${subTracks.length}</span></h6>`;
  if (subTracks.length) {
    html += '<table class="table table-sm details-table mb-0"><thead><tr><th>#</th><th>Format</th><th>Language</th><th>Title</th></tr></thead><tbody>';
    subTracks.forEach(sub => {
      const isImage = ['PGS','VOBSUB','DVDSUB'].includes(sub.codec.toUpperCase());
      const fmtBadge = isImage
        ? `<span class="badge details-sub-image">${sub.codec}</span>`
        : `<span class="badge details-sub-text">${sub.codec}</span>`;
      html += `<tr><td class="text-secondary">${sub.track}</td><td>${fmtBadge}</td><td><span class="badge bg-secondary">${sub.language}</span></td><td>${sub.title || '—'}</td></tr>`;
    });
    html += '</tbody></table>';
  } else {
    html += '<p class="text-secondary small mb-0">No subtitle tracks found.</p>';
  }

  body.innerHTML = html;
  new bootstrap.Modal(document.getElementById('detailsModal')).show();
}

function retryFile(index) {
  const f = _files[index];
  const row = document.getElementById('row-' + index);
  if (row) {
    row.classList.remove('tr-failed');
    row.cells[5].innerHTML = '<span class="badge badge-pending">pending</span>';
  }
  _files[index].status = 'pending';
  addLog('Queued for retry: ' + f.name, 'info');
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

// Sync icon to whatever theme was applied before paint
(function() {
  const saved = _storedTheme();
  const icon = document.getElementById('themeIcon');
  if (icon) icon.className = saved === 'dark' ? 'bi bi-moon-stars-fill' : 'bi bi-sun-fill';
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
      document.getElementById('settingsDefaultSort').value  = s.default_sort || 'bitrate';
      document.getElementById('settingsEstimation').checked = !!s.estimation_enabled;
    })
    .catch(() => {}); // show modal even if fetch fails — defaults already in HTML
  _settingsModal = new bootstrap.Modal(document.getElementById('settingsModal'));
  _settingsModal.show();
}

function saveSettings() {
  const payload = {
    qsv_quality:         parseInt(document.getElementById('qsvQuality').value, 10),
    sw_hevc_crf:         parseInt(document.getElementById('swCrf').value, 10),
    local_temp_dir:      document.getElementById('settingsTempDir').value.trim(),
    default_sort:        document.getElementById('settingsDefaultSort').value,
    estimation_enabled:  document.getElementById('settingsEstimation').checked,
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
(function _applyStartupSettings() {
  fetch('/api/settings')
    .then(r => r.json())
    .then(s => {
      if (s.default_sort) {
        _sortBy = s.default_sort;
        const ss = document.getElementById('sortSelect');
        if (ss) ss.value = _sortBy;
      }
      if (s.anime_mode !== undefined) {
        const am = document.getElementById('animeMode');
        if (am) am.checked = s.anime_mode;
      }
      // Estimation off by default — respect saved setting
      _estUserPaused = !(s.estimation_enabled);
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
      populateTable(_files);
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

async function cleanupLegacyFolders() {
  if (!_currentScanPath) return;
  _cancelProbeStream();
  const btn = document.getElementById('cleanupBtn');
  if (btn) btn.disabled = true;

  // Stop the estimation chain so no in-flight sample encode holds a file open.
  // We can't abort a fetch already sent, so wait up to 12 s for it to finish.
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

  try {
    const resp = await fetch('/api/cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: _currentScanPath }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      alert('Cleanup error: ' + (data.error || resp.statusText));
      return;
    }
    const moved   = (data.moved   || []).length;
    const skipped = (data.skipped || []).length;
    const errors  = (data.errors  || []).length;
    let msg = `Cleanup complete: ${moved} file(s) moved out of legacy folders.`;
    if (skipped) msg += ` ${skipped} skipped (destination exists).`;
    if (errors)  msg += ` ${errors} error(s).`;
    alert(msg);
    if (moved > 0) rescanFolder();
  } catch (e) {
    alert('Cleanup failed: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ============================================================
let _selectedPath = null;
let _modal = null;

function openBrowser() {
  _selectedPath = null;
  document.getElementById('confirmFolderBtn').disabled = true;
  document.getElementById('selectedPathDisplay').textContent = 'No folder selected';
  _modal = new bootstrap.Modal(document.getElementById('folderModal'));
  _modal.show();
  const lastFolder = localStorage.getItem('vc_last_folder') || '';
  browseTo(lastFolder);
}

function browseTo(path) {
  _selectedPath = null;
  document.getElementById('confirmFolderBtn').disabled = true;
  document.getElementById('selectedPathDisplay').textContent = 'No folder selected';
  const listing = document.getElementById('dirListing');
  listing.innerHTML = '<div class="text-center text-secondary py-5"><div class="spinner-border spinner-border-sm"></div> Loading\u2026</div>';

  fetch('/api/browse?path=' + encodeURIComponent(path))
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        listing.innerHTML = '<div class="p-3 text-danger">' + data.error + '</div>';
        return;
      }
      renderBreadcrumb(data.path, data.parent);
      renderListing(data.dirs, data.path, data.parent);
    })
    .catch(e => {
      listing.innerHTML = '<div class="p-3 text-danger">Error: ' + e + '</div>';
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
  document.getElementById('confirmFolderBtn').disabled = false;
}

function confirmFolder() {
  if (!_selectedPath) return;
  document.getElementById('folderPath').textContent = _selectedPath;
  if (_modal) _modal.hide();
  scanFolder(_selectedPath);
}
