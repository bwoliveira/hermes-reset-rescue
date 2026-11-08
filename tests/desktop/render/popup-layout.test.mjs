// Real-browser layout test for the Reset Rescue Desktop popup (D-024, D-027, D-029).
//
// jsdom has no layout engine, so the old tests passed with wrong heights. This
// test bundles tests/desktop/render/page-entry.mjs (the real desktop/plugin.js
// with fixture data), loads it in headless Chromium, opens the popup and
// measures it through the DevTools protocol. Dependencies: esbuild and the
// React/nanostores set that harness.mjs already resolves. No new packages.
//
// Each case runs at two text sizes: the default 13 px, and 15 px (zoom 15/13 on
// the root). The popup's own sizes are px, so a bigger root font-size alone
// would change nothing; zoom scales the text and every box with it, which is
// what a machine with larger text does.
//
// Run through scripts/render_test.sh (memory- and time-capped). Chromium is
// killed in a finally block; nothing is left behind.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { after, before, test } from 'node:test'

import { resolveNodeModules } from '../harness.mjs'
// D-032: the status-bar typography test rides on this gate step (it starts and
// stops its own Chromium inside its before hook, ahead of this file's).
import './statusbar-font.test.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const CHROMIUM = process.env.RR_CHROMIUM || '/usr/bin/chromium'
const PAGE_MS = 60_000
const FONTS = { normal: 1, large: 15 / 13 }
const CASE_IDS = ['a', 'b', 'c', 'd', 'dpt']
/** D-031: fewer than five accounts (g one, h two, i none). */
const SMALL_IDS = ['g', 'h', 'i']
const ALL_IDS = [...CASE_IDS, ...SMALL_IDS]
/** S.inner padding and gap (CSS constants, as in plugin.js ROW_LAYOUT). */
const PAD = 14
const GAP = 12
const TABS = ['accounts', 'history', 'settings', 'perAccount', 'perAccountExpanded']

const avail = resolveNodeModules()
const skip = avail.dir ? false : `render deps missing: ${avail.missing.join(', ')}`
if (skip) console.log(`# SKIP ${skip}`)

const scratch = mkdtempSync(join(process.env.RR_SCRATCH || process.env.TMPDIR || tmpdir(), 'rr-render-'))
let chrome = null
let ws = null
let bundleText = ''

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
  const out = join(scratch, 'page.js')
  await esbuild.build({
    entryPoints: [join(HERE, 'page-entry.mjs')],
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

function pageFile(caseId, font) {
  // The entry reads ?case= from location.search; a file:// URL cannot carry
  // one through the bundle, so each page gets its own HTML with the ids inline.
  const html = [
    '<!doctype html><html><head><meta charset="utf-8">',
    '<style>:root{--ui-bg-elevated:#1f2024;--ui-text-primary:#f2f2f4;--ui-text-secondary:#c4c5cc;--ui-text-tertiary:#8c8e99;--ui-stroke-secondary:#34363d;--ui-bg-quaternary:#2a2c33;--color-primary:#7aa2ff;--color-destructive:#ff6b6b}',
    'html,body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:13px;line-height:1.35;background:#15161a;color:#f2f2f4}',
    '</style></head><body>',
    `<script>window.__rrCase=${JSON.stringify(caseId)};window.__rrScale=${FONTS[font]};</script>`,
    '<script>',
    bundleText.replace(/location\.search/g, '"?case="+window.__rrCase'),
    '</script></body></html>'
  ].join('\n')
  const p = join(scratch, `page-${caseId}-${font}.html`)
  writeFileSync(p, html)
  return p
}

async function startChrome() {
  const port = await freePort()
  const profile = join(scratch, 'chrome-profile')
  chrome = spawn(
    CHROMIUM,
    [
      '--headless=new',
      '--no-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage',
      '--no-first-run',
      '--no-default-browser-check',
      '--window-size=1280,1000',
      `--user-data-dir=${profile}`,
      `--remote-debugging-port=${port}`,
      'about:blank'
    ],
    { stdio: 'ignore' }
  )
  const deadline = Date.now() + 15_000
  let info = null
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`)
      if (r.ok) {
        info = await r.json()
        break
      }
    } catch {
      // not listening yet
    }
    await new Promise(r => setTimeout(r, 100))
  }
  if (!info) throw new Error('chromium did not open its DevTools port within 15 s')
  ws = new WebSocket(info.webSocketDebuggerUrl)
  await new Promise((ok, fail) => {
    ws.onopen = ok
    ws.onerror = () => fail(new Error('DevTools websocket failed'))
  })
  return { send: cdpSender(ws) }
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

async function measureCase(cdp, caseId, font) {
  const file = await pageFile(caseId, font)
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true })
  await cdp.send('Page.enable', {}, sessionId)
  await cdp.send('Runtime.enable', {}, sessionId)
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId)
  await cdp.send('Page.navigate', { url: `file://${file}` }, sessionId)
  const evalExpr = async (expression, awaitPromise = false) => {
    const r = await cdp.send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true, timeout: PAGE_MS }, sessionId)
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? 'page evaluation failed')
    return r.result.value
  }
  try {
    await new Promise(r => setTimeout(r, 300))
    const ready = await evalExpr(
      `(async()=>{const t0=Date.now();while(!window.__rr||(!window.__rr.ready&&!window.__rr.error)){if(Date.now()-t0>${PAGE_MS})return 'timeout';await new Promise(r=>setTimeout(r,20));}return window.__rr.error||'ok';})()`,
      true
    )
    assert.equal(ready, 'ok', `page ready for case ${caseId} at ${font}`)
    const out = await evalExpr('window.__rr.measureAll()', true)
    // RR_RENDER_SHOTS=<dir>: keep a PNG of the last state (Per account, one
    // account expanded, D-034) for a human look. Off by default.
    if (process.env.RR_RENDER_SHOTS) {
      const shot = await cdp.send('Page.captureScreenshot', { format: 'png' }, sessionId)
      writeFileSync(join(process.env.RR_RENDER_SHOTS, `${caseId}-${font}.png`), Buffer.from(shot.data, 'base64'))
    }
    return out
  } finally {
    await cdp.send('Target.closeTarget', { targetId }).catch(() => {})
  }
}

let cdp = null
const results = {}

before(async () => {
  if (skip) return
  bundleText = await bundle()
  cdp = await startChrome()
  for (const font of Object.keys(FONTS)) {
    for (const id of ALL_IDS) results[`${id}-${font}`] = await measureCase(cdp, id, font)
  }
  if (process.env.RR_RENDER_DEBUG) console.log(`# render ${JSON.stringify(results)}`)
})

after(async () => {
  try {
    ws?.close()
  } catch {
    // socket already closed
  }
  if (chrome && chrome.exitCode === null) {
    chrome.kill('SIGKILL')
  }
  rmSync(scratch, { recursive: true, force: true })
})

function fitsHeight(v) {
  return v.scrollHeight <= v.scrollClientHeight
}

// Each assertion runs at both text sizes: the second font is the proof that the
// result does not depend on the machine's text size (f).
for (const font of Object.keys(FONTS)) {
  const tag = font === 'normal' ? '13 px' : '15 px'
  const scale = FONTS[font]
  const r = id => results[`${id}-${font}`]

  test(`D-024/D-027 (a, ${tag}): five accounts with two resets each fit with no vertical scroll`, { skip, timeout: PAGE_MS }, () => {
    const v = r('a').accounts
    assert.ok(fitsHeight(v), `case a scrolls: scrollHeight ${v.scrollHeight} > clientHeight ${v.scrollClientHeight}`)
  })

  test(`D-024 (b, ${tag}): five accounts where one has three resets scroll`, { skip, timeout: PAGE_MS }, () => {
    const v = r('b').accounts
    assert.ok(!fitsHeight(v), `case b does not scroll: scrollHeight ${v.scrollHeight} <= clientHeight ${v.scrollClientHeight}`)
  })

  test(`D-024 (c, ${tag}): six accounts with two resets each scroll`, { skip, timeout: PAGE_MS }, () => {
    const v = r('c').accounts
    assert.ok(!fitsHeight(v), `case c does not scroll: scrollHeight ${v.scrollHeight} <= clientHeight ${v.scrollClientHeight}`)
  })

  test(`D-024 (d, ${tag}): width is 400 px and no row overflows horizontally`, { skip, timeout: PAGE_MS }, () => {
    for (const id of CASE_IDS) {
      for (const tab of TABS) {
        const v = r(id)[tab]
        // Under zoom the rendered width is the authored 400 px times the scale.
        assert.equal(Math.round(v.popupWidth), Math.round(400 * scale), `case ${id} ${tab} width`)
        assert.deepEqual(v.overflowRows, [], `case ${id} ${tab} overflowing rows`)
      }
    }
  })

  test(`D-024/D-031 (e, ${tag}): height is identical on Accounts, History and Settings`, { skip, timeout: PAGE_MS }, () => {
    for (const id of ALL_IDS) {
      const x = r(id)
      const heights = TABS.filter(tab => x[tab]).map(tab => x[tab].styleHeight)
      assert.equal(new Set(heights).size, 1, `case ${id} style heights ${heights.join(', ')}`)
    }
  })

  test(`D-030/D-034 (d, ${tag}): Per account lists six accounts in 400 px with no horizontal overflow, popup height unchanged`, { skip, timeout: PAGE_MS }, () => {
    for (const id of ['d', 'dpt']) {
      const x = r(id)
      for (const tab of ['perAccount', 'perAccountExpanded']) {
        const v = x[tab]
        assert.equal(v.accountRows, 6, `case ${id} ${tab} rows`)
        assert.deepEqual(v.segmentsOutside, [], `case ${id} ${tab} SegmentedControls past the content edge`)
        assert.equal(Math.round(v.popupWidth), Math.round(400 * scale), `case ${id} ${tab} width`)
        assert.deepEqual(v.overflowRows, [], `case ${id} ${tab} overflowing elements`)
        assert.ok(v.scrollWidth <= v.scrollClientWidth, `case ${id} ${tab} scrolls sideways: ${v.scrollWidth} > ${v.scrollClientWidth}`)
        assert.equal(v.styleHeight, x.settings.styleHeight, `case ${id} ${tab} keeps the Settings height`)
        assert.equal(Math.round(v.popupHeight), Math.round(x.settings.popupHeight), `case ${id} ${tab} rendered height`)
        assert.equal(v.styleHeight, x.accounts.styleHeight, `case ${id} ${tab} keeps the Accounts height`)
      }
      // D-034: the expanded account shows four options, three segments each,
      // and the early threshold (case d expands acct_2, early on).
      const open = x.perAccountExpanded
      assert.deepEqual(open.options.map(o => o.field), ['auto_redeem_enabled', 'early_redeem_enabled', 'reminders_enabled', 'monitored'], `case ${id} options`)
      for (const o of open.options) assert.equal(o.segments.length, 3, `case ${id} ${o.field} segments`)
      assert.equal(open.threshold, true, `case ${id} threshold input`)
      assert.equal(x.perAccount.options.length, 0, `case ${id} collapsed shows no options`)
      // The four tagged accounts carry their tag on the Accounts tab.
      assert.equal(x.accounts.tags.length, 4, `case ${id} tags ${x.accounts.tags.join(', ')}`)
    }
    assert.deepEqual(r('dpt').accounts.tags.slice().sort(), ['Antecipado ligado', 'Auto desligado', 'Pausada', 'Personalizada'])
    assert.deepEqual(r('dpt').perAccountExpanded.options[0].segments, ['Padrão (Ligado)', 'Ligado', 'Desligado'])
    assert.deepEqual(r('d').perAccountExpanded.options[0].segments, ['Default (On)', 'On', 'Off'])
    assert.match(r('dpt').perAccount.texts[0], /Segue o global/)
  })

  test(`D-029 (${tag}): the authored height is the formula over the measured sizes`, { skip, timeout: PAGE_MS }, () => {
    for (const id of CASE_IDS) {
      const v = r(id).accounts
      // Both the written height and the formula are CSS px, so they compare directly.
      const authored = parseFloat(v.styleHeight)
      assert.ok(Math.abs(authored - v.formula) <= 1, `case ${id} height ${authored} vs formula ${v.formula}`)
    }
  })

  // D-031: the expected height from live measurements, independent of the
  // plugin's own hidden sample: natural = the Accounts view as rendered,
  // floor = header + footer + the first four global setting rows as rendered
  // on the Settings tab, cap = five full accounts (D-029).
  const expected = x => {
    const { header, footer } = x.accounts.sizes
    const natural = x.accounts.naturalCss
    const rows = x.settings.settingRows
    assert.equal(rows.length, 4, 'four setting rows measured')
    const floor = Math.ceil(PAD * 2 + header + rows.reduce((a, b) => a + b, 0) + footer + GAP * 2)
    const cap = x.accounts.formula
    return { natural, floor, cap, height: Math.min(cap, Math.max(floor, Math.ceil(natural))) }
  }

  for (const id of ['g', 'h']) {
    const what = id === 'g' ? 'one account' : 'two accounts'
    test(`D-031 (${id}, ${tag}): ${what} with two resets: height is clamp(natural, floor, cap), below the cap, no empty gap under the footer`, { skip, timeout: PAGE_MS }, () => {
      const x = r(id)
      const v = x.accounts
      const e = expected(x)
      const authored = parseFloat(v.styleHeight)
      assert.ok(Math.abs(authored - e.height) <= 1, `case ${id} height ${authored} vs clamp(${e.natural}, ${e.floor}, ${e.cap}) = ${e.height}`)
      assert.ok(authored < e.cap, `case ${id} height ${authored} is not below the cap ${e.cap}`)
      assert.ok(fitsHeight(v), `case ${id} scrolls: ${v.scrollHeight} > ${v.scrollClientHeight}`)
      // No empty space under the footer: its bottom edge sits on the popup's
      // bottom padding (within 2 px), whether the natural height or the floor wins.
      assert.ok(v.footerGap !== null && Math.abs(v.footerGap) <= 2, `case ${id} gap under the footer ${v.footerGap} px`)
    })
  }

  test(`D-031 (i, ${tag}): no account: the height is the floor`, { skip, timeout: PAGE_MS }, () => {
    const x = r('i')
    const e = expected(x)
    const authored = parseFloat(x.accounts.styleHeight)
    assert.ok(Math.abs(authored - e.floor) <= 1, `case i height ${authored} vs floor ${e.floor}`)
    assert.ok(authored < e.cap, `case i height ${authored} is not below the cap ${e.cap}`)
  })

  test(`D-031 (j, ${tag}): one account: Settings keeps the Accounts height and scrolls inside`, { skip, timeout: PAGE_MS }, () => {
    const x = r('g')
    assert.equal(x.settings.styleHeight, x.accounts.styleHeight, 'Settings height')
    assert.equal(x.history.styleHeight, x.accounts.styleHeight, 'History height')
    assert.ok(Math.abs(x.settings.popupHeight - x.accounts.popupHeight) <= 0.5, `rendered ${x.settings.popupHeight} vs ${x.accounts.popupHeight}`)
    assert.ok(x.settings.scrollHeight > x.settings.scrollClientHeight, `Settings does not scroll: ${x.settings.scrollHeight} <= ${x.settings.scrollClientHeight}`)
  })
}
