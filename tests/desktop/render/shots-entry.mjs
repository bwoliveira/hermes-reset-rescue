// Browser entry for scripts/screenshots.mjs (launch screenshots, not a test).
//
// Mounts the real desktop/plugin.js chip in a stand-in of Hermes' status bar,
// paints Hermes' default skin ("nous") in light or dark the way the Desktop
// does, opens the popup on one tab and reports the rectangle to capture.
//
// Theme: a replica of applyTheme() in
// /usr/local/lib/hermes-agent/apps/desktop/src/themes/context.tsx, fed by the
// installed Hermes source itself (palettes, color math), bundled read-only:
//   html.dark toggled, color-scheme, data-hermes-theme/mode, and the inline
//   --theme-* seeds, --theme-mix-* knobs and --dt-* palette on <html>.
// Language: the Desktop locale ($locale, 'en' or 'pt-br'); the plugin's own
// language setting stays 'auto', so it follows the Desktop.
import plugin from '../../../desktop/plugin.js'
import * as sdk from '@hermes/plugin-sdk'
import { createRoot } from 'react-dom/client'
import { THEME_PRESET_PALETTES } from 'rr-hermes/theme-presets'
import { ensureContrast, mix } from '@hermes/shared/color'
import { harmonize, readableInk } from 'rr-hermes/theme-color'
import { SETTINGS } from './cases.mjs'
import { NOW, account, credit, iso, overridden, settingsWithAccounts, status } from '../fixtures.mjs'

Date.now = () => NOW

const cfg = window.__rrShot
const root = document.documentElement

// ---- theme (context.tsx applyTheme, skin "nous", no accent override) ----
const EMOJI_FALLBACK = '"Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji", emoji'
// presets.ts SYSTEM_SANS / SYSTEM_MONO (DEFAULT_TYPOGRAPHY; nous adds no font).
const SYSTEM_SANS = '"Segoe WPC", "Segoe UI", -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif, ' + EMOJI_FALLBACK
const SYSTEM_MONO =
  '"JetBrains Mono", "Cascadia Code", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "Noto Mono", "SF Mono", ui-monospace, Menlo, Monaco, Consolas, monospace, ' +
  EMOJI_FALLBACK

function applyTheme(mode) {
  const seed = THEME_PRESET_PALETTES.nous
  const c = mode === 'dark' ? seed.darkColors : seed.colors
  const isDark = mode === 'dark'
  const midground = c.midground ?? c.ring
  root.style.setProperty('color-scheme', mode)
  root.dataset.hermesTheme = 'nous'
  root.dataset.hermesMode = mode
  root.classList.toggle('dark', isDark)
  const vars = {
    '--theme-foreground': c.foreground,
    '--theme-primary': c.primary,
    '--theme-secondary': c.secondary,
    '--theme-accent-soft': c.accent,
    '--theme-midground': midground,
    '--theme-warm': c.primary,
    '--theme-background-seed': c.background,
    '--theme-sidebar-seed': c.sidebarBackground ?? c.background,
    '--theme-card-seed': c.card,
    '--theme-elevated-seed': c.popover,
    '--theme-bubble-seed': c.userBubble ?? c.popover,
    '--theme-mix-chrome': isDark ? '74%' : '92%',
    '--theme-mix-sidebar': '100%',
    '--theme-mix-card': isDark ? '38%' : '22%',
    '--theme-mix-elevated': isDark ? '46%' : '28%',
    '--theme-mix-bubble': isDark ? '46%' : '0%',
    '--dt-primary-foreground': c.primaryForeground,
    '--dt-secondary-foreground': c.secondaryForeground,
    '--dt-accent-foreground': c.accentForeground,
    '--dt-border': c.border,
    '--dt-input': c.input,
    '--dt-ring': c.ring,
    '--dt-muted': c.muted,
    '--dt-midground-foreground': c.midgroundForeground ?? readableInk(midground),
    '--dt-primary-solid': ensureContrast(c.primary, '#fcfcfc', 4.5),
    '--dt-primary-solid-foreground': '#fcfcfc',
    '--dt-composer-ring': c.composerRing ?? midground,
    '--dt-destructive': c.destructive,
    '--dt-destructive-foreground': c.destructiveForeground,
    '--dt-sidebar-border': c.sidebarBorder ?? c.border,
    '--dt-user-bubble-border': c.userBubbleBorder ?? c.border,
    '--ui-success': harmonize('#10b981', midground, 0.25),
    '--dt-font-sans': SYSTEM_SANS,
    '--dt-font-mono': SYSTEM_MONO,
    '--noise-opacity-mul': isDark ? 'calc(0.04 / 0.21)' : 'calc(0.34 / 0.21)'
  }
  for (const [k, v] of Object.entries(vars)) root.style.setProperty(k, v)
  // index.html's boot pre-paint (the window background before the app paints).
  root.style.backgroundColor = mix(c.background, isDark ? '#0d0d0e' : '#f3f3f3', isDark ? 0.26 : 0.08)
}
applyTheme(cfg.theme)
root.lang = cfg.lang === 'pt-BR' ? 'pt-BR' : 'en'

// ---- demo data: five masked placeholder accounts, never real labels ----
// Kept consistent with the Settings tab (SETTINGS.fields: lead 10 min, early
// threshold 0 %) and with what the engine would really have scheduled:
// - every banked reset is inside the 48 h horizon (fire.HORIZON_S), so each one
//   has its armed primary at expiry - lead_minutes and backup at expiry -
//   backup_lead_minutes. (A reset beyond the horizon has no slot; the plugin
//   then shows the projected expiry - lead time, which would read as booked.)
// - a paused account has nothing scheduled (D-030: `account_paused` for every
//   role; the planner drops its slots), so it lists no banked reset here.
const setting = key => SETTINGS.fields.find(f => f.key === key).value
const LEAD_MIN = setting('lead_minutes')
const GLOBAL_PCT = setting('early_redeem_remaining_pct')
const HORIZON_H = 48
const MIN = 60_000
const BACKUP_LEAD = 4 // settings.MAX_BACKUP_LEAD_MINUTES; not a Settings field
const QUOTA = { plan_type: 'plus', five_hour_remaining_pct: 82.4, weekly_remaining_pct: 40 }
/** A banked reset `h` hours from NOW, armed the way the planner arms it:
 *  primary at expiry - lead_minutes, backup at expiry - backup_lead_minutes. */
function banked(key, h) {
  if (!(h > 0 && h <= HORIZON_H)) throw new Error(`${key}: ${h} h is outside the scheduling horizon`)
  const c = credit(key, { expiresIn: h, fireIn: h })
  const expires = Date.parse(c.expires_at)
  c.primary.fire_at = iso(expires - LEAD_MIN * MIN)
  c.backup.fire_at = iso(expires - BACKUP_LEAD * MIN)
  return c
}
/** overridden() with the global early threshold of SETTINGS.fields. */
function own(overrides = {}) {
  const extra = overridden(overrides)
  if (overrides.early_redeem_remaining_pct === undefined) extra.effective.early_redeem_remaining_pct = GLOBAL_PCT
  return extra
}
export const DEMO = status({
  settings: { lead_minutes: LEAD_MIN, backup_lead_minutes: BACKUP_LEAD, early_redeem_enabled: false, early_redeem_remaining_pct: GLOBAL_PCT },
  accounts: [
    // Two resets: the soonest expires in 9.6 h (inside the 24 h warning).
    account('acct_a', 'a***@e***.com', [banked('cr_a1', 9.6), banked('cr_a2', 33)], { quota: QUOTA, ...own() }),
    // Two resets.
    account('acct_b', 'b***@e***.com', [banked('cr_b1', 22), banked('cr_b2', 46)], {
      quota: { plan_type: 'pro', five_hour_remaining_pct: 64, weekly_remaining_pct: 71 },
      ...own()
    }),
    // One reset, its own override: early redeem on at 20 % (40 % weekly left:
    // not yet triggered, so the reset still waits for its rescue time).
    account('acct_c', 'c***@e***.com', [banked('cr_c1', 40)], { quota: QUOTA, ...own({ early_redeem_enabled: true, early_redeem_remaining_pct: 20 }) }),
    // No resets.
    account('acct_d', 'd***@e***.com', [], { quota: { plan_type: 'plus', five_hour_remaining_pct: 100, weekly_remaining_pct: 93 }, ...own() }),
    // Paused: not checked, nothing scheduled.
    account('acct_e', 'e***@e***.com', [], own({ monitored: false }))
  ]
})
/** The account opened on the Per-account shot (the one with an override). */
const EXPANDED = 'acct_c'

const routes = {
  '/status': DEMO,
  '/settings': settingsWithAccounts(SETTINGS.fields, DEMO),
  '/history': { items: [] }
}

// ---- status bar (statusbar-entry.mjs: the markup Hermes renders) ----
const FOOTER_CLASS =
  'flex h-5 shrink-0 items-stretch justify-between gap-2 bg-(--ui-sidebar-surface-background) px-1 py-0 text-(--ui-text-tertiary) [-webkit-app-region:no-drag]'
const GROUP_CLASS = 'flex min-w-0 items-stretch gap-0.5 overflow-x-clip'
function el(tag, className) {
  const n = document.createElement(tag)
  if (className) n.className = className
  return n
}
document.body.className = 'bg-background text-foreground'
document.body.style.margin = '0'
const footer = el('footer', FOOTER_CLASS)
footer.dataset.slot = 'statusbar'
footer.style.cssText = 'position:fixed;left:0;right:0;bottom:0;padding-right:16px'
const left = el('div', GROUP_CLASS)
const right = el('div', GROUP_CLASS)
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
sdk.$locale.set(cfg.lang === 'pt-BR' ? 'pt-br' : 'en')
plugin.register(ctx)
const chip = registered.find(c => c.area === sdk.STATUSBAR_AREAS.right)
createRoot(slot).render(chip.render())

// ---- drive ----
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
const $ = s => document.querySelector(s)

async function show(tab) {
  const n = DEMO.accounts.length
  await until(() => $('[data-rr-chip]'), 'chip')
  $('[data-rr-chip]').click()
  await until(() => $('[data-slot="popover-content"]') && document.querySelectorAll('[data-rr-account]').length === n, 'accounts')
  if (tab !== 'accounts') {
    $('[data-rr-settings="open"]').click()
    await until(() => document.querySelectorAll('[data-rr-setting]').length === SETTINGS.fields.length, 'settings')
  }
  if (tab === 'per-account') {
    $('[data-rr-scope-option="account"]').click()
    await until(() => document.querySelectorAll('[data-rr-account-row]').length === n, 'per-account list')
    const row = $(`[data-rr-account-row="${EXPANDED}"]`)
    row.querySelector('[data-rr-account-toggle]').click()
    await until(() => row.querySelector('[data-rr-account-detail]'), 'account detail')
    // Bring the open account up so its options show inside the fixed height.
    const scroll = $('[data-rr-scroll]')
    scroll.scrollTop = row.offsetTop - 120
  }
  // Fonts, the ResizeObserver remeasure and the zoom-in animation settle.
  await document.fonts.ready
  await sleep(600)
  const pop = $('[data-slot="popover-content"]').getBoundingClientRect()
  const chipRect = $('[data-rr-chip]').getBoundingClientRect()
  const text = $('[data-slot="popover-content"]').innerText + '\n' + $('[data-rr-chip]').innerText
  // A tight frame: the popup border and shadow edge, plus the chip below.
  const PAD = 6
  const x = Math.max(0, Math.min(pop.left, chipRect.left) - PAD)
  const y = Math.max(0, pop.top - PAD)
  const r = Math.min(window.innerWidth, Math.max(pop.right, chipRect.right) + PAD)
  const b = Math.min(window.innerHeight, chipRect.bottom + 4)
  return { clip: { x, y, width: r - x, height: b - y }, text, chip: $('[data-rr-chip]').dataset.rrChip }
}

window.__rr = { show }
until(() => $('[data-rr-chip]'), 'chip').then(
  () => {
    window.__rr.ready = true
  },
  err => {
    window.__rr.error = String(err && err.message)
  }
)
