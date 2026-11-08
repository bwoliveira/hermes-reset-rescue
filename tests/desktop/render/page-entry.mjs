// Browser entry for the layout test. Bundled as an IIFE by popup-layout.test.mjs
// and loaded from a file:// page in headless Chromium, so the measurements come
// from a real layout engine (jsdom has none).
//
// The page reads ?case=a|b|c, mounts the real desktop/plugin.js chip with a
// scripted ctx.rest, and exposes window.__rr.measureAll(), which opens the
// popup and measures it on the Accounts, History and Settings tabs.
import plugin, { POPOVER_HEIGHT, popoverHeightFor } from '../../../desktop/plugin.js'
import * as sdk from '../sdk-stub.mjs'
import { createRoot } from 'react-dom/client'
import { CASES, LOCALES, SETTINGS } from './cases.mjs'
import { NOW, settingsWithAccounts } from '../fixtures.mjs'

// Same clock as the fixtures (popup.test.mjs does this too).
Date.now = () => NOW

// (f) a larger text scale, 15 px against the 13 px default. The popup's own
// sizes are px, so changing only a root font-size would change nothing; zoom
// scales the text and every box with it, as a machine with bigger text does.
if (window.__rrScale && window.__rrScale !== 1) document.documentElement.style.zoom = String(window.__rrScale)

const caseId = new URLSearchParams(location.search).get('case')
const status = CASES[caseId]
if (!status) throw new Error(`unknown case: ${caseId}`)

const sleep = ms => new Promise(r => setTimeout(r, ms))

async function until(pred, label, ms = 8000) {
  const t0 = performance.now()
  for (;;) {
    let ok = false
    try {
      ok = Boolean(pred())
    } catch {
      ok = false
    }
    if (ok) return
    if (performance.now() - t0 > ms) throw new Error(`timeout waiting for ${label}`)
    await sleep(20)
  }
}

const routes = {
  '/status': status,
  // D-030: GET /settings also lists the accounts with their overrides.
  '/settings': settingsWithAccounts(SETTINGS.fields, status),
  '/history': { items: [] }
}

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
  rest: async (path, opts = {}) => {
    const key = path.split('?')[0]
    if (!(key in routes)) throw Object.assign(new Error(`HTTP 404 ${key}`), { status: 404 })
    return routes[key]
  },
  socket: () => {
    throw new Error('ctx.socket must not be used')
  },
  i18n: sdk.createI18n(plugin.id),
  storage: { get: (_k, fallback) => fallback, set: () => {}, remove: () => {} },
  os: { openExternal: async () => true }
}

sdk.host.state.profile.set('default')
sdk.$locale.set(LOCALES[caseId] ?? 'en')
plugin.register(ctx)
const chip = registered.find(c => c.area === sdk.STATUSBAR_AREAS.right)
const container = document.createElement('div')
document.body.appendChild(container)
createRoot(container).render(chip.render())

function measure(view) {
  const pop = document.querySelector('[data-slot="popover-content"]')
  const scroll = document.querySelector('[data-rr-scroll]')
  const inner = scroll.firstElementChild
  const rect = pop.getBoundingClientRect()
  // `inner` has min-height 100%, so its own box never shows the content height.
  // The natural height is the last child's bottom plus the bottom padding.
  const last = inner.lastElementChild
  const innerTop = inner.getBoundingClientRect().top
  const padBottom = parseFloat(getComputedStyle(inner).paddingBottom) || 0
  const naturalHeight = last ? last.getBoundingClientRect().bottom - innerTop + padBottom : 0
  // D-030 adds the scope control and the Accounts-tab tags; D-034 the account
  // list: rows, their header, the expanded detail, each option, its
  // SegmentedControl and the early threshold. None may clip or overflow.
  const ids = ['data-rr-account', 'data-rr-credit', 'data-rr-scope', 'data-rr-account-row', 'data-rr-account-toggle', 'data-rr-account-detail', 'data-rr-option', 'data-rr-segments', 'data-rr-threshold', 'data-rr-tag']
  const rows = [...document.querySelectorAll(ids.map(a => `[${a}]`).join(', '))]
  const overflowRows = rows
    .filter(el => el.scrollWidth > el.clientWidth)
    .map(el => {
      const id = ids.find(a => el.hasAttribute(a))
      const owner = el.closest('[data-rr-account-row]')?.getAttribute('data-rr-account-row')
      return `${id}=${el.getAttribute(id)}${owner && id !== 'data-rr-account-row' ? `@${owner}` : ''}`
    })
  const accountRows = [...document.querySelectorAll('[data-rr-account-row]')]
  // D-034: every SegmentedControl stays inside the popup's content box.
  const box = inner.getBoundingClientRect()
  const segOut = [...document.querySelectorAll('[data-rr-segments] > *')]
    .filter(el => el.getBoundingClientRect().right > box.right - 1 || el.getBoundingClientRect().left < box.left)
    .map(el => el.closest('[data-rr-option]')?.getAttribute('data-rr-option') ?? '?')
  // D-031: CSS px under zoom (rects are visual px), and the empty space between
  // the footer's bottom edge and the scroll box's bottom, less the inner padding.
  const scale = inner.getBoundingClientRect().width / (inner.offsetWidth || 1) || 1
  const footerEl = document.querySelector('[data-rr-footer]')
  const footerGap = footerEl ? (scroll.getBoundingClientRect().bottom - footerEl.getBoundingClientRect().bottom) / scale - padBottom : null
  // The first four global setting rows, as rendered (the D-031 floor).
  const settingRows = [...document.querySelectorAll('[data-rr-setting]')].slice(0, 4).map(el => el.getBoundingClientRect().height / scale)
  // The natural height from the items themselves (sum of heights and gaps),
  // so a footer pinned to the bottom does not count the space above it.
  const kids = [...inner.children]
  const gap = parseFloat(getComputedStyle(inner).rowGap) || 0
  const padTop = parseFloat(getComputedStyle(inner).paddingTop) || 0
  const naturalCss = padTop + padBottom + kids.reduce((a, el) => a + el.getBoundingClientRect().height / scale, 0) + gap * Math.max(0, kids.length - 1)
  return {
    naturalCss,
    footerGap,
    scrollTop: scroll.scrollTop,
    settingRows,
    view,
    popupHeight: rect.height,
    popupWidth: rect.width,
    styleHeight: pop.style.height,
    scrollClientHeight: scroll.clientHeight,
    scrollHeight: scroll.scrollHeight,
    contentHeight: inner.getBoundingClientRect().height,
    naturalHeight,
    rowCount: rows.length,
    overflowRows,
    scrollWidth: scroll.scrollWidth,
    scrollClientWidth: scroll.clientWidth,
    accountRows: accountRows.length,
    texts: accountRows.map(r => r.innerText.replace(/\s+/g, ' ').trim()),
    options: [...document.querySelectorAll('[data-rr-account-detail] [data-rr-option]')].map(el => ({
      field: el.getAttribute('data-rr-option'),
      segments: [...el.querySelectorAll('[data-rr-segments] button')].map(b => b.textContent)
    })),
    threshold: !!document.querySelector('[data-rr-account-detail] [data-rr-threshold]'),
    segmentsOutside: segOut,
    tags: [...document.querySelectorAll('[data-rr-tag]')].map(el => el.textContent),
    viewportHeight: window.innerHeight
  }
}

/** The four sizes the popup height is computed from (plugin.js measureLayout). */
function layoutSizes() {
  const scroll = document.querySelector('[data-rr-scroll]')
  const sample = scroll.querySelector('[data-rr-measure]')
  // D-030: the header is the sample's own Accounts-tab header (plugin.js measureLayout).
  const [header, sort, account, footer] = sample.children
  // The same unit conversion as plugin.js measureLayout (visual px to CSS px).
  const scale = sample.getBoundingClientRect().width / (sample.offsetWidth || 1) || 1
  const h = el => el.getBoundingClientRect().height / scale
  return { header: h(header), sort: h(sort), account: h(account), footer: h(footer) }
}

async function measureAll() {
  const n = status.accounts.length
  // D-031 (i): with no account the list is the empty paragraph.
  const accountRows = () => (n === 0 ? (document.querySelector('[data-rr-empty]') ? 0 : -1) : document.querySelectorAll('[data-rr-account]').length)
  await until(() => document.querySelector('[data-rr-chip]'), 'chip')
  document.querySelector('[data-rr-chip]').click()
  await until(() => document.querySelector('[data-slot="popover-content"]') && accountRows() === n, 'popover with accounts')
  // Let the ResizeObserver remeasure settle before reading the height.
  await sleep(100)
  // The height the runtime set, and the formula over the sizes it measured.
  const sizes = layoutSizes()
  const out = { accounts: { ...measure('accounts'), sizes, formula: popoverHeightFor(5, sizes) } }

  document.querySelector('[data-rr-view="history"]').click()
  await until(
    () => document.querySelector('[data-rr-history], [data-rr-history-empty], [data-rr-history-error]'),
    'history tab'
  )
  out.history = measure('history')

  document.querySelector('[data-rr-view="back"]').click()
  await until(() => accountRows() === n, 'accounts after history')

  document.querySelector('[data-rr-settings="open"]').click()
  await until(() => document.querySelectorAll('[data-rr-setting]').length === SETTINGS.fields.length, 'settings tab')
  await sleep(100)
  out.settings = measure('settings')

  // D-034: Settings > Per account (the account list), collapsed and with one
  // account expanded: the one with early redeem on (its threshold shows) when
  // the case has it, else the first. Only with two or more accounts (D-031
  // cases g and i have no switch).
  if (!document.querySelector('[data-rr-scope-option="account"]')) return out
  document.querySelector('[data-rr-scope-option="account"]').click()
  await until(() => document.querySelectorAll('[data-rr-account-row]').length === n, 'per-account list')
  out.perAccount = measure('perAccount')
  const rows = [...document.querySelectorAll('[data-rr-account-row]')]
  const target = rows.find(r => r.getAttribute('data-rr-account-row') === 'acct_2' && /early_redeem_enabled/.test(JSON.stringify(status.accounts[2]?.overrides ?? {}))) ?? rows[0]
  target.querySelector('[data-rr-account-toggle]').click()
  await until(() => target.querySelector('[data-rr-account-detail]'), 'account detail')
  // Scroll the open account into view inside the fixed height (for the screenshot).
  target.scrollIntoView({ block: 'start' })
  await sleep(100)
  out.perAccountExpanded = measure('perAccountExpanded')
  return out
}

window.__rr = {
  case: caseId,
  constants: { POPOVER_HEIGHT, popoverHeightFor: popoverHeightFor(5) },
  measureAll
}
// Mounted and status-bearing: the test drives measureAll() from here.
until(() => document.querySelector('[data-rr-chip]'), 'chip').then(
  () => {
    window.__rr.ready = true
  },
  err => {
    window.__rr.error = String(err && err.message)
  }
)
