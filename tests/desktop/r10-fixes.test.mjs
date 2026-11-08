// r10 review fixes (D-034 popup):
//  1. clearing an override (Default click, Reset this account, Reset all
//     accounts to global) shows the inherited global value at once, before
//     the engine answers; the reply's `effective` then wins;
//  2. Watch's Default label follows the engine's `excluded` flag (D-028);
//  3. Reset this account is always inside an expanded account, disabled
//     when the account has no own settings;
//  4. the Settings view shows its title once (the popup header).
import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'

import { accountRow, expand, optionEl, pick, segmentsOf } from './account-list.mjs'
import { account, credit, NOW, overridden, perAccount, settingsWithAccounts, status } from './fixtures.mjs'
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

const BASE_FIELDS = [
  { key: 'auto_redeem_enabled', type: 'bool', value: true },
  { key: 'early_redeem_enabled', type: 'bool', value: false },
  { key: 'early_redeem_remaining_pct', type: 'int', value: 10, min: 0, max: 100 },
  { key: 'reminders_enabled', type: 'bool', value: true }
]

/** A fake engine answering like the per-account API; `held` routes wait for
 *  `release(route)` before they answer. */
function engine(st = perAccount(), fields = BASE_FIELDS) {
  const state = structuredClone(st)
  const find = key => state.accounts.find(a => a.account_key === key)
  const reply = key => {
    for (const a of state.accounts) Object.assign(a, overridden(a.overrides))
    const a = find(key)
    return { account_key: key, overrides: { ...a.overrides }, effective: { ...a.effective }, status: structuredClone(state) }
  }
  const gates = {}
  const gate = route => {
    gates[route] = deferred()
    return gates[route].promise
  }
  return {
    state,
    gates,
    routes: {
      'GET /status': () => structuredClone(state),
      'GET /settings': () => settingsWithAccounts(fields, state),
      'POST /settings/account': async ({ account_key, field, value }) => {
        await gate('POST /settings/account')
        const a = find(account_key)
        if (value === null) delete a.overrides[field]
        else a.overrides[field] = value
        return reply(account_key)
      },
      'POST /settings/account/reset': async ({ account_key }) => {
        await gate('POST /settings/account/reset')
        find(account_key).overrides = {}
        return reply(account_key)
      },
      'POST /settings/account/reset-all': async () => {
        await gate('POST /settings/account/reset-all')
        for (const a of state.accounts) a.overrides = {}
        for (const a of state.accounts) Object.assign(a, overridden(a.overrides))
        return { ...settingsWithAccounts(fields, state), status: structuredClone(state) }
      }
    }
  }
}

async function mount({ st, fields, locale, routes = {} } = {}) {
  const e = engine(st, fields)
  h = await mountPlugin({ routes: { ...e.routes, ...routes }, ...(locale ? { locale } : {}) })
  await h.flush()
  return e
}

const pop = () => h.q('[data-slot="popover-content"]')

async function microtasks(n = 10) {
  for (let i = 0; i < n; i += 1) await h.act(async () => {})
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

/** The Default segment's label of an open option. */
const defaultLabel = (key, field) => segmentsOf(optionEl(h, key, field))[0].textContent
const inherited = (key, field) => optionEl(h, key, field).dataset.rrInherited
const threshold = key => accountRow(h, key).querySelector('[data-rr-threshold]')

/** acct_c: global Early off, the account's own Early on (threshold 20). */
function assertCFollowsGlobalEarlyOff(when) {
  assert.equal(optionEl(h, 'acct_c', 'early_redeem_enabled').dataset.rrState, 'global', `${when}: on Default`)
  assert.equal(inherited('acct_c', 'early_redeem_enabled'), 'off', `${when}: inherits the global Off`)
  assert.equal(defaultLabel('acct_c', 'early_redeem_enabled'), 'Default (Off)', `${when}: Default label`)
  absent(threshold('acct_c'), `${when}: no threshold while early redeem is off`)
}

// ---------------------------------------------- 1. clearing an override

test('r10-1: Default on an own Early On shows Default (Off) and hides the threshold before the reply', { skip }, async () => {
  const e = await mount()
  await openPerAccount()
  await expand(h, 'acct_c')
  assert.equal(defaultLabel('acct_c', 'early_redeem_enabled'), 'Default (Off)', 'the label before the click')
  assert.ok(threshold('acct_c'), 'the threshold shows while the own Early is on')
  mock.timers.enable({ apis: ['setTimeout'] })
  await pick(h, 'acct_c', 'early_redeem_enabled', 'default')
  await microtasks()
  assert.ok(e.gates['POST /settings/account'], 'the save is still waiting for the engine')
  assertCFollowsGlobalEarlyOff('before the reply')
  e.gates['POST /settings/account'].resolve()
  mock.timers.reset()
  await h.flush(5)
  assertCFollowsGlobalEarlyOff('after the reply')
})

test('r10-1: Default on an own Auto Off shows Default (On) at once', { skip }, async () => {
  const e = await mount({ locale: 'pt-br' })
  await openPerAccount()
  await expand(h, 'acct_b')
  mock.timers.enable({ apis: ['setTimeout'] })
  await pick(h, 'acct_b', 'auto_redeem_enabled', 'default')
  await microtasks()
  assert.equal(optionEl(h, 'acct_b', 'auto_redeem_enabled').dataset.rrState, 'global')
  assert.equal(inherited('acct_b', 'auto_redeem_enabled'), 'on')
  assert.equal(defaultLabel('acct_b', 'auto_redeem_enabled'), 'Padrão (Ligado)')
  e.gates['POST /settings/account'].resolve()
  mock.timers.reset()
  await h.flush(5)
  assert.equal(defaultLabel('acct_b', 'auto_redeem_enabled'), 'Padrão (Ligado)')
})

test('r10-1: Reset this account shows the global values before the reply', { skip }, async () => {
  const e = await mount()
  await openPerAccount()
  const row = await expand(h, 'acct_c')
  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(row.querySelector('[data-rr-account-reset]'))
  await microtasks()
  assert.ok(e.gates['POST /settings/account/reset'], 'the reset is still waiting for the engine')
  assert.equal(accountRow(h, 'acct_c').querySelector('[data-rr-summary]').textContent, 'Follows global')
  assertCFollowsGlobalEarlyOff('before the reply')
  e.gates['POST /settings/account/reset'].resolve()
  mock.timers.reset()
  await h.flush(5)
  assertCFollowsGlobalEarlyOff('after the reply')
})

test('r10-1: Reset all accounts to global shows the global values before the reply', { skip }, async () => {
  const e = await mount()
  await openPerAccount()
  await expand(h, 'acct_c')
  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(pop().querySelector('[data-rr-reset-all]'))
  await h.click(pop().querySelector('[data-rr-reset-all]'))
  await microtasks()
  assert.ok(e.gates['POST /settings/account/reset-all'], 'the reset is still waiting for the engine')
  assertCFollowsGlobalEarlyOff('before the reply')
  e.gates['POST /settings/account/reset-all'].resolve()
  mock.timers.reset()
  await h.flush(5)
  await expand(h, 'acct_c')
  assertCFollowsGlobalEarlyOff('after the reply')
})

test("r10-1: the reply's `effective` wins over the optimistic value", { skip }, async () => {
  // The engine answers with only {account_key, overrides, effective}: here it
  // says the inherited Early is On (the global changed elsewhere).
  await mount({
    routes: {
      'POST /settings/account': ({ account_key }) => ({
        account_key,
        overrides: { early_redeem_remaining_pct: 20 },
        effective: { auto_redeem_enabled: true, early_redeem_enabled: true, early_redeem_remaining_pct: 20, reminders_enabled: true }
      })
    }
  })
  await openPerAccount()
  await expand(h, 'acct_c')
  await pick(h, 'acct_c', 'early_redeem_enabled', 'default')
  await h.flush(5)
  assert.equal(optionEl(h, 'acct_c', 'early_redeem_enabled').dataset.rrState, 'global')
  assert.equal(inherited('acct_c', 'early_redeem_enabled'), 'on', 'the engine value')
  assert.equal(defaultLabel('acct_c', 'early_redeem_enabled'), 'Default (On)')
})

// ------------------------------- 2. Watch inheritance and Excluded accounts

/** acct_d sets its own Watch On while Excluded accounts names it. The engine
 *  reports `excluded` per account (the backend matcher, D-028), so the stub
 *  states it the way GET /settings and GET /status do. */
function excludedSetup(excluded) {
  const st = status({
    settings: { early_redeem_enabled: false, early_redeem_remaining_pct: 10 },
    accounts: [
      account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40, fireIn: 39.8 })], overridden()),
      { ...account('acct_d', 'd***@e***.com', [credit('cr_d1', { expiresIn: 5 })], overridden({ monitored: true })), excluded }
    ]
  })
  return { st, fields: [...BASE_FIELDS, { key: 'excluded_accounts', type: 'text', value: excluded ? 'd***@example.com' : '' }] }
}

test('r10-2: Watch Default reads Off when the engine reports the account excluded', { skip }, async () => {
  await mount(excludedSetup(true))
  await openPerAccount()
  await expand(h, 'acct_d')
  assert.equal(optionEl(h, 'acct_d', 'monitored').dataset.rrState, 'on', 'its own Watch On')
  assert.equal(inherited('acct_d', 'monitored'), 'off')
  assert.equal(defaultLabel('acct_d', 'monitored'), 'Default (Off)')
})

test('r10-2: a not-excluded account inherits On', { skip }, async () => {
  await mount(excludedSetup(false))
  await openPerAccount()
  await expand(h, 'acct_d')
  assert.equal(inherited('acct_d', 'monitored'), 'on')
  assert.equal(defaultLabel('acct_d', 'monitored'), 'Default (On)')
})

test('r10-2: an engine without the excluded flag falls back to the literal label check', { skip }, async () => {
  const st = status({
    settings: { early_redeem_enabled: false, early_redeem_remaining_pct: 10 },
    accounts: [
      account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40, fireIn: 39.8 })], overridden()),
      account('acct_d', 'd***@e***.com', [credit('cr_d1', { expiresIn: 5 })], overridden({ monitored: true }))
    ]
  })
  await mount({ st, fields: [...BASE_FIELDS, { key: 'excluded_accounts', type: 'text', value: 'd***@e***.com' }] })
  await openPerAccount()
  await expand(h, 'acct_d')
  assert.equal(inherited('acct_d', 'monitored'), 'off', 'the masked label is in the list')
})

test('r10-2: Default on an own Watch of an excluded account shows it paused at once', { skip }, async () => {
  const e = await mount(excludedSetup(true))
  await openPerAccount()
  await expand(h, 'acct_d')
  mock.timers.enable({ apis: ['setTimeout'] })
  await pick(h, 'acct_d', 'monitored', 'default')
  await microtasks()
  assert.equal(optionEl(h, 'acct_d', 'monitored').dataset.rrState, 'global')
  assert.equal(inherited('acct_d', 'monitored'), 'off')
  assert.equal(accountRow(h, 'acct_d').dataset.rrPaused, 'true', 'the row reads Paused before the reply')
  e.gates['POST /settings/account'].resolve()
})

// ---------------------------------------------------- 3. reset is always there

test('r10-3: Reset this account is shown disabled when the account has no own settings', { skip }, async () => {
  await mount({ locale: 'pt-br' })
  await openPerAccount()
  const row = await expand(h, 'acct_a')
  const reset = row.querySelector('[data-rr-account-reset]')
  assert.ok(reset, 'present inside the expanded account')
  assert.equal(reset.textContent, 'Restaurar esta conta')
  assert.equal(reset.disabled, true, 'disabled with nothing to reset')
  await h.click(reset)
  await h.flush()
  assert.equal(h.calls.filter(c => c.key === 'POST /settings/account/reset').length, 0, 'a disabled click posts nothing')
  const own = (await expand(h, 'acct_c')).querySelector('[data-rr-account-reset]')
  assert.equal(own.disabled, false, 'enabled with own settings')
})

// --------------------------------------------------------- 4. one heading

for (const [locale, title, hint] of [
  [undefined, 'Settings', 'Changes save as you make them.'],
  ['pt-br', 'Configurações', 'As mudanças são salvas na hora.']
]) {
  test(`r10-4: the Settings view shows its title once (${locale ?? 'en'})`, { skip }, async () => {
    await mount({ locale })
    const p = await openSettings()
    const headings = [...p.querySelectorAll('h1, h2, h3, h4')].filter(el => el.textContent === title)
    assert.equal(headings.length, 1, 'one heading with the title')
    assert.equal(headings[0].tagName, 'H2', 'the popup header keeps it')
    absent(p.querySelector('[data-rr-settings-tab] h3'), 'no section title')
    assert.ok(p.querySelector('[data-rr-settings-tab]').textContent.includes(hint), 'the hint stays')
  })
}
