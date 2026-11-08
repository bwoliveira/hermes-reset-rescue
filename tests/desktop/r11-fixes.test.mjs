// r11 review fixes (desktop/plugin.js), production-shaped payloads:
//  2. Reminders Off -> Default shows the inherited global On before the reply.
//     The Settings fields carry no global reminders field (it is internal, D-034),
//     so the popup must supply the schema default itself;
//  3. Reset all accounts clears the exclusion list, so an excluded account shows
//     Watch On (Default) before the reply, not Paused;
//  4. the "Rescue ..." line shows only for a scheduled primary/backup slot, not for
//     a far credit, a paused account or an account with auto-redeem off.
import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'

import { accountRow, expand, optionEl, pick, segmentsOf } from './account-list.mjs'
import { account, credit, NOW, overridden, status } from './fixtures.mjs'
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

// Production GET /settings fields: no reminders_enabled entry (D-034 keeps it internal).
const FIELDS = [
  { key: 'auto_redeem_enabled', type: 'bool', value: true },
  { key: 'early_redeem_enabled', type: 'bool', value: false },
  { key: 'early_redeem_remaining_pct', type: 'int', value: 10, min: 0, max: 100 },
  { key: 'excluded_accounts', type: 'str', value: '' }
]

/** GET /settings in the shape plugin_api.py returns: fields plus per-account rows. */
function settingsFor(fields, st) {
  return {
    fields,
    account_overrides: Object.fromEntries(
      st.accounts.filter(a => Object.keys(a.overrides ?? {}).length).map(a => [a.account_key, { label: a.label, ...a.overrides }])
    ),
    accounts: st.accounts.map(a => ({
      account_key: a.account_key,
      label: a.label,
      overrides: a.overrides,
      effective: a.effective,
      excluded: a.excluded,
      monitored: a.monitored
    }))
  }
}

const pop = () => h.q('[data-slot="popover-content"]')
const defaultLabel = (key, field) => segmentsOf(optionEl(h, key, field))[0].textContent
const inherited = (key, field) => optionEl(h, key, field).dataset.rrInherited

async function openPerAccount() {
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  return pop()
}

async function openPopover() {
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  return pop()
}

// ---------------------------------------------- 2. Reminders inheritance

test('r11-2: Reminders Default after Off shows the inherited On before the reply', { skip }, async () => {
  // Two accounts: the Per account scope (and its accordion) shows only for 2+ accounts.
  const st = status({
    accounts: [
      account('acct_a', 'a***@e***.com', [], overridden({ reminders_enabled: false })),
      account('acct_b', 'b***@e***.com', [], overridden({}))
    ]
  })
  const gate = deferred()
  h = await mountPlugin({
    routes: {
      'GET /status': () => structuredClone(st),
      'GET /settings': () => settingsFor(FIELDS, st),
      'POST /settings/account': () => gate.promise
    }
  })
  await h.flush()
  await openPerAccount()
  await expand(h, 'acct_a')
  assert.equal(defaultLabel('acct_a', 'reminders_enabled'), 'Default (On)', 'the inherited global On, before the click')

  mock.timers.enable({ apis: ['setTimeout'] })
  await pick(h, 'acct_a', 'reminders_enabled', 'default')
  await h.act(async () => {})
  assert.equal(
    inherited('acct_a', 'reminders_enabled'),
    'on',
    'the optimistic Default must show the inherited global reminders value (On)'
  )
  assert.equal(defaultLabel('acct_a', 'reminders_enabled'), 'Default (On)')
  gate.resolve({ account_key: 'acct_a', overrides: {}, effective: { reminders_enabled: true }, status: st })
  mock.timers.reset()
  await h.flush(5)
  assert.equal(defaultLabel('acct_a', 'reminders_enabled'), 'Default (On)', 'after the reply')
})

// ---------------------------------------------- 3. reset all accounts

test('r11-3: Reset all accounts shows an excluded account Watch On before the reply', { skip }, async () => {
  const st = status({
    accounts: [
      account('acct_a', 'a***@e***.com', [], { ...overridden({}, { monitored: false }), excluded: true }),
      account('acct_b', 'b***@e***.com', [], overridden({}))
    ]
  })
  const fields = FIELDS.map(f => (f.key === 'excluded_accounts' ? { ...f, value: 'acct_a' } : f))
  const gate = deferred()
  h = await mountPlugin({
    routes: {
      'GET /status': () => structuredClone(st),
      'GET /settings': () => settingsFor(fields, st),
      'POST /settings/account/reset-all': () => gate.promise
    }
  })
  await h.flush()
  await openPerAccount()
  await expand(h, 'acct_a')
  assert.equal(inherited('acct_a', 'monitored'), 'off', 'the excluded account starts paused')

  mock.timers.enable({ apis: ['setTimeout'] })
  await h.click(pop()?.querySelector('[data-rr-reset-all]') ?? h.q('[data-rr-reset-all]'))
  await h.click(pop()?.querySelector('[data-rr-reset-all]') ?? h.q('[data-rr-reset-all]'))
  await h.act(async () => {})
  assert.equal(
    inherited('acct_a', 'monitored'),
    'on',
    'reset all clears excluded_accounts: the optimistic Watch must not keep the account paused'
  )
  assert.equal(defaultLabel('acct_a', 'monitored'), 'Default (On)')
  // The server's reply after reset-all: no exclusions, every account watched.
  const after = structuredClone(st)
  for (const a of after.accounts) Object.assign(a, { excluded: false, monitored: true, overrides: {} })
  gate.resolve({ ...settingsFor(fields.map(f => (f.key === 'excluded_accounts' ? { ...f, value: '' } : f)), after), status: after })
  mock.timers.reset()
  await h.flush(5)
  // The reply remounts the list and collapses the accordion; open the row again.
  await expand(h, 'acct_a')
  assert.equal(inherited('acct_a', 'monitored'), 'on', 'after the reply')
})

// ---------------------------------------------- 4. the rescue line

test('r11-4: a scheduled primary slot shows the Rescue line', { skip }, async () => {
  h = await mountPlugin({
    routes: {
      'GET /status': () => status({ accounts: [account('acct_a', 'a***@e***.com', [credit('cr_s', { expiresIn: 3, fireIn: 2 })])] })
    }
  })
  await h.flush()
  const p = await openPopover()
  const line = p.querySelector('[data-rr-credit="cr_s"] [data-rr-rescue-at]')
  assert.ok(line, 'a scheduled primary slot must show its rescue line')
  assert.match(line.textContent, /^Rescue /)
})

test('r11-4: a far credit with no slot shows no Rescue line', { skip }, async () => {
  h = await mountPlugin({
    routes: {
      'GET /status': () => status({ accounts: [account('acct_a', 'a***@e***.com', [credit('cr_far', { expiresIn: 100 })])] })
    }
  })
  await h.flush()
  const p = await openPopover()
  const row = p.querySelector('[data-rr-credit="cr_far"]')
  assert.ok(row, 'the far credit is listed')
  assert.equal(row.dataset.rrRescue, 'none')
  absent(row.querySelector('[data-rr-rescue-at]'), 'no booked rescue for a credit outside the 48 h horizon')
})

test('r11-4: a credit more than 48 h out with no slot shows no Rescue line', { skip }, async () => {
  h = await mountPlugin({
    routes: {
      'GET /status': () => status({ accounts: [account('acct_a', 'a***@e***.com', [credit('cr_72', { expiresIn: 72 })])] })
    }
  })
  await h.flush()
  const p = await openPopover()
  const row = p.querySelector('[data-rr-credit="cr_72"]')
  assert.ok(row, 'the 72 h credit is listed')
  assert.equal(row.dataset.rrRescue, 'none')
  absent(row.querySelector('[data-rr-rescue-at]'), 'no booked rescue beyond the 48 h horizon')
})

test('r11-4: a paused account shows no Rescue line even with a scheduled slot', { skip }, async () => {
  h = await mountPlugin({
    routes: {
      'GET /status': () =>
        status({
          accounts: [account('acct_a', 'a***@e***.com', [credit('cr_p', { expiresIn: 3, fireIn: 2 })], overridden({}, { monitored: false }))]
        })
    }
  })
  await h.flush()
  const p = await openPopover()
  const row = p.querySelector('[data-rr-credit="cr_p"]')
  assert.equal(row.dataset.rrRescue, 'none')
  absent(row.querySelector('[data-rr-rescue-at]'), 'a paused account books no rescue')
})

test('r11-4: an account with auto-redeem off shows no Rescue line even with a scheduled slot', { skip }, async () => {
  h = await mountPlugin({
    routes: {
      'GET /status': () =>
        status({
          accounts: [
            account('acct_a', 'a***@e***.com', [credit('cr_o', { expiresIn: 3, fireIn: 2 })], overridden({ auto_redeem_enabled: false }))
          ]
        })
    }
  })
  await h.flush()
  const p = await openPopover()
  const row = p.querySelector('[data-rr-credit="cr_o"]')
  assert.equal(row.dataset.rrRescue, 'none')
  absent(row.querySelector('[data-rr-rescue-at]'), 'auto redeem off books no rescue')
})
