// T-27 + T-75 + D-013..D-018: chip states, the banked-only Popover (Desktop
// local time zone), quota line, Redeem now with ConfirmDialog, History view,
// sort control, Scan now (ensures first), empty state, one-line footer,
// Settings link, remaining-time suffix, language setting, profile-keyed polling.
process.env.TZ = 'America/Sao_Paulo' // -03:00, no DST in 2026: makes local times deterministic

import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { after, afterEach, before, test } from 'node:test'

import { account, calm, credit, iso, mixed, NOW, status } from './fixtures.mjs'
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
  if (h) await h.unmount()
  h = null
})

async function mount(st, opts = {}) {
  const { routes = {}, ...rest } = opts
  h = await mountPlugin({ routes: { 'GET /status': typeof st === 'function' ? st : () => st, ...routes }, ...rest })
  await h.flush()
  return h
}

const chip = () => h.q('[data-rr-chip]')
// D-032: Hermes' STATUSBAR_ACTION_CLASS, verbatim from
// /usr/local/lib/hermes-agent/apps/desktop/src/app/shell/statusbar-controls.tsx:30-31,
// the class every native action/menu item (Agents, Memory, ...) carries.
const HERMES_STATUSBAR_ACTION_CLASS =
  'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground disabled:cursor-default disabled:opacity-45'
const NATIVE_TOKENS = HERMES_STATUSBAR_ACTION_CLASS.split(' ')
/** The classes the chip adds on top of the native ones (its state colour). */
const chipStateClasses = () =>
  chip()
    .className.split(' ')
    .filter(c => c && !NATIVE_TOKENS.includes(c))
    .join(' ')
const FONT_PROPS = ['font-size', 'font-weight', 'line-height', 'font-family', 'color', 'font']
const pop = () => h.q('[data-slot="popover-content"]')
const dialog = () => h.q('[data-slot="confirm-dialog"]')
const posts = () => h.calls.filter(c => c.method === 'POST').map(c => c.key)
const order = p => [...p.querySelectorAll('[data-rr-account]')].map(n => n.dataset.rrAccount)
async function openPopover() {
  await h.click(chip())
  await h.flush()
  return pop()
}

// ---------------------------------------------------------------- chip

test('chip: loading while /status is in flight', { skip }, async () => {
  await mount(() => new Promise(() => {}))
  assert.equal(chip().dataset.rrChip, 'loading')
  assert.equal(chip().textContent, 'Reset Rescue')
  // D-034: the tooltip is the SDK Tip (stub: data-tip), never a native title.
  assert.equal(chip().dataset.tip, 'Reset Rescue: loading')
  assert.equal(chip().getAttribute('title'), null, 'no native title')
})

test('chip: unreachable when /status fails', { skip }, async () => {
  await mount(() => Promise.reject(new Error('HTTP 500')))
  assert.equal(chip().dataset.rrChip, 'unreachable')
  assert.match(chip().dataset.tip, /could not load/)
})

test('chip: "Resets: 0" with no banked credit', { skip }, async () => {
  await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: 5, state: 'redeemed' })])] }))
  assert.equal(chip().dataset.rrChip, 'none')
  assert.equal(chip().textContent, 'Resets: 0')
  assert.equal(chip().dataset.tip, 'Reset Rescue: no banked reset credits')
})

test('chip: "Resets: N · Next MMM D" when the soonest rescue is 24 h or more away', { skip }, async () => {
  await mount(calm())
  assert.equal(chip().dataset.rrChip, 'bankedNext')
  // Armed primary 17:48Z Nov 9 = 14:48 local, Nov 9.
  assert.equal(chip().textContent, 'Resets: 2 · Next rescue Nov 9')
  assert.equal(chip().dataset.tip, 'Reset Rescue: 2 banked reset credits, next rescue Mon, Nov 9, 14:48')
  assert.equal(chip().getAttribute('style'), null, 'no emphasis when nothing is urgent')
  assert.equal(chipStateClasses(), '', 'no state colour when nothing is urgent')
})

test('chip: "Resets: N · Next HH:MM" when the rescue is within 24 h but the expiry is not', { skip }, async () => {
  await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: 30, fireIn: 20 })])] }))
  assert.equal(chip().dataset.rrChip, 'bankedNext')
  // 08:00Z Nov 9 = 05:00 local.
  assert.equal(chip().textContent, 'Resets: 1 · Next rescue 05:00')
})

test('chip: warning state when a banked credit expires within 24 h (theme token, text says it)', { skip }, async () => {
  await mount(mixed())
  assert.equal(chip().dataset.rrChip, 'expiring')
  // cr_b1 expires 21:36Z = 18:36 local.
  assert.equal(chip().textContent, 'Resets: 3 · Expires 18:36')
  assert.equal(chip().dataset.tip, 'Reset Rescue: a reset credit expires within 24 hours (Sun, Nov 8, 18:36)')
  // D-032: the host's own warning look (the update item's text-primary,
  // use-statusbar-items.tsx:116), not an inline colour and bold weight.
  assert.equal(chipStateClasses(), 'text-primary hover:text-primary')
  assert.equal(chip().getAttribute('style'), null)
})

test('chip: an already expired credit does not warn', { skip }, async () => {
  await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: -1 }), credit('c2', { expiresIn: 40 })])] }))
  assert.notEqual(chip().dataset.rrChip, 'expiring')
})

test('chip: failed wins over the warning and uses the theme destructive token', { skip }, async () => {
  const st = mixed()
  st.attention.failed = 1
  await mount(st)
  assert.equal(chip().dataset.rrChip, 'failed')
  assert.equal(chip().textContent, 'Reset failed')
  // D-032: the Agents item's failed look (use-statusbar-items.tsx:626-629).
  assert.equal(chipStateClasses(), 'text-destructive hover:text-destructive')
  assert.equal(chip().getAttribute('style'), null)
})

test('chip: a setup problem is "Reset failed"; no account and a busy lock are not', { skip }, async () => {
  await mount(status({ problem: 'cron_unavailable' }))
  assert.equal(chip().dataset.rrChip, 'failed')
  await h.unmount()
  await mount(status({ problem: 'no_account' }))
  assert.equal(chip().dataset.rrChip, 'none')
  await h.unmount()
  await mount(status({ problem: 'busy' }))
  assert.equal(chip().dataset.rrChip, 'none')
})


// Drift guard: when Hermes is installed, its source still has this class.
const HERMES_STATUSBAR_SRC = '/usr/local/lib/hermes-agent/apps/desktop/src/app/shell/statusbar-controls.tsx'
test('D-032: the mirrored STATUSBAR_ACTION_CLASS matches the installed Hermes source', { skip: skip || (!existsSync(HERMES_STATUSBAR_SRC) && 'Hermes source not installed') }, () => {
  const src = readFileSync(HERMES_STATUSBAR_SRC, 'utf8')
  const m = src.match(/const STATUSBAR_ACTION_CLASS =\s*'([^']+)'/)
  assert.ok(m, 'STATUSBAR_ACTION_CLASS found in statusbar-controls.tsx')
  assert.equal(m[1], HERMES_STATUSBAR_ACTION_CLASS)
})

test('D-032: the chip is a native status-bar item button inside PopoverTrigger, not an SDK Button', { skip }, async () => {
  await mount(status())
  assert.equal(chip().tagName, 'BUTTON')
  assert.equal(chip().getAttribute('type'), 'button')
  assert.equal(chip().dataset.popoverTrigger, 'true')
  // The SDK Button's own size/variant classes (text-xs leading-4 font-medium,
  // ghost = --ui-text-secondary) made the chip larger and brighter than its
  // neighbours: it must not be used here.
  absent(chip().getAttribute('data-variant'), 'no SDK Button variant')
  absent(chip().getAttribute('data-size'), 'no SDK Button size')
  // Every Hermes status-bar class, in Hermes' order, first.
  assert.equal(chip().className.split(' ').slice(0, NATIVE_TOKENS.length).join(' '), HERMES_STATUSBAR_ACTION_CLASS)
  // The label sits in the same <span class="truncate"> as a native label
  // (statusbar-controls.tsx:251).
  assert.equal(chip().querySelector(':scope > span.truncate')?.textContent, chip().textContent)
})

test('D-032: the chip carries no inline font size, weight, line height, family or colour in any state', { skip }, async () => {
  const failing = mixed()
  failing.attention.failed = 1
  const cases = [
    ['loading', () => new Promise(() => {})],
    ['unreachable', () => Promise.reject(new Error('HTTP 500'))],
    ['none', status()],
    ['bankedNext', calm()],
    ['expiring', mixed()],
    ['failed', failing]
  ]
  for (const [kind, st] of cases) {
    await mount(st)
    assert.equal(chip().dataset.rrChip, kind)
    for (const el of [chip(), ...chip().querySelectorAll('*')]) {
      for (const prop of FONT_PROPS) {
        assert.equal(el.style.getPropertyValue(prop), '', `${kind}: inline ${prop} on <${el.tagName.toLowerCase()}>`)
      }
    }
    absent(chip().getAttribute('style'), `${kind}: no inline style on the chip`)
    await h.unmount()
    h = null
  }
})

// ---------------------------------------------------------------- popover

test('Popover opens above the chip, aligned to the end, fixed-width, and closes again', { skip }, async () => {
  await mount(mixed())
  absent(pop())
  const p = await openPopover()
  assert.equal(p.dataset.side, 'top')
  assert.equal(p.dataset.align, 'end')
  // D-022 (replaces D-021): a fixed comfortable width; the rows clip instead.
  const style = p.getAttribute('style')
  assert.match(style, /(^|; ?)width: 400px/)
  assert.match(style, /min-width: 380px/)
  assert.match(style, /max-width: 90vw/)
  for (const when of p.querySelectorAll('[data-rr-when]')) assert.match(when.getAttribute('style'), /white-space: nowrap/)
  // D-024 (replaces the 70vh cap): one fixed height, the scroll box fills it.
  // D-029: the height is measured at runtime, so only its px form is pinned here.
  assert.match(p.getAttribute('style'), /(^|; ?)height: \d+px/)
  const scroll = p.querySelector('[data-rr-scroll]')
  assert.match(scroll.getAttribute('style'), /height: 100%/)
  assert.match(scroll.getAttribute('style'), /overflow-y: auto/)
  assert.equal(p.querySelector('h2').textContent, 'Reset Rescue')
  await h.click(chip())
  absent(pop())
})

test('header: Scan now, History, Settings and "Checked HH:MM" in local time', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  const scan = p.querySelector('[data-rr-scan]')
  assert.equal(scan.textContent, 'Check now')
  assert.equal(scan.disabled, false)
  assert.equal(p.querySelector('[data-rr-view]').textContent, 'History')
  assert.equal(p.querySelector('[data-rr-settings]').textContent, 'Settings')
  assert.equal(p.querySelector('[data-rr-checked]').textContent, 'Checked 08:00')
  absent(p.querySelector('[data-rr-banner]'), 'no banner when all is well')
})

test('header: "Checked" carries the date on another day, and "Not checked yet" without a scan', { skip }, async () => {
  await mount(status({ lastScan: iso(NOW - 30 * 3600_000) }))
  let p = await openPopover()
  assert.equal(p.querySelector('[data-rr-checked]').textContent, 'Checked Sat, Nov 7, 03:00')
  await h.unmount()
  await mount(status({ lastScan: null }))
  p = await openPopover()
  assert.equal(p.querySelector('[data-rr-checked]').textContent, 'Not checked yet')
})

test('header: one banner line only on dry run or an auth/setup error', { skip }, async () => {
  await mount(status({ dryRun: true }))
  let p = await openPopover()
  assert.equal(p.querySelectorAll('[data-rr-banner]').length, 1)
  assert.equal(p.querySelector('[data-rr-banner]').dataset.rrBanner, 'dryRun')
  await h.unmount()
  await mount(status({ failed: 2, dryRun: true }))
  p = await openPopover()
  assert.equal(p.querySelectorAll('[data-rr-banner]').length, 1, 'the error wins, still one line')
  assert.equal(p.querySelector('[data-rr-banner]').textContent, 'Could not read 2 accounts. Sign in to them again in Hermes.')
  await h.unmount()
  await mount(status({ problem: 'wrappers_unavailable' }))
  p = await openPopover()
  assert.equal(p.querySelector('[data-rr-banner]').dataset.rrBanner, 'setup')
  assert.ok(!p.textContent.includes('wrappers_unavailable'), 'raw problem codes never reach the UI')
})

test('rows: every account is listed, soonest expiry first, no-reset accounts last', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  // acct_c holds only a redeemed credit: listed last with No resets. acct_b's soonest (9.6 h) beats acct_a (40 h).
  assert.deepEqual(order(p), ['acct_b', 'acct_a', 'acct_c'])
  assert.equal(p.querySelector('[data-rr-account="acct_c"] [data-rr-no-resets]').textContent, 'No resets')
  const b = p.querySelector('[data-rr-account="acct_b"]')
  assert.deepEqual([...b.querySelectorAll('[data-rr-credit]')].map(n => n.dataset.rrCredit), ['cr_b1', 'cr_b2'])
  assert.equal(b.querySelector('[data-rr-count]').textContent, '2 resets')
  assert.equal(p.querySelector('[data-rr-account="acct_a"] [data-rr-count]').textContent, '1 reset')
})

test('D-013: the label exactly as the DTO sends it, masked or full', { skip }, async () => {
  await mount(mixed())
  let p = await openPopover()
  assert.equal(p.querySelector('[data-rr-account="acct_b"] [data-rr-label]').textContent, 'b***@e***.com')
  await h.unmount()
  // mask_labels off: the backend sends the full label and the UI shows it unchanged.
  await mount(status({ accounts: [account('acct_a', 'ana.full@example.com', [credit('c1', { expiresIn: 40 })])] }))
  p = await openPopover()
  assert.equal(p.querySelector('[data-rr-label]').textContent, 'ana.full@example.com')
})

test('rows: "Expires" then the muted "Rescue" line, in the Desktop local time zone', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-credit="cr_b1"] [data-rr-when]').textContent, 'Expires Sun, Nov 8, 18:36 (9 hours)')
  assert.equal(p.querySelector('[data-rr-credit="cr_b1"] [data-rr-rescue-at]').textContent, 'Rescue Sun, Nov 8, 18:30')
  // No slot yet: rescue = expires_at − lead_minutes (10).
  const later = p.querySelector('[data-rr-credit="cr_b2"]')
  assert.equal(later.querySelector('[data-rr-when]').textContent, 'Expires Mon, Nov 9, 21:00 (1 day)')
  assert.equal(later.querySelector('[data-rr-rescue-at]').textContent, 'Rescue Mon, Nov 9, 20:50')
  assert.equal(later.dataset.rrRescue, 'scheduled')
})

test('rows: another zone changes the rendered clock (Intl, no fixed zone)', { skip }, async () => {
  process.env.TZ = 'Asia/Tokyo' // +09:00
  try {
    await mount(mixed())
    const p = await openPopover()
    assert.equal(p.querySelector('[data-rr-credit="cr_b1"] [data-rr-when]').textContent, 'Expires Mon, Nov 9, 06:36 (9 hours)')
    assert.equal(p.querySelector('[data-rr-credit="cr_b1"] [data-rr-rescue-at]').textContent, 'Rescue Mon, Nov 9, 06:30')
    assert.equal(chip().textContent, 'Resets: 3 · Expires 06:36')
  } finally {
    process.env.TZ = 'America/Sao_Paulo'
  }
})

test('rows: a fired primary falls back to the backup; all slots spent shows no rescue', { skip }, async () => {
  const c1 = credit('c1', { expiresIn: 3, fireIn: 2 })
  c1.primary.job_state = 'fired'
  const c2 = credit('c2', { expiresIn: 4, fireIn: 3, jobState: 'removed' })
  await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [c1, c2])] }))
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-credit="c1"] [data-rr-when]').textContent, 'Expires Sun, Nov 8, 12:00 (3 hours)')
  assert.equal(p.querySelector('[data-rr-credit="c1"] [data-rr-rescue-at]').textContent, 'Rescue Sun, Nov 8, 11:06')
  const spent = p.querySelector('[data-rr-credit="c2"]')
  assert.equal(spent.dataset.rrRescue, 'none')
  assert.equal(spent.querySelector('[data-rr-when]').textContent, 'Expires Sun, Nov 8, 13:00 (4 hours)')
  absent(spent.querySelector('[data-rr-rescue-at]'), 'no rescue line when every slot is spent')
})

test('rows: unreachable account asks to sign in again; unknown expiry', { skip }, async () => {
  await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: null })], { reachable: false, last_error_code: 'http_401' })] }))
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-signin]').textContent, 'Sign in again in Hermes to keep this rescue working.')
  assert.equal(p.querySelector('[data-rr-credit="c1"] [data-rr-when]').textContent, 'Expiry unknown')
  assert.ok(!p.textContent.includes('http_401'), 'raw error codes never reach the UI')
})

// ---------------------------------------------------------------- D-017 quota line

test('quota: plan and remaining windows, under the banked resets', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  const b = p.querySelector('[data-rr-account="acct_b"]')
  const quota = b.querySelector('[data-rr-quota]')
  assert.equal(quota.textContent, 'Plus · 5 h: 82% left · Weekly: 40% left')
  // Secondary: after every credit row, smaller than the rows.
  const nodes = [...b.querySelectorAll('[data-rr-credit], [data-rr-quota]')]
  assert.equal(nodes.at(-1), quota)
  assert.match(quota.getAttribute('style'), /font-size: 11px/)
  absent(p.querySelector('[data-rr-account="acct_a"] [data-rr-quota]'), 'no line without a reading')
})

test('quota: only the windows the backend reports; plan as reported; clamped', { skip }, async () => {
  const accounts = [
    account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: 40 })], { quota: { plan_type: 'pro_200', five_hour_remaining_pct: null, weekly_remaining_pct: 104 } }),
    account('acct_b', 'b***@e***.com', [credit('c2', { expiresIn: 50 })], { quota: { plan_type: null, five_hour_remaining_pct: 0, weekly_remaining_pct: null } }),
    account('acct_c', 'c***@e***.com', [credit('c3', { expiresIn: 60 })], { quota: { plan_type: '', five_hour_remaining_pct: null, weekly_remaining_pct: null } })
  ]
  await mount(status({ accounts }))
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-account="acct_a"] [data-rr-quota]').textContent, 'Pro 200 · Weekly: 100% left')
  assert.equal(p.querySelector('[data-rr-account="acct_b"] [data-rr-quota]').textContent, '5 h: 0% left')
  absent(p.querySelector('[data-rr-account="acct_c"] [data-rr-quota]'))
})

test('D-021: no Open Codex usage link in the popup', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  absent(p.querySelector('[data-rr-usage]'))
  assert.ok(!/Open Codex usage|Abrir uso do Codex/.test(p.textContent))
  assert.deepEqual(h.opened, [])
})

// ---------------------------------------------------------------- D-021 remaining time

test('rows: remaining time follows the clock while the popup is open (30 s tick)', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  const when = () => p.querySelector('[data-rr-credit="cr_b1"] [data-rr-when]').textContent
  assert.match(when(), / \(9 hours\)$/)
  const tick = h.tickers.find(t => t.ms === 30_000 && t.live)
  assert.ok(tick, 'a 30 s tick runs while the popup is open')
  try {
    Date.now = () => NOW + 9 * 3600_000 + 30 * 60_000 // 6 min to go
    await h.act(async () => tick.fn())
    assert.match(when(), / \(6 minutes\)$/)
  } finally {
    Date.now = () => NOW
  }
  await h.click(chip())
  await h.flush()
  assert.equal(tick.live, false, 'the tick stops when the popup closes')
})


// ---------------------------------------------------------------- D-018 sort

test('sort: soonest first by default; by account sorts by label and is remembered', { skip }, async () => {
  const storage = new Map()
  await mount(mixed(), { storage })
  let p = await openPopover()
  assert.equal(p.querySelector('[data-rr-sort]').dataset.rrSort, 'soonest')
  assert.equal(p.querySelector('[data-rr-sort-option="soonest"]').getAttribute('aria-pressed'), 'true')
  assert.equal(p.querySelector('[data-rr-sort-option="soonest"]').dataset.variant, 'textStrong')
  assert.deepEqual(order(p), ['acct_b', 'acct_a', 'acct_c'])
  await h.click(p.querySelector('[data-rr-sort-option="account"]'))
  p = pop()
  assert.deepEqual(order(p), ['acct_a', 'acct_b', 'acct_c'], 'a***@ before b***@')
  assert.equal(p.querySelector('[data-rr-sort-option="account"]').getAttribute('aria-pressed'), 'true')
  assert.equal(storage.get('sort'), 'account')
  await h.unmount()
  await mount(mixed(), { storage })
  p = await openPopover()
  assert.deepEqual(order(p), ['acct_a', 'acct_b', 'acct_c'], 'the choice survives a remount')
})

test('sort: hidden with a single account; an unknown stored value falls back', { skip }, async () => {
  await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: 40 })])] }), { storage: new Map([['sort', 'bogus']]) })
  const p = await openPopover()
  absent(p.querySelector('[data-rr-sort]'))
})

// ---------------------------------------------------------------- D-015 redeem now

test('Redeem now: per credit, opens a ConfirmDialog outside the Popover', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  assert.deepEqual([...p.querySelectorAll('[data-rr-redeem]')].map(n => n.dataset.rrRedeem), ['cr_b1', 'cr_b2', 'cr_a1'])
  assert.equal(p.querySelector('[data-rr-redeem]').textContent, 'Redeem now')
  await h.click(p.querySelector('[data-rr-redeem="cr_b1"]'))
  absent(pop(), 'opening the dialog closes the Popover')
  assert.ok(dialog())
  assert.equal(h.q('[data-slot="confirm-title"]').textContent, 'Redeem this reset now?')
  assert.equal(
    h.q('[data-slot="confirm-description"]').textContent,
    'This spends one banked reset on b***@e***.com now and resets its Codex limits. The reset expires Sun, Nov 8, 18:36.'
  )
  assert.deepEqual(h.sdk.confirmCalls.at(-1), { confirmLabel: 'Redeem now', cancelLabel: 'Cancel', busyLabel: 'Redeeming…', destructive: undefined })
  assert.deepEqual(posts(), [], 'opening the dialog sends nothing')
})

test('Redeem now: cancel sends nothing', { skip }, async () => {
  await mount(mixed(), { routes: { 'POST /redeem': () => ({ outcome: 'redeemed' }) } })
  await openPopover()
  await h.click(h.q('[data-rr-redeem="cr_a1"]'))
  await h.click(h.q('[data-slot="confirm-cancel"]'))
  absent(dialog())
  assert.deepEqual(posts(), [])
})

test('Redeem now: confirm posts the credit with confirm: true, toasts and refreshes', { skip }, async () => {
  let reads = 0
  await mount(() => (++reads, mixed()), { routes: { 'POST /redeem': () => ({ credit_key: 'cr_a1', outcome: 'redeemed', reason_code: null, windows_reset: 2 }) } })
  await openPopover()
  const before = reads
  await h.click(h.q('[data-rr-redeem="cr_a1"]'))
  await h.click(h.q('[data-slot="confirm-ok"]'))
  await h.flush()
  const call = h.calls.find(c => c.key === 'POST /redeem')
  assert.deepEqual(call.body, { credit_key: 'cr_a1', confirm: true })
  assert.equal(call.timeoutMs, 45_000)
  absent(dialog())
  assert.deepEqual(h.sdk.notifications, [{ kind: 'success', message: 'Reset redeemed on a***@e***.com.' }])
  assert.ok(reads > before, 'status re-read after the redemption')
})

test('Redeem now: dry run says nothing was spent', { skip }, async () => {
  await mount(mixed(), { routes: { 'POST /redeem': () => ({ outcome: 'dry_run', reason_code: 'dry_run' }) } })
  await openPopover()
  await h.click(h.q('[data-rr-redeem="cr_a1"]'))
  await h.click(h.q('[data-slot="confirm-ok"]'))
  await h.flush()
  assert.deepEqual(h.sdk.notifications, [{ kind: 'info', message: 'Dry run: nothing was spent.' }])
})

test('Redeem now: a failed outcome or a transport error stays in the dialog, no raw detail', { skip }, async () => {
  let mode = 'failed'
  await mount(mixed(), {
    routes: {
      'POST /redeem': () => {
        if (mode === 'failed') return { outcome: 'failed', reason_code: 'confirm_required' }
        if (mode === 'busy') return Promise.reject(Object.assign(new Error('409: {"error":"busy"}'), { statusCode: 409 }))
        return Promise.reject(new Error('500: {"error":"internal_error"}'))
      }
    }
  })
  await openPopover()
  await h.click(h.q('[data-rr-redeem="cr_a1"]'))
  await h.click(h.q('[data-slot="confirm-ok"]'))
  await h.flush()
  assert.equal(h.q('[data-slot="confirm-error"]').textContent, 'The reset was not redeemed. Please try again.')
  mode = 'busy'
  await h.click(h.q('[data-slot="confirm-ok"]'))
  await h.flush()
  assert.equal(h.q('[data-slot="confirm-error"]').textContent, 'A check or rescue is already running. Try again in a minute.')
  mode = 'boom'
  await h.click(h.q('[data-slot="confirm-ok"]'))
  await h.flush()
  assert.ok(!dialog().textContent.includes('internal_error'))
  assert.ok(!dialog().textContent.includes('confirm_required'))
  assert.deepEqual(h.sdk.notifications, [])
})

test('Redeem now: not offered for a pending credit or when the DTO says no', { skip }, async () => {
  const blocked = credit('c2', { expiresIn: 40 })
  blocked.can_redeem_now = false
  await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: 30, state: 'pending' }), blocked])] }))
  const p = await openPopover()
  assert.equal(p.querySelectorAll('[data-rr-credit]').length, 2)
  absent(p.querySelector('[data-rr-redeem]'))
})

// ---------------------------------------------------------------- D-014 history

const HISTORY = {
  items: [
    { at: iso(NOW - 600_000), action: 'redemption', credit_key: 'cr_x', account_key: 'acct_b', outcome: 'redeemed', reason_code: null },
    { at: iso(NOW - 26 * 3600_000), action: 'reminder', credit_key: 'cr_b1', account_key: 'acct_a', outcome: 'sent', reason_code: null },
    { at: iso(NOW - 26.5 * 3600_000), action: 'schedule', credit_key: 'cr_b1', account_key: 'acct_a', outcome: 'scheduled', reason_code: null },
    { at: iso(NOW - 27 * 3600_000), action: 'new_kind', credit_key: null, account_key: 'acct_gone', outcome: 'weird_code', reason_code: 'x' },
    { at: iso(NOW - 28 * 3600_000), action: 'toString', credit_key: null, account_key: null, outcome: 'ok', reason_code: null }
  ]
}

test('History: a short list of recent actions, then Back', { skip }, async () => {
  await mount(mixed(), { routes: { 'GET /history': () => HISTORY } })
  let p = await openPopover()
  await h.click(p.querySelector('[data-rr-view]'))
  await h.flush()
  p = pop()
  const call = h.calls.find(c => c.key === 'GET /history')
  assert.equal(call.path, '/history?limit=20')
  assert.equal(call.timeoutMs, 10_000)
  const items = [...p.querySelectorAll('[data-rr-history-item]')].map(n => n.textContent)
  assert.deepEqual(items, [
    '08:50 · Redemption · b***@e***.com · redeemed',
    'Sat, Nov 7, 07:00 · Reminder · a***@e***.com · sent',
    'Sat, Nov 7, 06:30 · Rescue scheduled · a***@e***.com · scheduled',
    'Sat, Nov 7, 06:00 · Activity',
    'Sat, Nov 7, 05:00 · Activity · done'
  ])
  absent(p.querySelector('[data-rr-account]'), 'the list view is replaced')
  assert.ok(!p.textContent.includes('cr_x') && !p.textContent.includes('weird_code'), 'keys and raw codes never show')
  assert.equal(p.querySelector('[data-rr-view]').textContent, 'Back')
  await h.click(p.querySelector('[data-rr-view]'))
  assert.deepEqual(order(pop()), ['acct_b', 'acct_a', 'acct_c'])
})

test('History: empty and unreachable states; reopening the Popover returns to the list', { skip }, async () => {
  let fail = false
  await mount(mixed(), { routes: { 'GET /history': () => (fail ? Promise.reject(new Error('x')) : { items: [] }) } })
  let p = await openPopover()
  await h.click(p.querySelector('[data-rr-view]'))
  await h.flush()
  assert.equal(pop().querySelector('[data-rr-history-empty]').textContent, 'No activity yet.')
  await h.click(chip()) // close
  p = await openPopover()
  assert.ok(p.querySelector('[data-rr-account]'), 'back on the list after reopening')
  fail = true
  await h.click(p.querySelector('[data-rr-view]'))
  await h.flush()
  assert.equal(pop().querySelector('[data-rr-history-error]').textContent, 'Could not load the history.')
})

// ---------------------------------------------------------------- empty, footer

test('empty state: scanning M accounts every H h', { skip }, async () => {
  // D-027: an empty list means no account is listed at all; the scan count comes from account_count.
  const st = status({ accounts: [] })
  st.account_count = 2
  await mount(st)
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-empty]').textContent, 'No banked Codex resets right now — checking 2 accounts every 12 h.')
  absent(p.querySelector('[data-rr-account]'), 'no account row')
})

test('empty state: account_count from a banked-only DTO, singular', { skip }, async () => {
  const st = status({ settings: { scan_interval_hours: 6 } })
  st.account_count = 1
  await mount(st)
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-empty]').textContent, 'No banked Codex resets right now — checking 1 account every 6 h.')
})

test('empty state: account_count null is "unknown", not "no account"', { skip }, async () => {
  // The engine sends null when it cannot count (dead store/pool). The popup
  // must not turn that into "No Codex account in Hermes".
  const st = status({ settings: { scan_interval_hours: 6 } })
  st.account_count = null
  await mount(st)
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-empty]').textContent, 'No banked Codex resets right now.')
  absent(p.querySelector('[data-rr-banner]'))
})

test('empty state: account_count 0 is the only "no account" signal besides a problem code', { skip }, async () => {
  const st = status()
  st.account_count = 0
  await mount(st)
  assert.equal((await openPopover()).querySelector('[data-rr-empty]').textContent,
    'No Codex account in Hermes — run hermes auth add openai-codex.')
})

test('empty state: no Codex account in Hermes', { skip }, async () => {
  await mount(status({ problem: 'no_account' }))
  let p = await openPopover()
  assert.equal(p.querySelector('[data-rr-empty]').textContent, 'No Codex account in Hermes — run hermes auth add openai-codex.')
  absent(p.querySelector('[data-rr-banner]'))
  await h.unmount()
  const st = status()
  st.account_count = 0
  await mount(st)
  p = await openPopover()
  assert.equal(p.querySelector('[data-rr-empty]').textContent, 'No Codex account in Hermes — run hermes auth add openai-codex.')
})

test('empty state: an empty banked-only list without a count is not "no account"', { skip }, async () => {
  await mount(status())
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-empty]').textContent, 'No banked Codex resets right now.')
})

test('loading and error states', { skip }, async () => {
  await mount(() => new Promise(() => {}))
  let p = await openPopover()
  assert.ok(p.querySelector('[data-rr-loading] [data-slot="spinner"]'))
  absent(p.querySelector('[data-rr-footer]'))
  await h.unmount()
  await mount(() => Promise.reject(new Error('x')))
  p = await openPopover()
  assert.ok(p.querySelector('[data-rr-error]'))
})

test('footer: one line, scans and notice target', { skip }, async () => {
  await mount(mixed())
  const p = await openPopover()
  const footers = p.querySelectorAll('[data-rr-footer]')
  assert.equal(footers.length, 1)
  assert.equal(footers[0].textContent, 'Checks every 12 h · notices → telegram')
})

test('footer: early rescue when enabled, dry run when on', { skip }, async () => {
  await mount(status({ dryRun: true, settings: { early_redeem_enabled: true, early_redeem_remaining_pct: 5, notify_target_display: 'telegram: B***' } }))
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-footer]').textContent, 'Checks every 12 h · notices → telegram: B*** · Early rescue ≤ 5% · Dry run')
  await h.unmount()
  await mount(status({ settings: { early_redeem_enabled: false, early_redeem_remaining_pct: 5 } }))
  assert.ok(!(await openPopover()).querySelector('[data-rr-footer]').textContent.includes('Early'))
})

test('footer: the real SettingsViewDTO keys drive the early-rescue clause', { skip }, async () => {
  // Exactly the keys the engine emits (SettingsViewDTO.FIELDS), nothing else:
  // a rename on either side would silently drop the clause.
  const st = status({
    settings: {
      early_redeem_enabled: true,
      early_redeem_remaining_pct: 0,
      early_redeem_weekly_remaining_pct: 99, // the old name must be ignored
      notify_target_display: ''
    }
  })
  await mount(st)
  assert.equal((await openPopover()).querySelector('[data-rr-footer]').textContent,
    'Checks every 12 h · Early rescue ≤ 0%')
})

test('footer: a threshold that is not a number never renders "≤ undefined%"', { skip }, async () => {
  for (const value of [null, undefined, '5', NaN]) {
    const st = status({ settings: { early_redeem_enabled: true, early_redeem_remaining_pct: value } })
    await mount(st)
    const text = (await openPopover()).querySelector('[data-rr-footer]').textContent
    assert.ok(!text.includes('Early'), `${String(value)} rendered ${text}`)
    await h.unmount()
  }
})

// ---------------------------------------------------------------- Scan now

test('Scan now: POST /scan, disabled with a spinner while busy, then the new status', { skip }, async () => {
  const gate = deferred()
  await mount(status(), { routes: { 'POST /scan': () => gate.promise } })
  let p = await openPopover()
  await h.click(p.querySelector('[data-rr-scan]'))
  await h.flush()
  const scan = h.q('[data-rr-scan]')
  assert.equal(scan.dataset.rrScan, 'busy')
  assert.equal(scan.disabled, true)
  assert.ok(scan.querySelector('[data-slot="spinner"]'))
  assert.equal(chip().dataset.rrChip, 'scanning')
  assert.equal(chip().textContent, 'Checking…')
  const call = h.calls.find(c => c.key === 'POST /scan')
  assert.deepEqual(call.body, {})
  assert.equal(call.timeoutMs, 45_000)
  gate.resolve(mixed())
  await h.flush()
  p = pop()
  assert.equal(h.q('[data-rr-scan]').dataset.rrScan, 'idle')
  assert.equal(chip().textContent, 'Resets: 3 · Expires 18:36')
  assert.deepEqual(order(p), ['acct_b', 'acct_a', 'acct_c'])
})

test('Scan now: a held run lock (409) shows a quiet busy line', { skip }, async () => {
  await mount(mixed(), { routes: { 'POST /scan': () => Promise.reject(Object.assign(new Error('409: {"error":"busy"}'), { statusCode: 409 })) } })
  const p = await openPopover()
  await h.click(p.querySelector('[data-rr-scan]'))
  await h.flush()
  assert.equal(h.q('[data-rr-scan-note]').textContent, 'A check or rescue is already running. Try again in a minute.')
  assert.equal(chip().textContent, 'Resets: 3 · Expires 18:36', 'the last good status stays')
})

test('Scan now: any other failure says so without raw detail', { skip }, async () => {
  await mount(mixed(), { routes: { 'POST /scan': () => Promise.reject(new Error('504: {"error":"timeout"}')) } })
  const p = await openPopover()
  await h.click(p.querySelector('[data-rr-scan]'))
  await h.flush()
  assert.equal(h.q('[data-rr-scan-note]').dataset.rrScanNote, 'failed')
  assert.ok(!pop().textContent.includes('504'))
})

// ---------------------------------------------------------------- first open, settings

test('first open: unarmed jobs with no problem → one POST /scan (which ensures), once per session', { skip }, async () => {
  await mount(status({ armed: false }), { routes: { 'POST /scan': () => mixed() } })
  await h.flush(5)
  assert.deepEqual(posts(), ['POST /scan'])
  assert.equal(chip().textContent, 'Resets: 3 · Expires 18:36')
  await openPopover()
  await h.flush()
  assert.deepEqual(posts(), ['POST /scan'])
})

test('first open: armed, or a known problem → no mutation at all', { skip }, async () => {
  await mount(mixed())
  await openPopover()
  assert.deepEqual(posts(), [])
  await h.unmount()
  await mount(status({ armed: false, problem: 'no_account' }))
  await h.flush()
  assert.deepEqual(posts(), [])
})

const SETTINGS_FIELDS = { fields: [{ key: 'lead_minutes', type: 'int', value: 10, min: 5, max: 240 }] }

test('Settings opens the in-popup tab and never navigates away (D-025)', { skip }, async () => {
  await mount(mixed(), { routes: { 'GET /settings': () => SETTINGS_FIELDS } })
  const p = await openPopover()
  await h.click(p.querySelector('[data-rr-settings]'))
  await h.flush()
  // D-025 replaced the P3 hand-off: the tab saves through the plugin's own
  // route, which Hermes scopes to the profile the popup is polling, so there
  // is no second scope to hand off to and nothing to navigate to.
  assert.deepEqual(h.sdk.navigations, [])
  assert.ok(pop().querySelector('[data-rr-settings-tab]'))
  assert.equal(pop().querySelector('[data-rr-settings]').textContent, 'Back')
})

// ---------------------------------------------------------------- behavior

test('polling: GET /status on mount; 5 min while closed, 1 min while open', { skip }, async () => {
  await mount(mixed())
  assert.equal(h.calls.filter(c => c.key === 'GET /status').length, 1)
  assert.equal(h.calls[0].timeoutMs, 10_000)
  assert.deepEqual(h.intervals, [300_000])
  await openPopover()
  assert.deepEqual(h.intervals, [300_000, 60_000, 30_000])
  assert.equal(h.calls.filter(c => c.key === 'GET /status').length, 2, 'opening refreshes at once')
  assert.deepEqual(posts(), [], 'reading never mutates')
})

test("only the plugin's own backend routes are ever called", { skip }, async () => {
  await mount(status({ armed: false }), {
    routes: {
      'POST /scan': () => mixed(),
      'POST /redeem': () => ({ outcome: 'redeemed' }),
      'GET /history': () => HISTORY,
      'GET /settings': () => SETTINGS_FIELDS
    }
  })
  await h.flush(5)
  let p = await openPopover()
  await h.click(p.querySelector('[data-rr-scan]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-view]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-view]'))
  await h.flush()
  // D-025: the settings tab reads once when it opens.
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-redeem]'))
  await h.click(h.q('[data-slot="confirm-ok"]'))
  await h.flush()
  const used = new Set(h.calls.map(c => c.key))
  // D-025 added the settings pair; nothing outside the plugin's namespace is used.
  assert.deepEqual([...used].sort(), ['GET /history', 'GET /settings', 'GET /status', 'POST /redeem', 'POST /scan'])
})

test('profile change clears the data and drops the stale response', { skip }, async () => {
  const slow = deferred()
  let n = 0
  await mount(() => {
    n += 1
    if (n === 1) return mixed()
    if (n === 2) return slow.promise // the first request for 'work'
    return status()
  })
  assert.equal(chip().dataset.rrChip, 'expiring')
  await h.setProfile('work')
  assert.equal(chip().dataset.rrChip, 'loading', 'old profile data vanishes at once')
  await h.setProfile('default')
  await h.flush()
  assert.equal(chip().dataset.rrChip, 'none')
  slow.resolve(mixed()) // the 'work' request resolves late
  await h.flush()
  assert.equal(chip().dataset.rrChip, 'none', 'stale response ignored')
})

test('no ctx.socket use (the harness socket throws)', { skip }, async () => {
  await mount(mixed())
  await openPopover()
  assert.ok(chip(), 'rendered without touching ctx.socket')
})

// ---------------------------------------------------------------- D-016 language

test('language auto: pt-BR via the Desktop locale id (lowercase pt-br)', { skip }, async () => {
  await mount(calm(), { locale: 'pt-br' })
  assert.equal(chip().textContent, 'Resets: 2 · Próximo resgate 9 de nov.')
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-scan]').textContent, 'Verificar agora')
  assert.match(p.querySelector('[data-rr-credit="cr_b1"] [data-rr-when]').textContent, /^Vence seg\.?, 9 de nov\.?,? 15:00 \(1 dia\)$/)
  assert.match(p.querySelector('[data-rr-credit="cr_b1"] [data-rr-rescue-at]').textContent, /^Resgate seg\.?, 9 de nov\.?,? 14:48$/)
  assert.equal(p.querySelector('[data-rr-footer]').textContent, 'Verifica a cada 12 h · avisos → telegram')
})

test('language setting pt-BR wins over an English Desktop, and en over a pt-br one', { skip }, async () => {
  const pt = mixed()
  pt.language = 'pt-BR'
  await mount(pt, { locale: 'en' })
  assert.equal(chip().textContent, 'Resets: 3 · Vence 18:36')
  let p = await openPopover()
  assert.equal(p.querySelector('[data-rr-view]').textContent, 'Histórico')
  await h.unmount()
  const en = mixed()
  en.language = 'en'
  await mount(en, { locale: 'pt-br' })
  assert.equal(chip().textContent, 'Resets: 3 · Expires 18:36')
  p = await openPopover()
  assert.equal(p.querySelector('[data-rr-scan]').textContent, 'Check now')
})

test('language auto from the real DTO follows a pt-BR Desktop', { skip }, async () => {
  // The engine sends the setting, not a resolved bundle: `auto` is what a
  // default install has, and the Desktop's own locale must win (D-016).
  const st = mixed()
  st.language = 'auto'
  await mount(st, { locale: 'pt-br' })
  assert.equal(chip().textContent, 'Resets: 3 · Vence 18:36')
  const p = await openPopover()
  assert.equal(p.querySelector('[data-rr-scan]').textContent, 'Verificar agora')
  assert.equal(p.querySelector('[data-rr-footer]').textContent, 'Verifica a cada 12 h · avisos → telegram')
})

test('language auto follows an English Desktop too', { skip }, async () => {
  const st = mixed()
  st.language = 'auto'
  await mount(st, { locale: 'en' })
  assert.equal(chip().textContent, 'Resets: 3 · Expires 18:36')
  assert.equal((await openPopover()).querySelector('[data-rr-scan]').textContent, 'Check now')
})

test('an unknown language value falls back to the Desktop, never crashes', { skip }, async () => {
  for (const value of [null, undefined, '', 'de-DE', 'xx']) {
    const st = mixed()
    st.language = value
    await mount(st, { locale: 'pt-br' })
    assert.equal(chip().textContent, 'Resets: 3 · Vence 18:36', String(value))
    await h.unmount()
  }
})

test('every visible string goes through the translator (marker mode)', { skip }, async () => {
  await mount(mixed())
  await h.act(async () => h.sdk.$markerMode.set(true))
  const p = await openPopover()
  let rest = p.textContent + chip().textContent
  rest = rest.replace(/«[^»]+»/g, '')
  // '***' is the fixed label of the hidden measuring sample (D-029), not user text.
  for (const d of ['b***@e***.com', 'a***@e***.com', 'c***@e***.com', '***', 'Plus']) rest = rest.split(d).join('')
  assert.equal(rest.trim(), '', `untranslated text: ${JSON.stringify(rest)}`)
})

test('times arrive as UTC Z strings and fixtures match the DTO shape', () => {
  assert.match(iso(NOW), /^2026-11-08T12:00:00Z$/)
})
