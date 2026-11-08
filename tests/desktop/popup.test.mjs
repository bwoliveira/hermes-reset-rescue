// D-024/D-025: the fixed popup height and the in-popup Settings tab.
//
// D-024: one fixed height, identical on every tab, sized from the real row
// layout so five full accounts (two two-line resets each, D-027) fit; more
// scroll inside. D-031: fewer accounts shrink the popup to clamp(natural,
// floor, cap), still one height on every tab.
// D-025: the Settings tab lists all twelve config_schema keys, translated
// through ctx.i18n, and saves each change immediately (no Save button).
import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'

import { account, credit, mixed, NOW, status } from './fixtures.mjs'
import { absent, closeDom, harnessAvailability, mountPlugin } from './harness.mjs'

const avail = harnessAvailability()
const skip = avail.skip
const realNow = Date.now
before(() => {
  Date.now = () => NOW
})
after(() => {
  Date.now = realNow
  closeDom()
})

let h = null
afterEach(async () => {
  if (h) await h.unmount()
  h = null
})

const SETTINGS = {
  fields: [
    { key: 'scan_interval_hours', type: 'int', value: 12, min: 1, max: 24 },
    { key: 'lead_minutes', type: 'int', value: 10, min: 5, max: 240 },
    { key: 'reminder_1_hours', type: 'int', value: 24, min: 0, max: 168 },
    { key: 'reminder_2_hours', type: 'int', value: 1, min: 0, max: 72 },
    { key: 'notify_target', type: 'str', value: '' },
    { key: 'auto_redeem_enabled', type: 'bool', value: true },
    { key: 'early_redeem_enabled', type: 'bool', value: false },
    { key: 'early_redeem_remaining_pct', type: 'int', value: 0, min: 0, max: 100 },
    { key: 'excluded_accounts', type: 'str', value: '' },
    { key: 'mask_labels', type: 'bool', value: true },
    { key: 'language', type: 'enum', value: 'auto', choices: ['auto', 'en', 'pt-BR'] },
    { key: 'dry_run', type: 'bool', value: false }
  ]
}

async function mount(st, opts = {}) {
  const { routes = {}, ...rest } = opts
  h = await mountPlugin({
    routes: { 'GET /status': typeof st === 'function' ? st : () => st, 'GET /settings': () => SETTINGS, ...routes },
    ...rest
  })
  await h.flush()
  return h
}

const chip = () => h.q('[data-rr-chip]')
const pop = () => h.q('[data-slot="popover-content"]')
const scroll = () => h.q('[data-rr-scroll]')

async function openPopover() {
  await h.click(chip())
  await h.flush()
  return pop()
}

async function openSettings() {
  const p = await openPopover()
  await h.click(p.querySelector('[data-rr-settings]'))
  await h.flush()
  return pop()
}

// ------------------------------------------------------------ D-024 height

test('D-024/D-031: the popover has one height, identical on every tab', { skip }, async () => {
  await mount(mixed())
  let p = await openPopover()
  const height = /(?:^|; ?)height: (\d+)px/.exec(p.getAttribute('style'))?.[1]
  assert.ok(height, 'a px height is set')
  const listHeight = scroll().getAttribute('style')
  assert.match(listHeight, /overflow-y: auto/)
  await h.click(p.querySelector('[data-rr-view]'))
  await h.flush()
  p = pop()
  assert.match(p.getAttribute('style'), new RegExp(`(^|; ?)height: ${height}px`), 'History keeps the height')
  assert.equal(scroll().getAttribute('style'), listHeight, 'History keeps the scroll box')
  await h.click(p.querySelector('[data-rr-settings]'))
  await h.flush()
  p = pop()
  assert.match(p.getAttribute('style'), new RegExp(`(^|; ?)height: ${height}px`), 'Settings keeps the height')
  assert.equal(scroll().getAttribute('style'), listHeight, 'Settings keeps the scroll box')
})

test('D-029: the height is the formula over the measured sizes', { skip }, async () => {
  const { popoverHeightFor } = await import('./harness.mjs').then(m => m.loadBundle())
  // Sizes measured in headless Chromium by tests/desktop/render (case a, the
  // popup at 13 px and at 15 px). Their sums are 828.63 and 829.67 px.
  const at13 = { header: 41.19, sort: 21, account: 125.45, footer: 27.19 }
  const at15 = { header: 41.23, sort: 21.04, account: 125.67, footer: 27.05 }
  assert.equal(popoverHeightFor(5, at13), 829)
  assert.equal(popoverHeightFor(5, at15), 830)
  // A sixth account adds one block and one gap (within a pixel of rounding).
  const step = popoverHeightFor(6, at13) - popoverHeightFor(5, at13)
  assert.ok(Math.abs(step - (at13.account + 12)) <= 1, `one more account adds ${step} px`)
})

test('D-029: without a layout engine the fallback fits five full accounts', { skip }, async () => {
  const { POPOVER_HEIGHT, ROW_LAYOUT, popoverHeightFor } = await import('./harness.mjs').then(m => m.loadBundle())
  // jsdom reports 0 px, so the runtime keeps ROW_LAYOUT until a real measurement.
  assert.equal(POPOVER_HEIGHT, popoverHeightFor(5))
  assert.equal(popoverHeightFor(5), popoverHeightFor(5, ROW_LAYOUT))
  assert.ok(ROW_LAYOUT.account >= 144, 'fallback block is at least the two-reset block')
})

// ------------------------------------------------------- D-031 auto height

test('D-031: clampHeight is clamp(natural, floor, cap), rounded up', { skip }, async () => {
  const { clampHeight } = await import('./harness.mjs').then(m => m.loadBundle())
  assert.equal(clampHeight(500, 360, 829), 500)
  assert.equal(clampHeight(500.2, 360, 829), 501)
  assert.equal(clampHeight(200, 360, 829), 360)
  assert.equal(clampHeight(1200, 360, 829), 829)
  assert.equal(clampHeight(829, 360, 829), 829)
  // A floor above the cap never makes the popup taller than five accounts.
  assert.equal(clampHeight(100, 900, 829), 829)
})

test('D-031: natural and floor heights from measured sizes; 360 px floor before measurement', { skip }, async () => {
  const { FLOOR_FALLBACK, floorHeightFor, naturalHeightFor } = await import('./harness.mjs').then(m => m.loadBundle())
  assert.equal(FLOOR_FALLBACK, 360)
  assert.equal(floorHeightFor(null), 360)
  assert.equal(floorHeightFor({ header: 41, footer: 27 }), 360, 'no settings sample measured yet')
  // padding 2 x 14, header, four rows, footer, two gaps of 12.
  assert.equal(floorHeightFor({ header: 41.2, footer: 27.2, settings: 200.1 }), Math.ceil(28 + 41.2 + 200.1 + 27.2 + 24))
  // padding 2 x 14, header, one gap, the body below it (list + footer).
  assert.equal(naturalHeightFor({ header: 41.2, body: 180.4 }), Math.ceil(28 + 41.2 + 12 + 180.4))
})

test('D-031: one account shrinks to the floor without a layout engine', { skip }, async () => {
  const { POPOVER_HEIGHT, FLOOR_FALLBACK } = await import('./harness.mjs').then(m => m.loadBundle())
  const one = status({ accounts: [account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40 })])] })
  await mount(one)
  const p = await openPopover()
  const height = Number(/(?:^|; ?)height: (\d+)px/.exec(p.getAttribute('style'))?.[1])
  assert.equal(height, FLOOR_FALLBACK)
  assert.ok(height < POPOVER_HEIGHT)
  assert.match(scroll().getAttribute('style'), /overflow-y: auto/)
})

test('D-031: six accounts keep the five-account cap without a layout engine', { skip }, async () => {
  const { POPOVER_HEIGHT } = await import('./harness.mjs').then(m => m.loadBundle())
  const labels = ['a', 'b', 'c', 'd', 'e', 'f']
  const six = status({ accounts: labels.map((l, i) => account(`acct_${l}`, `${l}***@e***.com`, [credit(`cr_${l}1`, { expiresIn: 40 + i }), credit(`cr_${l}2`, { expiresIn: 50 + i })])) })
  await mount(six)
  const p = await openPopover()
  assert.match(p.getAttribute('style'), new RegExp(`(^|; ?)height: ${POPOVER_HEIGHT}px`))
})

// ----------------------------------------------------- D-025 settings tab

test('D-025: Settings opens a tab inside the popup instead of navigating away', { skip }, async () => {
  await mount(mixed())
  const p = await openSettings()
  assert.deepEqual(h.sdk.navigations, [])
  assert.ok(p.querySelector('[data-rr-settings-tab]'), 'the settings tab rendered')
  assert.equal(p.querySelector('[data-rr-settings]').textContent, 'Back')
  await h.click(p.querySelector('[data-rr-settings]'))
  await h.flush()
  absent(pop().querySelector('[data-rr-settings-tab]'), 'Back returns to the list')
  assert.ok(pop().querySelector('[data-rr-account]'))
})

test('D-025: every config_schema key has a row and the right control', { skip }, async () => {
  await mount(mixed())
  const p = await openSettings()
  const rows = [...p.querySelectorAll('[data-rr-setting]')].map(n => n.dataset.rrSetting)
  assert.deepEqual(rows, SETTINGS.fields.map(f => f.key))
  assert.equal(rows.length, 12)
  // Toggle, number, select, text — one control per type, no Save button.
  // D-031: counted in the visible tab, not in the hidden floor sample.
  const tab = p.querySelector('[data-rr-settings-tab]')
  assert.equal(tab.querySelectorAll('[data-rr-toggle]').length, 4)
  assert.equal(tab.querySelectorAll('input[type="number"]').length, 5)
  assert.equal(tab.querySelectorAll('select').length, 1)
  assert.equal(tab.querySelectorAll('input[type="text"]').length, 2)
  absent(p.querySelector('[data-rr-save]'), 'there is no save button')
})

test('D-025: a toggle saves immediately and refreshes the popup data', { skip }, async () => {
  const after = mixed()
  after.settings.auto_redeem_enabled = false
  await mount(mixed(), {
    routes: { 'POST /settings': (body) => ({ key: body.key, value: body.value, status: after }) }
  })
  const p = await openSettings()
  await h.click(p.querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-toggle]'))
  await h.flush()
  const post = h.calls.find(c => c.key === 'POST /settings')
  assert.ok(post, 'the change was posted')
  assert.deepEqual(post.body, { key: 'auto_redeem_enabled', value: false })
  absent(p.querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-saved]'), 'no Saved after success')
  assert.equal(p.querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-toggle]').getAttribute('aria-pressed'), 'false')
  assert.equal(h.calls.filter(c => c.key === 'GET /status').length, 2, 'the popup data refreshed')
})

test('D-025: a select saves on change', { skip }, async () => {
  await mount(mixed(), { routes: { 'POST /settings': body => ({ key: body.key, value: body.value, status: mixed() }) } })
  const p = await openSettings()
  await h.type(p.querySelector('[data-rr-setting="language"] select'), 'pt-BR')
  await h.flush()
  const post = h.calls.find(c => c.key === 'POST /settings')
  assert.deepEqual(post.body, { key: 'language', value: 'pt-BR' })
})

test('D-025: a number is debounced ~600 ms, then saved', { skip }, async () => {
  const { SAVE_DEBOUNCE_MS } = await import('./harness.mjs').then(m => m.loadBundle())
  assert.equal(SAVE_DEBOUNCE_MS, 600)
  await mount(mixed(), { routes: { 'POST /settings': body => ({ key: body.key, value: body.value, status: mixed() }) } })
  const p = await openSettings()
  const input = p.querySelector('[data-rr-setting="lead_minutes"] input[type="number"]')
  await h.type(input, '15')
  await h.flush()
  assert.equal(h.calls.filter(c => c.key === 'POST /settings').length, 0, 'not saved on the first keystroke')
  await h.waitFor(() => h.calls.some(c => c.key === 'POST /settings'), 'the debounced save', 3000)
  assert.deepEqual(h.calls.find(c => c.key === 'POST /settings').body, { key: 'lead_minutes', value: 15 })
  await h.flush()
  absent(p.querySelector('[data-rr-setting="lead_minutes"] [data-rr-saved]'), 'no Saving… / Saved on a number either')
  assert.ok(!/Saving|Saved/.test(p.querySelector('[data-rr-setting="lead_minutes"]').textContent))
})

test('D-025: a rejected value shows the error inline and keeps the popup alive', { skip }, async () => {
  await mount(mixed(), {
    routes: {
      'POST /settings': () => {
        throw Object.assign(new Error('HTTP 422'), { status: 422 })
      }
    }
  })
  const p = await openSettings()
  await h.click(p.querySelector('[data-rr-setting="dry_run"] [data-rr-toggle]'))
  await h.flush()
  assert.equal(p.querySelector('[data-rr-setting="dry_run"] [data-rr-error]').textContent, 'Not saved: HTTP 422')
  absent(p.querySelector('[data-rr-setting="dry_run"] [data-rr-saved]'))
})

test('D-025: the tab is translated when Hermes is pt-BR', { skip }, async () => {
  await mount(mixed(), { locale: 'pt-br' })
  const p = await openSettings()
  assert.equal(p.querySelector('header h2').textContent, 'Configurações')
  assert.equal(p.querySelector('[data-rr-setting="lead_minutes"] [data-rr-setting-label]').textContent, 'Antecedência do resgate (minutos antes de vencer)')
  assert.equal(p.querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-toggle]').textContent, 'Ligado')
  assert.equal(p.querySelector('[data-rr-setting="language"] [data-rr-setting-label]').textContent, 'Idioma')
})

test('D-025: every field label comes from the bundle, not from the schema', { skip }, async () => {
  await mount(mixed(), {
    routes: {
      'GET /settings': () => ({
        fields: SETTINGS.fields.map(f => ({ ...f, label: 'SCHEMA LABEL', description: 'SCHEMA DESCRIPTION' }))
      })
    }
  })
  const p = await openSettings()
  assert.ok(!p.textContent.includes('SCHEMA LABEL'))
  assert.ok(!p.textContent.includes('SCHEMA DESCRIPTION'))
})

test('D-025: the mask example is neutral, never a real-looking address', { skip }, async () => {
  await mount(mixed())
  const p = await openSettings()
  const hint = p.querySelector('[data-rr-setting="mask_labels"] [data-rr-setting-hint]')
  assert.equal(hint.textContent, 'Hides part of each email address, including the domain, for example example@example.com → e***@e***.com. On by default.')
  // D-031: the visible column; the hidden Accounts copy holds the labels.
  assert.ok(!p.querySelector('[data-rr-scroll]').firstElementChild.textContent.includes('b***@'))
})
