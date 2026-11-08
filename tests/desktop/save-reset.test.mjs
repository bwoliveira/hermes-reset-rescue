// D-033: per-account cells that visibly save, and the two reset buttons.
//
// Maintainer report (real Desktop): a click on a per-account cell showed
// "Salvando…" and nothing else; the cell never moved. In this harness a POST
// that 404s left the cell on its old value with a bare "Not saved", and a POST
// that never answers left the row on "Saving…" for good.
//
// Contract (D-034: one click on a Default / On / Off segment picks the final
// value; the matrix cycling is gone): the option shows the new value at once, with no row text at all (no "Saving…", no "Saved"), like
// Hermes Desktop's own settings. Text appears only when something is wrong:
// a write not settled after 3 s reads a quiet "Checking…" until the engine's
// value is known (review r6; see save-order.test.mjs), and on a refusal the
// cell reverts and the row reads "Not saved: <reason>". The Global tab footer has "Restore defaults" (POST /settings/reset
// {}) and the Per-account tab footer "Reset all accounts to global" (POST
// /settings/account/reset-all {}); each needs a second click within 5 s, then
// the tab re-reads from the response (same payload as GET /settings). No Save
// button anywhere.
import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'

import { accountRow, expand, optionEl, pick } from './account-list.mjs'
import { NOW, overridden, perAccount, settingsWithAccounts } from './fixtures.mjs'
import { absent, closeDom, deferred, harnessAvailability, loadBundle, mountPlugin } from './harness.mjs'

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
  { key: 'auto_redeem_enabled', type: 'bool', value: false },
  { key: 'early_redeem_enabled', type: 'bool', value: true },
  { key: 'early_redeem_remaining_pct', type: 'int', value: 10, min: 0, max: 100 },
  { key: 'notify_target', type: 'str', value: 'telegram:42' }
]
const DEFAULTS = { scan_interval_hours: 12, auto_redeem_enabled: true, early_redeem_enabled: false, early_redeem_remaining_pct: 0 }

/** A fake engine with the D-030 routes and the D-033 resets (contract items 2-3). */
function engine(st = perAccount()) {
  const state = structuredClone(st)
  const fields = structuredClone(FIELDS)
  const rebuild = () => {
    for (const a of state.accounts) Object.assign(a, overridden(a.overrides))
    return structuredClone(state)
  }
  const find = key => state.accounts.find(a => a.account_key === key)
  const payload = () => settingsWithAccounts(structuredClone(fields), rebuild())
  return {
    state,
    fields,
    routes: {
      'GET /status': () => structuredClone(state),
      'GET /settings': payload,
      'POST /settings': ({ key, value }) => {
        fields.find(f => f.key === key).value = value
        return { key, value, status: rebuild() }
      },
      'POST /settings/account': ({ account_key, field, value }) => {
        const a = find(account_key)
        if (value === null) delete a.overrides[field]
        else a.overrides[field] = value
        return rebuild()
      },
      'POST /settings/account/reset': ({ account_key }) => {
        find(account_key).overrides = {}
        return rebuild()
      },
      'POST /settings/reset': () => {
        for (const f of fields) if (f.key in DEFAULTS) f.value = DEFAULTS[f.key]
        return payload()
      },
      'POST /settings/account/reset-all': () => {
        for (const a of state.accounts) a.overrides = {}
        return payload()
      }
    }
  }
}

async function mount(opts = {}) {
  const { routes = {}, st, ...rest } = opts
  const e = engine(st)
  h = await mountPlugin({ routes: { ...e.routes, ...routes }, ...rest })
  await h.flush()
  return e
}

const pop = () => h.q('[data-slot="popover-content"]')
const posts = key => h.calls.filter(c => c.key === key)
// D-034: the per-account list. `cell` is the option of an expanded account;
// `tap` expands it and clicks one segment (Default / On / Off).
const row = key => accountRow(h, key)
const cell = (key, field) => optionEl(h, key, field)
const tap = (key, field, choice) => pick(h, key, field, choice)
const optionStates = key => [...row(key).querySelectorAll('[data-rr-option]')].map(el => [el.dataset.rrOption, el.dataset.rrState])
const note = key => row(key)?.querySelector('[data-rr-saved], [data-rr-error]')?.textContent ?? null
const notFound = () => {
  throw Object.assign(new Error('404: {"detail":"Not Found"}'), { status: 404 })
}

async function openSettings() {
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  return pop()
}

async function openPerAccount() {
  await openSettings()
  await h.click(pop().querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  return pop()
}

/** Settles promise callbacks without a timer, for use under mocked timers. */
async function microtasks(n = 5) {
  for (let i = 0; i < n; i += 1) await h.act(async () => {})
}

// ------------------------------------------------------- per-account options

test('D-033/D-034: On, Off, Default each post one value: true, false, null', { skip }, async () => {
  await mount()
  await openPerAccount()
  await expand(h, 'acct_a')
  const field = 'reminders_enabled'
  const seen = [cell('acct_a', field).dataset.rrState]
  for (const choice of ['on', 'off', 'default']) {
    await tap('acct_a', field, choice)
    await h.flush()
    seen.push(cell('acct_a', field).dataset.rrState)
  }
  assert.deepEqual(seen, ['global', 'on', 'off', 'global'])
  assert.deepEqual(
    posts('POST /settings/account').map(c => c.body),
    [true, false, null].map(value => ({ account_key: 'acct_a', field, value }))
  )
  assert.equal(note('acct_a'), null, 'no Saved after success')
})

test('D-033: the option shows the new value on the same tick, with no row text while the POST is in flight', { skip }, async () => {
  const hold = deferred()
  const e = await mount({ routes: { 'POST /settings/account': () => hold.promise } })
  await openPerAccount()
  await tap('acct_a', 'auto_redeem_enabled', 'on')
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'on', 'optimistic: On before the reply')
  assert.equal(note('acct_a'), null, 'no Saving… while in flight')
  absent(row('acct_a').querySelector('[data-rr-saved]'), 'no note element while in flight')
  // A second click while the first is in flight waits in the save queue
  // until the first POST answers (review r6).
  await tap('acct_a', 'auto_redeem_enabled', 'off')
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'off')
  assert.deepEqual(posts('POST /settings/account').map(c => c.body.value), [true])
  e.state.accounts[0].overrides.auto_redeem_enabled = false
  for (const a of e.state.accounts) Object.assign(a, overridden(a.overrides))
  hold.resolve(structuredClone(e.state))
  await h.flush()
  assert.deepEqual(posts('POST /settings/account').map(c => c.body.value), [true, false])
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'off')
  assert.equal(note('acct_a'), null, 'no Saved after success')
})

test('D-033: a 404 reverts the option and the row reads Not saved: <reason>', { skip }, async () => {
  await mount({ routes: { 'POST /settings/account': notFound } })
  await openPerAccount()
  await tap('acct_a', 'early_redeem_enabled', 'on')
  await h.flush()
  assert.equal(cell('acct_a', 'early_redeem_enabled').dataset.rrState, 'global', 'reverted to Global')
  assert.equal(row('acct_a').querySelector('[data-rr-error]').textContent, 'Not saved: route not found (HTTP 404)')
  absent(row('acct_a').querySelector('[data-rr-saved="saving"]'), 'not left on Saving…')
})

test('D-033: pt-BR failure reads Não salvou: <motivo>; a JSON detail is the reason', { skip }, async () => {
  let calls = 0
  await mount({
    locale: 'pt-br',
    routes: {
      'POST /settings/account': () => {
        calls += 1
        if (calls === 1) notFound()
        throw Object.assign(new Error('500: {"error":"write_failed","detail":"config.yaml is read-only"}'), { status: 500 })
      }
    }
  })
  await openPerAccount()
  await tap('acct_b', 'auto_redeem_enabled', 'default')
  await h.flush()
  assert.equal(cell('acct_b', 'auto_redeem_enabled').dataset.rrState, 'off', 'Off override kept')
  assert.equal(row('acct_b').querySelector('[data-rr-error]').textContent, 'Não salvou: rota não encontrada (HTTP 404)')
  await tap('acct_b', 'auto_redeem_enabled', 'default')
  await h.flush()
  assert.equal(row('acct_b').querySelector('[data-rr-error]').textContent, 'Não salvou: config.yaml is read-only')
})

test('D-033: a request that never answers reads Confirmando… after 3 s, then Ainda não confirmado after 60 s', { skip }, async () => {
  // Review r6 P1-02, r7 P1-01: no reply is an unknown result, not a failure;
  // after 60 s with no answer the intent stays and the row says so.
  await mount({ locale: 'pt-br', routes: { 'POST /settings/account': () => new Promise(() => {}) } })
  await openPerAccount()
  mock.timers.enable({ apis: ['setTimeout'] })
  await tap('acct_a', 'monitored', 'on')
  assert.equal(cell('acct_a', 'monitored').dataset.rrState, 'on', 'optimistic On')
  await h.act(async () => mock.timers.tick(2_999))
  await microtasks()
  assert.equal(note('acct_a'), null, 'silent while in flight at 2.999 s')
  await h.act(async () => mock.timers.tick(1))
  await microtasks()
  assert.equal(note('acct_a'), 'Confirmando…', 'not settled at 3 s')
  await h.act(async () => mock.timers.tick(7_000))
  await microtasks()
  assert.equal(note('acct_a'), 'Confirmando…', 'unknown at 10 s')
  assert.equal(cell('acct_a', 'monitored').dataset.rrState, 'on', 'the intent stays on screen')
  await h.act(async () => mock.timers.tick(50_000))
  await microtasks(25)
  mock.timers.reset()
  await h.flush()
  assert.equal(note('acct_a'), 'Ainda não confirmado: reabra o popup para conferir')
  absent(row('acct_a').querySelector('[data-rr-error]'), 'never Não salvou for an unknown result')
  assert.equal(cell('acct_a', 'monitored').dataset.rrState, 'on', 'the intent stays on screen')
})

// ------------------------------------------------------------------ resets

test('D-033: Restore defaults needs a second click within 5 s, posts {} and re-reads the reply', { skip }, async () => {
  await mount({ locale: 'pt-br' })
  await openSettings()
  const btn = () => pop().querySelector('[data-rr-reset-defaults]')
  assert.equal(btn().tagName, 'BUTTON')
  assert.equal(btn().textContent, 'Restaurar padrões')
  absent(pop().querySelector('[data-rr-reset-all]'), 'no reset-all on the Global tab')
  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(btn())
  assert.equal(btn().textContent, 'Confirmar')
  assert.equal(posts('POST /settings/reset').length, 0, 'the first click only asks')
  await h.act(async () => mock.timers.tick(5_000))
  assert.equal(btn().textContent, 'Restaurar padrões', 'the ask expires after 5 s')
  await h.click(btn())
  await h.click(btn())
  mock.timers.reset()
  await h.flush()
  assert.deepEqual(posts('POST /settings/reset').map(c => c.body), [{}])
  const toggle = key => pop().querySelector(`[data-rr-setting="${key}"] [data-rr-toggle]`)
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'true', 'Auto back on')
  assert.equal(toggle('early_redeem_enabled').getAttribute('aria-pressed'), 'false', 'Early back off')
  assert.equal(pop().querySelector('[data-rr-setting="scan_interval_hours"] input').value, '12')
  assert.equal(pop().querySelector('[data-rr-setting="notify_target"] input').value, 'telegram:42', 'destination untouched')
  assert.equal(btn().textContent, 'Restaurar padrões')
})

test('D-033: Reset all accounts to global clears every row at once, then from the reply', { skip }, async () => {
  const e = await mount()
  await openPerAccount()
  const btn = () => pop().querySelector('[data-rr-reset-all]')
  assert.equal(btn().textContent, 'Reset all accounts to global')
  absent(pop().querySelector('[data-rr-reset-defaults]'), 'no Restore defaults on the Per-account tab')
  await expand(h, 'acct_b')
  assert.equal(cell('acct_b', 'auto_redeem_enabled').dataset.rrState, 'off')
  await h.click(btn())
  assert.equal(btn().textContent, 'Confirm')
  const hold = deferred()
  h.routes['POST /settings/account/reset-all'] = () => hold.promise.then(() => e.routes['POST /settings/account/reset-all']())
  await h.click(btn())
  // The values change at once, with no Saving… text.
  for (const [field, state] of optionStates('acct_b')) assert.equal(state, 'global', `at once: acct_b ${field}`)
  absent(pop().querySelector('[data-rr-reset-note] [data-rr-saved]'), 'no Saving… on the reset line')
  hold.resolve()
  await h.flush()
  assert.deepEqual(posts('POST /settings/account/reset-all').map(c => c.body), [{}])
  for (const key of ['acct_a', 'acct_b', 'acct_c', 'acct_d']) {
    await expand(h, key)
    const states = optionStates(key)
    assert.equal(states.length, 4, key)
    for (const [field, state] of states) assert.equal(state, 'global', `${key} ${field}`)
    assert.equal(row(key).querySelector('[data-rr-account-reset]').disabled, true, `no account Reset left to run on ${key}`)
  }
})

test('D-033: Voltar todas para o global in pt-BR, and a failed reset says why', { skip }, async () => {
  await mount({ locale: 'pt-br', routes: { 'POST /settings/account/reset-all': notFound } })
  await openPerAccount()
  const btn = () => pop().querySelector('[data-rr-reset-all]')
  assert.equal(btn().textContent, 'Voltar todas para o global')
  await h.click(btn())
  await h.click(btn())
  await h.flush()
  assert.equal(pop().querySelector('[data-rr-reset-note] [data-rr-error]').textContent, 'Não salvou: rota não encontrada (HTTP 404)')
  await expand(h, 'acct_b')
  assert.equal(cell('acct_b', 'auto_redeem_enabled').dataset.rrState, 'off', 'nothing changed')
  assert.equal(row('acct_b').querySelector('[data-rr-account-reset]').textContent, 'Restaurar esta conta', 'the per-account Restaurar stays')
})

test('D-033: no Save / Salvar control on either tab, in either language', { skip }, async () => {
  for (const locale of ['en', 'pt-br']) {
    await mount({ locale })
    await openSettings()
    for (const scope of ['all', 'account']) {
      await h.click(pop().querySelector(`[data-rr-scope-option="${scope}"]`))
      await h.flush()
      const labels = h.qa('button, [role="button"], input[type="submit"]').map(el => [el.textContent, el.getAttribute('aria-label') ?? '', el.getAttribute('title') ?? ''].join('|'))
      const bad = labels.filter(l => /(^|\|)\s*(Save|Salvar)\s*(\||$)/i.test(l))
      assert.deepEqual(bad, [], `${locale} ${scope}: a Save control`)
      assert.ok(labels.length > 5, `${locale} ${scope}: buttons were scanned`)
    }
    await h.unmount()
    h = null
  }
})

test('D-033: the new strings exist in both bundles', { skip }, async () => {
  const { STRINGS } = await loadBundle()
  assert.equal(STRINGS.en.settings.notSaved('x'), 'Not saved: x')
  assert.equal(STRINGS['pt-BR'].settings.notSaved('x'), 'Não salvou: x')
  assert.equal(STRINGS.en.reset.defaults, 'Restore defaults')
  assert.equal(STRINGS['pt-BR'].reset.defaults, 'Restaurar padrões')
  assert.equal(STRINGS.en.reset.allAccounts, 'Reset all accounts to global')
  assert.equal(STRINGS['pt-BR'].reset.allAccounts, 'Voltar todas para o global')
  assert.equal(STRINGS.en.reset.confirm, 'Confirm')
  assert.equal(STRINGS['pt-BR'].reset.confirm, 'Confirmar')
})

// --------------------------------------------- instant, like Hermes' own settings

test('D-033 fast: a successful click shows no row text at any point (POST answers at 200 ms)', { skip }, async () => {
  const e = await mount({ routes: {} })
  const inner = e.routes['POST /settings/account']
  h.routes['POST /settings/account'] = body => new Promise(resolve => setTimeout(() => resolve(inner(body)), 200))
  await openPerAccount()
  mock.timers.enable({ apis: ['setTimeout'] })
  const texts = []
  const look = () => {
    texts.push(note('acct_a'))
    if (/Saving|Saved|Checking|Not saved/.test(row('acct_a').textContent)) texts.push(row('acct_a').textContent)
  }
  await tap('acct_a', 'auto_redeem_enabled', 'on')
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'on', 'On on the same tick as the click')
  look()
  for (let ms = 0; ms < 4_000; ms += 100) {
    await h.act(async () => mock.timers.tick(100))
    await microtasks()
    look()
  }
  mock.timers.reset()
  await h.flush()
  look()
  assert.deepEqual(posts('POST /settings/account').map(c => c.body.value), [true])
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'on')
  assert.deepEqual(texts.filter(Boolean), [], 'no Saving…, Saved or Checking… at any point')
  assert.ok(!/Saving|Saved|Checking|Salvando|Salvo|Confirmando/.test(pop().textContent))
})

test('D-033 fast: a POST still pending at 3 s shows the quiet Confirmando…', { skip }, async () => {
  const hold = deferred()
  await mount({ locale: 'pt-br', routes: { 'POST /settings': () => hold.promise } })
  await openSettings()
  mock.timers.enable({ apis: ['setTimeout'] })
  const toggle = () => pop().querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-toggle]')
  const fieldNote = () => pop().querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-saved], [data-rr-setting="auto_redeem_enabled"] [data-rr-error]')?.textContent ?? null
  await h.click(toggle())
  assert.equal(toggle().getAttribute('aria-pressed'), 'true', 'On on the same tick')
  assert.equal(fieldNote(), null, 'no Salvando…')
  await h.act(async () => mock.timers.tick(2_999))
  await microtasks()
  assert.equal(fieldNote(), null, 'still silent at 2.999 s')
  await h.act(async () => mock.timers.tick(1))
  await microtasks()
  assert.equal(fieldNote(), 'Confirmando…')
  hold.resolve({ key: 'auto_redeem_enabled', value: true })
  mock.timers.reset()
  await h.flush()
  assert.equal(fieldNote(), null, 'the note goes once the reply lands; no Salvo')
  assert.equal(toggle().getAttribute('aria-pressed'), 'true')
})

test('D-033 fast: a refusal still reverts and reads Não salvou: <motivo>', { skip }, async () => {
  await mount({ locale: 'pt-br', routes: { 'POST /settings': notFound } })
  await openSettings()
  const toggle = () => pop().querySelector('[data-rr-setting="early_redeem_enabled"] [data-rr-toggle]')
  await h.click(toggle())
  await h.flush()
  assert.equal(toggle().getAttribute('aria-pressed'), 'true', 'reverted to On')
  assert.equal(pop().querySelector('[data-rr-setting="early_redeem_enabled"] [data-rr-error]').textContent, 'Não salvou: rota não encontrada (HTTP 404)')
})

test('D-033 fast: the Saving / Saved strings are gone from both bundles', { skip }, async () => {
  const { STRINGS } = await loadBundle()
  for (const lang of ['en', 'pt-BR']) {
    assert.equal(STRINGS[lang].settings.saving, undefined, `${lang} saving`)
    assert.equal(STRINGS[lang].settings.saved, undefined, `${lang} saved`)
  }
})
