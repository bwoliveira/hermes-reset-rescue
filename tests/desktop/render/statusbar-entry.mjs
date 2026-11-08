// Browser entry for the D-032 status-bar typography test. Bundled as an IIFE by
// statusbar-font.test.mjs and loaded from a file:// page in headless Chromium.
//
// The page builds a stand-in of Hermes' status bar with the markup Hermes
// renders, mounts the real desktop/plugin.js chip inside the same right-hand
// group, and exposes window.__rrSb.measure(): the computed typography of the
// chip and of the native items next to it.
//
// Markup mirrored from /usr/local/lib/hermes-agent/apps/desktop/src:
//   app/shell/statusbar-controls.tsx:114-121  the <footer data-slot="statusbar">
//   app/shell/statusbar-controls.tsx:131      the right-hand item group
//   app/shell/statusbar-controls.tsx:30-31    STATUSBAR_ACTION_CLASS (every item)
//   app/shell/statusbar-controls.tsx:248-253  icon + <span truncate> label + detail
//   app/shell/statusbar-controls.tsx:354-366  the action <button>
//   app/shell/hooks/use-statusbar-items.tsx:625-649  the Agents item (failed: text-destructive)
//   app/shell/hooks/use-statusbar-items.tsx:116      an update item (text-primary)
import plugin from '../../../desktop/plugin.js'
import * as sdk from '../sdk-stub.mjs'
import { createRoot } from 'react-dom/client'
import { NOW, calm, mixed } from '../fixtures.mjs'

Date.now = () => NOW

const STATUSBAR_ACTION_CLASS =
  'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground disabled:cursor-default disabled:opacity-45'
const FOOTER_CLASS =
  'flex h-5 shrink-0 items-stretch justify-between gap-2 bg-(--ui-sidebar-surface-background) px-1 py-0 text-(--ui-text-tertiary) [-webkit-app-region:no-drag]'
const GROUP_CLASS = 'flex min-w-0 items-stretch gap-0.5 overflow-x-clip'

const failed = () => {
  const st = mixed()
  st.attention.failed = 1
  return st
}
/** Chip state -> its fixture and the native item class that carries the same look. */
const STATES = {
  bankedNext: { status: calm, nativeExtra: '' },
  expiring: { status: mixed, nativeExtra: 'text-primary hover:text-primary' },
  failed: { status: failed, nativeExtra: 'text-destructive hover:text-destructive' }
}

const cfg = window.__rrSbCfg
const state = STATES[cfg.state]
if (!state) throw new Error(`unknown state: ${cfg.state}`)
const root = document.documentElement
// Hermes' own text settings: the theme writes --dt-base-size and --dt-font-sans
// on the root (src/themes/context.tsx:294), the zoom setting sets the root zoom
// (src/lib/browser-zoom.ts:18).
if (cfg.baseSize) root.style.setProperty('--dt-base-size', cfg.baseSize)
if (cfg.fontSans) root.style.setProperty('--dt-font-sans', cfg.fontSans)
if (cfg.zoom && cfg.zoom !== 1) root.style.zoom = String(cfg.zoom)

function el(tag, className, children = []) {
  const n = document.createElement(tag)
  if (className) n.className = className
  for (const c of children) n.append(c)
  return n
}

function nativeItem(id, label, detail, extra) {
  const icon = el('span', 'codicon size-3')
  const kids = [icon, el('span', 'truncate', [label])]
  if (detail) kids.push(el('span', 'truncate text-muted-foreground/80', [detail]))
  const b = el('button', [STATUSBAR_ACTION_CLASS, extra].filter(Boolean).join(' '), kids)
  b.type = 'button'
  b.dataset.native = id
  return b
}

const footer = el('footer', FOOTER_CLASS)
footer.dataset.slot = 'statusbar'
const left = el('div', GROUP_CLASS)
const right = el('div', GROUP_CLASS)
right.append(nativeItem('agents', 'Agents', '1 subagent', state.nativeExtra), nativeItem('memory', 'Memory', null, ''))
// Hermes mounts a render() contribution with no wrapper element
// (contrib/react/boundary.tsx:23-25); display: contents keeps the chip a flex
// child of the group, as it is there.
const slot = el('div', '')
slot.style.display = 'contents'
right.append(slot)
footer.append(left, right)
document.body.append(footer)

const registered = []
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
  onDispose: () => {},
  setTimeout: (fn, ms) => {
    const h = window.setTimeout(fn, ms)
    return () => window.clearTimeout(h)
  },
  setInterval: (fn, ms) => {
    const h = window.setInterval(fn, ms)
    return () => window.clearInterval(h)
  },
  rest: async path => {
    if (path.split('?')[0] === '/status') return state.status()
    throw Object.assign(new Error(`HTTP 404 ${path}`), { status: 404 })
  },
  socket: () => {
    throw new Error('ctx.socket must not be used')
  },
  i18n: sdk.createI18n(plugin.id),
  storage: { get: (_k, fallback) => fallback, set: () => {}, remove: () => {} },
  os: { openExternal: async () => true }
}

sdk.host.state.profile.set('default')
sdk.$locale.set('en')
plugin.register(ctx)
const chip = registered.find(c => c.area === sdk.STATUSBAR_AREAS.right)
createRoot(slot).render(chip.render())

const PROPS = ['fontSize', 'fontWeight', 'lineHeight', 'fontFamily', 'color', 'fontStyle', 'letterSpacing']
function typo(node) {
  const cs = getComputedStyle(node)
  const out = {}
  for (const p of PROPS) out[p] = cs[p]
  out.height = node.getBoundingClientRect().height
  return out
}

window.__rrSb = {
  measure() {
    const ours = document.querySelector('[data-rr-chip]')
    const agents = document.querySelector('[data-native="agents"]')
    const memory = document.querySelector('[data-native="memory"]')
    const label = n => n.querySelector(':scope > span.truncate') ?? n
    return {
      kind: ours.dataset.rrChip,
      text: ours.textContent,
      rootFontSize: getComputedStyle(root).fontSize,
      ours: typo(ours),
      oursLabel: typo(label(ours)),
      agents: typo(agents),
      agentsLabel: typo(label(agents)),
      memory: typo(memory),
      memoryLabel: typo(label(memory))
    }
  }
}

const t0 = performance.now()
;(function poll() {
  const n = document.querySelector('[data-rr-chip]')
  if (n && n.dataset.rrChip === cfg.state) window.__rrSb.ready = true
  else if (performance.now() - t0 > 8000) window.__rrSb.error = `chip never reached ${cfg.state} (now ${n?.dataset.rrChip})`
  else setTimeout(poll, 20)
})()
