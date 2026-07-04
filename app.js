// Odisena Console — client app
(function () {
  'use strict';

  const state = {
    catalog: null,
    view: 'home',
    history: ['home'],
    filters: { runbooks: 'all', artifacts: 'all' },
    theme: null,
  };

  // ===== Theme =====
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    state.theme = t;
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
      btn.innerHTML = `
        <div class="doc-icon">${docIcon(r.name)}</div>
        <div class="doc-body">
          <div class="doc-name">${escapeHtml(r.display_name)}</div>
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
    document.getElementById('reader-title').textContent = r.display_name;
    const body = document.getElementById('reader-body');
    body.innerHTML = '<div class="empty"><div class="empty-emoji">⏳</div>Loading…</div>';
    show('reader');
    try {
      const res = await fetch(r.path);
      const md = await res.text();
      body.innerHTML = window.marked.parse(md);
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
      const link = document.createElement('a');
      link.className = 'artifact-item';
      link.href = a.path;
      link.download = a.name.replace(/^[a-f0-9]{8}__/, '');
      const cleanName = a.name.replace(/^[a-f0-9]{8}__/, '');
      const isUuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\.[a-z]+$/i.test(cleanName);
      const displayName = isUuid ? `Image · ${a.session_title}` : cleanName;
      link.innerHTML = `
        <div class="ext-badge ext-${a.ext}">${a.ext}</div>
        <div class="artifact-body">
          <div class="artifact-name">${escapeHtml(displayName)}</div>
          <div class="artifact-meta">${escapeHtml(isUuid ? cleanName : a.session_title)}</div>
        </div>
        <div class="artifact-size">${fmtSize(a.size)}</div>
      `;
      list.appendChild(link);
    });
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
    const chips = s.files.slice(0, 4).map(f => `<span class="session-file-chip">${escapeHtml(f.replace(/^[a-f0-9]{8}__/, '').slice(0, 24))}</span>`).join('');
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
    tags.innerHTML = unique.map(w => `<span class="ops-tag">${escapeHtml(w)}</span>`).join('');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Search handlers
  document.getElementById('runbook-search').addEventListener('input', renderRunbooks);
  document.getElementById('artifact-search').addEventListener('input', renderArtifacts);
  document.getElementById('session-search').addEventListener('input', renderSessions);

  // Hash routing (Safari back-swipe returns to home)
  window.addEventListener('hashchange', () => {
    const v = location.hash.replace('#/', '') || 'home';
    if (['home', 'runbooks', 'artifacts', 'sessions', 'ops', 'reader'].includes(v)) {
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

  // Init
  initTheme();
  initInstallBanner();
  loadCatalog().catch(err => {
    console.error(err);
    document.body.innerHTML = '<div class="empty"><div class="empty-emoji">⚠️</div>Failed to load catalog</div>';
  });
})();
