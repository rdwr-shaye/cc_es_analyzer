/* ═══════════════════════════════════════════════════════════════════════
   CC ES Analyzer — Frontend Application
   ═══════════════════════════════════════════════════════════════════════ */

/* ── Constants ────────────────────────────────────────────────────────── */
const LS_ACTIVE  = 'cc_es_active_conn';   // last-used connection settings
const LS_PROFILES = 'cc_es_profiles';     // saved named profiles

/* ── Base path (reverse-proxy prefix support) ───────────────────────────
   The app may be served at "/" (direct, e.g. :8801) OR under a path prefix
   behind nginx (e.g. http://<host>/cc_es_analyzer/). Derive the prefix from
   the page URL so every API/static request stays inside it. */
const APP_BASE = new URL('.', window.location.href).pathname.replace(/\/$/, '');
/** Prefix an absolute app path ("/api/...", "/static/...") with APP_BASE. */
function appUrl(path) {
  return path.startsWith('/') ? APP_BASE + path : path;
}

/* ── App state ──────────────────────────────────────────────────────────── */
let allIndices   = [];
let chartCategory = null;
let chartTimeline = null;
let isConnected  = false;

// Summary charts
let chartSummaryTimeline = null;
let chartSummaryCategory = null;
let chartSummaryTraffic  = null;
let summaryData          = null;
let summaryGranularity   = 'day';

/* ══════════════════════════════════════════════════════════════════════════
   VIEW ROUTING
   ══════════════════════════════════════════════════════════════════════════ */
let currentView = 'connection';   // which .view-panel is visible (for auto-refresh)

function showView(name) {
  currentView = name;
  document.querySelectorAll('.view-panel').forEach(p => p.classList.add('d-none'));
  const panel = document.getElementById(`view-${name}`);
  if (panel) panel.classList.remove('d-none');

  // Highlight sidebar button
  document.querySelectorAll('.sidebar-nav-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(`nav-${name}`);
  if (btn) btn.classList.add('active');

  // Restore the Query Editor's own results viewer when returning to it.
  if (name === 'query' && typeof activateViewer === 'function' && activeViewer !== 'query') {
    activateViewer('query');
    renderResultViews();
  }

  // Lazy-load per view
  if (name === 'attacks'  && isConnected) loadAttacks();
  if (name === 'dashboard'&& isConnected) loadClusterHealth();
  if (name === 'summary'  && isConnected) loadSummary();
}

/* ══════════════════════════════════════════════════════════════════════════
   REFRESH + AUTO-REFRESH — every screen can reload its data manually or on a
   fixed interval (the per-view select persists in localStorage). The interval
   only fires while its view is the visible one and a connection is active.
   ══════════════════════════════════════════════════════════════════════════ */
const LS_AUTOREFRESH = 'cc_es_autorefresh_';
const _autoTimers = {};   // view name -> setInterval handle

const REFRESHERS = {
  dashboard: () => { loadClusterHealth(); loadIndices(); },
  summary:   () => loadSummary(),
  attacks:   () => loadAttacks(),
  index:     () => refreshCurrentIndex(),
  query:     () => refreshQueryResults(),
};

function refreshView(view, manual = true) {
  if (view === 'query' && !(_queryBaseItems && _queryBaseItems.length)) {
    if (manual) showToast('Run a query first — nothing to refresh', 'bg-warning');
    return;
  }
  REFRESHERS[view]?.();
}

/** Refresh whichever results viewer is active (used by the pop-out window). */
function refreshActiveViewer() {
  refreshView(activeViewer === 'index' ? 'index' : 'query');
}

/** Re-run the last executed Query-Editor query (single or multi-index). */
function refreshQueryResults() {
  if (perIndexQueries.length > 1) runMultiQuery(perIndexQueries);
  else runQuery();
}

function onAutoRefreshChanged(view, sel) {
  const secs = parseInt(sel.value) || 0;
  localStorage.setItem(LS_AUTOREFRESH + view, String(secs));
  startAutoRefresh(view, secs);
  showToast(secs ? `Auto-refresh every ${secs}s (while this screen is open)` : 'Auto-refresh off', 'bg-info');
}

function startAutoRefresh(view, secs) {
  if (_autoTimers[view]) { clearInterval(_autoTimers[view]); delete _autoTimers[view]; }
  if (!secs) return;
  _autoTimers[view] = setInterval(() => {
    if (currentView === view && isConnected) refreshView(view, false);
  }, secs * 1000);
}

/** Restore saved auto-refresh intervals into the selects and start timers. */
function initAutoRefresh() {
  document.querySelectorAll('.auto-refresh-select').forEach(sel => {
    const view  = sel.id.replace('autoRefresh-', '');
    const saved = parseInt(localStorage.getItem(LS_AUTOREFRESH + view) || '0') || 0;
    sel.value = String(saved);
    startAutoRefresh(view, saved);
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   UI LAYOUT — sidebar toggle + resizable query/results panes
   ══════════════════════════════════════════════════════════════════════════ */
const LS_SIDEBAR   = 'cc_es_sidebar_collapsed';
const LS_QUERYSPLIT = 'cc_es_query_split';

/** Show / hide the left main-menu sidebar (persisted across reloads). */
function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  if (!sb) return;
  const collapsed = sb.classList.toggle('collapsed');
  localStorage.setItem(LS_SIDEBAR, collapsed ? '1' : '0');
}

/** Restore saved layout prefs (sidebar state + query split width). */
function initUiPrefs() {
  const sb = document.getElementById('sidebar');
  if (sb && localStorage.getItem(LS_SIDEBAR) === '1') sb.classList.add('collapsed');

  const leftPane = document.getElementById('qeditQueryPane');
  const savedW   = localStorage.getItem(LS_QUERYSPLIT);
  if (leftPane && savedW) leftPane.style.width = savedW;
}

/** Wire up the drag handle that resizes the query vs. results panes. */
function initQuerySplitter() {
  const split  = document.getElementById('qeditSplit');
  const gutter = document.getElementById('qeditGutter');
  const left   = document.getElementById('qeditQueryPane');
  if (!split || !gutter || !left) return;

  let dragging = false;

  gutter.addEventListener('mousedown', (e) => {
    dragging = true;
    gutter.classList.add('dragging');
    document.body.classList.add('qedit-resizing');
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = split.getBoundingClientRect();
    const MIN  = 260;                      // keep both panes usable
    const max  = rect.width - MIN - 10;    // 10 = gutter width
    let w = e.clientX - rect.left;
    w = Math.max(MIN, Math.min(max, w));
    left.style.width = w + 'px';
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    gutter.classList.remove('dragging');
    document.body.classList.remove('qedit-resizing');
    if (left.style.width) localStorage.setItem(LS_QUERYSPLIT, left.style.width);
  });
}

/* ── Results pop-out window ───────────────────────────────────────────────── */
let resultsWindow = null;

/* ── Results views: JSON / Table / CSV ────────────────────────────────────── */
// Two independent viewers (Query Editor + Index Detail) share all the rendering
// and control code; only their container element ids + data/state differ.
const RV_QUERY = { pre: 'queryResults', table: 'queryResultsTable', csv: 'queryResultsCsv',
                   btns: ['rv-json', 'rv-table', 'rv-csv'], meta: 'queryMeta' };
const RV_INDEX = { pre: 'idxResults', table: 'idxResultsTable', csv: 'idxResultsCsv',
                   btns: ['iv2-json', 'iv2-table', 'iv2-csv'], meta: 'idxMeta' };

let lastResultHits  = [];    // hit objects for the ACTIVE viewer
let lastResultTotal = 0;     // total docs MATCHING (may exceed loaded rows)
let lastResultJson  = '';    // text shown in the JSON view
let lastResultCols  = [];    // FROZEN column order (so deleting a field doesn't reorder)
let resultView      = 'json';// 'json' | 'table' | 'csv'
let RV = RV_QUERY;           // active viewer's container ids
let activeContextItems = () => currentQueryItems();  // [{index,query_body}] for export/bulk

const _viewers = {
  query: { ids: RV_QUERY, ctx: () => currentQueryItems(), state: _blankViewerState() },
  index: { ids: RV_INDEX, ctx: () => indexContextItems(), state: _blankViewerState() },
};
let activeViewer = 'query';

function _blankViewerState() {
  return { hits: [], total: 0, json: '', cols: [], view: 'json',
           sort: { col: null, dir: 'asc' }, filters: {},
           selRows: new Set(), selCols: new Set() };
}

/** Save the live globals back into the current viewer, then load another's. */
function activateViewer(name) {
  const cur = _viewers[activeViewer].state;
  cur.hits = lastResultHits; cur.total = lastResultTotal; cur.json = lastResultJson;
  cur.cols = lastResultCols; cur.view = resultView; cur.sort = tableSort; cur.filters = tableFilters;
  cur.selRows = selectedRows; cur.selCols = selectedCols;

  activeViewer = name;
  const v = _viewers[name], s = v.state;
  lastResultHits = s.hits; lastResultTotal = s.total; lastResultJson = s.json;
  lastResultCols = s.cols; resultView = s.view; tableSort = s.sort; tableFilters = s.filters;
  selectedRows = s.selRows; selectedCols = s.selCols;
  RV = v.ids; activeContextItems = v.ctx;
}

/** Write text into the active JSON pane and mirror it to the pop-out. */
function setQueryResults(text) {
  lastResultJson = text;
  const el = document.getElementById(RV.pre);
  if (el) el.textContent = text;
  syncResultsPopout();
}

/** Store the hit rows from a response and refresh the active view. */
function captureResults(data) {
  lastResultHits = Array.isArray(data?.hits) ? data.hits : [];
  lastResultTotal = data?.total ?? data?.total_hits ?? lastResultHits.length;
  lastResultJson = JSON.stringify(data, null, 2);
  lastResultCols = resultColumns(lastResultHits);   // freeze the column order
  tableSort = { col: null, dir: 'asc' };   // reset sort/filter for the new result set
  tableFilters = {};
  if (typeof _distinctCache !== 'undefined') _distinctCache.clear();  // drop cached value lists
  hiddenColumns = new Set();                // fresh result set → all columns visible
  if (typeof selectedRows !== 'undefined') { selectedRows.clear(); selectedCols.clear(); }
  renderResultViews();
}

/** Stable column list: the frozen order + any keys added later (appended). */
function currentColumns() {
  const cols = lastResultCols.slice();
  const seen = new Set(cols);
  for (const h of lastResultHits)
    for (const k of Object.keys(h)) if (!seen.has(k)) { seen.add(k); cols.push(k); }
  return cols;
}

/* ── Column visibility (display only — never touches the data or filters) ──── */
let hiddenColumns   = new Set();   // column names the user chose to hide
let mappedFieldNames = new Set();  // field names declared in the index mapping

/** Meta columns are always available and are never treated as "unmapped". */
function isMetaColumn(c) { return c === '_id' || c === '_index'; }

/** True when a column is NOT declared in the index mapping (mapping known). */
function isUnmappedColumn(c) {
  if (isMetaColumn(c)) return false;
  if (!mappedFieldNames.size) return false;   // mapping unknown → treat all as mapped
  return !mappedFieldNames.has(c);
}

/** Columns actually rendered = frozen order minus the ones the user hid. */
function visibleColumns() {
  return currentColumns().filter(c => !hiddenColumns.has(c));
}


/** Context items for the Index Detail viewer (whole index, match_all). */
function indexContextItems() {
  if (!_currentIndexName) return null;
  return [{ index: _currentIndexName, query_body: { query: { match_all: {} } } }];
}

/** Ordered column list: _id, _index first, then keys in first-seen order. */
function resultColumns(hits) {
  const seen = new Set(), cols = [];
  for (const k of ['_id', '_index']) if (hits.some(h => k in h)) { seen.add(k); cols.push(k); }
  for (const h of hits) for (const k of Object.keys(h)) if (!seen.has(k)) { seen.add(k); cols.push(k); }
  return cols;
}

/** Scalar → string; objects/arrays → compact JSON. */
function cellValue(v) {
  if (v == null) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** Return a parsed object/array if the value is (or encodes) JSON, else null. */
function asJsonObject(v) {
  if (v && typeof v === 'object') return v;
  if (typeof v === 'string') {
    const t = v.trim();
    if ((t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'))) {
      try {
        const parsed = JSON.parse(t);
        if (parsed && typeof parsed === 'object') return parsed;
      } catch (_) {}
    }
  }
  return null;
}

function buildResultsCsv(hits, cols) {
  if (!hits.length) return '';
  cols = cols || resultColumns(hits);
  const esc = (s) => /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  const lines = [cols.map(esc).join(',')];
  for (const h of hits) lines.push(cols.map(c => esc(cellValue(h[c]))).join(','));
  return lines.join('\r\n');
}

/** Switch the visible results view and (re)render it. */
function setResultView(mode) {
  resultView = mode;
  const map = { json: RV.btns[0], table: RV.btns[1], csv: RV.btns[2] };
  ['json', 'table', 'csv'].forEach(m => {
    document.getElementById(map[m])?.classList.toggle('active', m === mode);
  });
  renderResultViews();
}

/** Build the results table HTML (shared by the main pane and the pop-out). */
function resultsTableHtml(hits) {
  if (!hits.length) return '<div class="text-secondary p-2">No rows to display.</div>';
  const cols = resultColumns(hits);
  return `<table class="table table-sm table-striped table-hover mb-0" style="white-space:nowrap;">
      <thead class="table-dark"><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${hits.map(h =>
        `<tr>${cols.map(c => `<td>${esc(cellValue(h[c]))}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>`;
}

function renderResultViews() {
  const pre   = document.getElementById(RV.pre);
  const tbl   = document.getElementById(RV.table);
  const csvEl = document.getElementById(RV.csv);
  if (!pre || !tbl || !csvEl) return;

  pre.classList.toggle('d-none',   resultView !== 'json');
  tbl.classList.toggle('d-none',   resultView !== 'table');
  csvEl.classList.toggle('d-none', resultView !== 'csv');

  if (resultView === 'json') {
    pre.textContent = lastResultJson;
  } else if (resultView === 'table') {
    renderMainResultsTable();
  } else if (resultView === 'csv') {
    csvEl.textContent = lastResultHits.length ? buildResultsCsv(lastResultHits, visibleColumns()) : 'No rows to display.';
  }
  syncResultsPopout();   // keep the detached window in sync with the active view
}

/* ── Rich table: sticky header, per-column sort + value filter, cell edit ── */
let tableSort    = { col: null, dir: 'asc' };
let tableFilters = {};   // { colName: Set(value strings) }
let writeMode    = false; // edit/delete controls are gated behind this (off by default)
const SCOPE_AUTO_LIMIT = 10000; // ≤ this many matches → apply to all without asking

/** Toggle write mode — enables the in-cell edit/delete controls (ES writes). */
function toggleWriteMode() {
  writeMode = !writeMode;
  if (!writeMode && typeof selectedRows !== 'undefined') { selectedRows.clear(); selectedCols.clear(); }
  for (const b of document.querySelectorAll('.js-write-toggle')) {
    b.classList.toggle('btn-warning', writeMode);
    b.classList.toggle('btn-outline-secondary', !writeMode);
    b.innerHTML = writeMode
      ? '<i class="bi bi-unlock me-1"></i>Write'
      : '<i class="bi bi-lock me-1"></i>Read-only';
  }
  renderResultViews();   // refresh main + pop-out (controls appear/disappear)
}

const jsq    = (s) => String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
const cssId  = (s) => 'flt-' + String(s).replace(/[^a-zA-Z0-9_-]/g, '_');

/** Rows passing every active filter EXCEPT the one on `exceptCol`.
 *  Used for Excel-style cascading filter option lists. */
function rowsPassingFiltersExcept(exceptCol) {
  let rows = lastResultHits;
  for (const [col, set] of Object.entries(tableFilters)) {
    if (col === exceptCol) continue;
    if (set && set.size) rows = rows.filter(r => set.has(cellValue(r[col])));
  }
  return rows;
}

/** Distinct values a column's filter should offer. Excel-style: only values that
 *  still exist in rows already narrowed by the OTHER active filters.
 *  This is the INSTANT (loaded-rows) list; toggleColFilter then augments it with
 *  the full distinct set fetched from Elasticsearch. */
function columnUniqueValues(col) {
  const set = new Set();
  for (const r of rowsPassingFiltersExcept(col)) set.add(cellValue(r[col]));
  return [...set].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/** Indices to aggregate over for a column's distinct-value list.
 *  In the Query Editor viewer, use the executed query's index patterns so the
 *  aggregation covers the WHOLE pattern (not just concrete indices that happen
 *  to appear in the loaded page). Otherwise use the loaded rows' _index values,
 *  falling back to the current index-detail index. */
function filterContextIndices() {
  if (activeViewer === 'query' && _queryBaseItems && _queryBaseItems.length) {
    const set = new Set(_queryBaseItems.map(it => it.index).filter(Boolean));
    if (set.size) return [...set];
  }
  const set = new Set();
  for (const r of lastResultHits) if (r && r._index) set.add(r._index);
  if (!set.size && _currentIndexName) set.add(_currentIndexName);
  return [...set];
}

// Cache distinct-value fetches so re-opening a filter is instant.
// Key = indices | field | cascading-filters signature.
const _distinctCache = new Map();

/** Fetch the FULL distinct value set for `col` from Elasticsearch via a terms
 *  aggregation, honoring the other active filters (Excel-style cascading over
 *  the whole index rather than only the loaded page). Returns string values. */
async function fetchColumnDistinct(col) {
  const indices = filterContextIndices();
  if (!indices.length) return [];
  if (col === '_id' || col === '_index') return [];   // meta cols: use loaded values

  const clauses = buildFilterMustClauses(col);        // cascading: other filters
  // In the Query Editor viewer with a single base query, constrain the distinct
  // values to docs the base query matches — so selecting one never dead-ends.
  if (activeViewer === 'query' && _queryBaseItems && _queryBaseItems.length === 1) {
    const base = _queryBaseItems[0].query_body?.query;
    if (base && !base.match_all) clauses.unshift(base);
  }
  const query = clauses.length ? { bool: { must: clauses } } : { match_all: {} };
  const key   = JSON.stringify([indices, col, clauses]);
  if (_distinctCache.has(key)) return _distinctCache.get(key);

  try {
    const res = await api('/api/indices/field-values', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ indices, field: col, query, size: 1000 }),
    });
    const vals = Array.isArray(res?.values) ? res.values.map(String) : [];
    _distinctCache.set(key, vals);
    return vals;
  } catch (e) {
    return [];
  }
}

function tableDisplayRows() {
  let rows = lastResultHits.slice();
  for (const [col, set] of Object.entries(tableFilters)) {
    if (set && set.size) rows = rows.filter(r => set.has(cellValue(r[col])));
  }
  if (tableSort.col != null) {
    const c = tableSort.col, dir = tableSort.dir === 'desc' ? -1 : 1;
    rows.sort((a, b) => {
      const va = cellValue(a[c]), vb = cellValue(b[c]);
      const na = parseFloat(va), nb = parseFloat(vb);
      const numeric = !isNaN(na) && !isNaN(nb) && va.trim() !== '' && vb.trim() !== '';
      const cmp = numeric ? (na - nb) : va.localeCompare(vb, undefined, { numeric: true });
      return cmp * dir;
    });
  }
  return rows;
}

const ROWSEP = '||';
const rowKey = (r) => `${r._index}${ROWSEP}${r._id}`;
let selectedRows = new Set();   // keys "index\0id"
let selectedCols = new Set();   // column names (editable fields)

/** Full interactive table HTML — shared by the main pane and the pop-out.
 *  All onclick handlers use bare function names; the pop-out window has these
 *  bound to the opener's functions, so the same markup works in both. */
function buildInteractiveTableHtml() {
  if (!lastResultHits.length) return '<div class="text-secondary p-2">No rows to display.</div>';

  const cols = currentColumns();          // frozen order — deleting a field won't reorder
  const shownCols = cols.filter(c => !hiddenColumns.has(c));   // display-only visibility
  const rows = tableDisplayRows();
  const editable = (c) => c !== '_id' && c !== '_index';
  const sel = writeMode;   // selection/edit UI only in write mode

  for (const c of [...selectedCols]) if (!shownCols.includes(c)) selectedCols.delete(c);
  const allVisibleSelected = sel && rows.length > 0 && rows.every(r => selectedRows.has(rowKey(r)));

  const headSelTh = sel
    ? `<th class="rt-selcol"><input type="checkbox" ${allVisibleSelected ? 'checked' : ''}
         onclick="toggleAllRows(this)" title="Select all visible rows"/></th>` : '';

  const ths = shownCols.map(c => {
    const arrow    = tableSort.col === c ? (tableSort.dir === 'asc' ? '▲' : '▼') : '';
    const filtered = tableFilters[c] && tableFilters[c].size;
    const colChk = (sel && editable(c))
      ? `<input type="checkbox" class="rt-colsel" ${selectedCols.has(c) ? 'checked' : ''}
           onclick="toggleColSel('${jsq(c)}', this)" title="Select column"/>` : '';
    // Active-filter summary shown on its OWN line below the name so it can never hide it.
    const filterInfo = filtered
      ? `<div class="rt-th-filterval" title="Filtered to: ${esc([...tableFilters[c]].join(', '))}">= ${esc([...tableFilters[c]].join(', '))}</div>`
      : '';
    return `<th class="rt-th${filtered ? ' filtered' : ''}">
        <div class="rt-th-row">
          ${colChk}
          <span class="rt-sort" onclick="toggleTableSort('${jsq(c)}')">${esc(c)} <span class="rt-arrow">${arrow}</span></span>
          <button class="rt-funnel" onclick="toggleColFilter(event,'${jsq(c)}')" title="Filter values">
            <i class="bi bi-funnel${filtered ? '-fill' : ''}"></i>
          </button>
        </div>
        ${filterInfo}
        <div class="rt-filter d-none" id="${cssId(c)}"></div>
      </th>`;
  }).join('');

  const body = rows.map((r, ri) => {
    const id = r._id, idx = r._index;
    const canEdit = sel && id != null && idx != null;
    const key = rowKey(r);
    const rowChk = sel
      ? `<td class="rt-selcol"><input type="checkbox" ${selectedRows.has(key) ? 'checked' : ''}
           onclick="toggleRowSel('${jsq(key)}', this)"/></td>` : '';
    return '<tr>' + rowChk + shownCols.map(c => {
      if (!editable(c)) return `<td>${esc(cellValue(r[c]))}</td>`;

      // Field absent from this document → red-gray cell with tooltip + "add" control.
      if (!(c in r)) {
        const addBtn = canEdit ? `<span class="rt-cellctrl">
            <button onclick="editCell('${jsq(id)}','${jsq(idx)}','${jsq(c)}', this)" title="Add this field"><i class="bi bi-plus-lg"></i></button>
          </span>` : '';
        return `<td class="rt-cell rt-cell-missing" title="This field does not exist in this document">
          <span class="rt-missing">—</span>${addBtn}</td>`;
      }

      const isJson = asJsonObject(r[c]) !== null;
      const val = esc(cellValue(r[c]));
      let ctrlBtns = '';
      if (isJson) {
        ctrlBtns += `<button onclick="showJsonCell(${ri},'${jsq(c)}', this)" title="View as pretty JSON"><i class="bi bi-braces"></i></button>`;
      }
      if (canEdit) {
        ctrlBtns += `<button onclick="editCell('${jsq(id)}','${jsq(idx)}','${jsq(c)}', this)" title="Edit value"><i class="bi bi-pencil"></i></button>
          <button class="rt-del" onclick="deleteCell('${jsq(id)}','${jsq(idx)}','${jsq(c)}', this)" title="Delete field from document"><i class="bi bi-trash"></i></button>`;
      }
      const ctrls = ctrlBtns ? `<span class="rt-cellctrl">${ctrlBtns}</span>` : '';
      return `<td class="rt-cell"><span class="rt-val" title="${val}">${val}</span>${ctrls}</td>`;
    }).join('') + '</tr>';
  }).join('');

  const bar = selectionBarHtml();
  return `<div id="rtSelBar" class="${bar ? 'rt-selbar' : ''}">${bar}</div>
     ${fieldVisibilityBannerHtml()}
     <table class="table table-sm table-striped table-hover mb-0 rt-table">
       <thead><tr>${headSelTh}${ths}</tr></thead><tbody>${body}</tbody></table>`;
}

/** Banner above the table summarising hidden columns (esp. unmapped ones). */
function fieldVisibilityBannerHtml() {
  const hidden = currentColumns().filter(c => hiddenColumns.has(c));
  if (!hidden.length) return '';
  const unmapped = hidden.filter(isUnmappedColumn).length;
  const mapped   = hidden.length - unmapped;
  let parts = [];
  if (mapped)   parts.push(`${mapped} field${mapped > 1 ? 's' : ''}`);
  if (unmapped) parts.push(`${unmapped} unmapped field${unmapped > 1 ? 's' : ''}`);
  const summary = parts.join(' + ');
  return `<div class="rt-fieldbanner">
      <i class="bi bi-eye-slash"></i>
      <span>${summary} hidden</span>
      <button class="btn btn-sm btn-link p-0" onclick="showAllColumns()">Show all</button>
      <button class="btn btn-sm btn-link p-0" onclick="openFieldVisibility(this)">Manage fields…</button>
    </div>`;
}

/* ── Column-visibility controls (view only) ──────────────────────────────── */

/** Show every column again. */
function showAllColumns() {
  hiddenColumns.clear();
  refreshTables();
}

/** Hide/unhide a single column and re-render live. */
function setColumnHidden(col, hide) {
  if (hide) hiddenColumns.add(col); else hiddenColumns.delete(col);
  refreshTables();
  renderFieldVisibilityBody();
}

/** Hide (or reveal) every unmapped column at once. */
function setUnmappedHidden(hide) {
  for (const c of currentColumns()) {
    if (!isUnmappedColumn(c)) continue;
    if (hide) hiddenColumns.add(c); else hiddenColumns.delete(c);
  }
  refreshTables();
  renderFieldVisibilityBody();
}

/** Check/uncheck every mapped column. */
function setAllMappedHidden(hide) {
  for (const c of currentColumns()) {
    if (isUnmappedColumn(c) || isMetaColumn(c)) continue;
    if (hide) hiddenColumns.add(c); else hiddenColumns.delete(c);
  }
  refreshTables();
  renderFieldVisibilityBody();
}

/** Document the Field Visibility modal currently lives in (main or pop-out). */
let _fieldVisDoc = document;

/** Open the "Field Visibility" picker. Pass the clicked element (or nothing)
 *  — the modal renders in that element's document, so the same button works
 *  in the main window and in the results pop-out. */
function openFieldVisibility(el) {
  const doc = (el && el.ownerDocument) || document;
  const existing = doc.querySelector('.rt-modal-overlay.rt-fieldvis');
  if (existing) { existing.remove(); return; }
  _fieldVisDoc = doc;
  const wrap = doc.createElement('div');
  wrap.className = 'rt-modal-overlay rt-fieldvis';
  wrap.innerHTML = `<div class="rt-modal rt-modal-fields">
      <div class="rt-modal-title"><i class="bi bi-eye me-1"></i>Field Visibility</div>
      <input class="form-control form-control-sm rt-fieldvis-search mb-2"
             placeholder="search fields…" oninput="renderFieldVisibilityBody()"/>
      <div class="rt-fieldvis-body"></div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  doc.body.appendChild(wrap);
  const done = () => { wrap.remove(); doc.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') done(); };
  doc.addEventListener('keydown', onKey);
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap || e.target.dataset.act === 'close') done();
  });
  renderFieldVisibilityBody();
}

/** (Re)draw the checklist inside the open Field Visibility modal. */
function renderFieldVisibilityBody() {
  const doc = (_fieldVisDoc && !_fieldVisDoc.defaultView?.closed) ? _fieldVisDoc : document;
  const host = doc.querySelector('.rt-fieldvis-body');
  if (!host) return;
  const searchEl = doc.querySelector('.rt-fieldvis-search');
  const q = (searchEl?.value || '').toLowerCase();

  const cols = currentColumns().filter(isColumnPickable);
  const mapped   = cols.filter(c => !isUnmappedColumn(c));
  const unmapped = cols.filter(isUnmappedColumn);

  const mappedHiddenCount = mapped.filter(c => hiddenColumns.has(c)).length;
  const allMappedShown    = mappedHiddenCount === 0;
  const unmappedHidden    = unmapped.length && unmapped.every(c => hiddenColumns.has(c));

  const row = (c) => {
    if (q && !c.toLowerCase().includes(q)) return '';
    const shown = !hiddenColumns.has(c);
    const badge = isUnmappedColumn(c)
      ? `<span class="rt-field-badge" title="Not declared in the index mapping">unmapped</span>` : '';
    return `<label class="rt-field-row">
        <input type="checkbox" ${shown ? 'checked' : ''}
               onchange="setColumnHidden('${jsq(c)}', !this.checked)"/>
        <span class="rt-field-name">${esc(c)}</span>${badge}
      </label>`;
  };

  let html = `<div class="rt-field-group">
      <label class="rt-field-master">
        <input type="checkbox" ${allMappedShown ? 'checked' : ''}
               onchange="setAllMappedHidden(!this.checked)"/>
        <span>All mapped fields (${mapped.length})</span>
      </label>
      ${mapped.map(row).join('')}
    </div>`;

  if (unmapped.length) {
    html += `<div class="rt-field-group rt-field-group-unmapped">
        <label class="rt-field-master">
          <input type="checkbox" ${!unmappedHidden ? 'checked' : ''}
                 onchange="setUnmappedHidden(!this.checked)"/>
          <span>Unmapped fields (${unmapped.length})</span>
        </label>
        ${unmapped.map(row).join('')}
      </div>`;
  }
  host.innerHTML = html;
}

/** _id / _index are structural — keep them out of the picker checklist. */
function isColumnPickable(c) { return !isMetaColumn(c); }

/* ── Aggregate results by field(s) ────────────────────────────────────────── */

/** Columns whose loaded values look numeric (candidates for the metric). */
function numericColumns() {
  const cols = currentColumns().filter(c => c !== '_id' && c !== '_index');
  return cols.filter(c => lastResultHits.some(h =>
    typeof h[c] === 'number' && !Number.isNaN(h[c])));
}

/** Open the group-by aggregation dialog (doc-aware — works in the pop-out). */
function openAggregateDialog(el) {
  const doc = (el && el.ownerDocument) || document;
  doc.querySelector('.rt-modal-overlay.rt-aggregate')?.remove();

  const items = activeContextItems();
  if (!items) { showToast('Invalid query JSON — cannot aggregate', 'bg-danger'); return; }
  const cols = currentColumns().filter(c => c !== '_id');
  if (!cols.length) { showToast('Run a query first — no fields to aggregate', 'bg-warning'); return; }
  const numeric = numericColumns();

  const wrap = doc.createElement('div');
  wrap.className = 'rt-modal-overlay rt-aggregate';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:520px;max-width:860px;">
      <div class="rt-modal-title"><i class="bi bi-bar-chart me-1"></i>Aggregate results</div>
      <div class="rt-modal-body">
        <label class="small text-secondary mb-1">Group by field(s) — nested in the order checked</label>
        <div class="agg-fields rt-fieldvis-body mb-2" style="max-height:180px;overflow:auto;">
          ${cols.map(c => `<label class="rt-field-row">
              <input type="checkbox" value="${esc(c)}"/>
              <span class="rt-field-name">${esc(c)}</span>
            </label>`).join('')}
        </div>
        <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
          <label class="small text-secondary mb-0">Metric (numeric field)</label>
          <select class="form-select form-select-sm agg-metric" style="width:200px;">
            <option value="">— count only —</option>
            ${numeric.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}
          </select>
          <label class="small text-secondary mb-0 ms-2">Max groups</label>
          <input type="number" class="form-control form-control-sm agg-size" value="100" min="1" max="1000" style="width:90px;"/>
        </div>
        <div class="agg-status small text-secondary d-none"></div>
        <div class="agg-results mt-2" style="max-height:320px;overflow:auto;"></div>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-primary" data-act="run"><i class="bi bi-play-fill me-1"></i>Aggregate</button>
        <button class="btn btn-sm btn-outline-success d-none" data-act="csv"><i class="bi bi-download me-1"></i>CSV</button>
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  doc.body.appendChild(wrap);

  let lastAgg = null;   // {rows, cols} of the last run, for the CSV download

  const done = () => { wrap.remove(); doc.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') done(); };
  doc.addEventListener('keydown', onKey);

  const status = (msg) => {
    const s = wrap.querySelector('.agg-status');
    s.textContent = msg || '';
    s.classList.toggle('d-none', !msg);
  };

  async function run() {
    const groupBy = [...wrap.querySelectorAll('.agg-fields input:checked')].map(b => b.value);
    if (!groupBy.length) { status('Check at least one field to group by.'); return; }
    const metric = wrap.querySelector('.agg-metric').value;
    const size   = Math.max(1, Math.min(1000, parseInt(wrap.querySelector('.agg-size').value) || 100));

    // Aggregate what the user is LOOKING at: base query + active column filters.
    const filterClauses = buildFilterMustClauses();
    const merged = items.map(it => ({
      index: it.index,
      query_body: { query: mergeQueryWithFilters(
        (it.query_body || {}).query || { match_all: {} }, filterClauses) },
    }));

    status('Aggregating…');
    const res = await api('/api/query/aggregate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ per_index_queries: merged, group_by: groupBy,
                             metric_field: metric, size }),
    });
    if (!res || res.error) { status('✗ ' + (res?.error || 'unknown error')); return; }

    const hasMetric = !!metric && res.rows.some(r => 'sum' in r);
    const outCols = [...groupBy, 'count', ...(hasMetric ? ['sum', 'avg', 'min', 'max'] : [])];
    lastAgg = { rows: res.rows, cols: outCols };
    status(`${res.rows.length.toLocaleString()} group(s)`
      + (hasMetric ? ` · metric: ${metric}` : '')
      + (res.truncated ? ' · ⚠ truncated — raise Max groups' : ''));
    wrap.querySelector('[data-act="csv"]').classList.toggle('d-none', !res.rows.length);
    wrap.querySelector('.agg-results').innerHTML = res.rows.length
      ? `<table class="table table-sm table-striped table-hover mb-0" style="white-space:nowrap;font-size:0.78rem;">
          <thead class="table-dark"><tr>${outCols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
          <tbody>${res.rows.map(r =>
            `<tr>${outCols.map(c => `<td>${esc(r[c] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody>
        </table>`
      : '<div class="text-secondary p-2">No groups found.</div>';
  }

  function downloadCsv() {
    if (!lastAgg || !lastAgg.rows.length) return;
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const blob = new Blob([buildResultsCsv(lastAgg.rows, lastAgg.cols)],
                          { type: 'text/csv;charset=utf-8' });
    const a = doc.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `cc_aggregation_${ts}.csv`;
    doc.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  wrap.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) { if (e.target === wrap) done(); return; }
    const act = b.getAttribute('data-act');
    if (act === 'run') run();
    else if (act === 'csv') downloadCsv();
    else if (act === 'close') done();
  });
}


function renderMainResultsTable() {
  const host = document.getElementById(RV.table);
  if (host) host.innerHTML = buildInteractiveTableHtml();
}

/* Re-render the active view in the main window AND the pop-out. */
function refreshTables() { renderResultViews(); }

/** Inner HTML of the selection action bar, derived from selection state. */
function selectionBarHtml() {
  const nRows = selectedRows.size, cols = [...selectedCols];
  if (!nRows && !cols.length) return '';
  let html = '';
  if (nRows) {
    html += `<span class="me-2"><b>${nRows}</b> row${nRows > 1 ? 's' : ''} selected</span>
      <button class="btn btn-sm btn-danger py-0 px-2 me-3" onclick="deleteSelectedRows(this)">
        <i class="bi bi-trash me-1"></i>Delete document${nRows > 1 ? 's' : ''}</button>`;
  }
  if (cols.length) {
    const where = nRows ? `on ${nRows} selected row${nRows > 1 ? 's' : ''}` : 'whole column';
    html += `<span class="me-2">Field${cols.length > 1 ? 's' : ''} <b>${cols.map(esc).join(', ')}</b> (${where})</span>
      <button class="btn btn-sm btn-warning py-0 px-2 me-1" onclick="columnFieldOp('set', this)">
        <i class="bi bi-pencil me-1"></i>Edit</button>
      <button class="btn btn-sm btn-danger py-0 px-2 me-3" onclick="columnFieldOp('delete', this)">
        <i class="bi bi-trash me-1"></i>Delete field</button>`;
  }
  html += `<button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="clearSelection()">Clear</button>`;
  return html;
}

/* ── Selection state + bulk actions ──────────────────────────────────────── */
function toggleRowSel(key, cb) {
  if (cb.checked) selectedRows.add(key); else selectedRows.delete(key);
  refreshTables();
}
function toggleAllRows(cb) {
  for (const r of tableDisplayRows()) {
    const k = rowKey(r);
    if (cb.checked) selectedRows.add(k); else selectedRows.delete(k);
  }
  refreshTables();
}
function toggleColSel(col, cb) {
  if (cb.checked) selectedCols.add(col); else selectedCols.delete(col);
  refreshTables();
}

function clearSelection() {
  selectedRows.clear(); selectedCols.clear();
  refreshTables();
}

/** Build [{index, query_body}] for the current query (multi or single). */
function currentQueryItems() {
  if (perIndexQueries.length > 1) {
    return perIndexQueries.map(p => ({ index: p.index, query_body: p.query_body }));
  }
  let qb;
  try { qb = JSON.parse(document.getElementById('queryBody').value); }
  catch (e) { return null; }
  return [{ index: document.getElementById('queryIndex').value.trim(), query_body: qb }];
}

async function deleteSelectedRows(el) {
  if (!writeMode) { showToast('Enable Write mode first', 'bg-warning'); return; }
  const doc = el ? el.ownerDocument : document;
  const docs = [...selectedRows].map(k => { const [index, id] = k.split(ROWSEP); return { index, id }; });
  if (!docs.length) return;
  if (!await uiConfirm(doc, { title: `Delete ${docs.length} document(s) from Elasticsearch?`,
    message: 'This permanently deletes the selected documents and cannot be undone.',
    okText: 'Delete', danger: true })) return;
  const res = await api('/api/docs/bulk-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope: 'selected', docs }),
  });
  if (!res || res.error) { showToast('Delete failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  const keyset = new Set(selectedRows);
  lastResultHits = lastResultHits.filter(r => !keyset.has(rowKey(r)));
  selectedRows.clear();
  renderResultViews();
  showToast(`Deleted ${res.deleted} document(s)`, 'bg-success');
}

/* ── Doc-aware modals (render in the window the user is working in) ────────── */

/** Text-input modal → resolves to the string, or null on cancel. */
function uiPrompt(doc, opts) {
  doc = doc || document;
  return new Promise(resolve => {
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    wrap.innerHTML = `<div class="rt-modal">
        <div class="rt-modal-title">✎ ${esc(opts.title || 'Edit')}</div>
        <input class="form-control rt-modal-input" value="${esc(opts.value ?? '')}"/>
        <div class="rt-modal-actions">
          <button class="btn btn-sm btn-primary" data-ok="1">${esc(opts.okText || 'Save')}</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    doc.body.appendChild(wrap);
    const input = wrap.querySelector('.rt-modal-input');
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); else if (e.key === 'Enter') done(input.value); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1' ? input.value : null); return; }
      if (e.target === wrap) done(null);
    });
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}

/** Confirm modal → resolves to true/false. */
function uiConfirm(doc, opts) {
  doc = doc || document;
  return new Promise(resolve => {
    const danger = !!opts.danger;
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    wrap.innerHTML = `<div class="rt-modal">
        <div class="rt-modal-title">${danger ? '🗑' : '❓'} ${esc(opts.title || 'Confirm')}</div>
        ${opts.message ? `<div class="rt-modal-body">${esc(opts.message)}</div>` : ''}
        <div class="rt-modal-actions">
          <button class="btn btn-sm ${danger ? 'btn-danger' : 'btn-primary'}" data-ok="1">${esc(opts.okText || 'OK')}</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    doc.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(false); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1'); return; }
      if (e.target === wrap) done(false);
    });
  });
}

/** Multi-button choice modal → resolves to the chosen button's `value`
 *  (or `null` when dismissed via Escape / backdrop click). */
function uiChoice(doc, opts) {
  doc = doc || document;
  const buttons = opts.buttons || [{ value: true, text: 'OK', cls: 'btn-primary' }];
  return new Promise(resolve => {
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const btnHtml = buttons.map((b, i) =>
      `<button class="btn btn-sm ${b.cls || 'btn-outline-secondary'}" data-idx="${i}">${esc(b.text)}</button>`
    ).join('');
    wrap.innerHTML = `<div class="rt-modal">
        <div class="rt-modal-title">⬇ ${esc(opts.title || 'Choose')}</div>
        ${opts.message ? `<div class="rt-modal-body">${esc(opts.message)}</div>` : ''}
        <div class="rt-modal-actions">${btnHtml}</div></div>`;
    doc.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(buttons[+b.getAttribute('data-idx')].value); return; }
      if (e.target === wrap) done(null);
    });
  });
}

/** Open a read-only pretty-JSON viewer for an object/array cell value. */
function showJsonCell(ri, field, el) {
  const doc = el && el.ownerDocument ? el.ownerDocument : document;
  const rows = tableDisplayRows();
  const r = rows[ri];
  if (!r) return;
  showJsonModal(doc, {
    title: field, value: r[field],
    id: r._id, index: r._index, field,
  });
}

/** Pretty-print a JSON value in a modal. When write mode is on and the row is
 *  editable, the JSON can be edited in place and saved back to Elasticsearch. */
function showJsonModal(doc, opts) {
  doc = doc || document;
  const { title, value, id, index, field } = opts || {};
  const editable = !!(writeMode && id != null && index != null &&
                      field && field !== '_id' && field !== '_index');

  const obj = asJsonObject(value);
  const pretty = obj !== null ? JSON.stringify(obj, null, 2) : String(value ?? '');

  const wrap = doc.createElement('div');
  wrap.className = 'rt-modal-overlay';
  wrap.innerHTML = `<div class="rt-modal rt-modal-json">
      <div class="rt-modal-title"><i class="bi bi-braces me-1"></i>${esc(title || 'JSON')}</div>
      <div class="rt-json-host"></div>
      <div class="rt-json-err text-danger small mb-2 d-none"></div>
      <div class="rt-modal-actions"></div>
    </div>`;
  doc.body.appendChild(wrap);

  const host    = wrap.querySelector('.rt-json-host');
  const errEl   = wrap.querySelector('.rt-json-err');
  const actions = wrap.querySelector('.rt-modal-actions');
  let editing = false;

  const done = () => { wrap.remove(); doc.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape' && !editing) done(); };
  doc.addEventListener('keydown', onKey);

  function showErr(msg) {
    errEl.textContent = msg || '';
    errEl.classList.toggle('d-none', !msg);
  }

  function render() {
    showErr('');
    if (editing) {
      host.innerHTML = `<textarea class="rt-json-edit" spellcheck="false"></textarea>`;
      host.querySelector('.rt-json-edit').value = pretty;
      actions.innerHTML = `
        <button class="btn btn-sm btn-outline-secondary" data-act="format"><i class="bi bi-magic me-1"></i>Format</button>
        <button class="btn btn-sm btn-primary" data-act="save"><i class="bi bi-save me-1"></i>Save</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="view">Cancel</button>`;
      setTimeout(() => host.querySelector('.rt-json-edit')?.focus(), 0);
    } else {
      host.innerHTML = `<pre class="rt-json-pre">${esc(pretty)}</pre>`;
      actions.innerHTML = `
        <button class="btn btn-sm btn-outline-primary" data-act="copy"><i class="bi bi-clipboard me-1"></i>Copy</button>
        ${editable ? `<button class="btn btn-sm btn-warning" data-act="edit"><i class="bi bi-pencil me-1"></i>Edit</button>` : ''}
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>`;
    }
  }

  async function save() {
    const text = host.querySelector('.rt-json-edit').value;
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      showErr('Invalid JSON: ' + e.message);
      return;
    }
    done();
    await applyDocChange(id, index, field, 'set', parsed);
  }

  wrap.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) { if (e.target === wrap && !editing) done(); return; }
    const act = b.getAttribute('data-act');
    if (act === 'copy') {
      const nav = (doc.defaultView && doc.defaultView.navigator) || navigator;
      try { nav.clipboard.writeText(pretty); b.innerHTML = '<i class="bi bi-check2 me-1"></i>Copied'; } catch (_) {}
    } else if (act === 'edit') {
      editing = true; render();
    } else if (act === 'view') {
      editing = false; render();
    } else if (act === 'format') {
      const ta = host.querySelector('.rt-json-edit');
      try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2); showErr(''); }
      catch (err) { showErr('Invalid JSON: ' + err.message); }
    } else if (act === 'save') {
      save();
    } else if (act === 'close') {
      done();
    }
  });

  render();
}

/** Modal asking whether to apply a column op to the viewable rows or ALL hits.
 *  Resolves to 'all' | 'visible' | null (cancel). */
function chooseScopeDialog(doc, verb, fields, visibleCount, totalCount, warn) {
  doc = doc || document;
  return new Promise(resolve => {
    const danger = verb.toLowerCase().startsWith('delete');
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const note = warn ? `<div class="rt-modal-note">
        <i class="bi bi-exclamation-triangle-fill"></i>
        Over ${(10000).toLocaleString()} documents match. Choosing <b>viewable</b> changes only the
        loaded rows — the rest stay unchanged, so the displayed data will be
        <b>inconsistent</b> with Elasticsearch. Reload the affected index/indices afterward to refresh.
      </div>` : '';
    wrap.innerHTML = `
      <div class="rt-modal">
        <div class="rt-modal-title">${danger ? '🗑' : '✎'} ${esc(verb)} field${fields.length > 1 ? 's' : ''}
          <span class="text-info">${esc(fields.join(', '))}</span></div>
        <div class="rt-modal-body">
          You are viewing <b>${visibleCount}</b> of <b>${totalCount.toLocaleString()}</b>
          document(s) matching the query.<br>Apply this change to:
        </div>
        ${note}
        <div class="rt-modal-actions">
          <button class="btn btn-sm ${danger ? 'btn-danger' : 'btn-warning'}" data-c="all">
            ALL ${totalCount.toLocaleString()} matching</button>
          <button class="btn btn-sm btn-outline-primary" data-c="visible">
            Only ${visibleCount} viewable</button>
          <button class="btn btn-sm btn-outline-secondary" data-c="">Cancel</button>
        </div>
      </div>`;
    doc.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-c') || null); return; }
      if (e.target === wrap) done(null);
    });
  });
}

async function columnFieldOp(op, el) {
  if (!writeMode) { showToast('Enable Write mode first', 'bg-warning'); return; }
  const doc = el ? el.ownerDocument : document;
  const cols = [...selectedCols];
  if (!cols.length) return;

  let value = null;
  if (op === 'set') {
    value = await uiPrompt(doc, { title: `Set value for field(s) [${cols.join(', ')}]`, value: '', okText: 'Set' });
    if (value === null) return;
  }

  const rowsSelected = selectedRows.size > 0;
  let payloadBase, localPredicate;

  if (rowsSelected) {
    const docs = [...selectedRows].map(k => { const [index, id] = k.split(ROWSEP); return { index, id }; });
    const verb = op === 'delete' ? 'Delete' : 'Set';
    if (!await uiConfirm(doc, { title: `${verb} field(s) [${cols.join(', ')}] on ${docs.length} selected document(s)?`,
                                okText: verb, danger: op === 'delete' })) return;
    payloadBase = { scope: 'selected', docs };
    const idset = new Set(selectedRows);
    localPredicate = (r) => idset.has(rowKey(r));
  } else {
    // Whole column. Up to SCOPE_AUTO_LIMIT matching docs → just apply to all
    // (single confirm). Beyond that → ask all-vs-viewable, warning that
    // "viewable" leaves the loaded data inconsistent with Elasticsearch.
    const visible = tableDisplayRows();
    const verb  = op === 'delete' ? 'Delete' : 'Set';
    const totalMatching = Math.max(lastResultTotal || 0, visible.length);

    let choice;
    if (totalMatching <= visible.length) {
      // Everything matching is already on screen → no viewed/all distinction.
      if (!await uiConfirm(doc, { title: `${verb} field(s) [${cols.join(', ')}] on all ${visible.length} document(s)?`,
                                  okText: verb, danger: op === 'delete' })) return;
      choice = 'all';
    } else {
      // More match than are shown → let the user pick viewed vs all
      // (warn about inconsistency only for very large sets).
      choice = await chooseScopeDialog(doc, verb, cols, visible.length, totalMatching,
                                       /*warn=*/ totalMatching > SCOPE_AUTO_LIMIT);
      if (!choice) return;
    }

    if (choice === 'all') {
      const items = activeContextItems();
      if (!items) { showToast('Invalid query JSON — cannot target all docs', 'bg-danger'); return; }
      payloadBase = { scope: 'all', per_index_queries: items };
      localPredicate = () => true;   // every loaded row matches the query
    } else {
      const docs = visible.filter(r => r._id != null && r._index != null)
                          .map(r => ({ index: r._index, id: r._id }));
      payloadBase = { scope: 'selected', docs, _partial: true };
      const vis = new Set(visible.map(rowKey));
      localPredicate = (r) => vis.has(rowKey(r));
    }
  }

  let total = 0;
  const partial = payloadBase._partial;
  delete payloadBase._partial;
  for (const field of cols) {
    const res = await api('/api/docs/bulk-field', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payloadBase, field, op, value }),
    });
    if (!res || res.error) { showToast(`Failed on "${field}": ` + (res?.error || 'unknown'), 'bg-danger'); return; }
    total += res.updated || 0;
  }

  // Reflect locally on the affected loaded rows.
  for (const r of lastResultHits) {
    if (!localPredicate(r)) continue;
    for (const c of cols) { if (op === 'delete') delete r[c]; else r[c] = value; }
  }
  clearSelection();
  renderResultViews();
  showToast(`${op === 'delete' ? 'Deleted' : 'Updated'} ${total} field value(s)`, 'bg-success');
  if (partial) {
    await uiConfirm(doc, { title: 'Applied to visible rows only',
      message: 'Other matching documents were NOT changed, so the loaded data is now out of sync '
             + 'with Elasticsearch. Reload the affected index/indices (re-run the query) to refresh.',
      okText: 'Got it' });
  }
}

function toggleTableSort(col) {
  if (tableSort.col === col) {
    if (tableSort.dir === 'asc') tableSort.dir = 'desc';
    else { tableSort.col = null; }            // asc → desc → off
  } else { tableSort.col = col; tableSort.dir = 'asc'; }
  refreshTables();
}

/** One `<label>` per value for the filter list, checked per the selection. */
function _filterListHtml(uniques, sel) {
  const all = !sel;
  return uniques.map(v => {
    const checked = (all || sel.has(v)) ? 'checked' : '';
    return `<label><input type="checkbox" value="${esc(v)}" ${checked}/><span>${esc(v === '' ? '(empty)' : v)}</span></label>`;
  }).join('');
}

async function toggleColFilter(ev, col) {
  ev.stopPropagation();
  const doc = ev.target.ownerDocument;        // works in the main window OR the pop-out
  const panel = doc.getElementById(cssId(col));
  if (!panel) return;
  const wasOpen = !panel.classList.contains('d-none');
  doc.querySelectorAll('.rt-filter').forEach(p => p.classList.add('d-none'));
  if (wasOpen) return;

  const sel = tableFilters[col];
  const all = !sel;
  const loaded = columnUniqueValues(col);     // instant list from loaded rows
  panel.innerHTML = `
    <input class="rt-filter-search" placeholder="search values…" oninput="rtFilterSearch(this)"/>
    <label class="rt-filter-all">
      <input type="checkbox" onchange="rtFilterToggleAll(this)" ${all ? 'checked' : ''}/>
      <span>(Select all)</span>
    </label>
    <div class="rt-filter-loading text-secondary" style="font-size:.68rem;padding:2px 6px;">
      <span class="spinner-border spinner-border-sm" style="width:.7rem;height:.7rem;"></span> loading all values…
    </div>
    <div class="rt-filter-list">${_filterListHtml(loaded, sel)}</div>
    <div class="rt-filter-actions">
      <button class="btn btn-sm btn-primary py-0" onclick="applyColFilter('${jsq(col)}', this)">Apply</button>
      <button class="btn btn-sm btn-outline-light py-0" onclick="clearColFilter('${jsq(col)}')">Clear</button>
    </div>`;
  panel.classList.remove('d-none');

  // Augment with the FULL distinct set from Elasticsearch (whole index, and
  // Excel-style cascading-aware) so a small loaded page doesn't hide values.
  const full = await fetchColumnDistinct(col);
  if (panel.classList.contains('d-none')) return;   // closed while awaiting
  const listEl = panel.querySelector('.rt-filter-list');
  panel.querySelector('.rt-filter-loading')?.remove();
  if (!listEl || !full.length) return;

  // Preserve any picks the user made during the load, then union server values
  // with the loaded ones so nothing already visible disappears.
  const currentlyChecked = new Set(
    [...listEl.querySelectorAll('input[type=checkbox]')].filter(b => b.checked).map(b => b.value));
  const merged = [...new Set([...full, ...loaded])]
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const selNow = tableFilters[col];
  const allNow = !selNow;
  listEl.innerHTML = merged.map(v => {
    const isChecked = allNow ? true : (selNow.has(v) || currentlyChecked.has(v));
    return `<label><input type="checkbox" value="${esc(v)}" ${isChecked ? 'checked' : ''}/><span>${esc(v === '' ? '(empty)' : v)}</span></label>`;
  }).join('');
  // Re-apply any active search term to the freshly rendered list.
  const search = panel.querySelector('.rt-filter-search');
  if (search && search.value) rtFilterSearch(search);
}

function rtFilterSearch(inp) {
  const q = inp.value.toLowerCase();
  inp.closest('.rt-filter').querySelectorAll('.rt-filter-list label').forEach(l => {
    l.style.display = l.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

/** "(Select all)" master checkbox — sets every visible value checkbox to match. */
function rtFilterToggleAll(master) {
  master.closest('.rt-filter').querySelectorAll('.rt-filter-list label').forEach(l => {
    if (l.style.display === 'none') return;            // only affect visible (searched) items
    const cb = l.querySelector('input[type=checkbox]');
    if (cb) cb.checked = master.checked;
  });
}

function applyColFilter(col, btn) {
  // The "(Select all)" master isn't a value box — read only the value checkboxes.
  const doc = (btn && btn.ownerDocument) || document;
  const panel = doc.getElementById(cssId(col));
  const boxes = [...panel.querySelectorAll('.rt-filter-list input[type=checkbox]')];
  const checked = boxes.filter(b => b.checked).map(b => b.value);
  if (checked.length === 0 || checked.length === boxes.length) delete tableFilters[col];
  else tableFilters[col] = new Set(checked);
  commitFilters();
}

function clearColFilter(col) {
  delete tableFilters[col];
  commitFilters();
}

/** Clear ALL column filters at once */
function clearAllFilters() {
  tableFilters = {};
  commitFilters();
  showToast('All filters cleared', 'bg-info');
}

/** Apply the current filters. When the loaded page is only a slice of the
 *  matching docs, resolve the filters against Elasticsearch (so values absent
 *  from the loaded rows still match) — this works on ANY field of ANY index,
 *  in both the Index viewer and the Query Editor results. When every matching
 *  doc is already loaded, filter locally (instant). */
function commitFilters() {
  updateFilterButtonState();
  if (activeViewer === 'index' && _currentIndexName &&
      lastResultHits.length < (_indexFullTotal || 0)) {
    applyIndexFiltersServerSide();
    return;
  }
  if (activeViewer === 'query' && _queryBaseItems &&
      lastResultHits.length < (_queryTotalMatching || 0)) {
    applyQueryFiltersServerSide();
    return;
  }
  refreshTables();
  updateShowingCount();
}

/** Combine a base query with the active column-filter clauses (AND semantics). */
function mergeQueryWithFilters(baseQuery, filterClauses) {
  const must = [];
  if (baseQuery && !baseQuery.match_all) must.push(baseQuery);
  must.push(...filterClauses);
  if (!must.length) return { match_all: {} };
  if (must.length === 1) return must[0];
  return { bool: { must } };
}

/** Re-run the Query Editor results with the active column filters merged into
 *  each per-index query, preserving the filters + column visibility. */
async function applyQueryFiltersServerSide() {
  if (!_queryBaseItems || !_queryBaseItems.length) { refreshTables(); return; }
  // Snapshot filters/visibility — captureResults() resets them.
  const savedFilters = {};
  for (const [c, s] of Object.entries(tableFilters)) if (s?.size) savedFilters[c] = [...s];
  const savedHidden = [...hiddenColumns];

  const filterClauses = buildFilterMustClauses();
  const items = _queryBaseItems.map(it => {
    const qb = { ...(it.query_body || {}) };
    qb.query = mergeQueryWithFilters(qb.query || { match_all: {} }, filterClauses);
    return { index: it.index, query_body: qb };
  });
  const size = querySizeValue();
  const sortDir = document.querySelector('input[name="sortDir"]:checked')?.value ?? 'desc';

  const metaEl = document.getElementById('queryMeta');
  if (metaEl) metaEl.textContent = 'Filtering…';

  const data = await api('/api/query/multi-run', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ per_index_queries: items, size, sort_direction: sortDir }),
  });
  if (!data || data.error) {
    showToast('Filter query failed: ' + (data?.error || 'unknown'), 'bg-danger');
    return;
  }

  const matchTotal = (data.per_index_meta || []).reduce((s, m) => s + (m.total || 0), 0);
  captureResults(data);                     // freezes columns, clears filters/visibility
  lastResultTotal = matchTotal || data.total_hits || lastResultHits.length;
  tableFilters = {};
  for (const [c, vals] of Object.entries(savedFilters)) tableFilters[c] = new Set(vals);
  const present = new Set(currentColumns());
  hiddenColumns = new Set(savedHidden.filter(c => present.has(c)));
  renderResultViews();
  updateFilterButtonState();
  if (metaEl) {
    const loaded = lastResultHits.length;
    metaEl.textContent = `${loaded.toLocaleString()} shown of ${(matchTotal).toLocaleString()} matching`;
  }
}

/** Re-query the current index with the active column filters as an ES query,
 *  preserving the filters + column visibility across the reload. */
async function applyIndexFiltersServerSide() {
  if (!_currentIndexName) return;
  // Snapshot filters/visibility — captureResults() resets them.
  const savedFilters = {};
  for (const [c, s] of Object.entries(tableFilters)) if (s?.size) savedFilters[c] = [...s];
  const savedHidden = [...hiddenColumns];

  const must  = buildFilterMustClauses();
  const query = must.length ? { bool: { must } } : { match_all: {} };
  const size  = parseInt(document.getElementById('sampleSizeSelect')?.value || '10');
  const body  = { query };
  if (_indexSortField) body.sort = [{ [_indexSortField]: { order: 'desc' } }];

  const metaEl = document.getElementById(RV.meta);
  if (metaEl && activeViewer === 'index') metaEl.textContent = 'Filtering…';

  const data = await api('/api/query', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index: _currentIndexName, body, size }),
  });
  if (!data || data.error) {
    showToast('Filter query failed: ' + (data?.error || 'unknown'), 'bg-danger');
    return;
  }

  captureResults(data);                     // freezes columns, clears filters/visibility
  tableFilters = {};
  for (const [c, vals] of Object.entries(savedFilters)) tableFilters[c] = new Set(vals);
  const present = new Set(currentColumns());
  hiddenColumns = new Set(savedHidden.filter(c => present.has(c)));
  renderResultViews();
  updateFilterButtonState();
  updateShowingCount();
}

/** Update the "Showing / Total" stat card and meta text based on current filters */
function updateShowingCount() {
  // tableDisplayRows() returns an ARRAY of row objects — use its length, not the array itself.
  const visibleCount = tableDisplayRows().length;
  const totalRows = lastResultHits.length;
  const totalInIndex = lastResultTotal || totalRows;

  // Update the stat card if it exists
  const statEl = document.getElementById('showingTotalStat');
  if (statEl) {
    const hasFilters = Object.keys(tableFilters).some(k => tableFilters[k]?.size);
    if (hasFilters) {
      statEl.innerHTML = `<span class="text-warning">${visibleCount.toLocaleString()}</span> / ${totalInIndex.toLocaleString()}`;
    } else {
      statEl.textContent = `${totalRows.toLocaleString()} / ${totalInIndex.toLocaleString()}`;
    }
  }

  // Update meta text
  const metaEl = document.getElementById(RV.meta);
  if (metaEl && activeViewer === 'index') {
    const hasFilters = Object.keys(tableFilters).some(k => tableFilters[k]?.size);
    const filterNote = hasFilters ? ` · ${visibleCount.toLocaleString()} shown after filtering` : '';
    metaEl.textContent = `${totalRows.toLocaleString()} loaded of ${totalInIndex.toLocaleString()}${filterNote}`;
  }
}

/** Update filter button visual state */
function updateFilterButtonState() {
  const hasFilters = Object.keys(tableFilters).some(k => tableFilters[k]?.size);

  // Update both Index Detail and Query Editor Clear Filters buttons
  ['btnClearFilters', 'btnClearFiltersQuery'].forEach(btnId => {
    const clearBtn = document.getElementById(btnId);
    if (clearBtn) {
      clearBtn.classList.toggle('btn-outline-danger', !hasFilters);
      clearBtn.classList.toggle('btn-danger', hasFilters);
      if (hasFilters) {
        const filterCount = Object.keys(tableFilters).filter(k => tableFilters[k]?.size).length;
        clearBtn.innerHTML = `<i class="bi bi-x-circle me-1"></i>Clear Filters <span class="badge bg-light text-danger ms-1">${filterCount}</span>`;
      } else {
        clearBtn.innerHTML = `<i class="bi bi-x-circle me-1"></i>Clear Filters`;
      }
    }
  });

  ['btnQueryFromFilters', 'btnQueryFromFiltersQuery'].forEach(btnId => {
    const queryBtn = document.getElementById(btnId);
    if (queryBtn) {
      queryBtn.classList.toggle('btn-outline-info', !hasFilters);
      queryBtn.classList.toggle('btn-info', hasFilters);
    }
  });
}

/** Generate ES query from active table filters and populate query editor */
function generateQueryFromFilters() {
  const activeFilters = Object.entries(tableFilters).filter(([_, v]) => v?.size > 0);

  if (!activeFilters.length) {
    showToast('No active filters to convert', 'bg-warning');
    return;
  }

  const mustClauses = [];

  for (const [field, values] of activeFilters) {
    const valArray = [...values];
    if (valArray.length === 1) {
      // Single value: use term query
      mustClauses.push({ term: { [field]: valArray[0] } });
    } else {
      // Multiple values: use terms query
      mustClauses.push({ terms: { [field]: valArray } });
    }
  }

  const query = {
    query: {
      bool: {
        must: mustClauses
      }
    }
  };
  // Sort by the index's known date field (from the sample response) — never a
  // hardcoded name that may not exist. The backend also validates sort fields.
  if (activeViewer === 'index' && _indexSortField) {
    query.sort = [{ [_indexSortField]: { order: 'desc' } }];
  }

  // Populate the query editor
  const queryBodyEl = document.getElementById('queryBody');
  const queryIndexEl = document.getElementById('queryIndex');

  if (queryBodyEl) {
    queryBodyEl.value = JSON.stringify(query, null, 2);
  }

  // Set index pattern from the current context: Index Detail → that index's
  // pattern; Query Editor → the executed query's index pattern(s).
  if (queryIndexEl) {
    if (activeViewer === 'index' && _currentIndexName) {
      // e.g. "adc-network-hourly-ty-...-687" -> "adc-network-hourly-*"
      const baseName = _currentIndexName.split('-ty-')[0];
      queryIndexEl.value = baseName + '-*';
    } else if (activeViewer === 'query' && _queryBaseItems?.length) {
      queryIndexEl.value = [...new Set(_queryBaseItems.map(it => it.index).filter(Boolean))].join(',');
    }
  }

  // Switch to query view
  showView('query');
  showToast(`Query generated with ${activeFilters.length} filter(s). Edit indices and run.`, 'bg-success');
}

/* Close any open filter dropdown when clicking elsewhere (main window). */
document.addEventListener('click', (e) => {
  if (e.target.closest('.rt-filter') || e.target.closest('.rt-funnel')) return;
  document.querySelectorAll('.rt-filter:not(.d-none)').forEach(p => p.classList.add('d-none'));
});

/* ── Cell edit / delete (writes back to Elasticsearch) ─────────────────── */
async function editCell(id, index, field, el) {
  const doc = el ? el.ownerDocument : document;
  const row = lastResultHits.find(r => r._id === id && r._index === index);
  const cur = row ? row[field] : '';
  const shown = cur == null ? '' : (typeof cur === 'object' ? JSON.stringify(cur) : String(cur));
  const input = await uiPrompt(doc, { title: `Set "${field}" for _id ${id}`, value: shown, okText: 'Save' });
  if (input === null) return;                 // cancelled
  await applyDocChange(id, index, field, 'set', input);
}

async function deleteCell(id, index, field, el) {
  const doc = el ? el.ownerDocument : document;
  const ok = await uiConfirm(doc, { title: `Delete field "${field}"?`,
    message: `Remove "${field}" from document _id ${id}. This deletes the field from the ES document.`,
    okText: 'Delete', danger: true });
  if (!ok) return;
  await applyDocChange(id, index, field, 'delete', null);
}

async function applyDocChange(id, index, field, op, value) {
  if (!writeMode) { showToast('Enable Write mode first', 'bg-warning'); return; }
  const res = await api('/api/doc/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, id, field, op, value }),
  });
  if (!res || res.error) { showToast('Update failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }

  const row = lastResultHits.find(r => r._id === id && r._index === index);
  if (row) {
    if (op === 'delete') delete row[field];
    else row[field] = res.value;
  }
  renderResultViews();
  showToast(op === 'delete' ? `Removed "${field}"` : `Updated "${field}"`, 'bg-success');
}

/** Download the current results — CSV for table/csv views, JSON otherwise.
 *  When column filters are active AND not every matching doc is loaded (Size
 *  limit smaller than the total matching in the index), the user is asked
 *  whether to download just the rows shown here or ALL docs matching the same
 *  filter criteria server-side. */
async function downloadResults() {
  // "Download shown docs" → only the rows currently visible after column
  // filtering/sorting (the funnel filters act as the query here). Every field
  // that matches is exported, regardless of column show/hide visibility.
  const rows = tableDisplayRows();
  if (!rows.length) { showToast('No rows to export', 'bg-warning'); return; }

  const hasFilters   = Object.keys(tableFilters).some(k => tableFilters[k]?.size);
  const loaded       = lastResultHits.length;
  const totalInIndex = lastResultTotal || loaded;
  // More docs could match server-side than we loaded → offer the choice.
  const moreOnServer = hasFilters && loaded < totalInIndex && !!_currentIndexName;

  if (moreOnServer) {
    const choice = await uiChoice(document, {
      title: 'Download filtered docs',
      message: `${rows.length.toLocaleString()} doc(s) match your filters among the `
             + `${loaded.toLocaleString()} loaded here, but the index holds `
             + `${totalInIndex.toLocaleString()} docs total. More may match the same `
             + `filters server-side. What do you want to download?`,
      buttons: [
        { value: 'shown', text: `Shown only (${rows.length.toLocaleString()})`, cls: 'btn-primary' },
        { value: 'all',   text: 'All matching (server-side)', cls: 'btn-info' },
        { value: null,    text: 'Cancel', cls: 'btn-outline-secondary' },
      ],
    });
    if (choice == null) return;
    if (choice === 'all') { await exportFilteredMatches(); return; }
    // choice === 'shown' → fall through to local download
  }
  downloadRowsLocally(rows);
}

/** Write the given rows to a file in the browser (CSV for table/csv, JSON otherwise). */
function downloadRowsLocally(rows) {
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  let content, mime, ext;
  if (resultView === 'json') {
    content = JSON.stringify(rows, null, 2);
    mime = 'application/json'; ext = 'json';
  } else {
    content = buildResultsCsv(rows, currentColumns());
    mime = 'text/csv'; ext = 'csv';
  }
  const blob = new Blob([content], { type: mime + ';charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `cc_query_results_${ts}.${ext}`;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

/** Build ES bool.must clauses from the active table filters (same mapping as
 *  "Query from Filters"): single value → term, multiple values → terms.
 *  Pass `exceptCol` to omit one column (used for cascading value lists). */
function buildFilterMustClauses(exceptCol) {
  return Object.entries(tableFilters)
    .filter(([col, v]) => col !== exceptCol && v?.size > 0)
    .map(([field, values]) => {
      const arr = [...values];
      return arr.length === 1 ? { term: { [field]: arr[0] } } : { terms: { [field]: arr } };
    });
}

/** Export EVERY doc in the current index matching the active filter criteria,
 *  scrolling ES server-side (ignores the Size limit). */
async function exportFilteredMatches() {
  if (!_currentIndexName) { showToast('No index in context', 'bg-danger'); return; }
  const must = buildFilterMustClauses();
  const query_body = must.length
    ? { query: { bool: { must } } }
    : { query: { match_all: {} } };
  await runServerExport([{ index: _currentIndexName, query_body }]);
}

/** Export ALL matching docs by scrolling ES server-side (ignores Size limit). */
async function exportAll() {
  const items = activeContextItems();
  if (!items) { showToast('Invalid query JSON — cannot export', 'bg-danger'); return; }
  await runServerExport(items);
}

/** Shared server-side export: POST per-index queries, stream back a file. */
async function runServerExport(items) {
  const format = resultView === 'json' ? 'json' : 'csv';
  const payload = { per_index_queries: items, format };

  const btns = [...document.querySelectorAll('.js-export-all')];
  const orig = btns.map(b => b.innerHTML);
  btns.forEach(b => { b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm" style="width:0.8rem;height:0.8rem;"></span>'; });
  try {
    const res = await fetch(appUrl('/api/query/export'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const t = await res.text();
      showToast('Export failed: ' + t.slice(0, 200), 'bg-danger');
      return;
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="?([^"]+)"?/);
    const name = m ? m[1] : `cc_export.${format}`;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    // Streamed response: any rows sent before a server-side failure are kept in
    // the file. JSON exports carry a trailing "truncated"/"error" field.
    showToast(`Exported ${(blob.size / 1024).toFixed(0)} KB`, 'bg-success');
  } catch (e) {
    showToast('Export error: ' + e.message, 'bg-danger');
  } finally {
    btns.forEach((b, i) => { b.disabled = false; b.innerHTML = orig[i]; });
  }
}

/** Build the pop-out body HTML for the currently active view. */
function popoutBodyHtml() {
  if (resultView === 'table') {
    return buildInteractiveTableHtml();   // same interactive table as the main pane
  }
  if (resultView === 'csv') {
    const csv = lastResultHits.length ? buildResultsCsv(lastResultHits, currentColumns()) : 'No rows to display.';
    return `<pre class="csv">${esc(csv)}</pre>`;
  }
  return `<pre>${esc(lastResultJson || '')}</pre>`;
}

/** Push the current results (in the active view) into the detached window. */
function syncResultsPopout() {
  if (!resultsWindow || resultsWindow.closed) return;
  const doc = resultsWindow.document;
  const out = doc.getElementById('out');
  const metaEl = doc.getElementById('meta');
  if (out)    out.innerHTML = popoutBodyHtml();
  if (metaEl) metaEl.textContent = document.getElementById(RV.meta)?.textContent || '';
  // Reflect the active view + write mode on the pop-out's own toolbar.
  ['json', 'table', 'csv'].forEach(m =>
    doc.getElementById('po-' + m)?.classList.toggle('active', m === resultView));
  const wb = doc.getElementById('po-write');
  if (wb) {
    wb.classList.toggle('btn-warning', writeMode);
    wb.classList.toggle('btn-outline-light', !writeMode);
    wb.innerHTML = writeMode ? '<i class="bi bi-unlock me-1"></i>Write'
                             : '<i class="bi bi-lock me-1"></i>Read-only';
  }
}

/** Open the results in a separate browser window for easier viewing. */
function popOutResults() {
  if (resultsWindow && !resultsWindow.closed) { resultsWindow.focus(); syncResultsPopout(); return; }
  resultsWindow = window.open('', 'cc_es_results', 'width=820,height=800,scrollbars=yes,resizable=yes');
  if (!resultsWindow) { showToast('Pop-up blocked — allow pop-ups for this site', 'bg-danger'); return; }
  resultsWindow.document.write(`<!DOCTYPE html><html lang="en" data-bs-theme="dark"><head><meta charset="utf-8"/>
    <title>CC ES Analyzer — Results</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"/>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet"/>
    <link rel="stylesheet" href="${appUrl('/static/css/style.css')}"/>
    <style>
      body{margin:0;background:#1e2530;color:#c9d1d9;font-family:Consolas,'Courier New',monospace;}
      header{background:#11161d;padding:8px 12px;border-bottom:1px solid #343a40;
             font-size:.8rem;color:#8aa;display:flex;gap:10px;align-items:center;}
      #meta{color:#9ab;font-size:.75rem;margin-right:auto;}
      #out{padding:10px;}
      #out pre{margin:0;font-size:.8rem;white-space:pre-wrap;word-break:break-all;}
      #out pre.csv{white-space:pre;word-break:normal;overflow:auto;}
      #out table{font-size:.74rem;white-space:nowrap;}
    </style></head><body>
    <header>
      <strong style="color:#5cc8ff;">Query Results</strong><span id="meta"></span>
      <div class="btn-group btn-group-sm" role="group">
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-json" onclick="setResultView('json')">JSON</button>
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-table" onclick="setResultView('table')">Table</button>
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-csv" onclick="setResultView('csv')">CSV</button>
      </div>
      <button class="btn btn-sm btn-outline-success py-0 px-2" onclick="downloadResults()" title="Download shown"><i class="bi bi-download"></i></button>
      <button class="btn btn-sm btn-outline-primary py-0 px-2 js-export-all" onclick="exportAll()" title="Export ALL matching"><i class="bi bi-cloud-download me-1"></i>All</button>
      <button class="btn btn-sm btn-outline-light py-0 px-2" id="po-write" onclick="toggleWriteMode()" title="Toggle edit mode"><i class="bi bi-lock me-1"></i>Read-only</button>
      <button class="btn btn-sm btn-outline-light py-0 px-2" onclick="openFieldVisibility(this)" title="Choose which fields (columns) to show"><i class="bi bi-eye me-1"></i>Fields</button>
      <button class="btn btn-sm btn-outline-warning py-0 px-2" onclick="openAggregateDialog(this)" title="Group the matching docs by selected field(s)"><i class="bi bi-bar-chart"></i></button>
      <button class="btn btn-sm btn-outline-danger py-0 px-2" onclick="clearAllFilters()" title="Clear all column filters"><i class="bi bi-x-circle me-1"></i>Clear Filters</button>
      <button class="btn btn-sm btn-outline-info py-0 px-2" onclick="generateQueryFromFilters()" title="Create ES query from active filters (opens in the main window)"><i class="bi bi-funnel"></i></button>
      <button class="btn btn-sm btn-outline-light py-0 px-2" onclick="refreshActiveViewer()" title="Reload the data"><i class="bi bi-arrow-clockwise"></i></button>
    </header>
    <div id="out"><pre>No results yet…</pre></div></body></html>`);
  resultsWindow.document.close();

  // Bind the interactive handlers into the pop-out's global scope so the shared
  // markup's onclick names resolve (they run in the main window's context,
  // updating shared state and re-rendering BOTH windows via refreshTables).
  ['editCell', 'deleteCell', 'toggleRowSel', 'toggleAllRows', 'toggleColSel',
   'toggleTableSort', 'toggleColFilter', 'rtFilterSearch', 'rtFilterToggleAll',
   'applyColFilter', 'clearColFilter', 'clearSelection', 'deleteSelectedRows',
   'columnFieldOp', 'applySuggestionField', 'dismissSuggestion',
   'setResultView', 'downloadResults', 'exportAll', 'toggleWriteMode',
   'showJsonCell', 'showJsonModal', 'clearAllFilters', 'generateQueryFromFilters',
   'openFieldVisibility', 'renderFieldVisibilityBody', 'setColumnHidden',
   'setUnmappedHidden', 'setAllMappedHidden', 'showAllColumns', 'refreshActiveViewer',
   'openAggregateDialog']
    .forEach(fn => { try { resultsWindow[fn] = window[fn]; } catch (_) {} });

  // Close filter dropdowns on outside click inside the pop-out.
  resultsWindow.document.addEventListener('click', (e) => {
    if (e.target.closest('.rt-filter') || e.target.closest('.rt-funnel')) return;
    resultsWindow.document.querySelectorAll('.rt-filter:not(.d-none)').forEach(p => p.classList.add('d-none'));
  });

  syncResultsPopout();
}

/* ══════════════════════════════════════════════════════════════════════════
   CONNECTION — FORM HELPERS
   ══════════════════════════════════════════════════════════════════════════ */

/** Read current scheme radio value */
function getScheme() {
  return document.querySelector('input[name="connScheme"]:checked')?.value || 'http';
}

/** Fill the form fields from a settings object */
function fillForm(s) {
  document.getElementById('connLabel').value = s.label || '';
  document.getElementById('connHost').value  = s.host  || '';
  document.getElementById('connPort').value  = s.port  || 9200;
  document.getElementById('connUser').value  = s.user  || '';
  document.getElementById('connPass').value  = s.password || '';
  document.getElementById('connVerify').checked = !!s.verify_certs;
  const schemeRadio = document.querySelector(`input[name="connScheme"][value="${s.scheme || 'http'}"]`);
  if (schemeRadio) schemeRadio.checked = true;
  // SSH fallback — default to enabled
  document.getElementById('connSshEnabled').checked = s.ssh_enabled !== false;  // default true
  document.getElementById('connSshUser').value = s.ssh_user || '';
  document.getElementById('connSshPass').value = s.ssh_password || '';
  document.getElementById('connSshPort').value = s.ssh_port || 22;
  toggleSshFields();
}

/** Read form into a settings object */
function readForm() {
  return {
    label:        document.getElementById('connLabel').value.trim(),
    host:         document.getElementById('connHost').value.trim(),
    port:         parseInt(document.getElementById('connPort').value) || 9200,
    scheme:       getScheme(),
    user:         document.getElementById('connUser').value.trim(),
    password:     document.getElementById('connPass').value,
    verify_certs: document.getElementById('connVerify').checked,
    ssh_enabled:  document.getElementById('connSshEnabled').checked,
    ssh_user:     document.getElementById('connSshUser').value.trim(),
    ssh_password: document.getElementById('connSshPass').value,
    ssh_port:     parseInt(document.getElementById('connSshPort').value) || 22,
  };
}

function clearForm() {
  fillForm({ port: 9200, scheme: 'http', ssh_port: 22, ssh_enabled: true });
  document.getElementById('formTitle').textContent = 'New Connection';
  hideFeedback();
}

/** Show/hide the SSH credential fields based on the enable checkbox */
function toggleSshFields() {
  const on = document.getElementById('connSshEnabled').checked;
  document.getElementById('sshFields').classList.toggle('d-none', !on);
}

function toggleSshPassVis() {
  const inp  = document.getElementById('connSshPass');
  const icon = document.getElementById('sshPassEyeIcon');
  if (inp.type === 'password') {
    inp.type = 'text';
    icon.className = 'bi bi-eye-slash';
  } else {
    inp.type = 'password';
    icon.className = 'bi bi-eye';
  }
}

function togglePassVis() {
  const inp  = document.getElementById('connPass');
  const icon = document.getElementById('passEyeIcon');
  if (inp.type === 'password') {
    inp.type = 'text';
    icon.className = 'bi bi-eye-slash';
  } else {
    inp.type = 'password';
    icon.className = 'bi bi-eye';
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   CONNECTION — CONNECT / DISCONNECT
   ══════════════════════════════════════════════════════════════════════════ */

async function doConnect() {
  const settings = readForm();

  if (!settings.host) {
    showFeedback('danger', '<i class="bi bi-exclamation-triangle me-2"></i>Please enter the CC machine IP / hostname.');
    return;
  }

  // Update button state to "connecting"
  const btn = document.getElementById('btnConnect');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Connecting…';
  setNavbarConnecting();
  const sshNote = settings.ssh_enabled
    ? ' <span class="text-secondary">(SSH port-open enabled as fallback)</span>' : '';
  showFeedback('info', '<i class="bi bi-hourglass-split me-2"></i>Connecting to Elasticsearch…' + sshNote);

  try {
    const res  = await fetch(appUrl('/api/connect'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    });
    const data = await res.json();

    if (data.connected) {
      // Persist as last-used
      localStorage.setItem(LS_ACTIVE, JSON.stringify(settings));
      isConnected = true;
      onConnected(settings, data);
      const sshMsg = data.ssh_tunnel
        ? `<br><small class="text-warning"><i class="bi bi-hdd-network me-1"></i>Connected via SSH tunnel: <code>${(data.tunnel||'').replace(/</g,'&lt;')}</code></small>`
        : data.ssh_opened_port
        ? `<br><small class="text-warning"><i class="bi bi-unlock me-1"></i>ES port opened via SSH: <code>${(data.ssh_command||'').replace(/</g,'&lt;')}</code></small>`
        : '';
      showFeedback('success',
        `<i class="bi bi-check-circle-fill me-2"></i>Connected! ` +
        `<strong>${data.cluster_name}</strong> — ES ${data.es_version}` + sshMsg
      );
      // Auto-navigate to dashboard after short delay
      setTimeout(() => showView('dashboard'), 900);
      refreshAll();
    } else {
      isConnected = false;
      onDisconnected();

      // Provide more specific error messages
      let errorMsg = data.error || 'Connection failed';
      let errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>${errorMsg}`;

      // Check for SSH-specific errors
      if (errorMsg.includes('SSH') || errorMsg.includes('ssh')) {
        if (errorMsg.includes('authentication')) {
          errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>SSH authentication failed. Check the SSH username and password.`;
        } else if (!settings.ssh_user || !settings.ssh_password) {
          errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>ES port isn't reachable directly. SSH credentials are required to tunnel to it.
            <br><small class="text-secondary">Please provide the SSH username and password.</small>`;
        }
      } else if (errorMsg.includes('Connection refused') || errorMsg.includes('timed out') || errorMsg.includes('port')) {
        if (settings.ssh_enabled && (!settings.ssh_user || !settings.ssh_password)) {
          errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>ES port appears blocked. SSH credentials are needed to tunnel to it.
            <br><small class="text-secondary">SSH is enabled but username/password are missing.</small>`;
        }
      }

      showFeedback('danger', errorHtml);
    }
  } catch (e) {
    isConnected = false;
    onDisconnected();
    showFeedback('danger', `<i class="bi bi-x-circle-fill me-2"></i>Network error: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-plug-fill me-1"></i>Connect';
  }
}

function disconnect() {
  isConnected = false;
  localStorage.removeItem(LS_ACTIVE);
  onDisconnected();
  showToast('Disconnected from Elasticsearch', 'bg-secondary');
  showView('connection');
}

/* ══════════════════════════════════════════════════════════════════════════
   CONNECTION — UI STATE
   ══════════════════════════════════════════════════════════════════════════ */

function onConnected(settings, info) {
  const displayHost = settings.label || settings.host;
  const cluster     = info.cluster_name || '';
  const version     = info.es_version   || '';
  const machineStr  = `${settings.host}:${settings.port}`;

  // Navbar pills
  document.getElementById('connectedPill').classList.remove('d-none');
  document.getElementById('disconnectedPill').classList.add('d-none');
  document.getElementById('pillMachine').textContent = displayHost;
  document.getElementById('pillCluster').textContent = cluster ? `(${cluster})` : '';
  document.getElementById('pillVersion').textContent = version ? `ES ${version}` : '';

  // Sidebar box
  const box = document.getElementById('sidebarConnBox');
  box.classList.remove('d-none');
  document.getElementById('sidebarMachine').textContent = displayHost;
  document.getElementById('sidebarCluster').textContent = `${machineStr} · ${cluster}`;
}

function onDisconnected() {
  document.getElementById('connectedPill').classList.add('d-none');
  document.getElementById('disconnectedPill').classList.remove('d-none');
  document.getElementById('sidebarConnBox').classList.add('d-none');
  document.getElementById('sidebarIndices').innerHTML =
    '<div class="text-secondary small px-2 py-2">Connect to see indices</div>';
  allIndices = [];
}

function setNavbarConnecting() {
  // Flash the disconnected pill to a "connecting" state
  const pill = document.getElementById('disconnectedPill');
  const dot  = pill.querySelector('.conn-dot');
  if (dot) { dot.classList.remove('disconnected'); dot.classList.add('connecting'); }
}

/* ══════════════════════════════════════════════════════════════════════════
   SAVED PROFILES
   ══════════════════════════════════════════════════════════════════════════ */

function loadProfiles() {
  try { return JSON.parse(localStorage.getItem(LS_PROFILES) || '[]'); }
  catch { return []; }
}

function saveProfiles(profiles) {
  localStorage.setItem(LS_PROFILES, JSON.stringify(profiles));
}

function saveProfile() {
  const s = readForm();
  if (!s.host) {
    showFeedback('warning', '<i class="bi bi-exclamation-triangle me-2"></i>Enter a host before saving.');
    return;
  }
  const profiles = loadProfiles();
  // Avoid duplicate hosts — update if same host:port exists
  const existing = profiles.findIndex(p => p.host === s.host && p.port === s.port);
  if (existing >= 0) {
    profiles[existing] = s;
  } else {
    profiles.push(s);
  }
  saveProfiles(profiles);
  renderProfiles();
  showFeedback('success', '<i class="bi bi-bookmark-check me-2"></i>Profile saved!');
}

function deleteProfile(index) {
  const profiles = loadProfiles();
  profiles.splice(index, 1);
  saveProfiles(profiles);
  renderProfiles();
}

function loadProfileIntoForm(index) {
  const profiles = loadProfiles();
  const p = profiles[index];
  if (p) {
    fillForm(p);
    document.getElementById('formTitle').textContent = `Edit — ${p.label || p.host}`;
    hideFeedback();
  }
}

function renderProfiles() {
  const profiles = loadProfiles();
  const container = document.getElementById('savedProfilesList');
  const badge = document.getElementById('profileCount');
  badge.textContent = profiles.length;

  if (!profiles.length) {
    container.innerHTML = '<p class="text-secondary small text-center py-2 mb-0">No saved profiles yet</p>';
    return;
  }

  container.innerHTML = profiles.map((p, i) => `
    <div class="profile-item border-bottom" onclick="loadProfileIntoForm(${i})">
      <i class="bi bi-hdd-network text-info"></i>
      <div>
        <div class="profile-name">${esc(p.label || p.host)}</div>
        <div class="profile-host">${esc(p.scheme)}://${esc(p.host)}:${p.port}${p.user ? ' · ' + esc(p.user) : ''}</div>
      </div>
      <div class="d-flex gap-1 ms-auto">
        <button class="btn btn-sm btn-link text-primary p-0 btn-del"
                onclick="event.stopPropagation(); connectFromProfile(${i})" title="Connect">
          <i class="bi bi-plug-fill"></i>
        </button>
        <button class="btn btn-sm btn-link text-danger p-0 btn-del"
                onclick="event.stopPropagation(); deleteProfile(${i})" title="Delete">
          <i class="bi bi-trash3"></i>
        </button>
      </div>
    </div>`).join('');
}

async function connectFromProfile(index) {
  loadProfileIntoForm(index);
  await doConnect();
}

/* ═════════════════════════════════════��════════════════════════════════════
   DATA LOADING
   ══════════════════════════════════════════════════════════════════════════ */

async function refreshAll() {
  await Promise.all([loadClusterHealth(), loadIndices(), loadAttackSummary()]);
}

async function loadClusterHealth() {
  try {
    const data = await api('/api/health');
    if (!data.connected) return;
    setText('c-status', data.status?.toUpperCase() || '—');
    document.getElementById('c-status').className = `stat-value health-${data.status}`;
    setText('c-nodes',      data.number_of_nodes   ?? '—');
    setText('c-shards',     data.active_shards      ?? '—');
    setText('c-unassigned', data.unassigned_shards  ?? '—');
    setText('c-version',    data.es_version         ?? '—');
  } catch (_) {}
}

async function loadIndices() {
  try {
    const data = await api('/api/indices?cc_only=false');
    if (data.error) return;
    allIndices = data.indices || [];
    renderSidebarIndices(allIndices);
    renderIndicesTable(allIndices);
  } catch (_) {}
}

/** Categories the user has collapsed in the sidebar (persisted). */
const LS_CAT_COLLAPSED = 'cc_es_cat_collapsed';
let collapsedCats = new Set(JSON.parse(localStorage.getItem(LS_CAT_COLLAPSED) || '[]'));

const _catSlug = (cat) => 'cat_' + cat.replace(/[^a-z0-9]+/gi, '_');

function toggleCategory(cat) {
  if (collapsedCats.has(cat)) collapsedCats.delete(cat);
  else collapsedCats.add(cat);
  localStorage.setItem(LS_CAT_COLLAPSED, JSON.stringify([...collapsedCats]));

  const slug = _catSlug(cat);
  const body = document.getElementById('cat-body-' + slug);
  const chev = document.getElementById('cat-chev-' + slug);
  const collapsed = collapsedCats.has(cat);
  if (body) body.classList.toggle('d-none', collapsed);
  if (chev) chev.className = 'bi ms-auto ' + (collapsed ? 'bi-chevron-right' : 'bi-chevron-down');
}

function renderSidebarIndices(indices) {
  const container = document.getElementById('sidebarIndices');
  if (!indices.length) {
    container.innerHTML = '<div class="text-secondary small px-2 py-2">No indices found</div>';
    return;
  }
  const groups = {};
  for (const idx of indices) {
    const cat = idx.cc_meta?.category || 'Other';
    (groups[cat] = groups[cat] || []).push(idx);
  }
  let html = '';
  for (const [cat, list] of Object.entries(groups)) {
    const collapsed = collapsedCats.has(cat);
    const slug      = _catSlug(cat);
    const catArg    = cat.replace(/'/g, "\\'");
    html += `<div class="category-header cat-toggle d-flex align-items-center" onclick="toggleCategory('${catArg}')">
      <span>${esc(cat)}</span>
      <span class="cat-count ms-2">${list.length}</span>
      <i id="cat-chev-${slug}" class="bi ms-auto ${collapsed ? 'bi-chevron-right' : 'bi-chevron-down'}"></i>
    </div>`;
    html += `<div id="cat-body-${slug}" class="${collapsed ? 'd-none' : ''}">`;
    for (const idx of list) {
      const hColor = idx.health === 'green' ? '#198754' : idx.health === 'yellow' ? '#ffc107' : '#dc3545';
      html += `<button class="index-btn" onclick="showIndexDetail('${esc(idx.name)}')" title="${esc(idx.name)}">
        <span class="dot" style="background:${hColor};"></span>${esc(idx.name)}</button>`;
    }
    html += `</div>`;
  }
  container.innerHTML = html;
}

/* ── Dashboard indices: search filter + multi-select deletion ─────────────── */
let _dashboardIndexFilter = '';
let selectedIndices = new Set();      // index names checked for bulk deletion

function onDashboardIndexSearch(inp) {
  _dashboardIndexFilter = (inp.value || '').toLowerCase();
  renderIndicesTable(allIndices);
}

function toggleIndexSel(name, cb) {
  if (cb.checked) selectedIndices.add(name); else selectedIndices.delete(name);
  updateDeleteSelectedBtn();
}

function toggleAllIndexSel(cb) {
  for (const idx of _dashboardVisibleIndices(allIndices)) {
    if (cb.checked) selectedIndices.add(idx.name); else selectedIndices.delete(idx.name);
  }
  renderIndicesTable(allIndices);
}

function updateDeleteSelectedBtn() {
  const n = selectedIndices.size;
  for (const [btnId, cntId] of [['btnDeleteSelected', 'delSelCount'],
                                ['btnExportSelected', 'expSelCount']]) {
    const cnt = document.getElementById(cntId);
    if (cnt) cnt.textContent = n;
    document.getElementById(btnId)?.classList.toggle('d-none', n === 0);
  }
}

/** Export the checked indices. Server archive is the default (works for ANY
 *  size — the backend scrolls ES and gzips the CSV; the browser only downloads
 *  the finished file). Direct browser download remains for small indices. */
async function exportSelectedIndices() {
  const names = [...selectedIndices];
  if (!names.length) return;
  const mode = await uiChoice(document, {
    title: `Export ${names.length} ${names.length > 1 ? 'indices' : 'index'} — all documents`,
    message: 'Archive on server: the backend writes one compressed <index>.csv.gz per index '
           + '(recommended — any size, survives browser closes), then you download the finished '
           + 'files from the Archives panel. Direct download streams through this browser tab — '
           + 'only for small indices.',
    buttons: [
      { value: 'server',  text: 'Archive on server (recommended)', cls: 'btn-info' },
      { value: 'browser', text: 'Direct browser download', cls: 'btn-outline-primary' },
      { value: null,      text: 'Cancel', cls: 'btn-outline-secondary' },
    ],
  });
  if (!mode) return;
  if (mode === 'server') {
    const res = await api('/api/exports', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ indices: names }),
    });
    if (!res || res.error) { showToast('Export failed to start: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
    showToast(`Archiving ${names.length} ${names.length > 1 ? 'indices' : 'index'} on the server…`, 'bg-info');
    openArchivesPanel();
    return;
  }
  await exportSelectedIndicesBrowser(names);
}

/** Direct client-side export (small indices): folder picker where supported,
 *  otherwise regular downloads named "<index>.csv". */
async function exportSelectedIndicesBrowser(names) {

  // Ask for the destination folder where supported.
  let dirHandle = null;
  if (window.showDirectoryPicker) {
    try {
      dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
    } catch (e) {
      if (e && e.name === 'AbortError') return;   // user cancelled the picker
      dirHandle = null;                           // not permitted → fallback
    }
  }
  if (!dirHandle) {
    showToast('Folder picker unavailable — files will go to the browser\'s Downloads folder', 'bg-info');
  }

  let done = 0; const failures = [];
  for (const name of names) {
    showToast(`Exporting ${done + failures.length + 1}/${names.length}: ${name}…`, 'bg-secondary');
    try {
      const res = await fetch(appUrl('/api/query/export'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          per_index_queries: [{ index: name, query_body: { query: { match_all: {} } } }],
          format: 'csv',
          max_rows: 10_000_000,      // "all documents" — effectively uncapped
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 150)}`);

      if (dirHandle) {
        // Stream the response straight into "<index>.csv" in the chosen folder.
        const fileHandle = await dirHandle.getFileHandle(`${name}.csv`, { create: true });
        await res.body.pipeTo(await fileHandle.createWritable());
      } else {
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${name}.csv`;
        document.body.appendChild(a); a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
      }
      done++;
    } catch (e) {
      failures.push(`${name}: ${e.message || e}`);
    }
  }

  showToast(`Exported ${done}/${names.length} ${names.length === 1 ? 'index' : 'indices'}`
    + (dirHandle ? ` to "${dirHandle.name}"` : '')
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-success');
  if (failures.length) console.warn('Index export failures:', failures);
}

async function deleteSelectedIndices() {
  const names = [...selectedIndices];
  if (!names.length) return;
  const ok = await uiConfirm(document, {
    title: `Delete ${names.length} ${names.length > 1 ? 'indices' : 'index'}?`,
    message: 'This permanently deletes: ' + names.join(', '),
    okText: 'Delete', danger: true,
  });
  if (!ok) return;
  let deleted = 0; const failures = [];
  for (const name of names) {
    const res = await api(`/api/indices/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res && !res.error) { deleted++; selectedIndices.delete(name); }
    else failures.push(`${name}: ${res?.error || 'unknown'}`);
  }
  showToast(`Deleted ${deleted} ${deleted === 1 ? 'index' : 'indices'}`
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-success');
  if (failures.length) console.warn('Bulk index delete failures:', failures);
  if (names.includes(_currentIndexName)) _currentIndexName = null;
  await loadIndices();
}

/* ── Archives panel — server-side exports, downloads, restore/upload ──────── */
let _archivesTimer = null;
const _archSelected = new Set();   // archive names checked in the panel
let _archFilter = '';              // name filter typed in the panel
let _archLastFiles = [];           // last file list fetched (re-render on filter)

function closeArchivesPanel() {
  if (_archivesTimer) { clearInterval(_archivesTimer); _archivesTimer = null; }
  document.querySelector('.rt-modal-overlay.rt-archives')?.remove();
}

function openArchivesPanel() {
  closeArchivesPanel();
  _archSelected.clear();
  _archFilter = '';
  _archLastFiles = [];
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-archives';
  // Resizable (drag the bottom-right corner) and never taller than the
  // viewport: the body scrolls, the title and footer buttons stay reachable.
  wrap.innerHTML = `<div class="rt-modal" style="min-width:620px;width:780px;max-width:95vw;
        max-height:90vh;min-height:260px;display:flex;flex-direction:column;
        resize:both;overflow:hidden;">
      <div class="rt-modal-title" style="flex:0 0 auto;"><i class="bi bi-archive me-1"></i>Index Archives (on server)</div>
      <div class="rt-modal-body" style="flex:1 1 auto;min-height:0;overflow:auto;">
        <div class="arch-jobs mb-2" style="max-height:45vh;overflow:auto;"></div>
        <div class="arch-bulk d-flex gap-1 mb-1 align-items-center">
          <input type="text" class="form-control form-control-sm arch-filter"
                 placeholder="Filter by name…" style="max-width:200px;">
          <span class="arch-filtercount small text-secondary me-1"></span>
          <button class="btn btn-sm btn-outline-success" data-act="dl-sel" disabled>
            <i class="bi bi-download me-1"></i>Download selected (<span class="arch-selcount">0</span>)</button>
          <button class="btn btn-sm btn-outline-info" data-act="restore-sel" disabled>
            <i class="bi bi-box-arrow-in-up me-1"></i>Restore selected</button>
          <button class="btn btn-sm btn-outline-danger" data-act="del-sel" disabled>
            <i class="bi bi-trash me-1"></i>Delete selected</button>
        </div>
        <div class="arch-files" style="overflow:auto;"></div>
      </div>
      <div class="rt-modal-actions" style="flex:0 0 auto;">
        <button class="btn btn-sm btn-outline-primary" data-act="upload">
          <i class="bi bi-upload me-1"></i>Upload archive(s)…</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="refresh">
          <i class="bi bi-arrow-clockwise me-1"></i>Refresh</button>
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) { if (e.target === wrap) closeArchivesPanel(); return; }
    const act = b.getAttribute('data-act');
    if (act === 'close') closeArchivesPanel();
    else if (act === 'refresh') refreshArchivesPanel();
    else if (act === 'upload') uploadArchive();
    else if (act === 'dl-sel') downloadArchives([..._archSelected]);
    else if (act === 'restore-sel') restoreArchives([..._archSelected]);
    else if (act === 'del-sel') deleteArchives([..._archSelected]);
  });
  // Checkbox selection (delegated so it survives the 2 s table re-render).
  wrap.addEventListener('change', (e) => {
    const cb = e.target;
    if (cb.classList?.contains('arch-sel')) {
      cb.checked ? _archSelected.add(cb.dataset.name) : _archSelected.delete(cb.dataset.name);
      _updateArchBulkButtons(wrap);
    } else if (cb.classList?.contains('arch-sel-all')) {
      wrap.querySelectorAll('.arch-sel').forEach(x => {
        x.checked = cb.checked;
        cb.checked ? _archSelected.add(x.dataset.name) : _archSelected.delete(x.dataset.name);
      });
      _updateArchBulkButtons(wrap);
    }
  });
  // Filter box lives in the once-rendered toolbar so typing keeps focus while
  // the table re-renders (both on input and on the 2 s poll).
  wrap.querySelector('.arch-filter').addEventListener('input', (e) => {
    _archFilter = e.target.value.trim().toLowerCase();
    _renderArchFiles(wrap);
  });
  refreshArchivesPanel();
  _archivesTimer = setInterval(refreshArchivesPanel, 2000);
}

function _updateArchBulkButtons(wrap) {
  wrap = wrap || document.querySelector('.rt-modal-overlay.rt-archives');
  if (!wrap) return;
  const n = _archSelected.size;
  wrap.querySelector('.arch-selcount').textContent = n;
  wrap.querySelectorAll('.arch-bulk button').forEach(b => { b.disabled = !n; });
  const all = wrap.querySelector('.arch-sel-all');
  const boxes = [...wrap.querySelectorAll('.arch-sel')];
  if (all) all.checked = boxes.length > 0 && boxes.every(b => b.checked);
}

const _fmtBytes = (n) => n >= 1 << 30 ? (n / (1 << 30)).toFixed(2) + ' GB'
                       : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + ' MB'
                       : (n / 1024).toFixed(1) + ' KB';

async function refreshArchivesPanel() {
  const wrap = document.querySelector('.rt-modal-overlay.rt-archives');
  if (!wrap) { closeArchivesPanel(); return; }
  let data;
  try { data = await api('/api/exports'); } catch (e) { return; }
  if (!data || data.error) return;

  // ── Jobs (running first) ──────────────────────────────────────────────────
  const jobsEl = wrap.querySelector('.arch-jobs');
  const jobs = (data.jobs || []).filter(j =>
    j.status === 'running' ||
    (j.finished_at && (Date.now() - Date.parse(j.finished_at)) < 5 * 60_000));
  jobsEl.innerHTML = jobs.length ? jobs.map(j => {
    const badge = j.status === 'running'
      ? '<span class="badge bg-info text-dark">running</span>'
      : j.status === 'done'
        ? '<span class="badge bg-success">done</span>'
        : j.status === 'cancelled'
          ? '<span class="badge bg-secondary">cancelled</span>'
          : `<span class="badge bg-danger" title="${esc(j.error || '')}">error</span>`;
    const cancelBtn = j.status === 'running'
      ? `<button class="btn btn-sm btn-outline-warning py-0 px-1 ms-auto"
                 onclick="cancelArchiveJob('${jsq(j.id)}')"
                 title="Stop this job">Cancel</button>` : '';
    const items = j.items.map(it => {
      const pct = it.total ? Math.min(100, Math.round(it.done / it.total * 100)) : null;
      const label = it.total != null
        ? `${it.done.toLocaleString()} / ${(it.total ?? 0).toLocaleString()} docs`
        : `${it.done.toLocaleString()} docs`;
      return `<div class="small">${esc(it.index)} — ${label}
          ${pct != null ? `<div class="progress" style="height:5px;">
            <div class="progress-bar ${j.status === 'error' ? 'bg-danger' : 'bg-info'}" style="width:${pct}%"></div>
          </div>` : ''}</div>`;
    }).join('');
    const err = j.status === 'error' && j.error
      ? `<div class="small text-danger">${esc(j.error)}</div>` : '';
    return `<div class="border border-secondary rounded p-2 mb-1">
        <div class="d-flex align-items-center gap-2">
          <i class="bi ${j.kind === 'export' ? 'bi-box-arrow-down' : 'bi-box-arrow-in-up'}"></i>
          <span class="small fw-semibold">${j.kind}</span>${badge}${cancelBtn}
        </div>${items}${err}</div>`;
  }).join('') : '';

  // ── Archive files ─────────────────────────────────────────────────────────
  _archLastFiles = data.files || [];
  // Drop selections for files that no longer exist on the server.
  const names = new Set(_archLastFiles.map(f => f.name));
  for (const n of [..._archSelected]) if (!names.has(n)) _archSelected.delete(n);
  _renderArchFiles(wrap);
}

/** Render the archive-files table, applying the name filter. The select-all
 *  checkbox acts on the VISIBLE (filtered) rows — filter then one click. */
function _renderArchFiles(wrap) {
  const filesEl = wrap.querySelector('.arch-files');
  const files = _archFilter
    ? _archLastFiles.filter(f => f.name.toLowerCase().includes(_archFilter))
    : _archLastFiles;
  wrap.querySelector('.arch-filtercount').textContent =
    _archFilter ? `${files.length}/${_archLastFiles.length}` : '';
  filesEl.innerHTML = files.length
    ? `<table class="table table-sm table-hover mb-0" style="font-size:0.78rem;">
        <thead class="table-dark"><tr>
          <th style="width:1.6rem;"><input type="checkbox" class="form-check-input arch-sel-all"
              title="Select all${_archFilter ? ' filtered results' : ''}"></th>
          <th>Archive</th><th>Source</th><th class="text-end">Size</th><th>Created (UTC)</th><th class="text-end">Actions</th></tr></thead>
        <tbody>${files.map(f => `<tr>
          <td><input type="checkbox" class="form-check-input arch-sel" data-name="${esc(f.name)}"
                     ${_archSelected.has(f.name) ? 'checked' : ''}></td>
          <td class="font-monospace">${esc(f.name)}</td>
          <td title="Machine the data was exported from">${esc(f.source || '—')}</td>
          <td class="text-end">${_fmtBytes(f.size)}</td>
          <td>${esc((f.mtime || '').replace('T', ' ').slice(0, 19))}</td>
          <td class="text-end text-nowrap">
            <a class="btn btn-sm btn-outline-success py-0 px-1 me-1"
               href="${appUrl('/api/exports/download/' + encodeURIComponent(f.name))}" download
               title="Download this archive"><i class="bi bi-download"></i></a>
            <button class="btn btn-sm btn-outline-info py-0 px-1 me-1"
                    onclick="restoreArchives(['${jsq(f.name)}'])"
                    title="Restore into an index on the connected ES"><i class="bi bi-box-arrow-in-up"></i></button>
            <button class="btn btn-sm btn-outline-danger py-0 px-1"
                    onclick="deleteArchives(['${jsq(f.name)}'])"
                    title="Delete this archive from the server"><i class="bi bi-trash"></i></button>
          </td></tr>`).join('')}</tbody>
      </table>`
    : _archLastFiles.length
      ? `<div class="text-secondary small p-2">No archives match "${esc(_archFilter)}".</div>`
      : '<div class="text-secondary small p-2">No archives yet — check indices on the dashboard and use "Export selected".</div>';
  _updateArchBulkButtons(wrap);
}

/** Ask the backend to stop a running export/restore job. */
async function cancelArchiveJob(jobId) {
  const res = await api(`/api/exports/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
  if (!res || res.error) { showToast('Cancel failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(res.note || 'Job cancelling — completed archives are kept', 'bg-info');
  refreshArchivesPanel();
}

/** Download several archives — one browser download per file (the browser may
 *  ask to allow multiple downloads from this site; that's expected). */
async function downloadArchives(names) {
  for (const name of names) {
    const a = document.createElement('a');
    a.href = appUrl('/api/exports/download/' + encodeURIComponent(name));
    a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => a.remove(), 0);
    await new Promise(r => setTimeout(r, 400));   // let each download register
  }
  showToast(`Started ${names.length} download${names.length > 1 ? 's' : ''}`, 'bg-success');
}

/** Ask for a target index name per archive/file. The default is always the
 *  file-name stem (archive X.csv.gz → index X). One entry → a plain prompt;
 *  several → per-file choice with "Keep default for ALL" to skip the rest of
 *  the prompts. Returns [{i, target}] (skipped entries omitted) or null when
 *  the whole operation was cancelled. `labels` = [{name, detail?}]. */
async function _chooseRestoreTargets(labels, okText) {
  const out = [];
  let keepAll = false;
  for (let i = 0; i < labels.length; i++) {
    const { name, detail } = labels[i];
    const stem = name.replace(/\.csv(\.gz)?$/, '');
    let target = stem;
    if (labels.length === 1) {
      const t = await uiPrompt(document, {
        title: `Restore "${name}"${detail || ''} — target index name`,
        value: stem, okText: okText || 'Restore' });
      if (t == null || !t.trim()) return null;
      target = t.trim();
    } else if (!keepAll) {
      const choice = await uiChoice(document, {
        title: `${i + 1}/${labels.length}: ${name}${detail || ''}`,
        message: `Target index name (from the file name): "${stem}"`,
        buttons: [
          { value: 'keep',     text: `Keep "${stem}"`,        cls: 'btn-info' },
          { value: 'keep-all', text: 'Keep defaults for ALL', cls: 'btn-outline-info' },
          { value: 'modify',   text: 'Modify…',               cls: 'btn-outline-primary' },
          { value: 'skip',     text: 'Skip this one',         cls: 'btn-outline-secondary' },
        ] });
      if (!choice) return null;                       // Escape/backdrop → abort all
      if (choice === 'skip') continue;
      if (choice === 'keep-all') keepAll = true;
      if (choice === 'modify') {
        const t = await uiPrompt(document, {
          title: `Target index for "${name}"`, value: stem, okText: 'OK' });
        if (t == null || !t.trim()) continue;         // no name → skip this one
        target = t.trim();
      }
    }
    out.push({ i, target });
  }
  return out.length ? out : null;
}

/** One combined warning for restore targets that already exist in ES. */
async function _confirmExistingTargets(targets) {
  const existing = [...new Set(targets.filter(t => allIndices.some(ix => ix.name === t)))];
  if (!existing.length) return true;
  return uiConfirm(document, {
    title: existing.length > 1
      ? `${existing.length} target indices already exist`
      : `Index "${existing[0]}" already exists`,
    message: 'Restored documents will be ADDED to: ' + existing.join(', ')
           + '. Docs with the same _id are overwritten.',
    okText: 'Restore anyway' });
}

/** Restore one or more server-side archives into the connected ES. */
async function restoreArchives(names) {
  if (!names.length) return;
  const chosen = await _chooseRestoreTargets(names.map(n => ({ name: n })));
  if (!chosen) return;
  if (!await _confirmExistingTargets(chosen.map(c => c.target))) return;
  let started = 0; const failures = [];
  for (const c of chosen) {
    const fd = new FormData();
    fd.append('filename', names[c.i]);
    fd.append('target', c.target);
    const res = await api('/api/exports/restore', { method: 'POST', body: fd });
    if (res && !res.error) started++;
    else failures.push(`${names[c.i]}: ${res?.error || 'unknown'}`);
  }
  showToast(`Started ${started} restore${started === 1 ? '' : 's'}`
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-info');
  if (failures.length) console.warn('Restore failures:', failures);
  refreshArchivesPanel();
}

/** Upload one or more .csv/.csv.gz archives (e.g. exported on another machine)
 *  and restore each into the ES this analyzer is connected to. Default index
 *  name = the file name stem. */
function uploadArchive() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.accept = '.gz,.csv,application/gzip,text/csv';
  inp.onchange = async () => {
    const files = [...(inp.files || [])];
    if (!files.length) return;
    const chosen = await _chooseRestoreTargets(
      files.map(f => ({ name: f.name, detail: ` (${_fmtBytes(f.size)})` })),
      'Upload & Restore');
    if (!chosen) return;
    if (!await _confirmExistingTargets(chosen.map(c => c.target))) return;
    let started = 0; const failures = [];
    for (const c of chosen) {
      const f = files[c.i];
      showToast(`Uploading ${f.name}…`, 'bg-secondary');
      const fd = new FormData();
      fd.append('file', f, f.name);
      fd.append('target', c.target);
      const res = await api('/api/exports/restore', { method: 'POST', body: fd });
      if (res && !res.error) started++;
      else failures.push(`${f.name}: ${res?.error || 'unknown'}`);
    }
    showToast(`Started ${started} restore${started === 1 ? '' : 's'}`
      + (failures.length ? ` — ${failures.length} failed` : ''),
      failures.length ? 'bg-warning' : 'bg-info');
    if (failures.length) console.warn('Upload failures:', failures);
    refreshArchivesPanel();
  };
  inp.click();
}

/** Delete one or more archives from the server (indices are not touched). */
async function deleteArchives(names) {
  if (!names.length) return;
  const ok = await uiConfirm(document, {
    title: names.length > 1 ? `Delete ${names.length} archives?` : `Delete archive "${names[0]}"?`,
    message: 'Removes from the server: ' + names.join(', ')
           + '. Indices in Elasticsearch are not touched.',
    okText: 'Delete', danger: true });
  if (!ok) return;
  let deleted = 0; const failures = [];
  for (const name of names) {
    const res = await api(`/api/exports/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res && !res.error) { deleted++; _archSelected.delete(name); }
    else failures.push(`${name}: ${res?.error || 'unknown'}`);
  }
  showToast(`Deleted ${deleted} archive${deleted === 1 ? '' : 's'}`
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-success');
  if (failures.length) console.warn('Archive delete failures:', failures);
  refreshArchivesPanel();
}

/** The rows the dashboard table currently shows (search filter applied). */
function _dashboardVisibleIndices(indices) {
  if (!_dashboardIndexFilter) return indices;
  return indices.filter(i =>
    i.name.toLowerCase().includes(_dashboardIndexFilter) ||
    (i.cc_meta?.category || '').toLowerCase().includes(_dashboardIndexFilter));
}

function renderIndicesTable(indices) {
  const tbody = document.getElementById('indicesTableBody');
  // Drop selections for indices that no longer exist.
  const known = new Set(indices.map(i => i.name));
  for (const n of [...selectedIndices]) if (!known.has(n)) selectedIndices.delete(n);

  const visible = _dashboardVisibleIndices(indices);
  const selAll = document.getElementById('idxSelAll');
  if (selAll) selAll.checked = visible.length > 0 && visible.every(i => selectedIndices.has(i.name));
  updateDeleteSelectedBtn();

  if (!visible.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-center text-secondary py-3">${
      indices.length ? 'No indices match the search' : 'No indices found'}</td></tr>`;
  } else {
    tbody.innerHTML = visible.map(idx => {
      const hClass = idx.health === 'green' ? 'success' : idx.health === 'yellow' ? 'warning' : 'danger';
      const cat = idx.cc_meta?.category || '<span class="text-secondary">—</span>';
      return `<tr onclick="showIndexDetail('${esc(idx.name)}')">
        <td onclick="event.stopPropagation()">
          <input type="checkbox" ${selectedIndices.has(idx.name) ? 'checked' : ''}
                 onclick="toggleIndexSel('${jsq(idx.name)}', this)"/></td>
        <td class="fw-semibold">${esc(idx.name)}</td>
        <td><span class="badge bg-${hClass}">${idx.health || '?'}</span></td>
        <td>${(idx.docs_count ?? 0).toLocaleString()}</td>
        <td>${idx.store_size || '—'}</td>
        <td>${cat}</td>
        <td class="text-end text-nowrap">
          <button class="btn btn-sm btn-outline-info py-0 px-1 me-1"
                  onclick="event.stopPropagation(); duplicateIndex('${jsq(idx.name)}')"
                  title="Duplicate this index (optionally shifting dates)"><i class="bi bi-copy"></i></button>
          <button class="btn btn-sm btn-outline-danger py-0 px-1 me-1"
                  onclick="event.stopPropagation(); deleteIndexByName('${jsq(idx.name)}')"
                  title="Delete this index"><i class="bi bi-trash"></i></button>
          <i class="bi bi-chevron-right text-secondary"></i>
        </td>
      </tr>`;
    }).join('');
  }
  if (indicesView === 'csv') setIndicesView('csv');   // keep CSV view in sync
}

/* ── Dashboard indices: Table / CSV views ──────────────────────────────────── */
let indicesView = 'table';

/** Flatten the indices list into plain rows for CSV/table export. */
function indicesRows() {
  return (allIndices || []).map(i => ({
    name: i.name, health: i.health, status: i.status,
    docs_count: i.docs_count, store_size: i.store_size,
    primaries: i.primaries, replicas: i.replicas,
    category: i.cc_meta?.category || '',
  }));
}

function setIndicesView(mode) {
  indicesView = mode;
  document.getElementById('iv-table')?.classList.toggle('active', mode === 'table');
  document.getElementById('iv-csv')?.classList.toggle('active', mode === 'csv');
  const wrap  = document.getElementById('indicesTableWrap');
  const csvEl = document.getElementById('indicesCsv');
  wrap?.classList.toggle('d-none', mode !== 'table');
  csvEl?.classList.toggle('d-none', mode !== 'csv');
  if (mode === 'csv' && csvEl) {
    const rows = indicesRows();
    csvEl.textContent = rows.length ? buildResultsCsv(rows) : 'No indices.';
  }
}

function downloadIndicesCsv() {
  const rows = indicesRows();
  if (!rows.length) { showToast('No indices to export', 'bg-warning'); return; }
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const blob = new Blob([buildResultsCsv(rows)], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `cc_indices_${ts}.csv`;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

/* ── Index detail ─────────────────────────────────────────────────────────── */
let _currentIndexName = null;
let _preservedIndexFilters = {};  // Preserved filters when refreshing/changing Show count
let _preservedHiddenColumns = []; // Preserved column visibility across a reload
let _indexFullTotal = 0;          // total docs in the current index (match_all count)
let _indexSortField = null;       // date field the sample is sorted by (desc)

function refreshCurrentIndex() {
  if (_currentIndexName) {
    // Preserve current filters before reload.
    // NOTE: tableFilters values are Set objects — JSON.stringify would turn them
    // into empty {} and silently drop every filter, so serialise them as arrays.
    _preservedIndexFilters = {};
    for (const [col, set] of Object.entries(tableFilters)) {
      if (set && set.size) _preservedIndexFilters[col] = [...set];
    }
    _preservedHiddenColumns = [...hiddenColumns];   // keep the user's column visibility
    showIndexDetail(_currentIndexName, true);  // true = preserve filters
  }
}

/* ── Create / delete index · import CSV ─────────────────────────────────────── */

/** Prompt for a name and create a new (empty) index, then open it. */
async function createIndex() {
  const name = await uiPrompt(document, {
    title: 'Create new index — enter a lowercase name',
    value: '', okText: 'Create',
  });
  if (name == null) return;
  const clean = name.trim();
  if (!clean) return;
  const res = await api('/api/indices/create', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: clean }),
  });
  if (!res || res.error) { showToast('Create failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(`Index "${res.name}" created`, 'bg-success');
  await loadIndices();
  showIndexDetail(res.name);
}

/** Delete any index by name (typed confirmation) — used from the detail view
 *  header and from the dashboard table's per-row action. */
async function deleteIndexByName(name) {
  if (!name) return;
  const typed = await uiPrompt(document, {
    title: `Delete index — type "${name}" to confirm`,
    value: '', okText: 'Delete',
  });
  if (typed == null) return;
  if (typed.trim() !== name) { showToast('Name did not match — deletion cancelled', 'bg-warning'); return; }
  const res = await api(`/api/indices/${encodeURIComponent(name)}`, { method: 'DELETE' });
  if (!res || res.error) { showToast('Delete failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(`Index "${name}" deleted`, 'bg-success');
  if (_currentIndexName === name) {
    _currentIndexName = null;
    showView('dashboard');
  }
  await loadIndices();
}

/** Delete the index currently shown in the detail view (typed confirmation). */
async function deleteCurrentIndex() {
  await deleteIndexByName(_currentIndexName);
}

/** Digit sections of an index name: [{start, len, value, context}]. The
 *  context shows the digits with their surrounding name chunk (e.g. "sid-0"). */
function indexNameDigitSections(name) {
  return [...name.matchAll(/\d+/g)].map(m => {
    const start = m.index, len = m[0].length;
    const before = name.slice(0, start).match(/[a-z]+[-_.]*$/i)?.[0] || '';
    return { start, len, value: parseInt(m[0]),
             context: `${before}${m[0]}` };
  });
}

/** Build the target name for copy k: each digit section stepped by step×k
 *  (clamped at 0). If nothing steps, fall back to "<base>-copy<k>". */
function buildDuplicateName(sourceName, sections, steps, k) {
  if (!steps.some(s => s)) return `${sourceName}-copy${k}`;
  let out = '', pos = 0;
  sections.forEach((sec, i) => {
    out += sourceName.slice(pos, sec.start);
    out += String(Math.max(0, sec.value + (steps[i] || 0) * k));
    pos = sec.start + sec.len;
  });
  return out + sourceName.slice(pos);
}

/** Modal collecting duplicate options. Resolves to
 *  {copies, steps[], target, shift_amount, shift_unit, shift_direction} or null. */
function duplicateIndexDialog(sourceName) {
  const sections = indexNameDigitSections(sourceName);
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const sectionRows = sections.map((sec, i) => `
        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="badge bg-secondary font-monospace">${esc(sec.context)}</span>
          <span class="small text-secondary">step per copy</span>
          <input type="number" class="form-control form-control-sm dup-step" data-i="${i}"
                 value="0" style="width:90px;" title="Positive = increase, negative = decrease"/>
        </div>`).join('');
    wrap.innerHTML = `<div class="rt-modal" style="min-width:480px;max-width:640px;">
        <div class="rt-modal-title"><i class="bi bi-copy me-1"></i>Duplicate index "${esc(sourceName)}"</div>
        <div class="rt-modal-body">
          <div class="d-flex align-items-center gap-2 mb-2">
            <label class="small text-secondary mb-0">Number of copies</label>
            <input type="number" class="form-control form-control-sm dup-copies" value="1" min="1" max="100" style="width:90px;"/>
          </div>
          ${sections.length ? `
            <label class="small text-secondary mb-1">Step the name's number sections per copy (0 = keep)</label>
            ${sectionRows}` : ''}
          <div class="dup-single-name">
            <label class="small text-secondary mb-1">New index name (lowercase)</label>
            <input class="form-control form-control-sm mb-2 dup-target" value="${esc(sourceName)}-copy"/>
          </div>
          <div class="dup-preview small text-info mb-2 d-none" style="word-break:break-all;"></div>
          <label class="small text-secondary mb-1">Shift all date fields per copy (0 = exact copy; copy k shifts k × gap)</label>
          <div class="d-flex gap-2 align-items-center">
            <input type="number" class="form-control form-control-sm dup-amount" value="0" min="0" style="width:90px;"/>
            <select class="form-select form-select-sm dup-unit" style="width:110px;">
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
              <option value="days" selected>days</option>
              <option value="weeks">weeks</option>
              <option value="months">months</option>
            </select>
            <select class="form-select form-select-sm dup-dir" style="width:110px;">
              <option value="past" selected>in the past</option>
              <option value="future">in the future</option>
            </select>
          </div>
          <div class="small text-secondary mt-1">A "month" is a fixed 30 days — every document in a copy shifts by the same offset.</div>
        </div>
        <div class="rt-modal-actions">
          <button class="btn btn-sm btn-info" data-ok="1"><i class="bi bi-copy me-1"></i>Duplicate</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    document.body.appendChild(wrap);

    const readSteps  = () => sections.map((_, i) =>
      parseInt(wrap.querySelector(`.dup-step[data-i="${i}"]`)?.value) || 0);
    const readCopies = () => Math.max(1, Math.min(100,
      parseInt(wrap.querySelector('.dup-copies').value) || 1));

    // Single-name input applies only to the plain 1-copy/no-step case; loop
    // mode generates names — show a live preview of the first/last instead.
    const updatePreview = () => {
      const copies = readCopies(), steps = readSteps();
      const looping = copies > 1 || steps.some(s => s);
      wrap.querySelector('.dup-single-name').classList.toggle('d-none', looping);
      const prev = wrap.querySelector('.dup-preview');
      prev.classList.toggle('d-none', !looping);
      if (looping) {
        const first = buildDuplicateName(sourceName, sections, steps, 1);
        const last  = buildDuplicateName(sourceName, sections, steps, copies);
        prev.innerHTML = copies > 1
          ? `<i class="bi bi-arrow-return-right me-1"></i>${esc(first)} … ${esc(last)} (${copies} copies)`
          : `<i class="bi bi-arrow-return-right me-1"></i>${esc(first)}`;
      }
    };
    wrap.querySelectorAll('.dup-copies, .dup-step').forEach(el =>
      el.addEventListener('input', updatePreview));
    updatePreview();

    const done = (v) => { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const read = () => ({
      copies:          readCopies(),
      steps:           readSteps(),
      target:          wrap.querySelector('.dup-target').value.trim(),
      shift_amount:    Math.max(0, parseInt(wrap.querySelector('.dup-amount').value) || 0),
      shift_unit:      wrap.querySelector('.dup-unit').value,
      shift_direction: wrap.querySelector('.dup-dir').value,
    });
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1' ? read() : null); return; }
      if (e.target === wrap) done(null);
    });
    setTimeout(() => { const inp = wrap.querySelector('.dup-target'); inp?.focus(); inp?.select(); }, 0);
  });
}

/** Duplicate an index N times (with per-copy name stepping + cumulative
 *  date-shift), calling the duplicate endpoint once per copy. */
async function duplicateIndex(name) {
  if (!name) return;
  const opts = await duplicateIndexDialog(name);
  if (!opts) return;

  const sections = indexNameDigitSections(name);
  const looping  = opts.copies > 1 || opts.steps.some(s => s);
  if (!looping && !opts.target) { showToast('Enter a name for the new index', 'bg-warning'); return; }

  let created = 0, lastTarget = '';
  for (let k = 1; k <= opts.copies; k++) {
    const target = looping ? buildDuplicateName(name, sections, opts.steps, k) : opts.target;
    showToast(`Copying ${k}/${opts.copies}: "${name}" → "${target}"…`, 'bg-secondary');
    const res = await api(`/api/indices/${encodeURIComponent(name)}/duplicate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target,
        shift_amount:    opts.shift_amount * k,     // cumulative: copy k = k × gap
        shift_unit:      opts.shift_unit,
        shift_direction: opts.shift_direction,
      }),
    });
    if (!res || res.error) {
      showToast(`Copy ${k}/${opts.copies} ("${target}") failed: ${res?.error || 'unknown'}`
        + (created ? ` — ${created} cop${created > 1 ? 'ies' : 'y'} already created` : ''), 'bg-danger');
      break;
    }
    created++;
    lastTarget = res.target;
    if (res.failed) showToast(`"${target}": ${res.failed} doc(s) failed to index`, 'bg-warning');
  }

  if (created) {
    showToast(`Created ${created} cop${created > 1 ? 'ies' : 'y'} of "${name}"`, 'bg-success');
    await loadIndices();
    if (created === 1) showIndexDetail(lastTarget);
  }
}

/** Pick a CSV file and import its rows as documents into the current index. */
function importCsvToIndex() {
  const name = _currentIndexName;
  if (!name) return;
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.csv,text/csv';
  inp.onchange = () => { const f = inp.files && inp.files[0]; if (f) doImportCsv(name, f); };
  inp.click();
}

async function doImportCsv(indexName, file) {
  const ok = await uiConfirm(document, {
    title: `Import into "${indexName}"?`,
    message: `Add rows from "${file.name}" (${(file.size / 1024).toFixed(1)} KB) as documents. `
      + 'Rows carrying an _id that already exists are overwritten.',
    okText: 'Import',
  });
  if (!ok) return;
  showToast(`Importing ${file.name}…`, 'bg-secondary');
  const fd = new FormData();
  fd.append('file', file, file.name);
  let res;
  try {
    res = await api(`/api/indices/${encodeURIComponent(indexName)}/import`, { method: 'POST', body: fd });
  } catch (e) {
    showToast('Import failed: ' + e, 'bg-danger'); return;
  }
  if (!res || res.error) { showToast('Import failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  const msg = `Imported ${res.indexed}/${res.rows} row(s)` + (res.failed ? ` — ${res.failed} failed` : '');
  showToast(msg, res.failed ? 'bg-warning' : 'bg-success');
  if (res.failed && Array.isArray(res.errors) && res.errors.length) {
    console.warn('CSV import errors (first few):', res.errors);
  }
  refreshCurrentIndex();
}

async function showIndexDetail(indexName, preserveFilters = false) {
  _currentIndexName = indexName;
  showView('index');
  activateViewer('index');
  document.getElementById('indexDetailTitle').textContent = indexName;
  document.getElementById('indexStatCards').innerHTML = '<div class="text-secondary small">Loading stats…</div>';
  setQueryResults('Loading…');

  const size = parseInt(document.getElementById('sampleSizeSelect')?.value || '10');

  const [stats, sample] = await Promise.all([
    api(`/api/indices/${encodeURIComponent(indexName)}/stats`),
    api(`/api/indices/${encodeURIComponent(indexName)}/sample?size=${size}`),
  ]);

  const meta    = stats.cc_meta || {};
  const storeMB = stats.store_bytes ? (stats.store_bytes / 1e6).toFixed(1) + ' MB' : '—';
  const shownCount = sample.hits?.length ?? 0;
  const totalCount = sample.total ?? stats.docs_count ?? 0;
  // Remember the full index size + sort field so column filters can decide
  // whether to filter the loaded rows locally or re-query Elasticsearch.
  _indexFullTotal = stats.docs_count ?? sample.total ?? 0;
  _indexSortField = sample.sort_field || null;
  // Remember which fields are declared in the mapping so the field-visibility
  // picker can tell mapped vs. unmapped columns apart.
  mappedFieldNames = new Set(Array.isArray(stats.mapping_field_names) ? stats.mapping_field_names : []);
  document.getElementById('indexStatCards').innerHTML = `
    <div class="col-auto"><div class="stat-card"><div class="stat-value">${(stats.docs_count ?? 0).toLocaleString()}</div><div class="stat-label">Documents</div></div></div>
    <div class="col-auto"><div class="stat-card"><div class="stat-value text-warning">${(stats.docs_deleted ?? 0).toLocaleString()}</div><div class="stat-label">Deleted Docs</div></div></div>
    <div class="col-auto"><div class="stat-card"><div class="stat-value text-info">${storeMB}</div><div class="stat-label">Store Size</div></div></div>
    <div class="col-auto"><div class="stat-card stat-card-action" onclick="openFieldVisibility()" title="Choose which fields to show/hide"><div class="stat-value">${stats.mapping_fields ?? '—'} <i class="bi bi-eye stat-card-icon"></i></div><div class="stat-label">Mapped Fields</div></div></div>
    <div class="col-auto"><div class="stat-card"><div class="stat-value text-success" id="showingTotalStat">${shownCount.toLocaleString()} / ${totalCount.toLocaleString()}</div><div class="stat-label">Showing / Total</div></div></div>
    ${meta.description ? `<div class="col-12"><div class="alert alert-info py-2 small mb-0"><strong>${esc(meta.display || indexName)}</strong> — ${esc(meta.description)}</div></div>` : ''}`;

  const metaEl = document.getElementById('idxMeta');
  if (sample.error) {
    lastResultHits = []; lastResultTotal = 0; lastResultJson = '✗ ' + sample.error;
    selectedRows.clear(); selectedCols.clear();
    if (metaEl) metaEl.textContent = '';
    renderResultViews();
    return;
  }
  if (metaEl) {
    metaEl.textContent = `${shownCount.toLocaleString()} shown of ${totalCount.toLocaleString()}`
      + (sample.sort_field ? ` · sorted by ${sample.sort_field} desc` : '');
  }

  // Capture results but restore preserved filters if requested
  if (preserveFilters) {
    // Temporarily store the filters to restore after captureResults resets them
    const filtersToRestore = _preservedIndexFilters;
    const hiddenToRestore  = _preservedHiddenColumns;
    captureResults(sample);
    // Restore preserved filters (stored as arrays of values)
    tableFilters = {};
    for (const [col, values] of Object.entries(filtersToRestore)) {
      if (Array.isArray(values)) {
        tableFilters[col] = new Set(values);
      } else if (values && values.size !== undefined) {
        tableFilters[col] = new Set(values);
      } else if (values && typeof values === 'object') {
        tableFilters[col] = new Set(Object.values(values));
      }
    }
    // Restore preserved column visibility (only for columns still present)
    const present = new Set(currentColumns());
    hiddenColumns = new Set((hiddenToRestore || []).filter(c => present.has(c)));
    _preservedIndexFilters = {};
    _preservedHiddenColumns = [];
    // If filters are active but the sample is only a slice of the index, the
    // loaded rows can't represent all matches — re-query Elasticsearch instead.
    if (Object.keys(tableFilters).length && lastResultHits.length < _indexFullTotal) {
      await applyIndexFiltersServerSide();
    } else {
      renderResultViews();
      updateFilterButtonState();
      updateShowingCount();
    }
  } else {
    captureResults(sample);
    updateFilterButtonState();
  }
}

/* ── Attack summary charts ───────────────────────────────────────────────── */
async function loadAttackSummary() {
  // The dashboard attack charts were removed; skip if the canvases are absent.
  if (!document.getElementById('chartAttackCategory') &&
      !document.getElementById('chartAttackTimeline')) return;
  try {
    const data = await api('/api/cc/attacks/summary');
    if (data.error) return;
    renderCategoryChart(data.by_category || []);
    renderTimelineChart(data.attacks_over_time || []);
  } catch (_) {}
}

function renderCategoryChart(buckets) {
  const el = document.getElementById('chartAttackCategory');
  if (!el) return;
  const ctx = el.getContext('2d');
  if (chartCategory) chartCategory.destroy();
  if (!buckets.length) return;
  chartCategory = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: buckets.map(b => b.key || 'Unknown'),
      datasets: [{ data: buckets.map(b => b.count),
        backgroundColor: ['#e74c3c','#e67e22','#f1c40f','#2ecc71','#3498db','#9b59b6','#1abc9c','#e91e63','#607d8b'] }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'right', labels: { font: { size: 11 } } } } },
  });
}

function renderTimelineChart(buckets) {
  const el = document.getElementById('chartAttackTimeline');
  if (!el) return;
  const ctx = el.getContext('2d');
  if (chartTimeline) chartTimeline.destroy();
  if (!buckets.length) return;
  chartTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: buckets.map(b => b.date?.slice(0, 10) || ''),
      datasets: [{ label: 'Attacks', data: buckets.map(b => b.count),
        backgroundColor: 'rgba(220,53,69,.6)', borderColor: '#dc3545', borderWidth: 1 }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 10 } } } },
  });
}

/* ── Attacks view ────────────────────────────────────────────────────────── */
async function loadAttacks() {
  const status = document.getElementById('attackStatusFilter').value;
  const params = new URLSearchParams({ size: 50 });
  if (status) params.set('status', status);
  const data  = await api(`/api/cc/attacks?${params}`);
  const tbody = document.getElementById('attacksTableBody');
  if (data.error) {
    tbody.innerHTML = `<tr><td colspan="11" class="text-center text-danger">${esc(data.error)}</td></tr>`;
    return;
  }
  const attacks = data.attacks || [];
  if (!attacks.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="text-center text-secondary py-3">No attacks found</td></tr>';
    return;
  }

  // Update header badge
  const hdr = document.querySelector('#view-attacks h5');
  if (hdr) {
    hdr.innerHTML =
      `<i class="bi bi-shield-exclamation me-2 text-danger"></i>Recent Attacks` +
      ` <span class="badge bg-danger ms-2">${data.total_unique_attacks ?? attacks.length} unique</span>` +
      ` <span class="badge bg-secondary ms-1">${(data.total_records ?? 0).toLocaleString()} raw records</span>`;
  }

  tbody.innerHTML = attacks.map(a => {
    const bwFmt  = a.totalBw       != null ? Number(a.totalBw).toFixed(1)       : '—';
    const pktFmt = a.totalPackets  != null ? Number(a.totalPackets).toLocaleString() : '—';
    const recFmt = a.recordCount   != null ? a.recordCount.toLocaleString()      : '—';
    const blkBadge = a.blockingState
      ? `<span class="badge ${a.blockingState === 'Blocking' ? 'bg-danger' : 'bg-warning text-dark'}">${esc(a.blockingState)}</span>`
      : '<span class="text-secondary">—</span>';
    const typeBadge = a.attackType
      ? `<span class="badge bg-info text-dark">${esc(a.attackType)}</span>`
      : '—';
    return `<tr>
      <td class="font-monospace small">${esc(a.attackIpsId || '—')}</td>
      <td>${typeBadge}</td>
      <td class="text-nowrap">${fmtTime(a.startTime)}</td>
      <td class="text-nowrap">${fmtTime(a.endTime)}</td>
      <td>${blkBadge}</td>
      <td>${esc(a.deviceIp     || '—')}</td>
      <td>${esc(a.protection   || '—')}</td>
      <td>${esc(a.destinationIp|| '—')}</td>
      <td class="text-end">${bwFmt}</td>
      <td class="text-end">${pktFmt}</td>
      <td class="text-center"><span class="badge bg-secondary">${recFmt}</span></td>
    </tr>`;
  }).join('');
}

/* ══════════════════════════════════════════════════════════════════════════
   SUMMARY ANALYTICS VIEW
   ══════════════════════════════════════════════════════════════════════════ */

const CAT_COLORS = [
  '#e74c3c','#e67e22','#f1c40f','#2ecc71','#3498db',
  '#9b59b6','#1abc9c','#e91e63','#607d8b','#00bcd4','#ff5722',
];

async function downloadSummaryJson() {
  const btn = document.getElementById('btnDownloadJson');
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Building…';

  try {
    const res = await fetch(appUrl('/api/cc/summary/export'));
    if (!res.ok) throw new Error('HTTP ' + res.status);

    // Extract filename from Content-Disposition header
    const cd = res.headers.get('Content-Disposition') || '';
    const match = cd.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : 'cc_summary.json';

    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showToast(`Downloaded ${filename}`, 'bg-success');
  } catch (e) {
    showToast('Download failed: ' + e.message, 'bg-danger');
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

async function loadSummary() {  const data = await api('/api/cc/summary');
  if (data.error) {
    showToast('Summary error: ' + data.error, 'bg-danger');
    return;
  }
  summaryData = data;

  // ── Stat cards ──────────────────────────────────────────────────────────
  setText('s-totalAttacks', (data.total_attacks ?? 0).toLocaleString());
  const dur = data.duration_overall || {};
  setText('s-avgDur', dur.avg_s != null ? dur.avg_s.toLocaleString() : '—');
  setText('s-maxDur', dur.max_s != null ? dur.max_s.toLocaleString() : '—');
  setText('s-maxBps',  data.max_attack_bps != null ? fmtBps(data.max_attack_bps) : '—');
  const gaps = data.inter_attack_gaps?.overall || {};
  setText('s-avgGap',  gaps.avg_s  != null ? gaps.avg_s.toLocaleString()  : '—');
  const tr = data.traffic || {};
  setText('s-trafficAvg', tr.avg_bps != null ? fmtBps(tr.avg_bps) : '—');

  // ── Charts ──────────────────────────────────────────────────────────────
  renderSummaryTimeline(data);
  renderSummaryCategory(data.by_category || []);
  renderSummaryTraffic(tr.by_day || []);

  // ── Risk / Status mini-lists ─────────────────────────────────────────────
  renderKeyValueList('summaryRiskList',   data.by_risk   || [], 'risk');
  renderKeyValueList('summaryStatusList', data.by_status || [], 'status');

  // ── Duration table ────────────────────────────────────────────────────────
  const tbody1 = document.getElementById('durationByCatBody');
  if (!data.duration_by_cat?.length) {
    tbody1.innerHTML = '<tr><td colspan="7" class="text-center text-secondary py-2">No data</td></tr>';
  } else {
    tbody1.innerHTML = data.duration_by_cat.map(r => `<tr>
      <td><span class="badge" style="background:${catColor(r.category)}">${esc(r.category)}</span></td>
      <td class="text-center">${(r.count ?? 0).toLocaleString()}</td>
      <td class="text-end">${r.min_s ?? '—'}</td>
      <td class="text-end fw-semibold">${r.avg_s ?? '—'}</td>
      <td class="text-end text-danger">${r.max_s ?? '—'}</td>
      <td class="text-end">${r.avg_bps != null ? fmtBps(r.avg_bps) : '—'}</td>
      <td class="text-end text-warning">${r.max_bps != null ? fmtBps(r.max_bps) : '—'}</td>
    </tr>`).join('');
  }

  // ── Gap table ──────────────────────────────────────────────────────────────
  const tbody2 = document.getElementById('gapsByCatBody');
  const gapsByCat = data.inter_attack_gaps?.by_category || {};
  const overallGap = data.inter_attack_gaps?.overall || {};
  const rows = [
    { scope: '⬛ Overall (all categories)', ...overallGap, _overall: true },
    ...Object.entries(gapsByCat)
              .sort((a, b) => (b[1].attack_count || 0) - (a[1].attack_count || 0))
              .map(([cat, g]) => ({ scope: cat, ...g })),
  ];
  tbody2.innerHTML = rows.map(r => {
    const boldClass = r._overall ? 'fw-bold table-active' : '';
    const scopeCell = r._overall
      ? `<td class="${boldClass}">${esc(r.scope)}</td>`
      : `<td><span class="badge" style="background:${catColor(r.scope)}">${esc(r.scope)}</span></td>`;
    return `<tr class="${boldClass}">
      ${scopeCell}
      <td class="text-center">${(r.attack_count ?? '—').toLocaleString?.() ?? '—'}</td>
      <td class="text-center">${(r.gap_count   ?? '—').toLocaleString?.() ?? '—'}</td>
      <td class="text-end">${r.min_s ?? '—'}</td>
      <td class="text-end fw-semibold">${r.avg_s ?? '—'}</td>
      <td class="text-end text-danger">${r.max_s ?? '—'}</td>
    </tr>`;
  }).join('');
}

function setTimeGranularity(gran) {
  summaryGranularity = gran;
  ['day','week','month'].forEach(g => {
    const btn = document.getElementById('btnGran' + g.charAt(0).toUpperCase() + g.slice(1));
    if (btn) btn.classList.toggle('active', g === gran);
  });
  if (summaryData) renderSummaryTimeline(summaryData);
}

function renderSummaryTimeline(data) {
  const buckets = (summaryGranularity === 'week'  ? data.attacks_by_week  :
                   summaryGranularity === 'month' ? data.attacks_by_month :
                                                    data.attacks_by_day) || [];
  const ctx = document.getElementById('chartSummaryTimeline').getContext('2d');
  if (chartSummaryTimeline) chartSummaryTimeline.destroy();
  if (!buckets.length) return;
  chartSummaryTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: buckets.map(b => (b.date || '').slice(0, 10)),
      datasets: [{
        label: 'Attacks',
        data: buckets.map(b => b.count),
        backgroundColor: 'rgba(220,53,69,.7)',
        borderColor: '#dc3545',
        borderWidth: 1,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 15 } } },
    },
  });
}

function renderSummaryCategory(buckets) {
  const ctx = document.getElementById('chartSummaryCategory').getContext('2d');
  if (chartSummaryCategory) chartSummaryCategory.destroy();
  if (!buckets.length) return;
  chartSummaryCategory = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: buckets.map(b => b.key),
      datasets: [{
        data: buckets.map(b => b.count),
        backgroundColor: buckets.map((_, i) => CAT_COLORS[i % CAT_COLORS.length]),
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'right', labels: { font: { size: 10 }, boxWidth: 12 } } },
    },
  });
}

function renderSummaryTraffic(byDay) {
  const ctx = document.getElementById('chartSummaryTraffic').getContext('2d');
  if (chartSummaryTraffic) chartSummaryTraffic.destroy();
  if (!byDay.length) return;
  chartSummaryTraffic = new Chart(ctx, {
    type: 'line',
    data: {
      labels: byDay.map(b => (b.date || '').slice(0, 10)),
      datasets: [{
        label: 'Avg bps',
        data: byDay.map(b => b.avg_bps || 0),
        borderColor: '#0dcaf0', backgroundColor: 'rgba(13,202,240,.15)',
        fill: true, tension: 0.3, pointRadius: 3,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 12 } } },
    },
  });
}

function renderKeyValueList(containerId, items, type) {
  const el = document.getElementById(containerId);
  if (!items.length) { el.innerHTML = '<span class="text-secondary small">No data</span>'; return; }
  const total = items.reduce((s, i) => s + (i.count || 0), 0);
  const colors = { High: 'danger', Medium: 'warning', Low: 'success',
                   Terminated: 'secondary', Active: 'danger', Unknown: 'dark' };
  el.innerHTML = items.map(i => {
    const pct = total ? Math.round((i.count / total) * 100) : 0;
    const badgeC = colors[i.key] || 'info';
    return `<div class="d-flex justify-content-between align-items-center mb-1">
      <span class="badge bg-${badgeC}">${esc(i.key)}</span>
      <span class="small text-secondary">${i.count.toLocaleString()} <span class="text-muted">(${pct}%)</span></span>
    </div>
    <div class="progress mb-2" style="height:4px;">
      <div class="progress-bar bg-${badgeC}" style="width:${pct}%"></div>
    </div>`;
  }).join('');
}

/* ── Helpers ────────────────────────────────────────────────────────────── */
function fmtBps(bps) {
  if (bps == null) return '—';
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + ' Gbps';
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + ' Mbps';
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + ' Kbps';
  return bps.toFixed(0) + ' bps';
}

function catColor(cat) {
  const cats = ['DNS','WebDDoS','BehavioralDOS','SynFlood','Intrusions',
                'Anomalies','AntiScanning','ACL','StatefulACL','DOSShield','TrafficFilters'];
  const idx = cats.indexOf(cat);
  return idx >= 0 ? CAT_COLORS[idx] : '#6c757d';
}

/* ── Query editor ────────────────────────────────────────────────────────── */

/** Parse the Size input. Unlike `parseInt(...) || 10`, an explicit 0 is kept
 *  (size 0 = totals/aggregations only) — only empty/invalid falls back. */
function querySizeValue(dflt = 10) {
  const n = parseInt(document.getElementById('querySize')?.value);
  return Number.isNaN(n) ? dflt : Math.max(0, n);
}

async function runQuery() {
  const index = document.getElementById('queryIndex').value.trim();
  const size  = querySizeValue();
  let body;
  try { body = JSON.parse(document.getElementById('queryBody').value); }
  catch (e) { setQueryResults('✗ Invalid JSON: ' + e.message); return; }

  setQueryResults('Running…');
  document.getElementById('queryMeta').textContent = '';
  captureResults({ hits: [] });

  const data = await api('/api/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, body, size }),
  });
  if (data.error) {
    setQueryResults('✗ Error: ' + data.error);
    return;
  }
  // The server may have substituted/dropped a sort on a nonexistent field.
  if (data.sort_note) showToast(data.sort_note, 'bg-info');
  document.getElementById('queryMeta').textContent =
    `${(data.total ?? 0).toLocaleString()} hits · ${data.took_ms ?? '?'} ms`;
  setQueryResults(JSON.stringify(data, null, 2));
  captureResults(data);
  // Remember the executed query so column filters can re-query ES server-side.
  _queryBaseItems     = [{ index, query_body: body }];
  _queryTotalMatching = data.total ?? 0;
}

function formatQuery() {
  try {
    const el = document.getElementById('queryBody');
    el.value = JSON.stringify(JSON.parse(el.value), null, 2);
  } catch (_) {}
}

const TEMPLATES = {
  matchAll:      { query: { match_all: {} }, sort: [{ startTime: { order: 'desc' } }] },
  activeAttacks: { query: { term: { 'status.keyword': 'active' } }, sort: [{ startTime: { order: 'desc' } }] },
  aggCategory:   { size: 0, aggs: { by_category: { terms: { field: 'category.keyword', size: 20 } } } },
};
function loadTemplate(name) {
  const tpl = TEMPLATES[name];
  if (tpl) document.getElementById('queryBody').value = JSON.stringify(tpl, null, 2);
}

/* ── Natural language query translator ──────────────────────────────────────── */
let perIndexQueries = [];   // [{index, date_fields, query_body}, ...]

// Base query context + true matching total for the Query Editor results viewer,
// so table column filters can re-query ES when the loaded page is only a slice.
let _queryBaseItems     = null;  // [{index, query_body}] actually executed
let _queryTotalMatching = 0;     // sum of real per-index match totals

/** Default query shown in the editor (also what Clear resets to). */
const DEFAULT_QUERY_BODY = {
  query: { match_all: {} },
  sort: [{ startTime: { order: 'desc' } }],
};

/** Reset the generated-query state: JSON body, multi-index plan, suggestions,
 *  interpretation line. (Used by the Clear button and by an empty Translate.) */
function resetGeneratedQuery() {
  document.getElementById('queryBody').value = JSON.stringify(DEFAULT_QUERY_BODY, null, 2);
  perIndexQueries = [];
  renderPerIndexQueries([]);
  fieldSuggestions = [];
  renderSuggestions([]);
  const infoEl = document.getElementById('nlInterpretation');
  if (infoEl) { infoEl.classList.add('d-none'); infoEl.innerHTML = ''; }
}

/** Full Query-Editor reset: query JSON, free text, types, time range, plan. */
function clearQueryEditor() {
  document.getElementById('nlQueryInput').value = '';
  resetGeneratedQuery();
  clearAttackTypeSelection();
  clearTimeRange();
  showToast('Query editor cleared', 'bg-info');
}

async function translateNlQuery() {
  const text   = document.getElementById('nlQueryInput').value.trim();
  const index  = document.getElementById('queryIndex').value.trim() || 'dp-attack-raw-*';
  const infoEl = document.getElementById('nlInterpretation');
  const types  = [...selectedAttackTypes];

  // Time range — both bounds per Start/End, validated before translating.
  const t = readTimeRange();
  const timeErr = timeRangeError();
  if (timeErr) { showToast(timeErr, 'bg-danger'); showTimeRangePicker(); return; }
  const hasTime = !!(t.startAfter || t.startBefore || t.endAfter || t.endBefore);

  // Sort
  const sortHint = document.getElementById('sortHint')?.value     ?? 'start';
  const sortDir  = document.querySelector('input[name="sortDir"]:checked')?.value ?? 'desc';

  // Nothing to translate from → clear the generated query instead of keeping
  // a stale one (the user emptied the free text and hit Translate).
  if (!text && !types.length && !hasTime) {
    resetGeneratedQuery();
    showToast('Nothing to translate — query reset to default', 'bg-info');
    return;
  }

  infoEl.className = 'small text-secondary px-1';
  infoEl.textContent = '⏳ Translating…';
  infoEl.classList.remove('d-none');

  try {
    const data = await api('/api/query/translate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        index,
        attack_types:    types,
        start_after:     t.startAfter,
        start_before:    t.startBefore,
        end_after:       t.endAfter,
        end_before:      t.endBefore,
        sort_hint:       sortHint,
        sort_direction:  sortDir,
      }),
    });

    if (data.error) {
      infoEl.className = 'small text-danger px-1';
      infoEl.textContent = '✗ ' + data.error;
      return;
    }

    // Store per-index queries
    perIndexQueries = data.per_index_queries || [];

    // Show first query in the textarea
    if (perIndexQueries.length > 0) {
      document.getElementById('queryBody').value =
        JSON.stringify(perIndexQueries[0].query_body, null, 2);
    }

    // Render multi-index accordion + Run All button
    renderPerIndexQueries(perIndexQueries);

    // Render field suggestions (ambiguous references needing confirmation)
    fieldSuggestions = data.suggestions || [];
    renderSuggestions(fieldSuggestions);

    // Show interpretation
    if (data.interpreted?.length) {
      infoEl.className = 'small text-success px-1';
      infoEl.innerHTML = '<i class="bi bi-check-circle me-1"></i><strong>Interpreted as:</strong> ' +
        data.interpreted.map(s =>
          `<span class="badge bg-success bg-opacity-25 text-success border border-success me-1">${esc(s)}</span>`
        ).join('');
    } else {
      infoEl.className = 'small text-warning px-1';
      infoEl.textContent = '⚠ No filters matched — using match_all';
    }
  } catch (e) {
    infoEl.className = 'small text-danger px-1';
    infoEl.textContent = '✗ Network error: ' + e.message;
  }
}

function renderPerIndexQueries(queries) {
  const section   = document.getElementById('perIndexQueriesSection');
  const list      = document.getElementById('perIndexQueriesList');
  const countBadge = document.getElementById('perIndexCount');
  const btnAll    = document.getElementById('btnRunAll');
  const multi     = !!queries && queries.length > 1;

  // Single-query controls (textarea + Run Query + Format) belong to one-index mode.
  const singleEls = ['queryBody', 'btnRunSingle', 'btnFormat'].map(id => document.getElementById(id));
  singleEls.forEach(el => el && el.classList.toggle('d-none', multi));
  if (btnAll) btnAll.classList.toggle('d-none', !multi);
  if (!section || !list) return;

  if (!multi) {                       // one index/type → use the single textarea
    section.classList.add('d-none');
    return;
  }

  // Multi-index → hide the single textarea, show one editable query per index.
  section.classList.remove('d-none');
  if (countBadge) countBadge.textContent = `${queries.length} groups`;

  list.innerHTML = queries.map((q, i) => {
    const sortEntry  = q.query_body?.sort?.[0] || {};
    const sortField  = Object.keys(sortEntry)[0] || '—';
    const sortOrder  = sortEntry[sortField]?.order || '—';
    const dateFields = (q.date_fields || []).join(', ') || '—';
    return `<div class="border border-secondary rounded mb-1" style="font-size:0.78rem;overflow:hidden;">
      <div class="d-flex align-items-center px-2 py-1 gap-2"
           style="cursor:pointer;background:rgba(255,255,255,0.04);"
           onclick="this.nextElementSibling.classList.toggle('d-none')">
        <i class="bi bi-layers text-info"></i>
        <span class="text-info fw-semibold">${esc(q.index)}</span>
        <span class="text-secondary small ms-2">date fields: ${esc(dateFields)}</span>
        <span class="text-warning small ms-auto">sort: ${esc(sortField)} ${esc(sortOrder)}</span>
        <i class="bi bi-chevron-down text-secondary ms-1"></i>
      </div>
      <div class="m-0 p-2" style="background:#111;">
        <textarea class="form-control perindex-query" data-idx="${i}" spellcheck="false"
                  oninput="updatePerIndexQuery(${i}, this)"
                  style="font-size:0.72rem;font-family:monospace;min-height:170px;resize:vertical;
                         background:#0d0d0d;color:#e6e6e6;border-color:#333;">${esc(JSON.stringify(q.query_body, null, 2))}</textarea>
        <div class="perindex-err small text-danger mt-1 d-none"></div>
      </div>
    </div>`;
  }).join('');
}

/** Keep an edited per-index query in sync; flag invalid JSON inline. */
function updatePerIndexQuery(i, el) {
  const errEl = el.parentElement.querySelector('.perindex-err');
  try {
    const parsed = JSON.parse(el.value);
    if (perIndexQueries[i]) perIndexQueries[i].query_body = parsed;
    el.style.borderColor = '#333';
    if (errEl) errEl.classList.add('d-none');
  } catch (e) {
    el.style.borderColor = '#dc3545';
    if (errEl) { errEl.textContent = '✗ Invalid JSON: ' + e.message; errEl.classList.remove('d-none'); }
  }
}

/* ── Field suggestions (ambiguous references the user must confirm) ─────────── */
let fieldSuggestions = [];

const _OP_SYMBOL = { eq: '=', contains: 'contains', neq: '≠', ncontains: 'not-contains' };

function _suggestionLabel(s) {
  if (s.kind === 'exists') return `${s.label} ${s.present ? 'exists' : 'missing'}`;
  return `${s.label} ${_OP_SYMBOL[s.op] || '='} ${s.value}`;
}

/** Inject a clause into a query_body's bool (wrapping non-bool queries). */
function injectClause(qb, clause, where) {
  qb.query = qb.query || { match_all: {} };
  let q = qb.query;
  if (!q.bool) {
    q = q.match_all ? { bool: { must: [] } } : { bool: { must: [q] } };
    qb.query = q;
  }
  q.bool[where] = q.bool[where] || [];
  q.bool[where].push(clause);
  if (where === 'must_not' && !(q.bool.must && q.bool.must.length)) {
    q.bool.must = [{ match_all: {} }];
  }
}

/** Build the ES clause for a chosen candidate field. */
function buildSuggestionClause(s, field) {
  if (s.kind === 'exists') return { exists: { field } };
  if (s.op === 'contains' || s.op === 'ncontains') return { wildcard: { [field]: `*${s.value}*` } };
  return { match: { [field]: s.value } };
}

/** Apply a specific candidate field for suggestion i. */
function applySuggestionField(i, field) {
  const s = fieldSuggestions[i];
  if (!s || s._applied) return;
  const item = perIndexQueries.find(p => p.index === s.index);
  if (item) {
    injectClause(item.query_body, buildSuggestionClause(s, field), s.where);
    if (perIndexQueries[0]?.index === s.index) {
      document.getElementById('queryBody').value =
        JSON.stringify(perIndexQueries[0].query_body, null, 2);
    }
    renderPerIndexQueries(perIndexQueries);
  }
  s._applied = true;
  s._appliedField = field;
  renderSuggestions(fieldSuggestions);
}

function dismissSuggestion(i) {
  if (fieldSuggestions[i]) fieldSuggestions[i]._dismissed = true;
  renderSuggestions(fieldSuggestions);
}

function renderSuggestions(list) {
  const box = document.getElementById('fieldSuggestions');
  if (!box) return;
  const pending = (list || []).filter(s => !s._applied && !s._dismissed);
  if (!pending.length) { box.classList.add('d-none'); box.innerHTML = ''; return; }

  box.classList.remove('d-none');
  box.innerHTML = `
    <div class="rounded p-2" style="background:rgba(255,193,7,0.08);border:1px solid rgba(255,193,7,0.35);">
      <div class="d-flex align-items-center gap-2 mb-2">
        <i class="bi bi-question-circle text-warning"></i>
        <span class="small fw-semibold text-warning">Field suggestions — pick a field to apply, or dismiss</span>
      </div>
      ${list.map((s, i) => {
        if (s._applied || s._dismissed) return '';
        const cands = (s.candidates || []);
        const btns = cands.length
          ? cands.map(c =>
              `<button class="btn btn-sm btn-warning py-0 px-2" onclick="applySuggestionField(${i}, '${esc(c.field)}')"
                       title="matched ${c.score}/${s.total} words">
                 <i class="bi bi-check-lg me-1"></i>${esc(c.field)}
                 <span class="opacity-75" style="font-size:0.65rem;">${c.score}/${s.total}</span>
               </button>`).join('')
          : `<span class="text-secondary fst-italic">no similar field found</span>`;
        return `<div class="d-flex align-items-center gap-2 mb-1 flex-wrap" style="font-size:0.78rem;">
          <span class="text-secondary">No field matches</span>
          <span class="badge bg-secondary">${esc(_suggestionLabel(s))}</span>
          <span class="text-secondary">in</span>
          <span class="text-info">${esc(s.index)}</span>
          <span class="text-secondary">— did you mean:</span>
          ${btns}
          <button class="btn btn-sm btn-outline-secondary py-0 px-2 ms-auto" onclick="dismissSuggestion(${i})" title="Dismiss">
            <i class="bi bi-x-lg"></i>
          </button>
        </div>`;
      }).join('')}
    </div>`;
}

async function runMultiQuery(queries) {
  if (!queries || !queries.length) return;
  const size = querySizeValue();
  const metaEl = document.getElementById('queryMeta');
  setQueryResults(`⏳ Running ${queries.length} index queries…`);
  metaEl.textContent = '';
  captureResults({ hits: [] });

  try {
    const data = await api('/api/query/multi-run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        per_index_queries: queries,
        size,
        sort_direction: document.querySelector('input[name="sortDir"]:checked')?.value ?? 'desc',
      }),
    });

    if (data.error) {
      setQueryResults('✗ Error: ' + data.error);
      return;
    }

    // Build per-index meta line
    const metaParts = (data.per_index_meta || []).map(m =>
      m.error
        ? `${m.index.split('-ty-')[0]}: ✗ ${m.error}`
        : `${m.index.split('-ty-')[0]}: ${(m.total||0).toLocaleString()} total, ${m.returned} shown, ${m.took_ms}ms`
    );
    metaEl.textContent =
      `${(data.total_hits ?? 0).toLocaleString()} hits across ${queries.length} indices  ·  ${metaParts.join('  |  ')}`;
    setQueryResults(JSON.stringify(data, null, 2));
    captureResults(data);
    // Remember the executed queries + true match total for server-side filtering.
    _queryBaseItems     = queries.map(q => ({ index: q.index, query_body: q.query_body }));
    _queryTotalMatching = (data.per_index_meta || []).reduce((s, m) => s + (m.total || 0), 0);
  } catch (e) {
    setQueryResults('✗ Network error: ' + e.message);
  }
}

/* ── Time-range picker ───────────────────────────────────────────────────── */
function showTimeRangePicker() {
  const picker = document.getElementById('timeRangePicker');
  if (!picker) return;
  picker.classList.remove('d-none');
  onTimeRangeChanged();
}

function hideTimeRangePicker() {
  document.getElementById('timeRangePicker')?.classList.add('d-none');
}

function toggleTimeRangePicker() {
  const picker = document.getElementById('timeRangePicker');
  if (!picker) return;
  if (picker.classList.contains('d-none')) showTimeRangePicker();
  else hideTimeRangePicker();
}

const TIME_RANGE_IDS = ['startAfterValue', 'startBeforeValue', 'endAfterValue', 'endBeforeValue'];

/** Read the four time-range bounds { startAfter, startBefore, endAfter, endBefore }. */
function readTimeRange() {
  const v = (id) => document.getElementById(id)?.value || '';
  return { startAfter: v('startAfterValue'), startBefore: v('startBeforeValue'),
           endAfter:   v('endAfterValue'),   endBefore:   v('endBeforeValue') };
}

/** Validate the bounds: within each row, "after" must not exceed "before".
 *  Returns an error string, or '' when legal. datetime-local values compare
 *  correctly as strings (ISO format). Also paints the offending inputs red. */
function timeRangeError() {
  const t = readTimeRange();
  const mark = (ids, bad) => ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('is-invalid', bad);
  });
  let err = '';
  const startBad = !!(t.startAfter && t.startBefore && t.startAfter > t.startBefore);
  const endBad   = !!(t.endAfter   && t.endBefore   && t.endAfter   > t.endBefore);
  mark(['startAfterValue', 'startBeforeValue'], startBad);
  mark(['endAfterValue', 'endBeforeValue'], endBad);
  if (startBad) err = 'Illegal Started-At range — "after" is later than "before".';
  else if (endBad) err = 'Illegal Ended-At range — "after" is later than "before".';
  return err;
}

function clearTimeRange() {
  TIME_RANGE_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.value = ''; el.classList.remove('is-invalid'); }
  });
  document.getElementById('timeRangeLabel')?.classList.add('d-none');
  document.getElementById('timeRangeError')?.classList.add('d-none');
  _updateTimeRangeBtn();
}

function onTimeRangeChanged() {
  const t     = readTimeRange();
  const label = document.getElementById('timeRangeLabel');
  const errEl = document.getElementById('timeRangeError');

  const err = timeRangeError();
  if (errEl) {
    errEl.textContent = err;
    errEl.classList.toggle('d-none', !err);
  }

  if (label) {
    const fmt = (v) => v.replace('T', ' ');
    const parts = [];
    if (t.startAfter)  parts.push(`Started ≥ ${fmt(t.startAfter)}`);
    if (t.startBefore) parts.push(`Started ≤ ${fmt(t.startBefore)}`);
    if (t.endAfter)    parts.push(`Ended ≥ ${fmt(t.endAfter)}`);
    if (t.endBefore)   parts.push(`Ended ≤ ${fmt(t.endBefore)}`);
    if (parts.length && !err) { label.textContent = parts.join('  ·  '); label.classList.remove('d-none'); }
    else                      { label.classList.add('d-none'); }
  }
  _updateTimeRangeBtn();
}

function _updateTimeRangeBtn() {
  const t   = readTimeRange();
  const btn = document.getElementById('btnTimeRange');
  if (!btn) return;
  const active = !!(t.startAfter || t.startBefore || t.endAfter || t.endBefore);
  btn.innerHTML = active
    ? `<i class="bi bi-calendar-check-fill me-1 text-warning"></i>Time`
    : `<i class="bi bi-calendar-range me-1"></i>Time`;
}

/* ── Sort picker ─────────────────────────────────────────────────────────── */
function onSortChanged() {
  const hint = document.getElementById('sortHint')?.value ?? 'start';
  const grp  = document.getElementById('sortDirGroup');
  // Hide direction buttons when "None" is selected
  if (grp) grp.style.opacity = hint ? '1' : '0.35';
}

/* ── Attack-type picker ──────────────────────────────────────────────────── */
let availableAttackTypes = [];
let selectedAttackTypes  = new Set();

const TYPE_FALLBACK = [
  'DNS','WebDDoS','BehavioralDOS','SynFlood','Intrusions',
  'Anomalies','AntiScanning','ACL','StatefulACL','DOSShield','TrafficFilters',
];

async function loadAttackTypes() {
  // Already loaded — just re-render
  if (availableAttackTypes.length) { renderAttackTypeChips(); return; }
  try {
    const data = await api('/api/cc/attack-types');
    availableAttackTypes = data.types?.length ? data.types : TYPE_FALLBACK;
  } catch (_) {
    availableAttackTypes = TYPE_FALLBACK;
  }
  renderAttackTypeChips();
}

function renderAttackTypeChips() {
  const container = document.getElementById('attackTypeChips');
  if (!container) return;
  if (!availableAttackTypes.length) {
    container.innerHTML = '<span class="text-secondary small fst-italic">No types available</span>';
    return;
  }
  container.innerHTML = availableAttackTypes.map(t => {
    const sel  = selectedAttackTypes.has(t);
    const cls  = sel
      ? 'bg-primary text-white'
      : 'text-secondary border border-secondary';
    return `<span class="badge px-2 py-1 ${cls}"
      style="cursor:pointer;user-select:none;font-size:0.78rem;background:${sel ? '' : 'rgba(255,255,255,0.05)'}"
      onclick="toggleAttackType('${t}')">${esc(t)}</span>`;
  }).join('');

  // Update selected-types label
  const label = document.getElementById('selectedTypesLabel');
  if (label) {
    const sel = [...selectedAttackTypes];
    if (sel.length) {
      label.innerHTML =
        `<i class="bi bi-check2-circle me-1"></i><strong>${sel.length}</strong> type${sel.length > 1 ? 's' : ''} selected: ` +
        sel.map(t => `<span class="badge bg-primary me-1">${esc(t)}</span>`).join('');
      label.classList.remove('d-none');
    } else {
      label.classList.add('d-none');
    }
  }

  // Reflect selection count on the Types button
  const btn = document.getElementById('btnTypePicker');
  if (btn) {
    const n = selectedAttackTypes.size;
    btn.innerHTML = n
      ? `<i class="bi bi-tags-fill me-1 text-primary"></i>Types <span class="badge bg-primary ms-1">${n}</span>`
      : `<i class="bi bi-tags me-1"></i>Types`;
  }
}

function toggleAttackType(type) {
  if (selectedAttackTypes.has(type)) selectedAttackTypes.delete(type);
  else selectedAttackTypes.add(type);
  renderAttackTypeChips();
}

function clearAttackTypeSelection() {
  selectedAttackTypes.clear();
  renderAttackTypeChips();
}

function showAttackTypePicker() {
  const picker = document.getElementById('attackTypePicker');
  if (!picker) return;
  picker.classList.remove('d-none');
  loadAttackTypes();
}

function hideAttackTypePicker() {
  document.getElementById('attackTypePicker')?.classList.add('d-none');
}

function toggleAttackTypePicker() {
  const picker = document.getElementById('attackTypePicker');
  if (!picker) return;
  if (picker.classList.contains('d-none')) showAttackTypePicker();
  else hideAttackTypePicker();
}


/* ── Sidebar search ──────────────────────────────────────────────────────── */
document.getElementById('indexSearch').addEventListener('input', function () {
  const q = this.value.toLowerCase();
  renderSidebarIndices(allIndices.filter(i => i.name.toLowerCase().includes(q)));
});

/* ── NL input: auto-show pickers when trigger words are typed ─── */
document.getElementById('nlQueryInput').addEventListener('input', function () {
  const v = this.value;
  // Attack type picker
  if (/\btype\b|\bcategor/i.test(v)) showAttackTypePicker();

  // Time range picker
  if (/started\s+at|ended\s+at|start\s+time|end\s+time|since\b|\bbefore\b|\bafter\b|\btime\s+range\b/i.test(v)) {
    showTimeRangePicker();
  }
});

/* ══════════════════════════════════════════════════════════════════════════
   FEEDBACK & TOAST
   ══════════════════════════════════════════════════════════════════════════ */
function showFeedback(type, html) {
  const el = document.getElementById('connFeedback');
  el.className = `mt-3 alert alert-${type} py-2`;
  el.innerHTML = html;
  el.classList.remove('d-none');
}
function hideFeedback() {
  document.getElementById('connFeedback').classList.add('d-none');
}
function showToast(msg, bgClass = 'bg-dark') {
  const toastEl = document.getElementById('toastMsg');
  document.getElementById('toastBody').textContent = msg;
  toastEl.className = `toast align-items-center text-white border-0 ${bgClass}`;
  bootstrap.Toast.getOrCreateInstance(toastEl, { delay: 3000 }).show();
}

/* ══════════════════════════════════════════════════════════════════════════
   UTILITIES
   ══════════════════════════════════════════════════════════════════════════ */
async function api(url, opts = {}) {
  const res = await fetch(appUrl(url), opts);
  return res.json();
}
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function fmtTime(ts) {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleString(); } catch { return String(ts); }
}
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ══════════════════════════════════════════════════════════════════════════
   INIT — auto-connect from localStorage on page load
   ══════════════════════════════════════════════════════════════════════════ */
(async function init() {
  initUiPrefs();
  initQuerySplitter();
  initAutoRefresh();
  renderProfiles();

  // Try to restore last-used connection
  const saved = localStorage.getItem(LS_ACTIVE);
  if (saved) {
    try {
      const settings = JSON.parse(saved);
      fillForm(settings);

      // Check if ES is still reachable with existing server-side state
      const health = await api('/api/health');
      if (health.connected) {
        isConnected = true;
        onConnected(settings, {
          cluster_name: health.cluster_name,
          es_version:   health.es_version,
        });
        showView('dashboard');
        refreshAll();
        return;
      }

      // Server lost state (e.g. restart) — reconnect silently
      const res  = await fetch(appUrl('/api/connect'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      });
      const data = await res.json();
      if (data.connected) {
        isConnected = true;
        onConnected(settings, data);
        showView('dashboard');
        refreshAll();
        return;
      }
    } catch (_) {}
  }

  // No saved connection — show connection settings view
  isConnected = false;
  onDisconnected();
  showView('connection');
})();

