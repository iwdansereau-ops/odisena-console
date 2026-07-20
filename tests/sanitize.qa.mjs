// Security regression test for the runbook markdown render path.
//
// Verifies that the HTML produced by window.marked.parse(md) is passed through
// sanitizeHtml() before being assigned to #reader-body.innerHTML, so that a
// malicious runbook payload cannot inject executable script, event handlers, or
// javascript:-scheme URLs. Drives the real app (no internal hooks): a runbook
// fetch is intercepted and fulfilled with a hostile payload, then the rendered
// DOM is inspected.
//
// Run:  node tests/sanitize.qa.mjs   (from the repo root; needs playwright + a
// cached Chromium). Starts and stops its own static server. Exit 0 = pass.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = 8137;
const BASE = `http://localhost:${PORT}/`;

const PAYLOAD = [
  '# Trusted heading',
  '',
  'A benign [external link](https://example.com/page) and `inline code`.',
  '',
  '![local image](assets/diagram.png)',
  '',
  '<script>window.__xss = "script";</script>',
  '',
  '<img src="x" onerror="window.__xss = \'onerror\'">',
  '',
  '<a href="javascript:window.__xss=\'jsurl\'">js link</a>',
  '',
  '<a href="  jAvaScript:alert(1)">spaced js link</a>',
  '',
  '<a href="java\tscript:alert(2)">tab js link</a>',
  '',
  '<iframe src="https://evil.example/frame"></iframe>',
  '',
  '<svg onload="window.__xss=\'svg\'"><rect /></svg>',
  '',
  '<div onclick="window.__xss=\'div\'">keep this text</div>',
  '',
  '<img src="data:text/html,<b>x</b>" alt="data uri">',
].join('\n');

const problems = [];
const note = m => console.log('  ' + m);

const server = spawn('python3', ['-m', 'http.server', String(PORT)], { stdio: 'ignore' });
await sleep(700);

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.route('**/*', route => {
    const url = route.request().url();
    // Any runbook body fetch (.md under runbooks/) gets the hostile payload.
    if (/\/runbooks\/.+\.md(\?|$)/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'text/markdown', body: PAYLOAD });
    }
    return route.continue();
  });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(400);
  await page.click('.tabbar [data-nav="runbooks"]');
  await page.waitForTimeout(300);

  const item = await page.$('#runbook-list button, #runbook-list a, #runbook-list [data-open]');
  if (!item) { problems.push('no runbook item to open'); throw new Error('setup'); }
  await item.click();
  await page.waitForTimeout(500);

  const readerActive = await page.evaluate(() =>
    document.querySelector('#view-reader')?.classList.contains('view-active'));
  if (!readerActive) problems.push('reader view did not activate');

  const r = await page.evaluate(() => {
    const body = document.getElementById('reader-body');
    const els = Array.from(body.querySelectorAll('*'));
    const hasOnAttr = els.some(e => Array.from(e.attributes).some(a => a.name.toLowerCase().startsWith('on')));
    const badUrls = [];
    els.forEach(e => {
      ['href', 'src'].forEach(k => {
        const v = (e.getAttribute(k) || '').replace(/[\u0000-\u0020\u007f]/g, '').toLowerCase();
        if (/^(javascript|data|vbscript):/.test(v)) badUrls.push(e.tagName + '@' + k + '=' + v.slice(0, 24));
      });
    });
    const links = els.filter(e => e.tagName === 'A').map(a => a.getAttribute('href'));
    const imgs = els.filter(e => e.tagName === 'IMG').map(i => i.getAttribute('src'));
    return {
      scripts: body.querySelectorAll('script').length,
      iframes: body.querySelectorAll('iframe').length,
      svgs: body.querySelectorAll('svg').length,
      hasOnAttr,
      badUrls,
      links,
      imgs,
      hasH1: !!body.querySelector('h1'),
      keptText: body.textContent.includes('keep this text'),
      headingText: body.textContent.includes('Trusted heading'),
      code: !!body.querySelector('code'),
      xss: window.__xss || null,
      title: document.title,
    };
  });

  // --- executable-content stripping ---
  if (r.scripts !== 0) problems.push(`<script> survived (${r.scripts})`);
  if (r.iframes !== 0) problems.push(`<iframe> survived (${r.iframes})`);
  if (r.svgs !== 0) problems.push(`<svg> survived (${r.svgs})`);
  if (r.hasOnAttr) problems.push('an on* event-handler attribute survived');
  if (r.badUrls.length) problems.push('dangerous URL scheme survived: ' + JSON.stringify(r.badUrls));
  if (r.xss !== null) problems.push('payload executed: window.__xss=' + r.xss);
  if (r.title === 'pwned') problems.push('payload mutated document.title');

  // --- trusted markdown preserved ---
  if (!r.hasH1) problems.push('benign <h1> heading was dropped');
  if (!r.headingText) problems.push('heading text lost');
  if (!r.code) problems.push('inline <code> lost');
  if (!r.keptText) problems.push('text inside a stripped-attribute <div> was lost');
  if (!r.links.includes('https://example.com/page')) problems.push('benign external link href lost: ' + JSON.stringify(r.links));
  if (!r.imgs.includes('assets/diagram.png')) problems.push('benign relative <img> src lost: ' + JSON.stringify(r.imgs));

  note(`scripts=${r.scripts} iframes=${r.iframes} svgs=${r.svgs} on*=${r.hasOnAttr} badUrls=${r.badUrls.length}`);
  note(`links=${JSON.stringify(r.links)}`);
  note(`imgs=${JSON.stringify(r.imgs)}`);
  note(`h1=${r.hasH1} code=${r.code} keptText=${r.keptText} xss=${r.xss}`);
} catch (e) {
  problems.push('threw: ' + e.message.split('\n')[0]);
} finally {
  await browser.close();
  server.kill('SIGTERM');
}

if (problems.length) {
  console.log('\nSANITIZE QA: FAIL');
  problems.forEach(p => console.log('  - ' + p));
  process.exit(1);
}
console.log('\nSANITIZE QA: PASS — script/event-handler/dangerous-URL stripped, trusted markdown preserved');
