// ============================================================
// State
// ============================================================
let _files      = [];   // file objects loaded by scan
let _simInterval = null;
let _simIndex   = 0;
let _simValue   = 0;
let _paused     = false;
let _activeFilter = 'all';
let _searchQuery  = '';

// ============================================================
// UI helpers
// ============================================================
function addLog(msg, cls) {
  const box = document.getElementById('logBox');
  const div = document.createElement('div');
  div.className = 'log-' + cls;
  div.textContent = '\u25b8 ' + msg;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// state: 'idle' | 'scanning' | 'ready' | 'running' | 'done'
function setButtonStates(state) {
  document.getElementById('startBtn').disabled  = state !== 'ready';
  document.getElementById('pauseBtn').disabled  = state !== 'running';
  document.getElementById('stopBtn').disabled   = !(state === 'running' || state === 'scanning');
  document.getElementById('hstopBtn').disabled  = state !== 'running';
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

  // col 0 — Folder
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

  // col 3 — Codec
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
  const badgeClass = f.status === 'done' ? 'badge-done'
                   : f.status === 'failed' ? 'badge-failed'
                   : f.status === 'converting' ? 'badge-converting'
                   : 'badge-pending';
  tdStatus.innerHTML = '<span class="badge ' + badgeClass + '">' + (f.status || 'pending') + '</span>';
  tr.appendChild(tdStatus);

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

  return tr;
}

function populateTable(files) {
  const tbody = document.getElementById('queueBody');
  tbody.innerHTML = '';
  if (files.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-center text-secondary py-4">No video files found in the selected folder.</td></tr>';
    return;
  }
  files.forEach((f, i) => tbody.appendChild(buildRow(f, i)));
  applyFilter();
}

function updateStats(files) {
  const totalMB  = files.reduce((s, f) => s + (parseFloat(f.size.replace(/,/g, '')) || 0), 0);
  const done     = files.filter(f => f.status === 'done').length;
  const failed   = files.filter(f => f.status === 'failed').length;
  const savedMB  = files.reduce((s, f) => s + (f.saved ? parseFloat(f.saved.replace(/,/g, '')) || 0 : 0), 0);
  document.getElementById('statTotal').textContent  = files.length;
  document.getElementById('statDone').textContent   = done;
  document.getElementById('statFailed').textContent = failed;
  document.getElementById('statSaved').textContent  = savedMB > 0 ? (savedMB / 1024).toFixed(1) + ' GB' : '—';
  document.getElementById('savedVal').textContent   = savedMB > 0 ? (savedMB / 1024).toFixed(1) + ' GB' : '—';
  const overallPct = files.length ? Math.round((done + failed) / files.length * 100) : 0;
  document.getElementById('overallPct').textContent = overallPct + '%';
  document.getElementById('overallBar').style.width = overallPct + '%';
  document.getElementById('totalSizeLabel').textContent = (totalMB / 1024).toFixed(1) + ' GB total';
  // Filter chip counts
  const pending = files.filter(f => f.status === 'pending').length;
  document.getElementById('chipCount-all').textContent     = files.length;
  document.getElementById('chipCount-pending').textContent = pending;
  document.getElementById('chipCount-done').textContent    = done;
  document.getElementById('chipCount-failed').textContent  = failed;
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

function applyFilter() {
  _files.forEach((f, i) => {
    const row = document.getElementById('row-' + i);
    if (!row) return;
    const matchStatus = _activeFilter === 'all' || f.status === _activeFilter;
    const matchSearch = !_searchQuery ||
      f.name.toLowerCase().includes(_searchQuery) ||
      (f.folder || '').toLowerCase().includes(_searchQuery);
    row.style.display = (matchStatus && matchSearch) ? '' : 'none';
  });
}

// ============================================================
// Scan  (simulated — swap for fetch('/api/scan') later)
// ============================================================
const DEMO_FILES = [
  { folder: '',         name: 'One.Piece.E1050.mp4',    size: '1,105', codec: 'H264', duration: '24:12', status: 'done',    output: '298',   saved: '807',   pct: '73', full_path: 'D:/Anime/One.Piece.E1050.mp4',    output_path: 'D:/Anime/One.Piece.E1050_hevc.mp4' },
  { folder: '',         name: 'One.Piece.E1051.mp4',    size: '1,098', codec: 'H264', duration: '24:05', status: 'done',    output: '312',   saved: '786',   pct: '72', full_path: 'D:/Anime/One.Piece.E1051.mp4',    output_path: 'D:/Anime/One.Piece.E1051_hevc.mp4' },
  { folder: 'Season 2', name: 'Blue.Lock.E24.mp4',      size: '1,876', codec: 'H264', duration: '23:45', status: 'done',    output: '521',   saved: '1,355', pct: '72', full_path: 'D:/Anime/Season 2/Blue.Lock.E24.mp4', output_path: 'D:/Anime/Season 2/Blue.Lock.E24_hevc.mp4' },
  { folder: 'Season 2', name: 'Blue.Lock.E25.mp4',      size: '1,743', codec: 'HEVC', duration: '22:58', status: 'done',    output: '501',   saved: '1,242', pct: '71', full_path: 'D:/Anime/Season 2/Blue.Lock.E25.mp4', output_path: 'D:/Anime/Season 2/Blue.Lock.E25_hevc.mp4' },
  { folder: 'Season 2', name: 'Blue.Lock.E26.mp4',      size: '1,811', codec: 'H264', duration: '24:01', status: 'failed',  output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Season 2/Blue.Lock.E26.mp4', output_path: null, ffmpeg_cmd: 'ffmpeg -y -i "D:/Anime/Season 2/Blue.Lock.E26.mp4" -c:v hevc_qsv ...', error_tail: 'Error initializing output stream 0:0 -- Error while opening encoder\nConversion failed!' },
  { folder: 'Movies',  name: 'Vinland.Saga.Movie.mkv', size: '8,231', codec: 'H264', duration: '1:52:04', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Movies/Vinland.Saga.Movie.mkv', output_path: null },
  { folder: 'Movies',  name: 'Steins.Gate.Movie.mkv',  size: '6,504', codec: 'H264', duration: '1:32:44', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Movies/Steins.Gate.Movie.mkv',  output_path: null },
  { folder: 'Extras',  name: 'Steins.Gate.E01.mkv',    size: '2,203', codec: 'H264', duration: '23:23', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Extras/Steins.Gate.E01.mkv', output_path: null },
  { folder: 'Extras',  name: 'Steins.Gate.E02.mkv',    size: '2,187', codec: 'H264', duration: '23:15', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Extras/Steins.Gate.E02.mkv', output_path: null },
  { folder: '',         name: 'Jujutsu.Kaisen.E01.mkv', size: '1,456', codec: 'H264', duration: '24:30', status: 'pending', output: null,    saved: null,    pct: null, full_path: 'D:/Anime/Jujutsu.Kaisen.E01.mkv',   output_path: null },
];

function scanFolder(path) {
  _files = [];
  _activeFilter = 'all';
  _searchQuery  = '';
  document.getElementById('searchBox').value = '';
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  document.getElementById('chip-all').classList.add('active');
  setButtonStates('scanning');
  document.getElementById('queueBody').innerHTML =
    '<tr><td colspan="9" class="text-center text-secondary py-4">' +
    '<div class="spinner-border spinner-border-sm me-2"></div>Scanning for video files\u2026</td></tr>';
  document.getElementById('totalSizeLabel').textContent = 'Scanning\u2026';
  addLog('Scanning: ' + path, 'info');

  // Replace setTimeout block with fetch('/api/scan?path=...') when backend is ready
  setTimeout(() => {
    _files = DEMO_FILES;
    populateTable(_files);
    updateStats(_files);
    addLog('Found ' + _files.length + ' video files \u2014 ' + document.getElementById('totalSizeLabel').textContent, 'ok');
    setButtonStates('ready');
  }, 1200);
}

// ============================================================
// Conversion  (simulated — swap for fetch('/api/start') + polling later)
// ============================================================
function startConversion() {
  if (_files.length === 0) return;
  const anime = document.getElementById('animeMode').checked;
  addLog('Starting conversion queue\u2026', 'info');
  addLog(anime
    ? 'Anime mode: ON \u2014 remux to MP4, AAC transcode, OCR subs'
    : 'Normal mode \u2014 compress only, copy all tracks', 'info');
  setButtonStates('running');
  _simIndex = 0;
  _simValue = 0;
  _paused   = false;
  _startNextFile();
}

function _startNextFile() {
  if (_simIndex >= _files.length) {
    document.getElementById('etaVal').textContent      = 'Done';
    document.getElementById('currentFilename').textContent = '\u2014';
    addLog('All files processed.', 'ok');
    setButtonStates('done');
    return;
  }
  const f = _files[_simIndex];
  // Mark active row
  const activeRow = document.getElementById('row-' + _simIndex);
  if (activeRow) {
    activeRow.classList.add('tr-converting');
    activeRow.cells[5].innerHTML = '<span class="badge badge-converting">converting</span>';
  }
  document.getElementById('currentFilename').textContent = f.name;
  document.getElementById('fpsVal').textContent = (40 + Math.random() * 20).toFixed(1);
  document.getElementById('etaVal').textContent = '\u2014';
  _simValue = 0;
  if (_simInterval) clearInterval(_simInterval);
  _simInterval = setInterval(_simTick, 80);
}

function _simTick() {
  if (_paused) return;
  _simValue = Math.min(100, _simValue + Math.random() * 3);
  document.getElementById('fileBar').style.width   = _simValue + '%';
  document.getElementById('filePct').textContent   = _simValue.toFixed(0) + '%';
  const overallPct = ((_simIndex + _simValue / 100) / _files.length * 100).toFixed(0);
  document.getElementById('overallBar').style.width = overallPct + '%';
  document.getElementById('overallPct').textContent = overallPct + '%';

  if (_simValue >= 100) {
    clearInterval(_simInterval);
    const f      = _files[_simIndex];
    const srcMB  = parseFloat(f.size.replace(/,/g, ''));
    const outMB  = Math.round(srcMB * (0.25 + Math.random() * 0.15));
    const savedMB = srcMB - outMB;
    const pct    = Math.round(savedMB / srcMB * 100);
    const row    = document.getElementById('row-' + _simIndex);
    if (row) {
      row.classList.remove('tr-converting');
      row.classList.add('tr-done');
      row.cells[5].innerHTML    = '<span class="badge badge-done">done</span>';
      row.cells[6].textContent  = outMB.toLocaleString() + ' MB';
      row.cells[7].textContent  = savedMB.toLocaleString() + ' MB';
      row.cells[8].innerHTML    = '<strong>' + pct + '%</strong>';
      addLog(f.name + ' \u2192 ' + outMB.toLocaleString() + ' MB (saved ' + savedMB.toLocaleString() + ' MB, ' + pct + '%)', 'ok');
      const doneEl = document.getElementById('statDone');
      doneEl.textContent = parseInt(doneEl.textContent || '0') + 1;
    }
    _simIndex++;
    _startNextFile();
  }
}

function pauseConversion() {
  _paused = !_paused;
  addLog(_paused ? 'Paused.' : 'Resumed.', 'warn');
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
  a.addEventListener('click', e => { e.preventDefault(); _rowMenu.style.display = 'none'; handler(); });
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

  if (isDone) {
    _rowMenu.appendChild(_menuDivider());
    _rowMenu.appendChild(_menuItem('bi-info-circle', 'Video Details',
      () => viewDetails(index)));
  }

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
  // Stub — will call /api/details?id=... when backend is ready
  body.innerHTML =
    '<table class="table table-sm table-borderless mb-0">' +
    '<tr><td class="text-secondary" style="width:140px">Filename</td><td class="fw-semibold">' + f.name + '</td></tr>' +
    '<tr><td class="text-secondary">Folder</td><td>' + (f.folder || '—') + '</td></tr>' +
    '<tr><td class="text-secondary">Size</td><td>' + f.size + ' MB</td></tr>' +
    '<tr><td class="text-secondary">Codec</td><td>' + (f.codec || '—') + '</td></tr>' +
    '<tr><td class="text-secondary">Duration</td><td>' + (f.duration || '—') + '</td></tr>' +
    (f.output ? '<tr><td class="text-secondary">Output size</td><td>' + f.output + ' MB</td></tr>' : '') +
    (f.saved  ? '<tr><td class="text-secondary">Saved</td><td class="text-success">' + f.saved + ' MB (' + f.pct + '%)</td></tr>' : '') +
    '</table>' +
    '<p class="text-secondary small mt-3 mb-0"><i class="bi bi-info-circle me-1"></i>Full stream details (resolution, bitrate, audio/subtitle tracks) available once backend is connected.</p>';
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
// Folder browser
// ============================================================
let _selectedPath = null;
let _modal = null;

function openBrowser() {
  _selectedPath = null;
  document.getElementById('confirmFolderBtn').disabled = true;
  document.getElementById('selectedPathDisplay').textContent = 'No folder selected';
  _modal = new bootstrap.Modal(document.getElementById('folderModal'));
  _modal.show();
  browseTo('');
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
