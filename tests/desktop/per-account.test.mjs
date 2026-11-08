// D-030: per-account settings in the popup.
//
// The Settings tab gets an All accounts / Per account switch. D-034 replaced
// the D-030 matrix with an account list (accordion, tests in
// d034-account-list.test.mjs); this file keeps the D-030 rules that still
// hold: the switch, saves through POST /settings/account {account_key, field,
// value} (null clears the override), the account reset, the Accounts-tab tags
// that open Settings > Per account with that account expanded, paused
// accounts greyed and last, and the single-account rule.
import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'

import { accountRow, expand, option, optionEl, pick, segmentsOf } from './account-list.mjs'
import { account, credit, NOW, overridden, perAccount, settingsWithAccounts, status } from './fixtures.mjs'
import { absent, closeDom, harnessAvailability, loadBundle, mountPlugin } from './harness.mjs'

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

const FIELDS = [
  { key: 'auto_redeem_enabled', type: 'bool', value: true },
  { key: 'early_redeem_enabled', type: 'bool', value: false },
  { key: 'early_redeem_remaining_pct', type: 'int', value: 10, min: 0, max: 100 }
]
const OPTIONS = ['auto_redeem_enabled', 'early_redeem_enabled', 'reminders_enabled', 'monitored']

/** A fake engine: keeps the overrides, answers both D-030 routes with a fresh
 *  StatusDTO built from them. */
function engine(st = perAccount()) {
  const state = structuredClone(st)
  const rebuild = () => {
    for (const a of state.accounts) Object.assign(a, overridden(a.overrides))
    return structuredClone(state)
  }
  const find = key => state.accounts.find(a => a.account_key === key)
  return {
    state,
    routes: {
      'GET /status': () => structuredClone(state),
      'GET /settings': () => settingsWithAccounts(FIELDS, state),
      'POST /settings/account': ({ account_key, field, value }) => {
        const a = find(account_key)
        if (value === null) delete a.overrides[field]
        else a.overrides[field] = value
        return rebuild()
      },
      'POST /settings/account/reset': ({ account_key }) => {
        find(account_key).overrides = {}
        return rebuild()
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
const row = key => accountRow(h, key)
const listKeys = () => h.qa('[data-rr-account-row]').map(r => r.dataset.rrAccountRow)

async function openPopover() {
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  return pop()
}

async function openPerAccount() {
  await openPopover()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  return pop()
}

// ------------------------------------------------------------- the switch

test('D-030: the Settings tab opens on All accounts with a two-option switch', { skip }, async () => {
  await mount()
  await openPopover()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  const p = pop()
  const options = [...p.querySelectorAll('[data-rr-scope-option]')]
  assert.deepEqual(options.map(o => o.dataset.rrScopeOption), ['all', 'account'])
  assert.deepEqual(options.map(o => o.textContent), ['All accounts', 'Per account'])
  assert.deepEqual(options.map(o => o.getAttribute('aria-pressed')), ['true', 'false'])
  assert.ok(p.querySelector('[data-rr-setting="auto_redeem_enabled"]'), 'All accounts shows the global rows')
  absent(p.querySelector('[data-rr-account-list]'), 'no account list on All accounts')
})

test('D-030: Per account swaps the global rows for the account list and back', { skip }, async () => {
  await mount()
  const p = await openPerAccount()
  assert.ok(p.querySelector('[data-rr-account-list]'), 'the account list rendered (D-034)')
  absent(p.querySelector('[data-rr-setting]'), 'the global rows are hidden')
  assert.equal(p.querySelector('[data-rr-scope-option="account"]').getAttribute('aria-pressed'), 'true')
  await h.click(p.querySelector('[data-rr-scope-option="all"]'))
  await h.flush()
  absent(pop().querySelector('[data-rr-account-list]'), 'All accounts hides the list')
  assert.ok(pop().querySelector('[data-rr-setting="auto_redeem_enabled"]'))
})

// ------------------------------------------------------------- the options

test('D-030/D-034: an option shows Default with the inherited value, or the own setting', { skip }, async () => {
  await mount()
  await openPerAccount()
  const a = await option(h, 'acct_a', 'auto_redeem_enabled')
  assert.equal(a.dataset.rrState, 'global')
  assert.equal(a.dataset.rrInherited, 'on')
  assert.equal(segmentsOf(a)[0].textContent, 'Default (On)')
  assert.equal(optionEl(h, 'acct_a', 'early_redeem_enabled').dataset.rrInherited, 'off')
  const b = await option(h, 'acct_b', 'auto_redeem_enabled')
  assert.equal(b.dataset.rrState, 'off')
  assert.deepEqual(segmentsOf(b).map(x => x.getAttribute('aria-pressed')), ['false', 'false', 'true'])
  assert.equal((await option(h, 'acct_c', 'early_redeem_enabled')).dataset.rrState, 'on')
  assert.equal((await option(h, 'acct_d', 'monitored')).dataset.rrState, 'off')
})

test('D-030: a save adopts the returned status, so the Accounts tab follows', { skip }, async () => {
  await mount()
  await openPerAccount()
  await pick(h, 'acct_a', 'auto_redeem_enabled', 'off')
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  assert.equal(pop().querySelector('[data-rr-account="acct_a"] [data-rr-tag]').textContent, 'Auto off')
})

test('D-030: a rejected option shows the error on its account and keeps the old state', { skip }, async () => {
  await mount({
    routes: {
      'POST /settings/account': () => {
        throw Object.assign(new Error('HTTP 422'), { status: 422 })
      }
    }
  })
  await openPerAccount()
  await pick(h, 'acct_a', 'reminders_enabled', 'off')
  await h.flush()
  assert.equal(row('acct_a').querySelector('[data-rr-error]').textContent, 'Not saved: HTTP 422')
  assert.equal(optionEl(h, 'acct_a', 'reminders_enabled').dataset.rrState, 'global')
})

test('D-030/D-034: Reset this account is enabled only with overrides and clears the whole account', { skip }, async () => {
  await mount()
  await openPerAccount()
  await expand(h, 'acct_a')
  assert.equal(row('acct_a').querySelector('[data-rr-account-reset]').disabled, true, 'Reset is disabled without overrides')
  await expand(h, 'acct_b')
  const reset = row('acct_b').querySelector('[data-rr-account-reset]')
  assert.equal(reset.textContent, 'Reset this account')
  assert.ok((reset.dataset.tip ?? '').length > 20, 'Reset explains itself (SDK Tip)')
  await h.click(reset)
  await h.flush()
  assert.deepEqual(posts('POST /settings/account/reset').map(c => c.body), [{ account_key: 'acct_b' }])
  assert.equal(optionEl(h, 'acct_b', 'auto_redeem_enabled').dataset.rrState, 'global')
  assert.equal(row('acct_b').querySelector('[data-rr-account-reset]').disabled, true, 'Reset is disabled once the overrides are gone')
})

// ------------------------------------------------------- Accounts tab tags

test('D-030: Accounts tab tags accounts with overrides: Auto off, Early on, Paused, Custom', { skip }, async () => {
  const st = perAccount()
  st.accounts.push(
    account('acct_e', 'e***@e***.com', [], overridden({ auto_redeem_enabled: false, reminders_enabled: false }))
  )
  await mount({ st })
  const p = await openPopover()
  const tag = key => p.querySelector(`[data-rr-account="${key}"] [data-rr-tag]`)
  absent(tag('acct_a'), 'no tag without overrides')
  assert.equal(tag('acct_b').textContent, 'Auto off')
  assert.equal(tag('acct_c').textContent, 'Early on', 'early on with its threshold is still one tag')
  assert.equal(tag('acct_d').textContent, 'Paused')
  assert.equal(tag('acct_e').textContent, 'Custom')
  assert.equal(tag('acct_b').tagName, 'BUTTON')
  assert.ok((tag('acct_b').dataset.tip ?? '').length > 20, 'the tag explains the click (SDK Tip)')
  absent(tag('acct_b').getAttribute('title'), 'no native title (D-034)')
})

test('D-030: clicking a tag opens Settings > Per account with that row expanded', { skip }, async () => {
  await mount()
  const p = await openPopover()
  await h.click(p.querySelector('[data-rr-account="acct_c"] [data-rr-tag]'))
  await h.flush()
  assert.ok(pop().querySelector('[data-rr-account-list]'), 'the per-account list is open')
  assert.equal(pop().querySelector('[data-rr-scope-option="account"]').getAttribute('aria-pressed'), 'true')
  assert.ok(row('acct_c').querySelector('[data-rr-account-detail]'), 'that account is expanded')
  absent(row('acct_a').querySelector('[data-rr-account-detail]'), 'only that account')
  assert.equal(row('acct_c').dataset.rrExpanded, 'true')
})

test('D-030: paused accounts are greyed and listed after the others on both tabs', { skip }, async () => {
  await mount()
  const p = await openPopover()
  const keys = [...p.querySelectorAll('[data-rr-account]')].map(n => n.dataset.rrAccount)
  // acct_d holds the soonest reset (5 h) but is paused: last.
  assert.equal(keys.at(-1), 'acct_d')
  const d = p.querySelector('[data-rr-account="acct_d"]')
  assert.equal(d.dataset.rrPaused, 'true')
  assert.match(d.getAttribute('style'), /opacity: 0\.\d/)
  absent(p.querySelector('[data-rr-account="acct_a"][data-rr-paused]'), 'a watched account is not greyed')
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  assert.equal(row('acct_d').dataset.rrPaused, 'true')
  assert.match(row('acct_d').querySelector('[data-rr-row-label]').getAttribute('style'), /opacity: 0\.\d/)
  assert.equal(listKeys().at(-1), 'acct_d')
})

test('D-030: without the accounts list in GET /settings the per-account list uses the status accounts', { skip }, async () => {
  await mount({ routes: { 'GET /settings': () => ({ fields: FIELDS }) } })
  await openPerAccount()
  assert.deepEqual(listKeys(), ['acct_a', 'acct_b', 'acct_c', 'acct_d'])
  assert.equal((await option(h, 'acct_b', 'auto_redeem_enabled')).dataset.rrState, 'off')
})

test('D-030: an empty pool shows no switch, only the global rows', { skip }, async () => {
  await mount({ st: status({ accounts: [] }) })
  await openPopover()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  absent(pop().querySelector('[data-rr-scope]'), 'no switch without accounts')
  assert.ok(pop().querySelector('[data-rr-setting="auto_redeem_enabled"]'))
})

// ------------------------------------------------------ single-account rule

const single = overrides => status({ accounts: [account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40, fireIn: 39.8 })], overridden(overrides))] })

async function openSettings({ open = true } = {}) {
  if (open) await openPopover()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  return pop()
}

test('D-030: one account without overrides has no switch, no tags and no notice', { skip }, async () => {
  await mount({ st: single() })
  const list = await openPopover()
  absent(list.querySelector('[data-rr-tag]'), 'no tag on the Accounts tab')
  const p = await openSettings({ open: false })
  absent(p.querySelector('[data-rr-scope]'), 'no All accounts / Per account switch')
  absent(p.querySelector('[data-rr-scope-option]'))
  absent(p.querySelector('[data-rr-single-notice]'), 'no notice without overrides')
  assert.ok(p.querySelector('[data-rr-setting="auto_redeem_enabled"]'), 'the global rows as before')
})

test('D-030: one account with overrides hides the switch and tags, shows a notice that resets it', { skip }, async () => {
  await mount({ st: single({ auto_redeem_enabled: false }) })
  const list = await openPopover()
  absent(list.querySelector('[data-rr-tag]'), 'no tag with a single account')
  const p = await openSettings({ open: false })
  absent(p.querySelector('[data-rr-scope]'), 'no switch with a single account')
  const notice = p.querySelector('[data-rr-single-notice]')
  assert.ok(notice, 'the notice shows')
  assert.match(notice.textContent, /This account has its own settings/)
  const link = notice.querySelector('[data-rr-single-reset]')
  assert.equal(link.textContent, 'Use global settings')
  assert.equal(link.tagName, 'BUTTON')
  assert.ok(p.querySelector('[data-rr-setting="auto_redeem_enabled"]'), 'the global rows stay')
  await h.click(link)
  await h.flush()
  assert.deepEqual(posts('POST /settings/account/reset').map(c => c.body), [{ account_key: 'acct_a' }])
  absent(pop().querySelector('[data-rr-single-notice]'), 'the notice goes away with the overrides')
})

test('D-030: one account with overrides in pt-BR', { skip }, async () => {
  await mount({ st: single({ auto_redeem_enabled: false }), locale: 'pt-br' })
  const p = await openSettings()
  const notice = p.querySelector('[data-rr-single-notice]')
  assert.match(notice.textContent, /Esta conta tem ajustes próprios/)
  assert.equal(notice.querySelector('[data-rr-single-reset]').textContent, 'Usar configurações globais')
})

test('D-030: per_account_available from the API wins over the account count', { skip }, async () => {
  const st = perAccount()
  st.per_account_available = false
  await mount({ st })
  const p = await openSettings()
  absent(p.querySelector('[data-rr-scope]'), 'the API says per-account is off')
})

test('D-030: two accounts show the switch', { skip }, async () => {
  const st = status({ accounts: [account('acct_a', 'a***@e***.com', [], overridden()), account('acct_b', 'b***@e***.com', [], overridden())] })
  await mount({ st })
  const p = await openSettings()
  assert.ok(p.querySelector('[data-rr-scope]'), 'the switch shows with two accounts')
  absent(p.querySelector('[data-rr-single-notice]'))
})

// D-030 engine deviation: `account_count` is the next scan's accounts (paused
// left out); the pool, paused included, is `pool_account_count`.
const pausedPool = () => {
  const paused = { monitored: false }
  const st = status({
    accounts: [
      account('acct_a', 'a***@e***.com', [], overridden()),
      account('acct_b', 'b***@e***.com', [], { ...overridden({ monitored: false }, paused), included: false }),
      account('acct_c', 'c***@e***.com', [], { ...overridden({ monitored: false }, paused), included: false })
    ]
  })
  st.account_count = 1
  return st
}

test('D-030: three pool accounts with two paused still show the switch (pool_account_count on the status)', { skip }, async () => {
  const st = pausedPool()
  st.pool_account_count = 3
  st.per_account_available = true
  await mount({ st })
  const p = await openSettings()
  assert.ok(p.querySelector('[data-rr-scope]'), 'the switch shows: the pool has three accounts')
  absent(p.querySelector('[data-rr-single-notice]'))
  await h.click(p.querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  assert.deepEqual(listKeys(), ['acct_a', 'acct_b', 'acct_c'])
  assert.equal((await option(h, 'acct_b', 'monitored')).dataset.rrState, 'off', 'a paused account can be watched again')
})

test('D-030: pool_account_count from GET /settings beats account_count', { skip }, async () => {
  const st = pausedPool()
  await mount({
    st,
    routes: { 'GET /settings': () => ({ ...settingsWithAccounts(FIELDS, st), pool_account_count: 3, per_account_available: true }) }
  })
  const p = await openSettings()
  assert.ok(p.querySelector('[data-rr-scope]'), 'the switch shows: GET /settings says three pool accounts')
})

test('D-030: a global save refreshes the inherited value the Default segment shows', { skip }, async () => {
  const st = perAccount()
  const fields = structuredClone(FIELDS)
  let reads = 0
  await mount({
    st,
    routes: {
      'GET /settings': () => {
        reads += 1
        return settingsWithAccounts(fields, st)
      },
      // The engine re-derives `effective` for every account without an override.
      'POST /settings': ({ key, value }) => {
        fields.find(f => f.key === key).value = value
        for (const a of st.accounts) if (a.overrides[key] === undefined && key in a.effective) a.effective[key] = value
        return { key, value, status: structuredClone(st) }
      }
    }
  })
  const p = await openSettings()
  await h.click(p.querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-toggle]'))
  await h.flush()
  assert.deepEqual(posts('POST /settings').map(c => c.body), [{ key: 'auto_redeem_enabled', value: false }])
  await h.click(pop().querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  const auto = await option(h, 'acct_a', 'auto_redeem_enabled')
  assert.equal(auto.dataset.rrState, 'global')
  assert.equal(auto.dataset.rrInherited, 'off', 'Default now inherits Off')
  assert.equal(segmentsOf(auto)[0].textContent, 'Default (Off)', 'the Default segment reads Off')
  assert.equal((await option(h, 'acct_b', 'auto_redeem_enabled')).dataset.rrState, 'off', 'an override is untouched')
  assert.equal(reads, 2, 'GET /settings was read again after the global save')
})

// ----------------------------------------------------------------- pt-BR

test('D-030/D-034: pt-BR switch, options, segments and tags', { skip }, async () => {
  const st = perAccount()
  st.accounts.push(account('acct_e', 'e***@e***.com', [], overridden({ auto_redeem_enabled: false, reminders_enabled: false })))
  await mount({ st, locale: 'pt-br' })
  let p = await openPopover()
  const tag = key => p.querySelector(`[data-rr-account="${key}"] [data-rr-tag]`).textContent
  assert.deepEqual(['acct_b', 'acct_c', 'acct_d', 'acct_e'].map(tag), ['Auto desligado', 'Antecipado ligado', 'Pausada', 'Personalizada'])
  await h.click(p.querySelector('[data-rr-settings]'))
  await h.flush()
  p = pop()
  assert.deepEqual([...p.querySelectorAll('[data-rr-scope-option]')].map(o => o.textContent), ['Todas as contas', 'Por conta'])
  await h.click(p.querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  p = pop()
  const b = await option(h, 'acct_b', 'auto_redeem_enabled')
  assert.deepEqual(segmentsOf(b).map(x => x.textContent), ['Padrão (Ligado)', 'Ligado', 'Desligado'])
  assert.equal(b.dataset.rrState, 'off')
  assert.equal(b.querySelector('[data-rr-option-label]').textContent, 'Resgate automático (créditos vencendo)')
  assert.equal(row('acct_b').querySelector('[data-rr-account-reset]').textContent, 'Restaurar esta conta')
  assert.equal((await option(h, 'acct_c', 'early_redeem_enabled')).dataset.rrState, 'on')
})

test('D-030: every new string exists in both bundles', { skip }, async () => {
  const { STRINGS } = await loadBundle()
  for (const lang of ['en', 'pt-BR']) {
    const b = STRINGS[lang]
    for (const k of ['all', 'account', 'label']) assert.equal(typeof b.scope[k], 'string', `${lang} scope.${k}`)
    for (const c of OPTIONS) {
      assert.equal(typeof b.perAccount.options[c], 'string', `${lang} option ${c}`)
      assert.ok(b.perAccount.tips[c].length > 30, `${lang} tip ${c}`)
    }
    for (const k of ['autoOff', 'earlyOn', 'paused', 'custom', 'tip']) assert.equal(typeof b.tag[k], 'string', `${lang} tag.${k}`)
    for (const k of ['resetTip', 'empty', 'singleNotice', 'singleReset', 'followsGlobal', 'paused', 'thresholdLead', 'resetAccount']) {
      assert.equal(typeof b.perAccount[k], 'string', `${lang} perAccount.${k}`)
    }
    assert.equal(typeof b.perAccount.segment.default, 'function')
    assert.equal(typeof b.perAccount.customCount, 'function')
  }
})
