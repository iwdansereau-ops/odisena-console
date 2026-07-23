// Odisena Console — client app
(function () {
  'use strict';

  const state = {
    catalog: null,
    view: 'home',
    history: ['home'],
    filters: { runbooks: 'all', artifacts: 'all' },
    system: { kind: 'all', query: '', mode: 'map', selected: null },
    theme: null,
  };

  // ===== Label formatting (acronym-aware) =====
  const ACRONYMS = {
    otel: 'OTel', otlp: 'OTLP', rds: 'RDS', ddl: 'DDL', iam: 'IAM',
    aws: 'AWS', ci: 'CI', sop: 'SOP', postgresql: 'PostgreSQL', '2tb': '2TB',
    oidc: 'OIDC', ddb: 'DDB', gha: 'GHA', gc: 'GC', pr: 'PR', e2e: 'E2E',
    gh: 'GH', sarif: 'SARIF', pprof: 'pprof', pg: 'PG',
  };
  function formatLabel(s) {
    return String(s || '').split(/(\s+)/).map(tok => {
      const key = tok.toLowerCase();
      if (ACRONYMS[key]) return ACRONYMS[key];
      const m = key.match(/^(otel|otlp)(.+)$/);
      if (m) return (m[1] === 'otel' ? 'OTel' : 'OTLP') + tok.slice(m[1].length);
      return tok;
    }).join('');
  }

  // ===== System model (static navigation surface) =====
  // Hand-authored from the Odisena master registry (2026-07-23 cutoff). This is
  // a recorded navigation model, NOT a live health/telemetry feed. States are
  // deliberately precise and are never reconciled automatically.
  const SYSTEM_STATES = {
    live:     { label: 'Live',         color: '#10b981', desc: 'Confirmed live / deployed and protected.' },
    preview:  { label: 'Preview',      color: '#3b82f6', desc: 'Built and preview-deployed; production domain / device gates pending.' },
    held:     { label: 'Held',         color: '#f59e0b', desc: 'Code green or ready, but promotion / rollout is intentionally held.' },
    blocked:  { label: 'Blocked',      color: '#dc2626', desc: 'Blocked on an authority-recovery or upstream dependency.' },
    proposed: { label: 'Proposed',     color: '#6b7280', desc: 'Proposed / readiness concept; not started.' },
    advisory: { label: 'Advisory',     color: '#8b5cf6', desc: 'Advisory-recorded from a non-live source; not directly confirmed.' },
    notexec:  { label: 'Not executed', color: '#a3a19c', desc: 'Defined but not yet executed.' },
    active:   { label: 'Active',       color: '#0891b2', desc: 'Active engineering corpus / repository.' },
  };

  const SYSTEM_KINDS = {
    engineering: 'Engineering domains',
    repo:        'Repositories',
    product:     'Products',
    surface:     'Deployment surfaces',
  };
  const SYSTEM_KIND_ORDER = ['engineering', 'repo', 'product', 'surface'];

  // Public system map. Scoped to the public engineering portfolio only.
  // Only already-public products/surfaces are modeled. Internal product
  // roadmap, private registries/links, DNS/authority-recovery detail,
  // unregistered-domain plans and legal/IP governance are intentionally NOT
  // modeled here — they are not appropriate for a public surface.
  const SYSTEM = {
    nodes: [
      // Engineering domains
      { id: 'eng-otel', label: 'OpenTelemetry Collector', kind: 'engineering', state: 'active', sub: 'Performance, drift & benchmark governance', detail: 'OTel Collector performance work: sharded state cache, OTTL auditor, benchmark aggregation and CI noise-floor analysis. Source corpus only — no production-deployment claim.' },
      { id: 'eng-rds', label: 'PostgreSQL RDS Migration', kind: 'engineering', state: 'active', sub: 'Zero-downtime DDL & 2TB backfill', detail: 'RDS Postgres migration runbooks and preflight tooling: hygiene checks, config verification, dashboard writers and structured run logging.' },
      { id: 'eng-iam', label: 'IAM & AWS Security', kind: 'engineering', state: 'active', sub: 'OIDC, DDB auditor & federated roles', detail: 'IAM security tooling: DynamoDB IAM refactor, resource auditor, GitHub OIDC↔AWS federation and federated-role audits. Illustrative; not a live audit of any account.' },

      // Products
      { id: 'prod-console', label: 'Odisena Console', kind: 'product', state: 'live', sub: 'This app', url: 'https://console.odisena.com', detail: 'Read-only engineering command center and system-navigation surface (this PWA). Published on GitHub Pages at console.odisena.com.' },
      { id: 'prod-helios', label: 'Helios 1 (Cool Wake)', kind: 'product', state: 'live', sub: 'WebXR level · live', url: 'https://helios.odisena.com', detail: 'Cool Wake — the first playable Helios 1 level, in WebXR. Promoted to production and publicly served at helios.odisena.com.' },

      // Deployment surfaces (public, live domains only)
      { id: 'srf-apex', label: 'odisena.com', kind: 'surface', state: 'live', sub: 'Apex · live', url: 'https://odisena.com', detail: 'Apex production domain. Live and publicly served. DNS is managed outside this repository.' },
      { id: 'srf-console', label: 'console.odisena.com', kind: 'surface', state: 'live', sub: 'This console · GitHub Pages', url: 'https://console.odisena.com', detail: 'Custom-domain binding for this console, served from GitHub Pages via the committed CNAME. DNS is managed outside this repository.' },
      { id: 'srf-chronicle', label: 'chronicle.odisena.com', kind: 'surface', state: 'live', sub: 'Identity & Chronicle · live', url: 'https://chronicle.odisena.com', detail: 'Identity and Chronicle surface. Publicly served. DNS is managed outside this repository.' },
      { id: 'srf-helios', label: 'helios.odisena.com', kind: 'surface', state: 'live', sub: 'Helios 1 XR · live', url: 'https://helios.odisena.com', detail: 'Production domain for the Helios 1 WebXR level (Cool Wake). Publicly served via Vercel. DNS is managed outside this repository.' },
      { id: 'srf-library', label: 'library.odisena.com', kind: 'surface', state: 'live', sub: 'Library of Alexandria · live', url: 'https://library.odisena.com', detail: 'The Library of Alexandria shelf at its odisena.com home. Publicly served via Vercel. DNS is managed outside this repository.' },
      { id: 'srf-readiness', label: 'readiness.odisena.com', kind: 'surface', state: 'live', sub: 'Readiness scanner · live', url: 'https://readiness.odisena.com', detail: 'Ubuntu 26.04 LTS upgrade readiness scanner. Publicly served via Vercel. DNS is managed outside this repository.' },

      // Repositories (public source of this console)
      { id: 'repo-console', label: 'odisena-console', kind: 'repo', state: 'active', sub: 'This console', url: 'https://github.com/iwdansereau-ops/odisena-console', detail: 'Public source repository for this console.' },
    ],
    links: [
      ['eng-otel', 'prod-console'], ['eng-rds', 'prod-console'], ['eng-iam', 'prod-console'],
      ['repo-console', 'srf-console'], ['prod-console', 'srf-console'],
      ['prod-helios', 'srf-helios'],
    ],
  };
  const SYSTEM_BY_ID = Object.fromEntries(SYSTEM.nodes.map(n => [n.id, n]));
  const SYSTEM_ADJ = (() => {
    const adj = {};
    SYSTEM.nodes.forEach(n => { adj[n.id] = new Set(); });
    SYSTEM.links.forEach(([a, b]) => { if (adj[a] && adj[b]) { adj[a].add(b); adj[b].add(a); } });
    return adj;
  })();

  // Home status summary + Ops rows draw from these curated id sets.
  const HOME_STATUS_IDS = ['srf-apex', 'srf-console', 'srf-chronicle', 'srf-helios', 'srf-library', 'srf-readiness'];

  // ===== Synthetic / sensitive classification =====
  function isSynthetic(item) {
    return !!(item && (item.synthetic || /sample/i.test(item.name || '')));
  }
  function isSensitive(a) {
    if (!a) return false;
    if (a.category === 'iam') return true;
    return /oidc|iam|audit|sarif|terraform|governance|role/i.test(a.name || '');
  }
  const UUID_FILE = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\.[a-z0-9]+$/i;
  function cleanName(name) {
    return String(name || '').replace(/^[a-f0-9]{8}__/, '');
  }
  function artifactLabel(a) {
    const clean = cleanName(a.name);
    if (UUID_FILE.test(clean)) return 'Image asset';
    return formatLabel(a.display_name || clean);
  }

  // ===== Theme =====
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    state.theme = t;
    const btn = document.getElementById('theme-toggle');
    if (btn) {
      const dark = t === 'dark';
      btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
      btn.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
    }
    try { window.__theme = t; } catch (e) {}
  }
  function initTheme() {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(prefersDark ? 'dark' : 'light');
  }
  document.getElementById('theme-toggle').addEventListener('click', () => {
    applyTheme(state.theme === 'dark' ? 'light' : 'dark');
  });

  // ===== Navigation =====
  function show(view, pushHistory = true) {
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('view-active', v.dataset.view === view));
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('tab-active', t.dataset.nav === view));
    state.view = view;
    if (pushHistory && state.history[state.history.length - 1] !== view) state.history.push(view);
    window.scrollTo(0, 0);
    if (location.hash !== '#/' + view) history.replaceState(null, '', '#/' + view);
    if (view === 'system' && state.system.mode === 'map') requestAnimationFrame(drawSystemEdges);
  }

  function goBack() {
    if (state.history.length > 1) {
      state.history.pop();
      const prev = state.history[state.history.length - 1];
      show(prev, false);
    } else {
      show('home', false);
    }
  }

  document.querySelectorAll('[data-nav]').forEach(el => {
    el.addEventListener('click', () => {
      const target = el.dataset.nav;
      if (el.dataset.filter) {
        if (target === 'runbooks') state.filters.runbooks = el.dataset.filter;
        if (target === 'artifacts') state.filters.artifacts = el.dataset.filter;
        renderChips();
        renderRunbooks();
        renderArtifacts();
      }
      show(target);
    });
  });

  document.querySelectorAll('[data-back]').forEach(b => b.addEventListener('click', goBack));

  // ===== Data =====
  async function loadCatalog() {
    const res = await fetch('catalog.json');
    state.catalog = await res.json();
    renderStats();
    renderCategories();
    renderChips();
    renderRunbooks();
    renderArtifacts();
    renderSessions();
    renderOps();
    renderRecentSessions();
  }

  function renderStats() {
    const s = state.catalog.stats;
    document.querySelector('[data-stat="sessions"]').textContent = s.total_sessions;
    document.querySelector('[data-stat="runbooks"]').textContent = s.total_runbooks;
    document.querySelector('[data-stat="artifacts"]').textContent = s.total_artifacts;
  }

  function renderCategories() {
    const grid = document.getElementById('cat-grid');
    grid.innerHTML = '';
    Object.entries(state.catalog.categories).forEach(([key, cat]) => {
      const btn = document.createElement('button');
      btn.className = 'cat-card';
      btn.innerHTML = `
        <div class="cat-dot" style="background:${cat.color}"></div>
        <div class="cat-body">
          <div class="cat-name">${escapeHtml(cat.name)}</div>
          <div class="cat-meta">${cat.count} files</div>
        </div>
        <svg class="cat-arrow" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg>
      `;
      btn.addEventListener('click', () => {
        state.filters.artifacts = key;
        state.filters.runbooks = key;
        renderChips();
        renderRunbooks();
        renderArtifacts();
        show('artifacts');
      });
      grid.appendChild(btn);
    });
  }

  function renderChips() {
    const cats = Object.entries(state.catalog.categories);

    ['runbook', 'artifact'].forEach(kind => {
      const container = document.getElementById(kind + '-chips');
      if (!container) return;
      container.innerHTML = '';
      const filterKey = kind + 's';
      const items = [['all', 'All']].concat(cats.map(([k, c]) => [k, c.name]));
      items.forEach(([key, label]) => {
        const chip = document.createElement('button');
        chip.className = 'chip' + (state.filters[filterKey] === key ? ' chip-active' : '');
        chip.textContent = label;
        chip.addEventListener('click', () => {
          state.filters[filterKey] = key;
          renderChips();
          if (kind === 'runbook') renderRunbooks();
          else renderArtifacts();
        });
        container.appendChild(chip);
      });
    });
  }

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(2) + ' MB';
  }

  function docIcon(name) {
    const n = name.toLowerCase();
    if (n.includes('runbook')) return '📘';
    if (n.includes('playbook')) return '📗';
    if (n.includes('memo') || n.includes('brief')) return '📝';
    if (n.includes('analysis') || n.includes('report') || n.includes('scorecard')) return '📊';
    if (n.includes('strategy') || n.includes('plan')) return '🎯';
    if (n.includes('dashboard')) return '📈';
    if (n.includes('stress') || n.includes('load')) return '💪';
    if (n.includes('sop') || n.includes('canary')) return '🚦';
    if (n.includes('regression') || n.includes('drift')) return '🔬';
    return '📄';
  }

  function renderRunbooks() {
    const list = document.getElementById('runbook-list');
    const q = (document.getElementById('runbook-search').value || '').toLowerCase();
    const filter = state.filters.runbooks;
    const items = state.catalog.runbooks.filter(r => {
      if (filter !== 'all' && r.category !== filter) return false;
      if (q && !r.display_name.toLowerCase().includes(q) && !r.session_title.toLowerCase().includes(q)) return false;
      return true;
    });
    if (!items.length) {
      list.innerHTML = '<div class="empty"><div class="empty-emoji">📭</div>No runbooks match</div>';
      return;
    }
    list.innerHTML = '';
    items.forEach(r => {
      const cat = state.catalog.categories[r.category];
      const btn = document.createElement('button');
      btn.className = 'doc-item';
      const badge = isSynthetic(r) ? '<span class="badge badge-sample">Sample</span>' : '';
      btn.innerHTML = `
        <div class="doc-icon">${docIcon(r.name)}</div>
        <div class="doc-body">
          <div class="doc-name">${escapeHtml(formatLabel(r.display_name))}${badge}</div>
          <div class="doc-meta">
            <span class="doc-cat-dot" style="background:${cat.color}"></span>
            ${escapeHtml(cat.name)} · ${fmtSize(r.size)}
          </div>
        </div>
      `;
      btn.addEventListener('click', () => openRunbook(r));
      list.appendChild(btn);
    });
  }

  async function openRunbook(r) {
    document.getElementById('reader-title').textContent = formatLabel(r.display_name);
    const banner = document.getElementById('reader-banner');
    if (isSynthetic(r)) {
      banner.hidden = false;
      banner.className = 'sample-banner';
      banner.innerHTML = '<strong>Sample data.</strong> This is a synthetic demonstration report. The findings, hostnames, and figures are illustrative and do not represent any real incident, environment, or production data.';
    } else {
      banner.hidden = true;
      banner.className = '';
      banner.innerHTML = '';
    }
    const body = document.getElementById('reader-body');
    body.innerHTML = '<div class="empty"><div class="empty-emoji">⏳</div>Loading…</div>';
    show('reader');
    try {
      const res = await fetch(r.path);
      const md = await res.text();
      body.innerHTML = sanitizeHtml(window.marked.parse(md));
    } catch (e) {
      body.innerHTML = '<div class="empty"><div class="empty-emoji">⚠️</div>Failed to load</div>';
    }
  }

  function renderArtifacts() {
    const list = document.getElementById('artifact-list');
    const q = (document.getElementById('artifact-search').value || '').toLowerCase();
    const filter = state.filters.artifacts;
    const items = state.catalog.artifacts.filter(a => {
      if (filter !== 'all' && a.category !== filter) return false;
      if (q && !a.display_name.toLowerCase().includes(q) && !a.name.toLowerCase().includes(q) && !a.session_title.toLowerCase().includes(q)) return false;
      return true;
    });
    if (!items.length) {
      list.innerHTML = '<div class="empty"><div class="empty-emoji">📭</div>No artifacts match</div>';
      return;
    }
    list.innerHTML = '';
    items.forEach(a => {
      const btn = document.createElement('button');
      btn.className = 'artifact-item';
      btn.type = 'button';
      const label = artifactLabel(a);
      const badges = (isSynthetic(a) ? '<span class="badge badge-sample">Sample</span>' : '') +
        (isSensitive(a) ? '<span class="badge badge-secure">Security</span>' : '');
      btn.innerHTML = `
        <div class="ext-badge ext-${a.ext}">${escapeHtml(a.ext)}</div>
        <div class="artifact-body">
          <div class="artifact-name">${escapeHtml(label)}${badges}</div>
          <div class="artifact-meta">${escapeHtml(formatLabel(a.session_title))}</div>
        </div>
        <svg class="artifact-arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
      `;
      btn.addEventListener('click', () => openArtifact(a));
      list.appendChild(btn);
    });
  }

  function openArtifact(a) {
    const body = document.getElementById('artifact-detail-body');
    const cat = state.catalog.categories[a.category];
    const label = artifactLabel(a);
    const dlName = cleanName(a.name);
    let notes = '';
    if (isSynthetic(a)) {
      notes += '<div class="sample-banner"><strong>Sample data.</strong> Synthetic demonstration artifact — not real findings or production data.</div>';
    }
    if (isSensitive(a)) {
      notes += '<div class="secure-banner"><strong>Security-related.</strong> Example security tooling or output shared for portfolio review. Review before any use; it is not a live audit of any system.</div>';
    }
    body.innerHTML = `
      <div class="detail-head">
        <div class="ext-badge ext-${a.ext}">${escapeHtml(a.ext)}</div>
        <div>
          <div class="detail-title">${escapeHtml(label)}</div>
          <div class="detail-sub">${escapeHtml(cat ? cat.name : '')}</div>
        </div>
      </div>
      ${notes}
      <dl class="detail-meta">
        <div><dt>Type</dt><dd>${escapeHtml(a.ext.toUpperCase())} file</dd></div>
        <div><dt>Size</dt><dd>${fmtSize(a.size)}</dd></div>
        <div><dt>From session</dt><dd>${escapeHtml(formatLabel(a.session_title))}</dd></div>
      </dl>
      <a class="download-btn" href="${escapeHtml(a.path)}" download="${escapeHtml(dlName)}">Download ${escapeHtml(a.ext.toUpperCase())} · ${fmtSize(a.size)}</a>
      <p class="detail-hint">Served from this site. Filenames are cleaned of internal identifiers before download.</p>
    `;
    show('artifact-detail');
  }

  function renderSessions() {
    const list = document.getElementById('session-list');
    const q = (document.getElementById('session-search').value || '').toLowerCase();
    const items = state.catalog.sessions.filter(s => {
      if (q && !s.title.toLowerCase().includes(q)) return false;
      return true;
    });
    if (!items.length) {
      list.innerHTML = '<div class="empty"><div class="empty-emoji">📭</div>No sessions</div>';
      return;
    }
    list.innerHTML = '';
    items.forEach(s => renderSessionItem(list, s));
  }

  function renderRecentSessions() {
    const list = document.getElementById('session-list-preview');
    list.innerHTML = '';
    state.catalog.sessions.slice(0, 5).forEach(s => renderSessionItem(list, s));
    const more = document.createElement('button');
    more.className = 'doc-item';
    more.innerHTML = `<div class="doc-icon">→</div><div class="doc-body"><div class="doc-name">View all sessions</div><div class="doc-meta">${state.catalog.sessions.length} total</div></div>`;
    more.addEventListener('click', () => show('sessions'));
    list.appendChild(more);
  }

  function renderSessionItem(container, s) {
    const cat = state.catalog.categories[s.category];
    const item = document.createElement('button');
    item.className = 'session-item';
    const chips = s.files.slice(0, 4).map(f => {
      const c = cleanName(f);
      const label = UUID_FILE.test(c) ? 'image' : c.slice(0, 24);
      return `<span class="session-file-chip">${escapeHtml(label)}</span>`;
    }).join('');
    const more = s.files.length > 4 ? `<span class="session-file-chip">+${s.files.length - 4}</span>` : '';
    item.innerHTML = `
      <div class="session-title">${escapeHtml(s.title)}</div>
      <div class="session-meta">
        <span class="session-cat-dot" style="background:${cat.color}"></span>
        ${escapeHtml(cat.name)} · ${s.files.length} files
      </div>
      <div class="session-files">${chips}${more}</div>
    `;
    item.addEventListener('click', () => {
      // Filter artifacts by this session's files
      state.filters.artifacts = 'all';
      document.getElementById('artifact-search').value = s.title.split(' ').slice(0, 2).join(' ');
      renderChips();
      renderArtifacts();
      show('artifacts');
    });
    container.appendChild(item);
  }

  function renderOps() {
    const tags = document.getElementById('ops-workflows');
    const workflows = state.catalog.artifacts
      .filter(a => a.ext === 'yml' || a.ext === 'yaml')
      .map(a => a.name.replace(/^[a-f0-9]{8}__/, '').replace(/\.(yml|yaml)$/, ''));
    const unique = [...new Set(workflows)].sort();
    tags.innerHTML = unique.map(w => `<span class="ops-tag">${escapeHtml(formatLabel(w))}</span>`).join('');
  }

  // ===== System map rendering =====
  function stateMeta(key) { return SYSTEM_STATES[key] || SYSTEM_STATES.proposed; }
  function stateBadge(key) {
    const s = stateMeta(key);
    return `<span class="state-badge" data-state="${key}"><span class="state-dot" style="background:${s.color}"></span>${escapeHtml(s.label)}</span>`;
  }

  function filteredSystemNodes() {
    const { kind, query } = state.system;
    const q = query.trim().toLowerCase();
    return SYSTEM.nodes.filter(n => {
      if (kind !== 'all' && n.kind !== kind) return false;
      if (q && !(n.label.toLowerCase().includes(q) || (n.sub || '').toLowerCase().includes(q) || (n.detail || '').toLowerCase().includes(q))) return false;
      return true;
    });
  }

  function renderSystemLegend() {
    const ul = document.getElementById('system-legend-list');
    if (!ul) return;
    ul.innerHTML = Object.entries(SYSTEM_STATES).map(([k, s]) =>
      `<li><span class="state-dot" style="background:${s.color}"></span><strong>${escapeHtml(s.label)}</strong> — ${escapeHtml(s.desc)}</li>`
    ).join('');
  }

  function renderSystemChips() {
    const row = document.getElementById('system-chips');
    if (!row) return;
    row.innerHTML = '';
    const items = [['all', 'All']].concat(SYSTEM_KIND_ORDER.map(k => [k, SYSTEM_KINDS[k]]));
    items.forEach(([key, label]) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (state.system.kind === key ? ' chip-active' : '');
      chip.textContent = label;
      chip.setAttribute('aria-pressed', state.system.kind === key ? 'true' : 'false');
      chip.addEventListener('click', () => {
        state.system.kind = key;
        renderSystemChips();
        renderSystemGraph();
      });
      row.appendChild(chip);
    });
  }

  // Renders whichever mode is active (map or list) plus keeps the other in sync.
  function renderSystemGraph() {
    renderSystemMap();
    renderSystemList();
  }

  function renderSystemMap() {
    const map = document.getElementById('system-map');
    if (!map) return;
    const nodes = filteredSystemNodes();
    map.innerHTML = '';
    if (!nodes.length) {
      map.innerHTML = '<div class="empty"><div class="empty-emoji">📭</div>No nodes match</div>';
      return;
    }
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'sys-edges');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    map.appendChild(svg);

    const cols = document.createElement('div');
    cols.className = 'sys-cols';
    const present = SYSTEM_KIND_ORDER.filter(k => nodes.some(n => n.kind === k));
    present.forEach(kind => {
      const col = document.createElement('div');
      col.className = 'sys-col';
      const h = document.createElement('div');
      h.className = 'sys-col-title';
      h.textContent = SYSTEM_KINDS[kind];
      col.appendChild(h);
      nodes.filter(n => n.kind === kind).forEach(n => col.appendChild(makeNodeButton(n, 'sys-node')));
      cols.appendChild(col);
    });
    map.appendChild(cols);
    requestAnimationFrame(drawSystemEdges);
  }

  function makeNodeButton(n, cls) {
    const s = stateMeta(n.state);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = cls + (state.system.selected === n.id ? ' is-selected' : '');
    btn.dataset.nodeId = n.id;
    btn.setAttribute('aria-label', `${n.label}. ${SYSTEM_KINDS[n.kind]}. State: ${s.label}.`);
    btn.innerHTML =
      `<span class="sys-node-dot" style="background:${s.color}"></span>` +
      `<span class="sys-node-body"><span class="sys-node-label">${escapeHtml(n.label)}</span>` +
      `<span class="sys-node-state">${escapeHtml(s.label)}</span></span>`;
    btn.addEventListener('click', () => selectSystemNode(n.id));
    return btn;
  }

  function drawSystemEdges() {
    const map = document.getElementById('system-map');
    const svg = map && map.querySelector('.sys-edges');
    if (!svg) return;
    const mapRect = map.getBoundingClientRect();
    if (!mapRect.width) return;
    svg.setAttribute('width', map.scrollWidth);
    svg.setAttribute('height', map.scrollHeight);
    svg.setAttribute('viewBox', `0 0 ${map.scrollWidth} ${map.scrollHeight}`);
    const centre = id => {
      const el = map.querySelector(`[data-node-id="${CSS.escape(id)}"]`);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        x: r.left - mapRect.left + r.width / 2,
        y: r.top - mapRect.top + r.height / 2,
        w: r.width,
      };
    };
    const sel = state.system.selected;
    let paths = '';
    SYSTEM.links.forEach(([a, b]) => {
      const p1 = centre(a), p2 = centre(b);
      if (!p1 || !p2) return;
      const active = sel && (a === sel || b === sel);
      const x1 = p1.x, x2 = p2.x;
      const dx = Math.max(24, Math.abs(x2 - x1) / 2);
      const d = `M ${x1} ${p1.y} C ${x1 + dx} ${p1.y}, ${x2 - dx} ${p2.y}, ${x2} ${p2.y}`;
      paths += `<path d="${d}" class="sys-edge${active ? ' sys-edge-active' : ''}" fill="none" />`;
    });
    svg.innerHTML = paths;
  }

  function renderSystemList() {
    const list = document.getElementById('system-list');
    if (!list) return;
    const nodes = filteredSystemNodes();
    list.innerHTML = '';
    if (!nodes.length) {
      list.innerHTML = '<div class="empty"><div class="empty-emoji">📭</div>No nodes match</div>';
      return;
    }
    const present = SYSTEM_KIND_ORDER.filter(k => nodes.some(n => n.kind === k));
    present.forEach(kind => {
      const group = document.createElement('div');
      group.className = 'sys-group';
      const h = document.createElement('h2');
      h.className = 'sys-group-title';
      h.textContent = SYSTEM_KINDS[kind];
      group.appendChild(h);
      nodes.filter(n => n.kind === kind).forEach(n => {
        const item = document.createElement('div');
        item.className = 'sys-row';
        const rels = [...SYSTEM_ADJ[n.id]].map(id => {
          const t = SYSTEM_BY_ID[id];
          return `<button type="button" class="sys-rel" data-sysnode="${id}">${escapeHtml(t.label)}</button>`;
        }).join('');
        const link = n.url ? `<a class="sys-row-link" href="${escapeHtml(n.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : '';
        item.innerHTML =
          `<div class="sys-row-head"><span class="sys-row-name">${escapeHtml(n.label)}</span>${stateBadge(n.state)}</div>` +
          (n.sub ? `<div class="sys-row-sub">${escapeHtml(n.sub)}</div>` : '') +
          `<div class="sys-row-detail">${escapeHtml(n.detail || '')}</div>` +
          (rels ? `<div class="sys-row-rels"><span class="sys-row-rels-label">Connects to</span>${rels}</div>` : '') +
          link;
        group.appendChild(item);
      });
      list.appendChild(group);
    });
    list.querySelectorAll('[data-sysnode]').forEach(b =>
      b.addEventListener('click', () => selectSystemNode(b.dataset.sysnode)));
  }

  function selectSystemNode(id) {
    const n = SYSTEM_BY_ID[id];
    const panel = document.getElementById('system-detail');
    if (!n || !panel) return;
    state.system.selected = id;
    const s = stateMeta(n.state);
    const rels = [...SYSTEM_ADJ[id]].map(rid => {
      const t = SYSTEM_BY_ID[rid];
      return `<button type="button" class="sys-rel" data-sysnode="${rid}">${escapeHtml(t.label)}</button>`;
    }).join('');
    panel.hidden = false;
    panel.innerHTML =
      `<div class="sys-detail-head"><span class="sys-detail-kind">${escapeHtml(SYSTEM_KINDS[n.kind])}</span>` +
      `<button class="sys-detail-close" type="button" aria-label="Clear selection">×</button></div>` +
      `<div class="sys-detail-title">${escapeHtml(n.label)}</div>` +
      `<div class="sys-detail-state">${stateBadge(n.state)}<span class="sys-detail-state-desc">${escapeHtml(s.desc)}</span></div>` +
      `<p class="sys-detail-body">${escapeHtml(n.detail || '')}</p>` +
      (n.url ? `<a class="sys-detail-link" href="${escapeHtml(n.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(n.url.replace(/^https?:\/\//, ''))} ↗</a>` : '') +
      (rels ? `<div class="sys-row-rels"><span class="sys-row-rels-label">Connects to</span>${rels}</div>` : '');
    panel.querySelector('.sys-detail-close').addEventListener('click', () => {
      state.system.selected = null;
      panel.hidden = true;
      renderSystemMap();
      renderSystemList();
    });
    panel.querySelectorAll('[data-sysnode]').forEach(b =>
      b.addEventListener('click', () => selectSystemNode(b.dataset.sysnode)));
    // Re-render so selection highlight + edge emphasis update.
    renderSystemMap();
    renderSystemList();
    panel.focus();
    panel.scrollIntoView({ block: 'nearest' });
  }

  function openSystemNode(id) {
    show('system');
    setSystemMode('list');
    selectSystemNode(id);
  }

  function setSystemMode(mode) {
    state.system.mode = mode;
    const map = document.getElementById('system-map-wrap');
    const list = document.getElementById('system-list');
    const bMap = document.getElementById('sys-mode-map');
    const bList = document.getElementById('sys-mode-list');
    const isMap = mode === 'map';
    if (map) map.hidden = !isMap;
    if (list) list.hidden = isMap;
    if (bMap) { bMap.classList.toggle('seg-active', isMap); bMap.setAttribute('aria-pressed', String(isMap)); }
    if (bList) { bList.classList.toggle('seg-active', !isMap); bList.setAttribute('aria-pressed', String(!isMap)); }
    if (isMap) requestAnimationFrame(drawSystemEdges);
  }

  function renderSystem() {
    renderSystemLegend();
    renderSystemChips();
    renderSystemGraph();
    setSystemMode(state.system.mode);
  }

  function statusRow(n, interactive) {
    const s = stateMeta(n.state);
    const tag = interactive ? 'button' : 'div';
    const attr = interactive ? ` type="button" data-sysnode="${n.id}"` : '';
    return `<${tag} class="status-row"${attr}>` +
      `<span class="state-dot" style="background:${s.color}"></span>` +
      `<span class="status-row-body"><span class="status-row-name">${escapeHtml(n.label)}</span>` +
      `<span class="status-row-sub">${escapeHtml(n.sub || '')}</span></span>` +
      `<span class="status-row-state">${escapeHtml(s.label)}</span></${tag}>`;
  }

  function renderHomeSystem() {
    const grid = document.getElementById('home-system');
    if (!grid) return;
    grid.innerHTML = HOME_STATUS_IDS.map(id => SYSTEM_BY_ID[id]).filter(Boolean)
      .map(n => statusRow(n, true)).join('');
    grid.querySelectorAll('[data-sysnode]').forEach(b =>
      b.addEventListener('click', () => openSystemNode(b.dataset.sysnode)));
  }

  function renderOpsStatus() {
    const map = { 'ops-surfaces': 'surface' };
    Object.entries(map).forEach(([elId, kind]) => {
      const el = document.getElementById(elId);
      if (!el) return;
      el.className = 'status-grid';
      el.innerHTML = SYSTEM.nodes.filter(n => n.kind === kind).map(n => statusRow(n, false)).join('');
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Defense-in-depth allowlist sanitizer for the HTML that marked.parse() emits.
  // Runbook markdown is first-party/trusted, but rendered HTML is never assigned
  // to innerHTML raw: we reparse it in an inert document and drop any tag,
  // attribute, or URL scheme outside the allowlist below. Pure DOM, no network.
  const SANITIZE_TAGS = new Set([
    'a', 'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'em', 'strong', 'b', 'i',
    's', 'del', 'ins', 'sup', 'sub', 'span', 'div', 'img', 'kbd', 'samp',
    'var', 'mark', 'abbr', 'figure', 'figcaption', 'caption', 'dl', 'dt', 'dd',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'col', 'colgroup',
  ]);
  const SANITIZE_GLOBAL_ATTR = new Set(['class', 'id', 'title', 'lang', 'dir', 'align']);
  const SANITIZE_TAG_ATTR = {
    a: new Set(['href']),
    img: new Set(['src', 'alt', 'width', 'height']),
    td: new Set(['colspan', 'rowspan', 'scope']),
    th: new Set(['colspan', 'rowspan', 'scope']),
    col: new Set(['span']),
    colgroup: new Set(['span']),
    ol: new Set(['start', 'type']),
  };
  const SANITIZE_URL_ATTR = { a: 'href', img: 'src' };
  const SAFE_SCHEMES = new Set(['http', 'https', 'mailto', 'tel']);

  function isSafeUrl(value) {
    // Strip ASCII whitespace/control chars browsers ignore when parsing a
    // scheme (defeats "java\tscript:" and entity-decoded tricks).
    const cleaned = String(value == null ? '' : value).replace(/[\u0000-\u0020\u007f]/g, '');
    const scheme = cleaned.match(/^([a-z][a-z0-9+.-]*):/i);
    if (!scheme) return true; // relative path, anchor, query, or protocol-relative
    return SAFE_SCHEMES.has(scheme[1].toLowerCase());
  }

  function sanitizeHtml(dirty) {
    const doc = new DOMParser().parseFromString(String(dirty), 'text/html');
    const walk = node => {
      Array.from(node.children).forEach(el => {
        const tag = el.tagName.toLowerCase();
        if (!SANITIZE_TAGS.has(tag)) {
          el.remove(); // drops the element and its contents (script/style/iframe/…)
          return;
        }
        Array.from(el.attributes).forEach(attr => {
          const name = attr.name.toLowerCase();
          const allowed = !name.startsWith('on') &&
            (SANITIZE_GLOBAL_ATTR.has(name) ||
              (SANITIZE_TAG_ATTR[tag] && SANITIZE_TAG_ATTR[tag].has(name)));
          if (!allowed) el.removeAttribute(attr.name);
        });
        const urlAttr = SANITIZE_URL_ATTR[tag];
        if (urlAttr && el.hasAttribute(urlAttr) && !isSafeUrl(el.getAttribute(urlAttr))) {
          el.removeAttribute(urlAttr);
        }
        walk(el);
      });
    };
    walk(doc.body);
    return doc.body.innerHTML;
  }

  // Search handlers
  document.getElementById('runbook-search').addEventListener('input', renderRunbooks);
  document.getElementById('artifact-search').addEventListener('input', renderArtifacts);
  document.getElementById('session-search').addEventListener('input', renderSessions);
  document.getElementById('system-search').addEventListener('input', e => {
    state.system.query = e.target.value || '';
    renderSystemGraph();
  });
  document.getElementById('sys-mode-map').addEventListener('click', () => setSystemMode('map'));
  document.getElementById('sys-mode-list').addEventListener('click', () => setSystemMode('list'));
  window.addEventListener('resize', () => {
    if (state.view === 'system' && state.system.mode === 'map') requestAnimationFrame(drawSystemEdges);
  });

  // Hash routing (Safari back-swipe returns to home)
  window.addEventListener('hashchange', () => {
    const v = location.hash.replace('#/', '') || 'home';
    if (['home', 'runbooks', 'artifacts', 'sessions', 'ops', 'system', 'reader', 'artifact-detail'].includes(v)) {
      show(v, false);
    }
  });

  // iOS install banner (in-memory state; sandboxed iframe blocks localStorage)
  function initInstallBanner() {
    const banner = document.getElementById('install-banner');
    if (!banner) return;
    const ua = navigator.userAgent || '';
    const isIOS = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
    const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
    if (isIOS && !standalone) {
      setTimeout(() => banner.hidden = false, 1500);
    }
    document.getElementById('install-close').addEventListener('click', () => banner.hidden = true);
  }

  // Service worker registration (moved out of an inline <script> so the page's
  // Content-Security-Policy can keep script-src 'self' without 'unsafe-inline').
  // Same-origin, best-effort; the app works without it.
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(() => {}));
  }

  // Init
  initTheme();
  initInstallBanner();
  renderSystem();
  renderHomeSystem();
  renderOpsStatus();
  loadCatalog().catch(err => {
    console.error(err);
    document.body.innerHTML = '<div class="empty"><div class="empty-emoji">⚠️</div>Failed to load catalog</div>';
  });
})();
