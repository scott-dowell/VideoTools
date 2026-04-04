// ============================================================
// State
// ============================================================
let _files      = [];   // file objects loaded by scan
let _simInterval = null;
let _simIndex   = 0;
let _simValue   = 0;
let _paused     = false;

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

  // col 5 — Status
  const tdStatus = document.createElement('td');
  tdStatus.innerHTML = '<span class="badge badge-pending">pending</span>';
  tr.appendChild(tdStatus);

  // col 6 — Output (empty until done)
  tr.appendChild(document.createElement('td'));

  // col 7 — Saved (empty until done)
  const tdSaved = document.createElement('td');
  tdSaved.className = 'text-end text-success';
  tr.appendChild(tdSaved);

  // col 8 — % (empty until done)
  const tdPct = document.createElement('td');
  tdPct.className = 'text-end';
  tr.appendChild(tdPct);

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
}

function updateStats(files) {
  document.getElementById('statTotal').textContent  = files.length;
  document.getElementById('statDone').textContent   = '0';
  document.getElementById('statSaved').textContent  = '0 GB';
  document.getElementById('statFailed').textContent = '0';
  document.getElementById('savedVal').textContent   = '\u2014';
  document.getElementById('overallPct').textContent = '0%';
  document.getElementById('overallBar').style.width = '0%';
  const totalMB  = files.reduce((s, f) => s + parseFloat(f.size.replace(/,/g, '')) || 0, 0);
  document.getElementById('totalSizeLabel').textContent = (totalMB / 1024).toFixed(1) + ' GB total';
}

// ============================================================
// Scan  (simulated — swap for fetch('/api/scan') later)
// ============================================================
const DEMO_FILES = [
  { folder: '',          name: 'One.Piece.E1050.mp4',      size: '1,105', codec: 'H264', duration: '24:12' },
  { folder: '',          name: 'One.Piece.E1051.mp4',      size: '1,098', codec: 'H264', duration: '24:05' },
  { folder: 'Season 2', name: 'Blue.Lock.E24.mp4',         size: '1,876', codec: 'H264', duration: '23:45' },
  { folder: 'Season 2', name: 'Blue.Lock.E25.mp4',         size: '1,743', codec: 'H265', duration: '22:58' },
  { folder: 'Season 2', name: 'Blue.Lock.E26.mp4',         size: '1,811', codec: 'H264', duration: '24:01' },
  { folder: 'Movies',   name: 'Vinland.Saga.Movie.mkv',    size: '8,231', codec: 'H264', duration: '1:52:04' },
  { folder: 'Movies',   name: 'Steins.Gate.Movie.mkv',     size: '6,504', codec: 'H264', duration: '1:32:44' },
  { folder: 'Extras',   name: 'Steins.Gate.E01.mkv',       size: '2,203', codec: 'H264', duration: '23:23' },
  { folder: 'Extras',   name: 'Steins.Gate.E02.mkv',       size: '2,187', codec: 'H264', duration: '23:15' },
  { folder: '',          name: 'Jujutsu.Kaisen.E01.mkv',   size: '1,456', codec: 'H264', duration: '24:30' },
];

function scanFolder(path) {
  _files = [];
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
// Theme toggle
// ============================================================
function toggleTheme() {
  const html = document.documentElement;
  const icon = document.getElementById('themeIcon');
  if (html.getAttribute('data-bs-theme') === 'dark') {
    html.setAttribute('data-bs-theme', 'light');
    icon.className = 'bi bi-sun-fill';
  } else {
    html.setAttribute('data-bs-theme', 'dark');
    icon.className = 'bi bi-moon-stars-fill';
  }
}

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
