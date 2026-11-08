// Restore defaults (Global tab) applies at once, like every other control.
//
// Maintainer report: "Restaurar padrões" changed the values only when
// the server replied (~0.6 s), because the defaults lived only in the engine.
// GET /settings now carries `defaults` ({field: value}, the fields POST
// /settings/reset changes; notify_target and excluded_accounts are not in it).
//
// Contract: on the confirming click every Global row shows its default on the
// same tick, with the POST still pending and no "Salvando…" text; the reply
// (or GET /settings) reconciles; a refusal reverts every row and reads
// "Não salvou: <motivo>". An older engine without `defaults` keeps the old
// behaviour: the rows change only when the reply lands.
import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'

import { NOW, perAccount, settingsWithAccounts } from './fixtures.mjs'
import { absent, closeDom, deferred, harnessAvailability, mountPlugin } from './harness.mjs'

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
  mock.timers.reset()
  if (h) await h.unmount()
  h = null
})

const FIELDS = [
  { key: 'scan_interval_hours', type: 'int', value: 6, min: 1, max: 24 },
  { key: 'lead_minutes', type: 'int', value: 90, min: 1, max: 720 },
  { key: 'notify_target', type: 'str', value: 'telegram:42' },
  { key: 'auto_redeem_enabled', type: 'bool', value: false },
  { key: 'early_redeem_enabled', type: 'bool', value: true },
  { key: 'excluded_accounts', type: 'str', value: 'acct_b' },
  { key: 'language', type: 'enum', value: 'pt-BR', choices: ['auto', 'en', 'pt-BR'] }
]
const DEFAULTS = { scan_interval_hours: 12, lead_minutes: 10, auto_redeem_enabled: true, early_redeem_enabled: false, language: 'auto' }

function engine({ withDefaults = true } = {}) {
  const state = structuredClone(perAccount())
  const fields = structuredClone(FIELDS)
  const payload = () => {
    const body = settingsWithAccounts(structuredClone(fields), structuredClone(state))
    if (withDefaults) body.defaults = { ...DEFAULTS }
    return body
  }
  return {
    fields,
    payload,
    routes: {
      'GET /status': () => structuredClone(state),
      'GET /settings': payload,
      'POST /settings/reset': () => {
        for (const f of fields) if (f.key in DEFAULTS) f.value = DEFAULTS[f.key]
        return payload()
      }
    }
  }
}

const pop = () => h.q('[data-slot="popover-content"]')
const posts = key => h.calls.filter(c => c.key === key)
const btn = () => pop().querySelector('[data-rr-reset-defaults]')
const resetNote = () => pop().querySelector('[data-rr-reset-note] [data-rr-saved], [data-rr-reset-note] [data-rr-error]')?.textContent ?? null

/** What each Global row shows, by key (as strings / booleans). */
function shownValues() {
  const out = {}
  for (const row of pop().querySelectorAll('[data-rr-setting]')) {
    const key = row.dataset.rrSetting
    const toggle = row.querySelector('[data-rr-toggle]')
    if (toggle) out[key] = toggle.getAttribute('aria-pressed') === 'true'
    else out[key] = row.querySelector('input, select').value
  }
  return out
}
const asShown = values => Object.fromEntries(Object.entries(values).map(([k, v]) => [k, typeof v === 'boolean' ? v : String(v)]))
const ORIGINAL = asShown(Object.fromEntries(FIELDS.map(f => [f.key, f.value])))
const RESET = { ...ORIGINAL, ...asShown(DEFAULTS) }

async function mount(opts = {}) {
  const e = engine(opts)
  h = await mountPlugin({ locale: 'pt-br', routes: { ...e.routes } })
  await h.flush()
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  return e
}

/** Settles promise callbacks without a timer, for use under mocked timers. */
async function microtasks(n = 5) {
  for (let i = 0; i < n; i += 1) await h.act(async () => {})
}

test('defaults-instant: the confirming click shows every default on the same tick, POST pending, no Salvando…', { skip }, async () => {
  const e = await mount()
  assert.deepEqual(shownValues(), ORIGINAL)
  const hold = deferred()
  h.routes['POST /settings/reset'] = () => hold.promise.then(() => e.routes['POST /settings/reset']())
  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(btn())
  assert.equal(btn().textContent, 'Confirmar')
  assert.deepEqual(shownValues(), ORIGINAL, 'the first click only asks')
  await h.click(btn())
  assert.deepEqual(shownValues(), RESET, 'every Global row shows its default at once')
  assert.equal(posts('POST /settings/reset').length, 1, 'the POST went out')
  assert.equal(resetNote(), null, 'no Salvando… while pending')
  assert.ok(!/Salvando|Saving/.test(pop().textContent))
  await h.act(async () => mock.timers.tick(2_999))
  await microtasks()
  assert.equal(resetNote(), null, 'still silent at 2.999 s')
  assert.deepEqual(shownValues(), RESET, 'still the defaults while pending')
  hold.resolve()
  mock.timers.reset()
  await h.flush()
  assert.deepEqual(shownValues(), RESET, 'reconciled with the reply')
  assert.equal(resetNote(), null, 'no Salvo after success')
  assert.equal(btn().textContent, 'Restaurar padrões')
})

test('defaults-instant: a pending reset reads Confirmando… only after 3 s', { skip }, async () => {
  await mount()
  const hold = deferred()
  h.routes['POST /settings/reset'] = () => hold.promise
  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(btn())
  await h.click(btn())
  await h.act(async () => mock.timers.tick(3_000))
  await microtasks()
  assert.equal(resetNote(), 'Confirmando…')
  assert.deepEqual(shownValues(), RESET)
  hold.resolve(null)
  mock.timers.reset()
})

test('defaults-instant: a refusal reverts every row and reads Não salvou: <motivo>', { skip }, async () => {
  await mount()
  const hold = deferred()
  h.routes['POST /settings/reset'] = () => hold.promise
  await h.click(btn())
  await h.click(btn())
  assert.deepEqual(shownValues(), RESET, 'optimistic first')
  hold.reject(Object.assign(new Error('404: {"detail":"Not Found"}'), { status: 404 }))
  await h.flush()
  assert.deepEqual(shownValues(), ORIGINAL, 'reverted to what the engine last confirmed')
  assert.equal(resetNote(), 'Não salvou: rota não encontrada (HTTP 404)')
})

test('defaults-instant: an engine without defaults waits for the reply (old behaviour)', { skip }, async () => {
  const e = await mount({ withDefaults: false })
  const hold = deferred()
  h.routes['POST /settings/reset'] = () => hold.promise.then(() => e.routes['POST /settings/reset']())
  await h.click(btn())
  await h.click(btn())
  assert.deepEqual(shownValues(), ORIGINAL, 'nothing to apply before the reply')
  assert.equal(posts('POST /settings/reset').length, 1)
  hold.resolve()
  await h.flush()
  assert.deepEqual(shownValues(), RESET, 'the reply sets the defaults')
  absent(pop().querySelector('[data-rr-reset-note] [data-rr-error]'), 'no error')
})

test('defaults-instant: a malformed defaults field is ignored (no row changes before the reply)', { skip }, async () => {
  const e = await mount({ withDefaults: false })
  h.routes['GET /settings'] = () => ({ ...e.payload(), defaults: ['not', 'a', 'map'] })
  // Re-open the tab so the malformed payload is the one read.
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  const hold = deferred()
  h.routes['POST /settings/reset'] = () => hold.promise.then(() => e.routes['POST /settings/reset']())
  await h.click(btn())
  await h.click(btn())
  assert.deepEqual(shownValues(), ORIGINAL)
  hold.resolve()
  await h.flush()
  assert.deepEqual(shownValues(), RESET)
})
