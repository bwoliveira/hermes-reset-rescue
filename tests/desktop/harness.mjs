// jsdom + React harness for desktop/plugin.js with a stubbed SDK.
// Resolves react, react-dom, jsdom, nanostores, @nanostores/react and esbuild
// from RR_NODE_MODULES, the repo's node_modules, or the installed Hermes
// Desktop's node_modules. Missing packages => skip with the reason, except
// under CI=1/true, where it fails (a silent skip would drop the UI suite).
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { afterEach } from 'node:test'

const HERE = dirname(fileURLToPath(import.meta.url))
export const REPO = resolve(HERE, '..', '..')
export const PLUGIN_PATH = process.env.RR_PLUGIN_JS ? resolve(process.env.RR_PLUGIN_JS) : join(REPO, 'desktop', 'plugin.js')
const NEEDED = ['react', 'react-dom', 'jsdom', 'nanostores', '@nanostores/react', 'esbuild']

function candidates() {
  const list = []
  if (process.env.RR_NODE_MODULES) list.push(resolve(process.env.RR_NODE_MODULES))
  list.push(join(REPO, 'node_modules'), join(REPO, 'tests', 'desktop', 'node_modules'))
  list.push('/usr/local/lib/hermes-agent/apps/desktop/node_modules')
  return list
}

export function resolveNodeModules() {
  for (const dir of candidates()) {
    if (NEEDED.every(p => existsSync(join(dir, p, 'package.json')))) return { dir, missing: [] }
  }
  const dir = candidates()[0]
  return { dir: null, missing: NEEDED.filter(p => !existsSync(join(dir, p, 'package.json'))), searched: candidates() }
}

const IS_CI = /^(1|true)$/i.test(process.env.CI ?? '')

/**
 * A scratch directory outside the real Hermes profile.
 *
 * `os.tmpdir()` reads TMPDIR, which in this environment is
 * a directory under `~/.hermes/cache` — inside the real profile, which Hermes resolves
 * back to `~/.hermes`. A harness that only guarded HERMES_HOME still wrote real
 * state (or died with EROFS against a read-only profile). The guard mirrors
 * `tests/support/isolation.py`: refuse TMPDIR under `~/.hermes`, and default to
 * `/var/tmp`.
 */
function scratchRoot() {
  const native = join(process.env.HOME || '/root', '.hermes')
  const tempRoot = process.env.RR_TEMP_ROOT || '/var/tmp'
  const candidate = process.env.RR_SCRATCH || join(tempRoot, 'rr-scratch')
  const resolved = resolve(candidate)
  if (resolved === native || resolved.startsWith(native + '/')) {
    throw new Error(`RR_SCRATCH=${resolved} is inside the real Hermes profile ${native}; temp state must live outside it`)
  }
  const tmp = resolve(process.env.TMPDIR || tmpdir())
  if (tmp === native || tmp.startsWith(native + '/')) {
    throw new Error(`TMPDIR=${tmp} is inside the real Hermes profile ${native}; export TMPDIR outside it (scripts/check.sh does)`)
  }
  return resolved
}

/** Returns `{ skip: string|false }` for node:test, or throws under CI. */
export function harnessAvailability() {
  const nm = resolveNodeModules()
  if (nm.dir) return { skip: false, dir: nm.dir }
  const reason = `desktop harness deps missing (${nm.missing.join(', ')}); searched ${nm.searched.join(', ')}; set RR_NODE_MODULES`
  if (IS_CI) throw new Error(reason)
  console.log(`# SKIP ${reason}`)
  return { skip: reason, dir: null }
}

let loaded = null

/** Bundle plugin.js + the SDK stub + react-dom into one ESM module and import it. */
export async function loadBundle() {
  if (loaded) return loaded
  const { dir } = resolveNodeModules()
  if (!dir) throw new Error('harness deps missing')
  installDom(dir)
  const req = createRequire(join(dir, 'noop.js'))
  const esbuild = req('esbuild')
  const work = mkdtempSync(join(scratchRoot(), 'rr-desktop-'))
  const entry = join(work, 'entry.mjs')
  writeFileSync(
    entry,
    [
      `export { default as plugin } from ${JSON.stringify(PLUGIN_PATH)}`,
      `export * from ${JSON.stringify(PLUGIN_PATH)}`,
      `export * as sdk from ${JSON.stringify(join(HERE, 'sdk-stub.mjs'))}`,
      `export { createRoot } from 'react-dom/client'`,
      `export { act } from 'react'`,
      `export { jsx } from 'react/jsx-runtime'`
    ].join('\n')
  )
  const out = join(work, 'bundle.mjs')
  await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    format: 'esm',
    platform: 'browser',
    outfile: out,
    nodePaths: [dir],
    alias: { '@hermes/plugin-sdk': join(HERE, 'sdk-stub.mjs') },
    define: { 'process.env.NODE_ENV': '"development"' },
    logLevel: 'error'
  })
  loaded = await import(pathToFileURL(out).href)
  rmSync(work, { recursive: true, force: true })
  return loaded
}

const openPorts = new Set()

function installDom(dir) {
  if (globalThis.__rrDom) return
  const req = createRequire(join(dir, 'noop.js'))
  const { JSDOM } = req('jsdom')
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/', pretendToBeVisual: true })
  globalThis.__rrDom = dom
  const g = globalThis
  g.window = dom.window
  g.document = dom.window.document
  for (const k of ['HTMLElement', 'Node', 'Element', 'Event', 'MouseEvent', 'KeyboardEvent', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) {
    if (!(k in g) || k === 'Event') g[k] = dom.window[k]
  }
  Object.defineProperty(g, 'navigator', { value: dom.window.navigator, configurable: true })
  g.IS_REACT_ACT_ENVIRONMENT = true
  // React's scheduler opens MessageChannel ports that keep node:test alive; track and close them.
  const Base = globalThis.MessageChannel
  g.MessageChannel = class TrackedChannel extends Base {
    constructor() {
      super()
      openPorts.add(this.port1)
      openPorts.add(this.port2)
    }
  }
}

export function closeDom() {
  for (const p of openPorts) p.close()
  openPorts.clear()
  globalThis.__rrDom?.window.close()
}

/** A scripted ctx.rest. `routes` maps "METHOD /path" to a handler(body) or value. */
export function makeRest(routes = {}) {
  const calls = []
  const pending = []
  const rest = async (path, opts = {}) => {
    const method = (opts.method ?? 'GET').toUpperCase()
    const key = `${method} ${path.split('?')[0]}`
    calls.push({ key, path, method, body: opts.body, timeoutMs: opts.timeoutMs })
    const h = routes[key]
    if (h === undefined) throw Object.assign(new Error(`HTTP 404 ${key}`), { status: 404 })
    const v = typeof h === 'function' ? h(opts.body, { path, calls }) : h
    return await v
  }
  return { rest, calls, routes, pending }
}

/** Deferred promise for holding a route open. */
export function deferred() {
  let resolveFn
  let rejectFn
  const promise = new Promise((res, rej) => {
    resolveFn = res
    rejectFn = rej
  })
  return { promise, resolve: resolveFn, reject: rejectFn }
}

/**
 * Asserts that a DOM query found nothing.
 *
 * Never hand a rendered element to `assert.equal`/`deepEqual`: on failure Node
 * formats the diff with `inspect(..., { depth: 1000, getters: true })`, and a
 * React-owned element carries enumerable `__reactFiber$…`/`__reactProps$…`
 * expandos that reach the whole fiber tree and the jsdom window. That one
 * failing line grew a test process past 80 GB. Here the failure message is the
 * element's tag and a short slice of its markup.
 */
export function absent(el, message) {
  if (el == null) return
  const markup = String(el.outerHTML ?? el).slice(0, 200)
  assert.fail(`${message ? `${message}: ` : ''}expected no element, found <${el.tagName?.toLowerCase?.() ?? typeof el}> ${markup}`)
}

/**
 * Every mounted harness still alive. A root `afterEach` unmounts them, so a
 * test that throws before its own `unmount()` still stops its intervals and
 * timers and removes its React root: nothing keeps re-rendering into the next
 * test or keeps the event loop alive.
 */
const live = new Set()
export const liveMounts = () => live.size
export async function teardownAll() {
  for (const h of [...live]) await h.unmount()
}
afterEach(teardownAll)

/** Register the plugin against a fake ctx and mount its status-bar contribution. */
export async function mountPlugin({ routes = {}, profile = 'default', connection = null, locale = 'en', storage = new Map() } = {}) {
  const b = await loadBundle()
  const { sdk, plugin, createRoot, act, jsx } = b
  sdk.host.state.profile.set(profile)
  sdk.host.state.connectionId?.set(connection)
  sdk.$locale.set(locale)
  sdk.navigations.length = 0
  sdk.notifications.length = 0
  sdk.confirmCalls.length = 0
  const restApi = makeRest(routes)
  const registered = []
  const disposers = []
  const timers = new Set()
  const intervals = []
  const tickers = []
  const opened = []
  const ctx = {
    source: `plugin:${plugin.id}`,
    register: c => {
      registered.push(c)
      return () => {}
    },
    registerMany: cs => {
      registered.push(...cs)
      return () => {}
    },
    onDispose: fn => disposers.push(fn),
    setTimeout: (fn, ms) => {
      const h = setTimeout(() => {
        timers.delete(h)
        fn()
      }, ms)
      timers.add(h)
      return () => {
        clearTimeout(h)
        timers.delete(h)
      }
    },
    setInterval: (fn, ms) => {
      intervals.push(ms)
      const ticker = { ms, fn, live: true }
      tickers.push(ticker)
      const h = setInterval(fn, ms)
      timers.add(h)
      return () => {
        ticker.live = false
        clearInterval(h)
        timers.delete(h)
      }
    },
    rest: restApi.rest,
    socket: () => {
      throw new Error('ctx.socket must not be used')
    },
    i18n: sdk.createI18n(plugin.id),
    storage: {
      get: (k, f) => (storage.has(k) ? storage.get(k) : f),
      set: (k, v) => storage.set(k, v),
      remove: k => storage.delete(k)
    },
    os: {
      openExternal: async url => {
        opened.push(url)
        return true
      }
    }
  }
  plugin.register(ctx)
  const chip = registered.find(c => c.area === sdk.STATUSBAR_AREAS.right)
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  await act(async () => {
    root.render(chip ? chip.render() : jsx('div', {}))
  })
  const h = {
    sdk,
    plugin,
    ctx,
    registered,
    chip,
    calls: restApi.calls,
    intervals,
    tickers,
    opened,
    storage,
    routes: restApi.routes,
    container,
    act,
    async flush(n = 3) {
      for (let i = 0; i < n; i += 1) {
        await act(async () => {
          await new Promise(r => setTimeout(r, 0))
        })
      }
    },
    // performance.now, not Date.now: tests that pin Date.now must still time out.
    async waitFor(pred, label = 'condition', ms = 2000) {
      const start = performance.now()
      for (;;) {
        let v
        try {
          v = pred()
        } catch {
          v = false
        }
        if (v) return v
        if (performance.now() - start > ms) throw new Error(`timeout waiting for ${label}`)
        await act(async () => {
          await new Promise(r => setTimeout(r, 10))
        })
      }
    },
    q: sel => document.body.querySelector(sel),
    qa: sel => [...document.body.querySelectorAll(sel)],
    text: sel => (sel ? document.body.querySelector(sel)?.textContent ?? null : document.body.textContent),
    async click(elOrSel) {
      const el = typeof elOrSel === 'string' ? document.body.querySelector(elOrSel) : elOrSel
      if (!el) throw new Error(`no element ${elOrSel}`)
      await act(async () => {
        el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
      })
    },
    /** Type into a controlled input/select the way a user does: set the value
     *  through the native setter (React's value tracker ignores a plain
     *  assignment) and fire the event React listens for. */
    async type(elOrSel, value, eventName) {
      const el = typeof elOrSel === 'string' ? document.body.querySelector(elOrSel) : elOrSel
      if (!el) throw new Error(`no element ${elOrSel}`)
      const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
      const name = eventName ?? (el.tagName === 'SELECT' ? 'change' : 'input')
      await act(async () => {
        if (setter) setter.call(el, value)
        else el.value = value
        el.dispatchEvent(new window.Event(name, { bubbles: true }))
      })
    },
    async setProfile(p) {
      await act(async () => {
        sdk.host.state.profile.set(p)
      })
    },
    /** The host's activation of (connectionId, profile): both atoms in one
     *  publication, as ensureGatewayAgent does. */
    async setOwner(connectionId, p = sdk.host.state.profile.get()) {
      await act(async () => {
        sdk.host.state.connectionId.set(connectionId)
        sdk.host.state.profile.set(p)
      })
    },
    async setLocale(l) {
      await act(async () => {
        sdk.$locale.set(l)
      })
    },
    /** Timers and intervals the plugin started through ctx and has not cleared. */
    activeTimers: () => timers.size,
    /** Idempotent; also run by the root afterEach when a test throws first. */
    async unmount() {
      if (!live.has(h)) return
      live.delete(h)
      for (const ticker of tickers) ticker.live = false
      for (const t of timers) {
        clearTimeout(t)
        clearInterval(t)
      }
      timers.clear()
      try {
        await act(async () => root.unmount())
      } finally {
        container.remove()
        for (const d of disposers) d()
        sdk.$markerMode.set(false)
      }
    }
  }
  live.add(h)
  return h
}
