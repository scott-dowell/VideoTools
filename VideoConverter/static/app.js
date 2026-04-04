// ============================================================
// Simulation
// ============================================================
let simInterval = null;
let simValue = 0;
let paused = false;

function addLog(msg, cls) {
  const box = document.getElementById('logBox');
  const div = document.createElement('div');
  div.className = 'log-' + cls;
  div.textContent = '\u25b8 ' + msg;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function startConversion() {
  addLog('Starting conversion queue\u2026', 'info');
  const anime = document.getElementById('animeMode').checked;
  addLog(anime
    ? 'Anime mode: ON \u2014 remux to MP4, AAC transcode, OCR subs'
    : 'Normal mode \u2014 compress only, copy all tracks', 'info');
  simValue = 0;
  paused = false;
  if (simInterval) clearInterval(simInterval);
  simInterval = setInterval(simTick, 80);
  document.getElementById('fpsVal').textContent = '45.2';
  document.getElementById('etaVal').textContent = '1:24:30';
  document.getElementById('currentFilename').textContent = 'Blue.Lock.E24.mp4';
}

function simTick() {
  if (paused) return;
  simValue = Math.min(100, simValue + Math.random() * 2.5);
  document.getElementById('fileBar').style.width = simValue + '%';
  document.getElementById('filePct').textContent = simValue.toFixed(0) + '%';
  if (simValue >= 100) {
    clearInterval(simInterval);
    const row = document.getElementById('row-5');
    row.cells[4].innerHTML = '<span class="badge badge-done">done</span>';
    row.cells[5].textContent = '712 MB';
    row.cells[6].textContent = '1,164 MB';
    row.cells[7].innerHTML = '<strong>62.1%</strong>';
    addLog('Blue.Lock.E24.mp4 \u2192 712 MB (saved 1,164 MB, 62.1%)', 'ok');
    document.getElementById('etaVal').textContent = 'Done';
    document.getElementById('currentFilename').textContent = '\u2014';
  }
}

function pauseSim() {
  paused = !paused;
  addLog(paused ? 'Paused.' : 'Resumed.', 'warn');
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
  addLog('Folder selected: ' + _selectedPath, 'ok');
  if (_modal) _modal.hide();
}
