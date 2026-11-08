#!/usr/bin/env node
// Reset Rescue — launch screenshots of the Desktop popup (T-62, T-65).
//
//   node scripts/screenshots.mjs [--out DIR] [--docs] [--scale N]
//
// Renders the real desktop/plugin.js in headless Chromium with demo fixtures
// (five masked placeholder accounts, never real labels), Hermes' compiled
// Desktop CSS and its default skin painted light or dark, and saves the popup
// plus the status-bar chip, cropped (no app sidebar), at 2x:
//
//   <out>/{accounts,per-account,settings}-{en,pt-BR}-{light,dark}.png   (12)
//
// --docs also copies the three README images into docs/screenshots/ (one
// per screen, English, dark theme) and removes any other PNG there:
//   accounts-en-dark, per-account-en-dark, settings-en-dark
//
// Needs an installed Hermes (/usr/local/lib/hermes-agent, or RR_HERMES_ROOT):
// its compiled main-*.css (or RR_HERMES_CSS=<file>) and, read-only, the theme
// palettes and color math under apps/shared/src and apps/desktop/src/themes.
// Chromium: RR_CHROMIUM (default /usr/bin/chromium). It runs with a private
// profile under TMPDIR and only that process is killed at the end.
//
// The page is tests/desktop/render/shots-entry.mjs with shots-sdk.mjs as the
// SDK (the test stub, plus the host's popover and segmented-control classes).
// Each shot fails if the popup shows a "Saving" note or an unmasked address.
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const RENDER = join(REPO, 'tests', 'desktop', 'render')
const HERMES = process.env.RR_HERMES_ROOT || '/usr/local/lib/hermes-agent'
const CHROMIUM = process.env.RR_CHROMIUM || '/usr/bin/chromium'
const TABS = ['accounts', 'per-account', 'settings']
const LANGS = ['en', 'pt-BR']
const THEMES = ['light', 'dark']
const README = ['accounts-en-dark', 'per-account-en-dark', 'settings-en-dark']
const VIEW = { width: 560, height: 820 }
const PAGE_MS = 30_000
const HERMES_MODULES = join(HERMES, 'apps', 'desktop', 'node_modules')

// The same search as tests/desktop/harness.mjs (not imported: it loads
// node:test). tailwind-merge and clsx come from the Hermes install.
function nodeModules() {
  const list = [process.env.RR_NODE_MODULES, join(REPO, 'node_modules'), join(REPO, 'tests', 'desktop', 'node_modules'), HERMES_MODULES].filter(Boolean)
  const needed = ['react', 'react-dom', 'nanostores', '@nanostores/react', 'esbuild']
  const dir = list.find(d => needed.every(p => existsSync(join(d, p, 'package.json'))))
  if (!dir) throw new Error(`render deps missing (${needed.join(', ')}) in ${list.join(', ')}`)
  for (const p of ['tailwind-merge', 'clsx']) if (!existsSync(join(HERMES_MODULES, p, 'package.json'))) throw new Error(`${p} missing in ${HERMES_MODULES}`)
  return dir
}

function args(argv) {
  const out = { out: '/var/tmp/rr-shots', docs: false, scale: 2 }
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--out') out.out = argv[++i]
    else if (argv[i] === '--docs') out.docs = true
    else if (argv[i] === '--scale') out.scale = Number(argv[++i])
    else throw new Error(`unknown argument ${argv[i]}`)
  }
  return out
}

function hermesCss() {
  const env = process.env.RR_HERMES_CSS
  if (env) {
    if (!existsSync(env) || !statSync(env).isFile()) throw new Error(`RR_HERMES_CSS=${env} is not a CSS file`)
    return env
  }
  const assets = join(HERMES, 'apps', 'desktop', 'dist', 'assets')
  const f = existsSync(assets) ? readdirSync(assets).find(n => /^main-.*\.css$/.test(n)) : null
  if (!f) throw new Error(`no compiled Hermes CSS under ${assets} (set RR_HERMES_CSS)`)
  return join(assets, f)
}

async function bundle(scratch, deps) {
  const esbuild = createRequire(join(deps, 'noop.js'))('esbuild')
  const out = join(scratch, 'shots.js')
  const src = p => {
    const f = join(HERMES, 'apps', p)
    if (!existsSync(f)) throw new Error(`Hermes source missing: ${f}`)
    return f
  }
  await esbuild.build({
    entryPoints: [join(RENDER, 'shots-entry.mjs')],
    bundle: true,
    format: 'iife',
    platform: 'browser',
    outfile: out,
    nodePaths: [deps, HERMES_MODULES],
    alias: {
      '@hermes/plugin-sdk': join(RENDER, 'shots-sdk.mjs'),
      '@hermes/shared/color': src('shared/src/color.ts'),
      'rr-hermes/theme-presets': src('shared/src/theme-presets.ts'),
      'rr-hermes/theme-color': src('desktop/src/themes/color.ts')
    },
    define: { 'process.env.NODE_ENV': '"production"' },
    logLevel: 'error'
  })
  return readFileSync(out, 'utf8').replace(/<\/script/gi, '<\\/script')
}

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

function cdpSender(socket) {
  let id = 0
  const pending = new Map()
  socket.onmessage = ev => {
    const msg = JSON.parse(typeof ev.data === 'string' ? ev.data : String(ev.data))
    if (msg.id && pending.has(msg.id)) {
      const { ok, fail } = pending.get(msg.id)
      pending.delete(msg.id)
      if (msg.error) fail(new Error(msg.error.message))
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

async function startChrome(scratch) {
  const port = await freePort()
  const proc = spawn(
    CHROMIUM,
    [
      '--headless=new',
      '--no-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage',
      '--no-first-run',
      '--no-default-browser-check',
      '--hide-scrollbars',
      '--allow-file-access-from-files',
      `--window-size=${VIEW.width},${VIEW.height}`,
      `--user-data-dir=${join(scratch, 'chrome-profile')}`,
      `--remote-debugging-port=${port}`,
      'about:blank'
    ],
    { stdio: 'ignore' }
  )
  const deadline = Date.now() + 15_000
  let info = null
  while (Date.now() < deadline && !info) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`)
      if (r.ok) info = await r.json()
    } catch {
      await new Promise(r => setTimeout(r, 100))
    }
  }
  if (!info) {
    proc.kill('SIGKILL')
    throw new Error('chromium did not open its DevTools port within 15 s')
  }
  const ws = new WebSocket(info.webSocketDebuggerUrl)
  await new Promise((ok, fail) => {
    ws.onopen = ok
    ws.onerror = () => fail(new Error('DevTools websocket failed'))
  })
  return { proc, ws, send: cdpSender(ws) }
}

// Masked labels only: an address-looking token must carry the mask stars.
// example@example.com is the plugin's own mask-setting help text.
const ADDRESS = /[\w.+*-]+@[\w*-]+(?:\.[\w*-]+)+/g
function checkText(name, text) {
  const leaks = (text.match(ADDRESS) ?? []).filter(a => !/^[a-z]\*\*\*@e\*\*\*\.com$/.test(a) && a !== 'example@example.com')
  if (leaks.length) throw new Error(`${name}: unmasked address on screen: ${leaks.join(', ')}`)
  if (/Saving|Salvando/.test(text)) throw new Error(`${name}: a save note is on screen`)
}

async function shoot(chrome, page, cfg, scale) {
  const { send } = chrome
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
  try {
    await send('Page.enable', {}, sessionId)
    await send('Emulation.setDeviceMetricsOverride', { ...VIEW, deviceScaleFactor: scale, mobile: false }, sessionId)
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: cfg.theme }] }, sessionId)
    await send('Page.navigate', { url: pathToFileURL(page).href }, sessionId)
    const evalExpr = async expression => {
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, timeout: PAGE_MS }, sessionId)
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? 'page evaluation failed')
      return r.result.value
    }
    await new Promise(r => setTimeout(r, 300))
    const ready = await evalExpr(
      `(async()=>{const t0=Date.now();while(!window.__rr||(!window.__rr.ready&&!window.__rr.error)){if(Date.now()-t0>${PAGE_MS})return 'timeout';await new Promise(r=>setTimeout(r,20));}return window.__rr.error||'ok';})()`
    )
    if (ready !== 'ok') throw new Error(`page not ready: ${ready}`)
    const res = await evalExpr(`window.__rr.show(${JSON.stringify(cfg.tab)})`)
    const shot = await send('Page.captureScreenshot', { format: 'png', clip: { ...res.clip, scale: 1 }, captureBeyondViewport: false }, sessionId)
    return { png: Buffer.from(shot.data, 'base64'), ...res }
  } finally {
    await send('Target.closeTarget', { targetId }).catch(() => {})
  }
}

async function main() {
  const opt = args(process.argv.slice(2))
  const deps = nodeModules()
  const css = hermesCss()
  mkdirSync(opt.out, { recursive: true })
  const scratch = mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'rr-shots-'))
  let chrome = null
  try {
    const js = await bundle(scratch, deps)
    chrome = await startChrome(scratch)
    console.log(`# Hermes CSS: ${css}`)
    for (const theme of THEMES) {
      for (const lang of LANGS) {
        for (const tab of TABS) {
          const name = `${tab}-${lang}-${theme}`
          const cfg = { tab, lang, theme }
          const page = join(scratch, `${name}.html`)
          writeFileSync(
            page,
            [
              `<!doctype html><html lang="${lang}"><head><meta charset="utf-8">`,
              '<meta name="color-scheme" content="light dark">',
              `<link rel="stylesheet" href="${pathToFileURL(css).href}">`,
              '</head><body>',
              `<script>window.__rrShot=${JSON.stringify(cfg)};</script>`,
              `<script>${js}</script>`,
              '</body></html>'
            ].join('\n')
          )
          const res = await shoot(chrome, page, cfg, opt.scale)
          checkText(name, res.text)
          const file = join(opt.out, `${name}.png`)
          writeFileSync(file, res.png)
          console.log(`${file}  ${Math.round(res.clip.width)}x${Math.round(res.clip.height)} css px  ${Math.ceil(res.png.length / 1024)} KB  chip=${res.chip}`)
        }
      }
    }
    if (opt.docs) {
      const docs = join(REPO, 'docs', 'screenshots')
      mkdirSync(docs, { recursive: true })
      for (const f of readdirSync(docs)) if (f.endsWith('.png') && !README.includes(f.slice(0, -4))) rmSync(join(docs, f))
      for (const n of README) {
        copyFileSync(join(opt.out, `${n}.png`), join(docs, `${n}.png`))
        console.log(`docs/screenshots/${n}.png`)
      }
    }
  } finally {
    if (chrome) {
      try {
        chrome.ws.close()
      } catch {
        // already closed
      }
      chrome.proc.kill('SIGKILL')
    }
    rmSync(scratch, { recursive: true, force: true })
  }
}

main().catch(err => {
  console.error(`screenshots: ${err.message}`)
  process.exit(1)
})
