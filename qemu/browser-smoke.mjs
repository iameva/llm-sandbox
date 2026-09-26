#!/usr/bin/env node
// Check that the image's baked browsers actually run in the sandbox.
// The image installs it on PATH, so inside a sandbox session:
//
//     browser-smoke.mjs [output-dir]      # default output dir is /tmp
//
// It serves a page over HTTP on loopback, loads it in each browser, and
// writes a screenshot. The screenshots are the point: a browser that
// launches can still draw every symbol glyph as tofu if the fonts or
// /etc/fonts/conf.d/99-symbol-fallback.conf went missing, and nothing but
// a human eye catches that.

import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';

// Playwright is installed globally in the image, and neither an ESM import
// nor NODE_PATH looks there. Require it by hand, preferring a local copy if
// the directory this runs from has one.
function loadPlaywright() {
  const roots = [
    path.join(process.cwd(), 'node_modules'),
    execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim(),
  ];
  for (const root of roots) {
    try {
      return createRequire(path.join(root, 'noop.js'))('@playwright/test');
    } catch {
      // try the next root
    }
  }
  throw new Error('cannot find @playwright/test locally or in the global npm root');
}

const { firefox, chromium } = loadPlaywright();

// The glyphs the apps actually use. U+FF0B is expected to stay tofu:
// pulling ~100MB of CJK to draw one fullwidth plus is not worth it.
const GLYPHS = '⚙ ✕ ✓ ⚠ ⋮ ⋯ ▾ ▸ ↻ ⓘ ☰ ⏱ ▁▂▃▄▅▆▇█';

const PAGE = `<!doctype html><meta charset="utf-8"><title>smoke</title>
<style>
  body { font-family: system-ui; margin: 2rem; color: #111 }
  h1 { color: #2a9d5c }
  .glyphs { font-size: 32px; letter-spacing: 6px }
</style>
<h1 id="heading">browser smoke</h1>
<p class="glyphs">${GLYPHS}</p>
<p>expected tofu, and only this: <span class="glyphs">＋</span></p>`;

const outDir = process.argv[2] ?? os.tmpdir();
const server = http.createServer((_req, res) => {
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
  res.end(PAGE);
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}/`;

// Chromium's own sandbox does not start under gVisor, so it is off here and
// the container is the sandbox. /dev/shm is 63M, too small for Chromium's
// default. Firefox needs neither accommodation.
const browsers = [
  ['firefox', firefox, {}],
  ['chromium', chromium, { chromiumSandbox: false, args: ['--disable-dev-shm-usage'] }],
];

let failed = 0;
for (const [name, type, options] of browsers) {
  const shot = path.join(outDir, `browser-smoke-${name}.png`);
  try {
    const browser = await type.launch(options);
    const page = await browser.newPage({ viewport: { width: 900, height: 300 } });
    const response = await page.goto(url);
    const heading = await page.textContent('#heading');
    await page.screenshot({ path: shot });
    await browser.close();

    if (response.status() !== 200) throw new Error(`HTTP ${response.status()}`);
    if (heading !== 'browser smoke') throw new Error(`heading was "${heading}"`);
    console.log(`ok    ${name} ${browser.version()}  ->  ${shot}`);
  } catch (err) {
    failed++;
    console.error(`FAIL  ${name}: ${err.message.split('\n')[0]}`);
  }
}

server.close();
if (failed) {
  console.error('\nA missing system library shows up as "Failed to launch"; run');
  console.error('ldd on the browser under $PLAYWRIGHT_BROWSERS_PATH to name it.');
  process.exit(1);
}
console.log('\nNow look at the screenshots: every glyph should be drawn, and');
console.log('only the fullwidth plus should be a box.');
