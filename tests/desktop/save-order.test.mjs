// D-033 (review r6 P1-01, P1-02): popup settings writes are applied in order,
// and a client timeout means "result unknown", never a revert stated as fact.
//
// P1-01: two quick clicks sent On and Off in parallel; when Off committed
// first and On last, the backend stayed On while the cell read Off.
// P1-02: after 10 s without a reply the cell reverted and read "Not saved",
// yet the server could still write the value a moment later.
//
// Contract: one queue for every settings POST of the popup (per-account options, the
// single-account notice, global rows, both resets), at most one in flight,
// the latest pending intent per target wins. The normal path is silent: no
// "Saving…", no "Saved". A write not settled after 3 s reads a quiet
// "Checking…" and keeps the intent on screen; after 10 s its result is
// unknown, and a 2xx reply or an answered GET /settings that matches the
// intent settles it (the note goes). With neither after 60 s
// the row reads "Not confirmed yet: reopen the popup to check" and keeps the
// intent (review r7 P1-01; save-unknown.test.mjs). Only a real HTTP error
// reverts, at once, with its reason. /redeem and /scan stay outside
// the queue and are never retried.
//
// The fake engine below is stateful: a POST changes the state only when it
// completes, like the real save that runs in the executor.
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
  { key: 'auto_redeem_enabled', type: 'bool', value: false },
  { key: 'early_redeem_enabled', type: 'bool', value: false },
  { key: 'early_redeem_remaining_pct', type: 'int', value: 10, min: 0, max: 100 }
]
const DEFAULTS = { auto_redeem_enabled: true, early_redeem_enabled: false, early_redeem_remaining_pct: 0 }

/**
 * A stateful engine. Every settings POST waits for `hold(route, body)` (a
 * promise, or nothing for an immediate answer) and only then applies its
 * payload. `inFlight` / `maxInFlight` count settings POSTs open at once.
 */
function engine({ hold = () => null, fail = () => null } = {}) {
  const state = structuredClone(perAccount())
  const fields = structuredClone(FIELDS)
  const meter = { inFlight: 0, maxInFlight: 0 }
  const rebuild = () => {
    for (const a of state.accounts) Object.assign(a, overridden(a.overrides))
    return structuredClone(state)
  }
  const find = key => state.accounts.find(a => a.account_key === key)
  const payload = () => settingsWithAccounts(structuredClone(fields), rebuild())
  const write = (route, apply) => async body => {
    meter.inFlight += 1
    meter.maxInFlight = Math.max(meter.maxInFlight, meter.inFlight)
    try {
      const gate = hold(route, body)
      if (gate) await gate
      const err = fail(route, body)
      if (err) throw err
      return apply(body)
    } finally {
      meter.inFlight -= 1
    }
  }
  return {
    state,
    fields,
    meter,
    find,
    routes: {
      'GET /status': rebuild,
      'GET /settings': payload,
      'POST /settings': write('POST /settings', ({ key, value }) => {
        fields.find(f => f.key === key).value = value
        return { key, value, status: rebuild() }
      }),
      'POST /settings/account': write('POST /settings/account', ({ account_key, field, value }) => {
        const a = find(account_key)
        if (value === null) delete a.overrides[field]
        else a.overrides[field] = value
        return rebuild()
      }),
      'POST /settings/account/reset': write('POST /settings/account/reset', ({ account_key }) => {
        find(account_key).overrides = {}
        return rebuild()
      }),
      'POST /settings/reset': write('POST /settings/reset', () => {
        for (const f of fields) if (f.key in DEFAULTS) f.value = DEFAULTS[f.key]
        return payload()
      }),
      'POST /settings/account/reset-all': write('POST /settings/account/reset-all', () => {
        for (const a of state.accounts) a.overrides = {}
        return payload()
      })
    }
  }
}

/** Gates every held POST; `releaseNewest()` lets the most recent one finish. */
function gates() {
  const open = []
  return {
    open,
    hold: () => {
      const d = deferred()
      open.push(d)
      return d.promise
    },
    releaseNewest() {
      const d = open.pop()
      d?.resolve()
      return Boolean(d)
    }
  }
}

async function mount(opts = {}) {
  const { hold, fail, ...rest } = opts
  const e = engine({ hold, fail })
  h = await mountPlugin({ routes: e.routes, ...rest })
  await h.flush()
  return e
}

const pop = () => h.q('[data-slot="popover-content"]')
const posts = key => h.calls.filter(c => c.key === key)
// D-034: the per-account list. `cell` is the option of an expanded account
// (sync, so it reads under mocked timers); `tap` expands it and clicks one
// segment (Default / On / Off).
const row = key => accountRow(h, key)
const cell = (key, field) => optionEl(h, key, field)
const tap = (key, field, choice) => pick(h, key, field, choice)
const note = key => row(key)?.querySelector('[data-rr-saved], [data-rr-error]')?.textContent ?? null
const toggle = key => pop().querySelector(`[data-rr-setting="${key}"] [data-rr-toggle]`)
const fieldNote = key => pop().querySelector(`[data-rr-setting="${key}"]`)?.querySelector('[data-rr-saved], [data-rr-error]')?.textContent ?? null
const resetNote = () => pop().querySelector('[data-rr-reset-note]')?.textContent ?? ''

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
async function microtasks(n = 25) {
  for (let i = 0; i < n; i += 1) await h.act(async () => {})
}

/** Lets the newest held POST finish, again and again, until none is left. */
async function drainNewestFirst(g) {
  for (let i = 0; i < 20; i += 1) {
    await h.flush()
    if (!g.releaseNewest()) break
  }
  await h.flush(5)
}

// ------------------------------------------------------------- P1-01 order

test('D-033 r6: Global → On → Off on an option: one POST at a time, the engine ends Off, the cell reads Off with no row text', { skip }, async () => {
  const g = gates()
  const e = await mount({ hold: g.hold })
  await openPerAccount()
  await expand(h, 'acct_a')
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'global')
  await tap('acct_a', 'auto_redeem_enabled', 'on')
  await tap('acct_a', 'auto_redeem_enabled', 'off') // the newest intent
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'off', 'optimistic: the latest intent shows')
  assert.equal(note('acct_a'), null, 'no Saving…')
  assert.equal(e.meter.inFlight, 1, 'the second click waits for the first POST')
  await drainNewestFirst(g)
  assert.equal(e.meter.maxInFlight, 1, 'never two settings POSTs at once')
  assert.deepEqual(posts('POST /settings/account').map(c => c.body.value), [true, false])
  assert.equal(e.find('acct_a').overrides.auto_redeem_enabled, false, 'engine ends Off')
  assert.equal(cell('acct_a', 'auto_redeem_enabled').dataset.rrState, 'off')
  assert.equal(note('acct_a'), null, 'no Saved')
})

test('D-033 r6: clicks queued behind a POST in flight coalesce; only the latest intent is sent', { skip }, async () => {
  const g = gates()
  const e = await mount({ hold: g.hold })
  await openPerAccount()
  const c = () => cell('acct_a', 'auto_redeem_enabled')
  await tap('acct_a', 'auto_redeem_enabled', 'on') // sent
  await tap('acct_a', 'auto_redeem_enabled', 'off') // queued
  await tap('acct_a', 'auto_redeem_enabled', 'default') // replaces the queued Off
  assert.equal(c().dataset.rrState, 'global')
  assert.equal(note('acct_a'), null, 'no Saving…')
  await drainNewestFirst(g)
  assert.deepEqual(posts('POST /settings/account').map(x => x.body.value), [true, null])
  assert.equal(e.find('acct_a').overrides.auto_redeem_enabled, undefined)
  assert.equal(c().dataset.rrState, 'global')
  assert.equal(note('acct_a'), null, 'no Saved')
})

test('D-033 r6: a global toggle clicked twice fast ends Off in the engine and on screen', { skip }, async () => {
  const g = gates()
  const e = await mount({ hold: g.hold })
  await openSettings()
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'false')
  await h.click(toggle('auto_redeem_enabled')) // On
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'true', 'optimistic On')
  await h.click(toggle('auto_redeem_enabled')) // Off
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'false', 'optimistic Off')
  await drainNewestFirst(g)
  assert.equal(e.meter.maxInFlight, 1)
  assert.deepEqual(posts('POST /settings').map(c => c.body.value), [true, false])
  assert.equal(e.fields.find(f => f.key === 'auto_redeem_enabled').value, false)
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'false')
  assert.equal(fieldNote('auto_redeem_enabled'), null, 'no Saved')
})

test('D-033 r6: a cell, a global row and a reset share one queue: one POST in flight, in click order', { skip }, async () => {
  const g = gates()
  const e = await mount({ hold: g.hold })
  await openPerAccount()
  await tap('acct_b', 'auto_redeem_enabled', 'default') // Off -> Default
  await h.click(pop().querySelector('[data-rr-reset-all]'))
  await h.click(pop().querySelector('[data-rr-reset-all]'))
  await h.click(pop().querySelector('[data-rr-scope-option="all"]'))
  await h.flush()
  await h.click(toggle('early_redeem_enabled'))
  await drainNewestFirst(g)
  assert.equal(e.meter.maxInFlight, 1)
  assert.deepEqual(
    h.calls.filter(c => c.method === 'POST').map(c => c.key),
    ['POST /settings/account', 'POST /settings/account/reset-all', 'POST /settings']
  )
  assert.equal(e.fields.find(f => f.key === 'early_redeem_enabled').value, true)
  assert.equal(toggle('early_redeem_enabled').getAttribute('aria-pressed'), 'true')
})

// ---------------------------------------------------------- P1-02 deadline

test('D-033 r6: Restore defaults answering at 12 s shows Checking…, then the engine value, never Not saved', { skip }, async () => {
  const e = await mount({
    hold: route => (route === 'POST /settings/reset' ? new Promise(r => setTimeout(r, 12_000)) : null)
  })
  await openSettings()
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'false')
  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(pop().querySelector('[data-rr-reset-defaults]'))
  await h.click(pop().querySelector('[data-rr-reset-defaults]'))
  await microtasks()
  assert.equal(resetNote(), '', 'no Saving… after the second click')
  await h.act(async () => mock.timers.tick(10_000))
  await microtasks()
  assert.equal(resetNote(), 'Checking…', 'unknown after 10 s, not a failure')
  assert.ok(!/Not saved/.test(pop().textContent), 'no Not saved at 10 s')
  await h.act(async () => mock.timers.tick(2_000))
  await microtasks()
  mock.timers.reset()
  await h.flush(5)
  assert.equal(e.fields.find(f => f.key === 'auto_redeem_enabled').value, true, 'engine wrote the defaults')
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'true', 'the toggle shows the engine value')
  assert.equal(resetNote(), '', 'no Saved')
  assert.ok(!/Not saved/.test(pop().textContent))
})

test('D-033 r6: Off → Global answering at 12 s: Checking…, the cell keeps the intent, then equals the engine', { skip }, async () => {
  const e = await mount({
    hold: route => (route === 'POST /settings/account' ? new Promise(r => setTimeout(r, 12_000)) : null)
  })
  await openPerAccount()
  await expand(h, 'acct_b')
  const c = () => cell('acct_b', 'auto_redeem_enabled')
  assert.equal(c().dataset.rrState, 'off')
  const statusReads = () => h.calls.filter(x => x.key === 'GET /status').length
  mock.timers.enable({ apis: ['setTimeout'] })
  await tap('acct_b', 'auto_redeem_enabled', 'default')
  assert.equal(c().dataset.rrState, 'global')
  await h.act(async () => mock.timers.tick(10_000))
  await microtasks()
  assert.equal(note('acct_b'), 'Checking…')
  assert.equal(c().dataset.rrState, 'global', 'no revert while the result is unknown')
  const before = statusReads()
  const settingsReads = h.calls.filter(x => x.key === 'GET /settings').length
  await h.act(async () => mock.timers.tick(2_000))
  await microtasks()
  mock.timers.reset()
  await h.flush(5)
  assert.equal(e.find('acct_b').overrides.auto_redeem_enabled, undefined, 'engine: Global')
  assert.equal(c().dataset.rrState, 'global', 'ui equals the engine')
  assert.equal(note('acct_b'), null, 'settled: no Saved')
  assert.ok(h.calls.filter(x => x.key === 'GET /settings').length > settingsReads, 'reconciled with GET /settings')
  assert.ok(statusReads() > before, 'the Accounts tab was refreshed too')
})

test('D-033 r7: a POST that never answers: Checking… until 60 s, then Not confirmed yet with the intent kept; a reopen shows the engine', { skip }, async () => {
  let first = true
  const e = await mount({
    hold: route => {
      if (route !== 'POST /settings/account' || !first) return null
      first = false
      return new Promise(() => {})
    }
  })
  await openPerAccount()
  const c = () => cell('acct_b', 'auto_redeem_enabled')
  mock.timers.enable({ apis: ['setTimeout'] })
  await tap('acct_b', 'auto_redeem_enabled', 'default')
  await h.act(async () => mock.timers.tick(10_000))
  await microtasks()
  assert.equal(note('acct_b'), 'Checking…')
  await tap('acct_a', 'auto_redeem_enabled', 'on')
  assert.equal(posts('POST /settings/account').length, 1, 'the queue stays blocked while the result is unknown')
  await h.act(async () => mock.timers.tick(49_999))
  await microtasks()
  assert.equal(note('acct_b'), 'Checking…', 'still waiting at 59.999 s')
  await h.act(async () => mock.timers.tick(1))
  await microtasks()
  assert.equal(note('acct_b'), 'Not confirmed yet: reopen the popup to check', 'unknown is not a failure')
  await expand(h, 'acct_b')
  assert.equal(c().dataset.rrState, 'global', 'the intent stays; the old value is not shown as saved')
  assert.ok(!/Not saved/.test(pop().textContent), 'never Not saved')
  mock.timers.reset()
  await h.flush(5)
  assert.equal(e.find('acct_b').overrides.auto_redeem_enabled, false, 'the engine never wrote it')
  assert.equal(posts('POST /settings/account').length, 2, 'the queued click went out after the cap')
  assert.equal(e.find('acct_a').overrides.auto_redeem_enabled, true)
  // Reopening reads GET /settings: the engine value, no stale intent.
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await openPerAccount()
  await expand(h, 'acct_b')
  assert.equal(c().dataset.rrState, 'off', 'a reopened popup shows what the engine holds')
  assert.equal(note('acct_b'), null)
})

test('D-033 r6: pt-BR reads Confirmando… while the result is unknown', { skip }, async () => {
  await mount({ locale: 'pt-br', hold: route => (route === 'POST /settings' ? new Promise(r => setTimeout(r, 11_000)) : null) })
  await openSettings()
  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(toggle('auto_redeem_enabled'))
  await h.act(async () => mock.timers.tick(10_000))
  await microtasks()
  assert.equal(fieldNote('auto_redeem_enabled'), 'Confirmando…')
  await h.act(async () => mock.timers.tick(1_000))
  await microtasks()
  mock.timers.reset()
  await h.flush(5)
  assert.equal(fieldNote('auto_redeem_enabled'), null, 'no Salvo')
  assert.equal(toggle('auto_redeem_enabled').getAttribute('aria-pressed'), 'true')
})

// ---------------------------------------------------------- real HTTP error

test('D-033 r6: an HTTP 422 still reverts at once with its reason, then the next intent goes out', { skip }, async () => {
  let refuse = true
  const e = await mount({
    fail: route =>
      route === 'POST /settings/account' && refuse
        ? Object.assign(new Error('422: {"error":"invalid_setting","detail":"That value is not allowed."}'), { status: 422 })
        : null
  })
  await openPerAccount()
  const reads = h.calls.filter(x => x.key === 'GET /settings').length
  await tap('acct_a', 'early_redeem_enabled', 'on')
  await h.flush()
  assert.equal(cell('acct_a', 'early_redeem_enabled').dataset.rrState, 'global', 'reverted to the confirmed value')
  assert.equal(row('acct_a').querySelector('[data-rr-error]').textContent, 'Not saved: That value is not allowed.')
  absent(row('acct_a').querySelector('[data-rr-saved="checking"]'), 'a real error is not "unknown"')
  assert.equal(h.calls.filter(x => x.key === 'GET /settings').length, reads, 'no reconcile for a definite answer')
  refuse = false
  await tap('acct_a', 'early_redeem_enabled', 'on')
  await h.flush()
  assert.equal(e.find('acct_a').overrides.early_redeem_enabled, true)
  assert.equal(note('acct_a'), null, 'no Saved')
})

// ---------------------------------------------------------- outside the queue

test('D-033 r6: /redeem is not queued behind a settings POST and is never retried', { skip }, async () => {
  const g = gates()
  let redeems = 0
  await mount({ hold: g.hold })
  h.routes['POST /redeem'] = () => {
    redeems += 1
    return Promise.reject(new Error('500: {"error":"internal_error"}'))
  }
  await openSettings()
  await h.click(toggle('auto_redeem_enabled')) // held in flight
  await h.click(h.q('[data-rr-chip]')) // close
  await h.flush()
  await h.click(h.q('[data-rr-chip]')) // reopen on Accounts
  await h.flush()
  await h.click(h.q('[data-rr-redeem="cr_a1"]'))
  await h.click(h.q('[data-slot="confirm-ok"]'))
  await h.flush(5)
  assert.equal(redeems, 1, '/redeem went out while the settings POST was still open, once')
  assert.equal(g.open.length, 1, 'the settings POST is still in flight')
  await drainNewestFirst(g)
  await h.flush(5)
  assert.equal(redeems, 1, 'not retried')
})

test('D-033 r6: the Checking… strings exist in both bundles', { skip }, async () => {
  const { STRINGS } = await loadBundle()
  assert.equal(STRINGS.en.settings.checking, 'Checking…')
  assert.equal(STRINGS['pt-BR'].settings.checking, 'Confirmando…')
})

test('D-033 R6b: the write applies and answers 504; the reconcile GET answers 504 twice, then the new value: a silent row, never Not saved', { skip }, async () => {
  const e = engine()
  const post = e.routes['POST /settings/account']
  let gets = 0
  const engineGet = e.routes['GET /settings']
  const routes = {
    ...e.routes,
    'POST /settings/account': async body => {
      await post(body)
      throw Object.assign(new Error('504: {"error":"timeout"}'), { status: 504 })
    },
    'GET /settings': () => {
      if (!armed) return engineGet()
      gets += 1
      if (gets <= 2) throw Object.assign(new Error('504: {"error":"busy"}'), { status: 504 })
      return engineGet()
    }
  }
  let armed = false
  h = await mountPlugin({ routes })
  await h.flush()
  await openPerAccount()
  const c = () => cell('acct_b', 'auto_redeem_enabled')
  armed = true
  mock.timers.enable({ apis: ['setTimeout'] })
  await tap('acct_b', 'auto_redeem_enabled', 'default')
  await microtasks()
  // D-033 r7: the reads back off, 2 s then 5 s.
  for (const step of [2_000, 5_000]) {
    await h.act(async () => mock.timers.tick(step))
    await microtasks()
  }
  mock.timers.reset()
  await h.flush(5)
  assert.equal(gets, 3, 'two 504 reads, then the read that answers')
  const sent = posts('POST /settings/account').map(x => x.body.value)
  assert.equal(sent.length, 1)
  assert.equal(e.find('acct_b').overrides.auto_redeem_enabled ?? null, sent[0], 'the engine holds the sent intent')
  assert.equal(c().dataset.rrState, sent[0] === null ? 'global' : sent[0] ? 'on' : 'off', 'the cell shows the engine value')
  assert.equal(note('acct_b'), null, 'settled: no Saved')
  assert.ok(!/Não salvou/.test(pop().textContent), 'never Not saved')
})
