// D-034: Settings > Per account is a list of accounts (accordion), replacing
// the D-030 matrix. One row per account: label, summary (Follows global / N
// custom settings, plus Paused) and a chevron. An expanded account shows four
// options, each an SDK SegmentedControl Default (inherited value) / On / Off;
// one click picks the final value (true / false / null). The early threshold
// shows while early redeem is effectively on. Tooltips are SDK Tips; no native
// `title` anywhere in the popup or on the chip.
import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'

import { SEGMENTS, accountRow, expand, option, optionEl, pick, segmentsOf } from './account-list.mjs'
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
  { key: 'early_redeem_remaining_pct', type: 'int', value: 10, min: 0, max: 100 },
  { key: 'reminders_enabled', type: 'bool', value: true }
]
const OPTIONS = ['auto_redeem_enabled', 'early_redeem_enabled', 'reminders_enabled', 'monitored']

/** A fake engine: keeps the overrides and answers like the API does for per-account replies:
 *  {account_key, overrides, effective, status}. */
function engine(st = perAccount()) {
  const state = structuredClone(st)
  const find = key => state.accounts.find(a => a.account_key === key)
  const reply = key => {
    for (const a of state.accounts) Object.assign(a, overridden(a.overrides))
    const a = find(key)
    return { account_key: key, overrides: { ...a.overrides }, effective: { ...a.effective }, status: structuredClone(state) }
  }
  return {
    state,
    find,
    routes: {
      'GET /status': () => structuredClone(state),
      'GET /settings': () => settingsWithAccounts(FIELDS, state),
      'POST /settings/account': ({ account_key, field, value }) => {
        const a = find(account_key)
        if (value === null) delete a.overrides[field]
        else a.overrides[field] = value
        return reply(account_key)
      },
      'POST /settings/account/reset': ({ account_key }) => {
        find(account_key).overrides = {}
        return reply(account_key)
      },
      'POST /settings/account/reset-all': () => {
        for (const a of state.accounts) a.overrides = {}
        for (const a of state.accounts) Object.assign(a, overridden(a.overrides))
        return { ...settingsWithAccounts(FIELDS, state), status: structuredClone(state) }
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

const rows = () => h.qa('[data-rr-account-row]')
const summary = key => accountRow(h, key).querySelector('[data-rr-summary]').textContent
const expandedKeys = () => rows().filter(r => r.dataset.rrExpanded === 'true').map(r => r.dataset.rrAccountRow)

// ------------------------------------------------------------------ the list

test('D-034: one row per account, masked labels, paused last, pt-BR summaries', { skip }, async () => {
  await mount({ locale: 'pt-br' })
  const p = await openPerAccount()
  absent(p.querySelector('[data-rr-matrix]'), 'the D-030 matrix is gone')
  absent(p.querySelector('[data-rr-cell]'), 'no matrix cells')
  assert.deepEqual(rows().map(r => r.dataset.rrAccountRow), ['acct_a', 'acct_b', 'acct_c', 'acct_d'])
  assert.deepEqual(
    rows().map(r => r.querySelector('[data-rr-row-label]').textContent),
    ['a***@e***.com', 'b***@e***.com', 'c***@e***.com', 'd***@e***.com']
  )
  assert.equal(summary('acct_a'), 'Segue o global')
  assert.equal(summary('acct_b'), '1 ajuste')
  assert.equal(summary('acct_c'), '2 ajustes')
  assert.equal(summary('acct_d'), '1 ajuste · Pausada')
  for (const r of rows()) {
    const toggle = r.querySelector('[data-rr-account-toggle]')
    assert.equal(toggle.tagName, 'BUTTON')
    assert.equal(toggle.getAttribute('aria-expanded'), 'false')
    assert.ok(toggle.querySelector('svg'), 'a chevron')
  }
  assert.deepEqual(expandedKeys(), [], 'every account starts collapsed')
})

test('D-034: English summaries', { skip }, async () => {
  await mount()
  await openPerAccount()
  assert.deepEqual(['acct_a', 'acct_b', 'acct_c', 'acct_d'].map(summary), ['Follows global', '1 custom setting', '2 custom settings', '1 custom setting · Paused'])
})

test('D-034: accordion: one account open at a time; a second click closes it', { skip }, async () => {
  await mount()
  await openPerAccount()
  await expand(h, 'acct_a')
  assert.deepEqual(expandedKeys(), ['acct_a'])
  assert.equal(accountRow(h, 'acct_a').querySelector('[data-rr-account-toggle]').getAttribute('aria-expanded'), 'true')
  assert.ok(accountRow(h, 'acct_a').querySelector('[data-rr-account-detail]'))
  await expand(h, 'acct_c')
  assert.deepEqual(expandedKeys(), ['acct_c'], 'opening another closes the first')
  absent(accountRow(h, 'acct_a').querySelector('[data-rr-account-detail]'), 'acct_a closed')
  await h.click(accountRow(h, 'acct_c').querySelector('[data-rr-account-toggle]'))
  assert.deepEqual(expandedKeys(), [], 'a second click closes it')
})

// --------------------------------------------------------------- the options

test('D-034: four options, each three segments; Default carries the inherited global value (pt-BR)', { skip }, async () => {
  await mount({ locale: 'pt-br' })
  await openPerAccount()
  const row = await expand(h, 'acct_a')
  const options = [...row.querySelectorAll('[data-rr-option]')]
  assert.deepEqual(options.map(o => o.dataset.rrOption), OPTIONS)
  assert.deepEqual(
    options.map(o => o.querySelector('[data-rr-option-label]').textContent),
    ['Resgate automático (créditos vencendo)', 'Resgate antecipado', 'Lembretes', 'Monitorar esta conta']
  )
  for (const o of options) {
    assert.equal(o.getAttribute('role'), 'group')
    assert.equal(segmentsOf(o).length, 3, `${o.dataset.rrOption} has three segments`)
  }
  const labels = field => segmentsOf(optionEl(h, 'acct_a', field)).map(b => b.textContent)
  assert.deepEqual(labels('auto_redeem_enabled'), ['Padrão (Ligado)', 'Ligado', 'Desligado'])
  assert.deepEqual(labels('early_redeem_enabled'), ['Padrão (Desligado)', 'Ligado', 'Desligado'])
  assert.deepEqual(labels('reminders_enabled'), ['Padrão (Ligado)', 'Ligado', 'Desligado'])
  assert.deepEqual(labels('monitored'), ['Padrão (Ligado)', 'Ligado', 'Desligado'])
  // Every option of acct_a follows the global value: Default is the pressed segment.
  for (const o of options) assert.deepEqual(segmentsOf(o).map(b => b.getAttribute('aria-pressed')), ['true', 'false', 'false'])
})

test('D-034: English segment labels and the pressed segment of an own setting', { skip }, async () => {
  await mount()
  await openPerAccount()
  const o = await option(h, 'acct_b', 'auto_redeem_enabled')
  assert.deepEqual(segmentsOf(o).map(b => b.textContent), ['Default (On)', 'On', 'Off'])
  assert.deepEqual(segmentsOf(o).map(b => b.getAttribute('aria-pressed')), ['false', 'false', 'true'])
  assert.equal(o.dataset.rrState, 'off')
})

test('D-034: one click on Off sends exactly one POST {account_key, field, value: false}', { skip }, async () => {
  const e = await mount({ locale: 'pt-br' })
  await openPerAccount()
  await pick(h, 'acct_a', 'reminders_enabled', 'off')
  await h.flush()
  assert.deepEqual(posts('POST /settings/account').map(c => c.body), [{ account_key: 'acct_a', field: 'reminders_enabled', value: false }])
  assert.equal(optionEl(h, 'acct_a', 'reminders_enabled').dataset.rrState, 'off')
  assert.equal(e.find('acct_a').overrides.reminders_enabled, false)
  assert.equal(summary('acct_a'), '1 ajuste')
  absent(accountRow(h, 'acct_a').querySelector('[data-rr-saved], [data-rr-error]'), 'no Salvando…/Salvo text')
  // A click on the segment already pressed sends nothing.
  await pick(h, 'acct_a', 'reminders_enabled', 'off')
  await h.flush()
  assert.equal(posts('POST /settings/account').length, 1)
})

test('D-034: a click on Default sends value: null and the option follows the global value again', { skip }, async () => {
  const e = await mount()
  await openPerAccount()
  await pick(h, 'acct_b', 'auto_redeem_enabled', 'default')
  await h.flush()
  assert.deepEqual(posts('POST /settings/account').map(c => c.body), [{ account_key: 'acct_b', field: 'auto_redeem_enabled', value: null }])
  assert.equal(e.find('acct_b').overrides.auto_redeem_enabled, undefined)
  assert.equal(optionEl(h, 'acct_b', 'auto_redeem_enabled').dataset.rrState, 'global')
  assert.equal(summary('acct_b'), 'Follows global')
})

test('D-034: On picks true in one click; no cycling through other values', { skip }, async () => {
  await mount()
  await openPerAccount()
  await pick(h, 'acct_b', 'auto_redeem_enabled', 'on')
  await h.flush()
  assert.deepEqual(posts('POST /settings/account').map(c => c.body.value), [true])
})

test('D-034: an inherited option reads muted, an own setting does not', { skip }, async () => {
  await mount()
  await openPerAccount()
  const row = await expand(h, 'acct_b')
  const label = field => row.querySelector(`[data-rr-option="${field}"] [data-rr-option-label]`)
  assert.equal(label('reminders_enabled').dataset.rrOptionLabel, 'inherited')
  assert.match(label('reminders_enabled').getAttribute('style'), /var\(--ui-text-tertiary\)/)
  assert.equal(label('auto_redeem_enabled').dataset.rrOptionLabel, 'own')
  assert.match(label('auto_redeem_enabled').getAttribute('style'), /var\(--ui-text-primary\)/)
  assert.doesNotMatch(label('auto_redeem_enabled').getAttribute('style'), /tertiary/)
})

// ------------------------------------------------------------ early threshold

test('D-034: the early % input shows only while early redeem is effectively on', { skip }, async () => {
  const { SAVE_DEBOUNCE_MS } = await loadBundle()
  assert.equal(SAVE_DEBOUNCE_MS, 600)
  await mount({ locale: 'pt-br' })
  await openPerAccount()
  let row = await expand(h, 'acct_a')
  absent(row.querySelector('[data-rr-threshold]'), 'early off (inherited): no input')
  // acct_c has early on and its own 20%.
  row = await expand(h, 'acct_c')
  const own = row.querySelector('[data-rr-threshold]')
  assert.ok(own, 'early on: the input shows')
  assert.equal(own.textContent.replace(/\s+/g, ' ').trim(), 'quando a cota semanal restante ≤%')
  assert.equal(own.querySelector('input[type="number"]').value, '20')
  // Turning early on for acct_a shows the input empty, the global 10 as placeholder.
  await pick(h, 'acct_a', 'early_redeem_enabled', 'on')
  await h.flush()
  row = accountRow(h, 'acct_a')
  const input = row.querySelector('[data-rr-threshold] input[type="number"]')
  assert.ok(input, 'the input appears with early on')
  assert.equal(input.value, '')
  assert.equal(input.getAttribute('placeholder'), '10')
  await h.type(input, '25')
  await h.flush()
  assert.equal(posts('POST /settings/account').length, 1, 'not saved on the keystroke')
  await h.waitFor(() => posts('POST /settings/account').length === 2, 'the debounced save', 3000)
  assert.deepEqual(posts('POST /settings/account')[1].body, { account_key: 'acct_a', field: 'early_redeem_remaining_pct', value: 25 })
  // Emptying it goes back to Default (null).
  await h.type(accountRow(h, 'acct_a').querySelector('[data-rr-threshold] input'), '')
  await h.waitFor(() => posts('POST /settings/account').length === 3, 'the debounced clear', 3000)
  assert.deepEqual(posts('POST /settings/account')[2].body, { account_key: 'acct_a', field: 'early_redeem_remaining_pct', value: null })
})

// -------------------------------------------------------------------- resets

test('D-034: Reset this account calls /settings/account/reset and the row follows global', { skip }, async () => {
  await mount({ locale: 'pt-br' })
  await openPerAccount()
  let row = await expand(h, 'acct_a')
  assert.equal(row.querySelector('[data-rr-account-reset]').disabled, true, 'nothing to reset without overrides')
  row = await expand(h, 'acct_c')
  const reset = row.querySelector('[data-rr-account-reset]')
  assert.equal(reset.textContent, 'Restaurar esta conta')
  assert.equal(reset.tagName, 'BUTTON')
  await h.click(reset)
  await h.flush()
  assert.deepEqual(posts('POST /settings/account/reset').map(c => c.body), [{ account_key: 'acct_c' }])
  assert.equal(summary('acct_c'), 'Segue o global')
  for (const o of accountRow(h, 'acct_c').querySelectorAll('[data-rr-option]')) assert.equal(o.dataset.rrState, 'global', o.dataset.rrOption)
})

test('D-034: Reset all accounts to global is still two clicks', { skip }, async () => {
  await mount({ locale: 'pt-br' })
  const p = await openPerAccount()
  const link = () => pop().querySelector('[data-rr-reset-all]')
  assert.ok(p.querySelector('[data-rr-account-list] [data-rr-reset-footer]'), 'the footer of the tab')
  await h.click(link())
  await h.flush()
  assert.equal(posts('POST /settings/account/reset-all').length, 0, 'the first click only arms it')
  assert.equal(link().dataset.rrConfirming, 'true')
  await h.click(link())
  await h.flush()
  assert.equal(posts('POST /settings/account/reset-all').length, 1)
  for (const key of ['acct_a', 'acct_b', 'acct_c', 'acct_d']) assert.equal(summary(key), 'Segue o global', key)
})

test('D-034: Default on Watch shows the Excluded-accounts value, not the account\'s own Off', { skip }, async () => {
  const fields = [...FIELDS, { key: 'excluded_accounts', type: 'list', value: [] }]
  const e = engine()
  h = await mountPlugin({ routes: { ...e.routes, 'GET /settings': () => settingsWithAccounts(fields, e.state) } })
  await h.flush()
  await openPerAccount()
  const o = await option(h, 'acct_d', 'monitored')
  assert.equal(o.dataset.rrState, 'off')
  assert.deepEqual(segmentsOf(o).map(b => b.textContent), ['Default (On)', 'On', 'Off'], 'not excluded: Default is On')
  await pick(h, 'acct_d', 'monitored', 'default')
  await h.flush()
  assert.equal(summary('acct_d'), 'Follows global', 'no longer paused')
})

// ---------------------------------------------------------------- unchanged

test('D-034: the single-account rule is unchanged: no switch and no list for one account', { skip }, async () => {
  const st = status({ accounts: [account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40, fireIn: 39.8 })], overridden({ auto_redeem_enabled: false }))] })
  await mount({ st })
  await openPopover()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  absent(pop().querySelector('[data-rr-scope]'), 'no switch')
  absent(pop().querySelector('[data-rr-account-list]'), 'no list')
  assert.ok(pop().querySelector('[data-rr-single-notice]'), 'the notice')
})

test('D-034: a tag on the Accounts tab opens the list with that account expanded', { skip }, async () => {
  await mount()
  const p = await openPopover()
  await h.click(p.querySelector('[data-rr-account="acct_c"] [data-rr-tag]'))
  await h.flush()
  assert.ok(pop().querySelector('[data-rr-account-list]'))
  assert.deepEqual(expandedKeys(), ['acct_c'])
})

// ------------------------------------------------------------------ tooltips

test('D-034: no element in the popup or on the chip carries a native title', { skip }, async () => {
  const st = perAccount()
  await mount({ st })
  const titled = () => h.qa('[title]').map(el => `${el.tagName.toLowerCase()} ${el.getAttribute('title')}`)
  assert.deepEqual(titled(), [], 'the chip')
  await openPopover()
  assert.deepEqual(titled(), [], 'Accounts tab (labels and tags)')
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-reset-defaults]'))
  assert.deepEqual(titled(), [], 'Global tab, reset armed')
  await h.click(pop().querySelector('[data-rr-scope-option="account"]'))
  await h.flush()
  for (const key of ['acct_a', 'acct_b', 'acct_c', 'acct_d']) {
    await expand(h, key)
    assert.deepEqual(titled(), [], `Per account, ${key} expanded`)
  }
  await h.click(pop().querySelector('[data-rr-reset-all]'))
  assert.deepEqual(titled(), [], 'reset-all armed')
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-view="history"]'))
  await h.flush()
  assert.deepEqual(titled(), [], 'History')
})

test('D-034: each option label carries its one-sentence description in an SDK Tip', { skip }, async () => {
  const { STRINGS } = await loadBundle()
  await mount()
  await openPerAccount()
  const row = await expand(h, 'acct_c')
  for (const field of OPTIONS) {
    const label = row.querySelector(`[data-rr-option="${field}"] [data-rr-option-label]`)
    assert.equal(label.dataset.slot, 'tooltip-trigger', `${field} is a Tip trigger`)
    assert.equal(label.dataset.tip, STRINGS.en.perAccount.tips[field])
  }
  assert.equal(row.querySelector('[data-rr-account-reset]').dataset.tip, STRINGS.en.perAccount.resetTip)
  assert.match(h.q('[data-rr-chip]').dataset.tip, /^Reset Rescue: /, 'the chip tooltip is an SDK Tip too')
})

// --------------------------------------------------------------- SDK pieces

test('D-034: SegmentedControl and Tip come from the SDK; the fallback keeps the same semantics', { skip }, async () => {
  const b = await loadBundle()
  assert.deepEqual(b.SDK_UI, { segmented: true, tip: true })
  const { createRoot, act, jsx, SegmentedFallback } = b
  const picked = []
  const box = document.createElement('div')
  document.body.appendChild(box)
  const root = createRoot(box)
  const options = SEGMENTS.map(id => ({ id, label: id }))
  await act(async () => root.render(jsx(SegmentedFallback, { options, value: 'on', onChange: id => picked.push(id) })))
  const buttons = [...box.querySelectorAll('button')]
  assert.deepEqual(buttons.map(x => x.textContent), ['default', 'on', 'off'])
  assert.deepEqual(buttons.map(x => x.getAttribute('aria-pressed')), ['false', 'true', 'false'])
  await act(async () => buttons[2].dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
  assert.deepEqual(picked, ['off'])
  await act(async () => root.unmount())
  box.remove()
})

// ------------------------------------------------------------------ strings

test('D-034: the new strings, verbatim, in both bundles', { skip }, async () => {
  const { STRINGS } = await loadBundle()
  const en = STRINGS.en.perAccount
  const pt = STRINGS['pt-BR'].perAccount
  assert.deepEqual(en.options, { auto_redeem_enabled: 'Auto-redeem expiring credits', early_redeem_enabled: 'Early redeem', reminders_enabled: 'Reminders', monitored: 'Watch this account' })
  assert.deepEqual(pt.options, { auto_redeem_enabled: 'Resgate automático (créditos vencendo)', early_redeem_enabled: 'Resgate antecipado', reminders_enabled: 'Lembretes', monitored: 'Monitorar esta conta' })
  assert.equal(en.segment.default('On'), 'Default (On)')
  assert.equal(pt.segment.default('Desligado'), 'Padrão (Desligado)')
  assert.deepEqual([en.segment.on, en.segment.off, pt.segment.on, pt.segment.off], ['On', 'Off', 'Ligado', 'Desligado'])
  assert.deepEqual([en.followsGlobal, pt.followsGlobal], ['Follows global', 'Segue o global'])
  assert.deepEqual([en.customCount(1), en.customCount(3), pt.customCount(1), pt.customCount(3)], ['1 custom setting', '3 custom settings', '1 ajuste', '3 ajustes'])
  assert.deepEqual([en.paused, pt.paused], ['Paused', 'Pausada'])
  assert.deepEqual([en.thresholdLead, pt.thresholdLead], ['when weekly quota left ≤', 'quando a cota semanal restante ≤'])
  assert.deepEqual([en.resetAccount, pt.resetAccount], ['Reset this account', 'Restaurar esta conta'])
  for (const gone of ['hint', 'account', 'columns', 'on', 'off', 'state', 'cellLabel', 'reset', 'expandTip', 'threshold', 'thresholdHelp', 'globalValue', 'custom']) {
    assert.equal(en[gone], undefined, `matrix-only en perAccount.${gone} removed`)
    assert.equal(pt[gone], undefined, `matrix-only pt-BR perAccount.${gone} removed`)
  }
})
