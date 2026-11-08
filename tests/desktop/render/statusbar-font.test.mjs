// D-032: the Reset Rescue status-bar chip paints with Hermes' own status-bar
// typography — the same computed font size, weight, line height, family and
// colour as the native items next to it ('Agents 1 subagent', 'Memory') — and
// follows Hermes' text settings (base size, sans font, zoom) instead of sizes
// the plugin picked.
//
// The page (statusbar-entry.mjs) mirrors Hermes' status-bar markup and loads
// Hermes' real compiled stylesheet when it is installed
// (/usr/local/lib/hermes-agent/apps/desktop/dist/assets/main-*.css, or
// RR_HERMES_CSS). Without it, MIRROR_CSS below stands in: the same compiled
// rules, copied from that stylesheet, for the classes the page uses.
//
// Imported by popup-layout.test.mjs so scripts/render_test.sh runs it; it also
// runs alone. Chromium is started and killed inside before(), so it is gone
// before the layout test starts its own.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createRequire } from 'node:module'
import { after, before, test } from 'node:test'

import { resolveNodeModules } from '../harness.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const CHROMIUM = process.env.RR_CHROMIUM || '/usr/bin/chromium'
const PAGE_MS = 30_000
const HERMES_ASSETS = '/usr/local/lib/hermes-agent/apps/desktop/dist/assets'

function hermesCss() {
  // RR_HERMES_CSS=none forces MIRROR_CSS; a path picks that stylesheet.
  const env = process.env.RR_HERMES_CSS
  if (env) return env !== 'none' && existsSync(env) && statSync(env).isFile() ? env : null
  try {
    const f = readdirSync(HERMES_ASSETS).find(n => /^main-.*\.css$/.test(n))
    return f ? join(HERMES_ASSETS, f) : null
  } catch {
    return null
  }
}

// Compiled rules copied from Hermes' main-*.css (Tailwind v4 output) for the
// classes the page and the chip use; only for hosts without Hermes installed.
const MIRROR_CSS = String.raw`
:root{--spacing:.25rem;--text-xs:.75rem;--text-xs--line-height:calc(1/.75);--font-weight-medium:500;--dt-base-size:1rem;
--dt-font-sans:"Segoe WPC","Segoe UI",-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
--theme-foreground:#17171a;--theme-primary:#0053fd;--ui-base:var(--theme-foreground);--default-font-family:var(--dt-font-sans);
--ui-text-primary:color-mix(in srgb,var(--ui-base) 94%,transparent);--ui-text-secondary:color-mix(in srgb,var(--ui-base) 74%,transparent);
--ui-text-tertiary:color-mix(in srgb,var(--ui-base) 54%,transparent);--dt-muted-foreground:var(--ui-text-tertiary);
--dt-primary:var(--theme-primary);--dt-destructive:#cf2d56;--dt-foreground:var(--ui-text-primary)}
html,:host{line-height:1.5;font-family:var(--default-font-family,sans-serif)}
html{font-size:var(--dt-base-size,.875rem)}
*,:after,:before{box-sizing:border-box;border:0 solid;margin:0;padding:0}
button{font:inherit;font-feature-settings:inherit;font-variation-settings:inherit;letter-spacing:inherit;color:inherit;background-color:#0000;border-radius:0}
.flex{display:flex}.inline-flex{display:inline-flex}.h-5{height:calc(var(--spacing)*5)}.h-full{height:100%}
.items-stretch{align-items:stretch}.items-center{align-items:center}.justify-between{justify-content:space-between}.justify-center{justify-content:center}
.gap-0\.5{gap:calc(var(--spacing)*.5)}.gap-1{gap:var(--spacing)}.gap-1\.5{gap:calc(var(--spacing)*1.5)}.gap-2{gap:calc(var(--spacing)*2)}
.min-w-0{min-width:0}.shrink-0{flex-shrink:0}.overflow-x-clip{overflow-x:clip}.rounded-none{border-radius:0}
.px-1{padding-inline:var(--spacing)}.px-1\.5{padding-inline:calc(var(--spacing)*1.5)}.px-2\.5{padding-inline:calc(var(--spacing)*2.5)}
.py-0{padding-block:0}.py-1{padding-block:var(--spacing)}
.truncate{text-overflow:ellipsis;white-space:nowrap;overflow:hidden}.whitespace-nowrap{white-space:nowrap}
.text-xs{font-size:var(--text-xs);line-height:var(--tw-leading,var(--text-xs--line-height))}
.text-\[0\.6875rem\]{font-size:.6875rem}
.leading-4{--tw-leading:calc(var(--spacing)*4);line-height:calc(var(--spacing)*4)}
.font-medium{--tw-font-weight:var(--font-weight-medium);font-weight:var(--font-weight-medium)}
.text-\(--ui-text-tertiary\){color:var(--ui-text-tertiary)}.text-\(--ui-text-secondary\){color:var(--ui-text-secondary)}
.text-primary{color:var(--dt-primary)}.text-destructive{color:var(--dt-destructive)}
.text-muted-foreground\/80{color:color-mix(in oklab,var(--dt-muted-foreground) 80%,transparent)}
`

/** Hermes text settings under test: the default, a bigger base size with another
 *  sans font, and zoom. Each must move the native items, and the chip with them. */
const SETTINGS = {
  '13px': { baseSize: '13px' },
  '16px-serif': { baseSize: '16px', fontSans: 'Georgia, "DejaVu Serif", serif' },
  '13px-zoom125': { baseSize: '13px', zoom: 1.25 }
}
const STATES = ['bankedNext', 'expiring', 'failed']
const TYPO = ['fontSize', 'fontWeight', 'lineHeight', 'fontFamily', 'color', 'fontStyle', 'letterSpacing']

const avail = resolveNodeModules()
const skip = avail.dir ? false : `render deps missing: ${avail.missing.join(', ')}`
const scratch = mkdtempSync(join(process.env.RR_SCRATCH || process.env.TMPDIR || tmpdir(), 'rr-sb-'))
const cssFile = hermesCss()

function freePort() {
  return new Promise((ok, fail) => {
    const s = createServer()
    s.unref()
    s.on('error', fail)
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address()
      s.close(() => ok(port))
    })
  })
}

async function bundle() {
  const req = createRequire(join(avail.dir, 'noop.js'))
  const esbuild = req('esbuild')
  const out = join(scratch, 'statusbar.js')
  await esbuild.build({
    entryPoints: [join(HERE, 'statusbar-entry.mjs')],
    bundle: true,
    format: 'iife',
    platform: 'browser',
    outfile: out,
    nodePaths: [avail.dir],
    alias: { '@hermes/plugin-sdk': join(HERE, '..', 'sdk-stub.mjs') },
    define: { 'process.env.NODE_ENV': '"development"' },
    logLevel: 'error'
  })
  return readFileSync(out, 'utf8').replace(/<\/script/gi, '<\\/script')
}

function pageFile(js, state, setting) {
  const css = cssFile ? `<link rel="stylesheet" href="${pathToFileURL(cssFile).href}">` : `<style>${MIRROR_CSS}</style>`
  const html = [
    '<!doctype html><html><head><meta charset="utf-8">',
    css,
    '</head><body>',
    `<script>window.__rrSbCfg=${JSON.stringify({ state, ...SETTINGS[setting] })};</script>`,
    `<script>${js}</script>`,
    '</body></html>'
  ].join('\n')
  const p = join(scratch, `sb-${state}-${setting}.html`)
  writeFileSync(p, html)
  return p
}

function cdpSender(socket) {
  let id = 0
  const pending = new Map()
  socket.onmessage = ev => {
    const msg = JSON.parse(typeof ev.data === 'string' ? ev.data : String(ev.data))
    if (msg.id && pending.has(msg.id)) {
      const { ok, fail } = pending.get(msg.id)
      pending.delete(msg.id)
      if (msg.error) fail(new Error(`${msg.error.message}`))
      else ok(msg.result)
    }
  }
  return (method, params = {}, sessionId) =>
    new Promise((ok, fail) => {
      const mid = ++id
      pending.set(mid, { ok, fail })
      socket.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }))
    })
}

async function withChrome(fn) {
  const port = await freePort()
  const chrome = spawn(
    CHROMIUM,
    [
      '--headless=new',
      '--no-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage',
      '--no-first-run',
      '--no-default-browser-check',
      '--allow-file-access-from-files',
      `--user-data-dir=${join(scratch, 'chrome-profile')}`,
      `--remote-debugging-port=${port}`,
      'about:blank'
    ],
    { stdio: 'ignore' }
  )
  let ws = null
  try {
    const deadline = Date.now() + 15_000
    let info = null
    while (Date.now() < deadline && !info) {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`)
        if (r.ok) info = await r.json()
      } catch {
        // not listening yet
      }
      if (!info) await new Promise(r => setTimeout(r, 100))
    }
    if (!info) throw new Error('chromium did not open its DevTools port within 15 s')
    ws = new WebSocket(info.webSocketDebuggerUrl)
    await new Promise((ok, fail) => {
      ws.onopen = ok
      ws.onerror = () => fail(new Error('DevTools websocket failed'))
    })
    return await fn(cdpSender(ws))
  } finally {
    try {
      ws?.close()
    } catch {
      // already closed
    }
    if (chrome.exitCode === null) chrome.kill('SIGKILL')
  }
}

async function measure(send, file) {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
  try {
    await send('Runtime.enable', {}, sessionId)
    await send('Emulation.setDeviceMetricsOverride', { width: 1000, height: 200, deviceScaleFactor: 1, mobile: false }, sessionId)
    await send('Page.navigate', { url: pathToFileURL(file).href }, sessionId)
    const ev = async (expression, awaitPromise = false) => {
      const r = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true, timeout: PAGE_MS }, sessionId)
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? 'page evaluation failed')
      return r.result.value
    }
    await new Promise(r => setTimeout(r, 200))
    const ready = await ev(
      `(async()=>{const t0=Date.now();while(!window.__rrSb||(!window.__rrSb.ready&&!window.__rrSb.error)){if(Date.now()-t0>${PAGE_MS})return 'timeout';await new Promise(r=>setTimeout(r,20));}await document.fonts.ready;await Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})));return window.__rrSb.error||'ok';})()`,
      true
    )
    assert.equal(ready, 'ok', `page ready: ${file}`)
    return await ev('window.__rrSb.measure()')
  } finally {
    await send('Target.closeTarget', { targetId }).catch(() => {})
  }
}

const results = {}

before(async () => {
  if (skip) return
  console.log(`# D-032 status-bar CSS: ${cssFile ?? 'MIRROR_CSS (Hermes not installed)'}`)
  const js = await bundle()
  await withChrome(async send => {
    for (const setting of Object.keys(SETTINGS)) {
      for (const state of STATES) results[`${state}@${setting}`] = await measure(send, pageFile(js, state, setting))
    }
  })
  if (process.env.RR_RENDER_DEBUG) console.log(`# statusbar ${JSON.stringify(results)}`)
})

after(() => {
  rmSync(scratch, { recursive: true, force: true })
})

const pick = (o, keys) => Object.fromEntries(keys.map(k => [k, o[k]]))

for (const setting of Object.keys(SETTINGS)) {
  test(`D-032 (${setting}): the chip has the native items' font size, weight, line height and family`, { skip, timeout: PAGE_MS }, () => {
    for (const state of STATES) {
      const r = results[`${state}@${setting}`]
      assert.equal(r.kind, state)
      const font = ['fontSize', 'fontWeight', 'lineHeight', 'fontFamily', 'fontStyle', 'letterSpacing']
      assert.deepEqual(pick(r.ours, font), pick(r.memory, font), `${state}: chip vs Memory`)
      assert.deepEqual(pick(r.ours, font), pick(r.agents, font), `${state}: chip vs Agents`)
      assert.deepEqual(pick(r.oursLabel, font), pick(r.agentsLabel, font), `${state}: chip label vs Agents label`)
      assert.equal(r.ours.height, r.memory.height, `${state}: same item height`)
    }
  })

  test(`D-032 (${setting}): the chip's colour is the native item colour, state for state`, { skip, timeout: PAGE_MS }, () => {
    // Calm: the plain tertiary text of Memory. Expiring: the native update
    // item's text-primary. Failed: the Agents item's failed text-destructive.
    // In those two cases the stand-in Agents item carries that class.
    const calm = results[`bankedNext@${setting}`]
    assert.equal(calm.ours.color, calm.memory.color, 'calm chip vs Memory')
    for (const state of ['expiring', 'failed']) {
      const r = results[`${state}@${setting}`]
      assert.equal(r.ours.color, r.agents.color, `${state} chip vs the native item with the same state class`)
      assert.notEqual(r.ours.color, r.memory.color, `${state} chip stands out from Memory`)
    }
    for (const state of STATES) {
      const r = results[`${state}@${setting}`]
      assert.deepEqual(pick(r.ours, TYPO), pick(r.oursLabel, TYPO), `${state}: the label inherits the chip's typography`)
    }
  })
}

test('D-032: the chip follows Hermes text settings (base size, sans font, zoom) with the native items', { skip, timeout: PAGE_MS }, () => {
  const a = results['bankedNext@13px']
  const b = results['bankedNext@16px-serif']
  const z = results['bankedNext@13px-zoom125']
  assert.equal(a.rootFontSize, '13px')
  assert.equal(b.rootFontSize, '16px')
  // 0.6875rem of the root, the native size (statusbar-controls.tsx:31).
  assert.equal(parseFloat(a.ours.fontSize), 13 * 0.6875)
  assert.equal(parseFloat(b.ours.fontSize), 16 * 0.6875)
  assert.notEqual(a.ours.fontFamily, b.ours.fontFamily, 'the sans font setting reaches the chip')
  assert.match(b.ours.fontFamily, /Georgia/)
  // Zoom scales the rendered box of both, equally.
  assert.ok(z.ours.height > a.ours.height, `zoomed chip ${z.ours.height} > ${a.ours.height}`)
  assert.equal(z.ours.height, z.memory.height)
})
