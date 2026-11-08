// D-022 (replaces D-021's one-line width): each reset is a tidy two-line
// block — expiry plus the time-left suffix on the first line, the rescue time
// muted underneath — inside a popup of a fixed, comfortable width. The lines
// never overflow: they clip with an ellipsis instead of widening the popover.
process.env.TZ = 'America/Sao_Paulo' // -03:00, no DST in 2026: deterministic clock

import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'

import { account, credit, iso, NOW, status } from './fixtures.mjs'
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

async function mount(st, opts = {}) {
  h = await mountPlugin({ routes: { 'GET /status': () => st }, ...opts })
  await h.flush()
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  return h.q('[data-slot="popover-content"]')
}

/** Two banked credits on one account: 36 h out (scheduled), and 9.6 h out. */
function twoLines() {
  return status({
    accounts: [
      account('acct_b', 'b***@e***.com', [
        credit('cr_b2', { expiresIn: 36, fireIn: 36 - (10 / 60) }),
        credit('cr_b1', { expiresIn: 9.6, fireIn: 9.5 })
      ])
    ]
  })
}

const when = (p, key = 'cr_b2') => p.querySelector(`[data-rr-credit="${key}"] [data-rr-when]`)
const rescueLine = (p, key = 'cr_b2') => p.querySelector(`[data-rr-credit="${key}"] [data-rr-rescue-at]`)

test('D-022: the popup has a fixed comfortable width, not max-content', { skip }, async () => {
  const p = await mount(twoLines())
  const style = p.getAttribute('style')
  assert.doesNotMatch(style, /max-content/, 'D-021 made the popup as wide as the longest line')
  const width = Number((/(^|; ?)width: (\d+)px/.exec(style) || [])[2])
  assert.ok(width >= 380 && width <= 420, `popup width ${width}px must be 380-420px`)
  assert.match(style, /max-width: 90vw/, 'a narrow window still wins')
})

test('D-022: line 1 is the expiry with the time-left suffix, line 2 the muted rescue time', { skip }, async () => {
  const p = await mount(twoLines())
  assert.equal(when(p).textContent, 'Expires Mon, Nov 9, 21:00 (1 day)')
  assert.equal(rescueLine(p).textContent, 'Rescue Mon, Nov 9, 20:50')
  assert.match(rescueLine(p).getAttribute('style'), /var\(--ui-text-tertiary\)/, 'the rescue line is muted')
  // The two lines are separate elements, not one joined string.
  assert.notEqual(when(p), rescueLine(p))
  assert.equal(when(p).parentElement, rescueLine(p).parentElement, 'both lines belong to the credit block')
})

test('D-022: a credit with no rescue slot still shows one line, without an empty second', { skip }, async () => {
  const p = await mount(
    status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: 4, state: 'redeemed' })])] })
  )
  // A spent credit is not banked: nothing is listed, so nothing renders.
  absent(p.querySelector('[data-rr-credit]'))
})

test('D-022: lines never overflow — nowrap plus an ellipsis, inside a 100% box', { skip }, async () => {
  const p = await mount(twoLines())
  for (const span of [when(p), rescueLine(p)]) {
    const style = span.getAttribute('style')
    assert.match(style, /white-space: nowrap/)
    assert.match(style, /text-overflow: ellipsis/)
    assert.match(style, /overflow: hidden/)
  }
  const block = when(p).parentElement
  assert.match(block.getAttribute('style'), /flex-direction: column/, 'the two lines stack')
  assert.match(block.getAttribute('style'), /min-width: 0/, 'a flex child may shrink, so the ellipsis can fire')
})

test('D-022: pt-BR renders the same two lines', { skip }, async () => {
  const p = await mount(twoLines(), { locale: 'pt-br' })
  assert.match(when(p).textContent, /^Vence seg\., 9 de nov\.,? 21:00 \(1 dia\)$/)
  assert.match(rescueLine(p).textContent, /^Resgate seg\., 9 de nov\.,? 20:50$/)
})

test('D-022: Redeem now still rides on the credit block', { skip }, async () => {
  const p = await mount(twoLines())
  assert.equal(p.querySelector('[data-rr-credit="cr_b2"] [data-rr-redeem]').textContent, 'Redeem now')
})

test('D-022: the Open Codex usage link stays gone', { skip }, async () => {
  const p = await mount(twoLines())
  absent(p.querySelector('[data-rr-usage]'))
  absent(p.querySelector('a[href*="usage"]'))
  assert.doesNotMatch(p.textContent, /Open Codex usage/)
})

test('D-022: an unknown expiry keeps its own line and no suffix', { skip }, async () => {
  const p = await mount(status({ accounts: [account('acct_a', 'a***@e***.com', [credit('c1', { expiresIn: null })])] }))
  assert.equal(when(p, 'c1').textContent, 'Expiry unknown')
  absent(rescueLine(p, 'c1'))
})

test('D-022: a credit whose slots all fired shows the expiry line only', { skip }, async () => {
  const p = await mount(
    status({
      accounts: [
        account('acct_a', 'a***@e***.com', [
          credit('c2', { expiresIn: 1, fireIn: 0.9, jobState: 'fired' })
        ])
      ]
    })
  )
  assert.equal(when(p, 'c2').textContent, 'Expires Sun, Nov 8, 10:00 (1 hour)')
  absent(rescueLine(p, 'c2'))
})

test('D-022: the header clock and the sort control are untouched', { skip }, async () => {
  const p = await mount(twoLines())
  assert.equal(p.querySelector('[data-rr-checked]').textContent, `Checked ${new Intl.DateTimeFormat('en', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(Date.parse(iso(NOW - 3600_000))))}`)
})

// ---------------------------------------------------------------- D-023 auto-redeem off

test('D-023: the popup says automatic redemption is off, without alarming', { skip }, async () => {
  const st = twoLines()
  st.auto_redeem_enabled = false
  st.settings.auto_redeem_enabled = false
  const p = await mount(st)
  const banner = p.querySelector('[data-rr-banner]')
  assert.equal(banner.dataset.rrBanner, 'autoOff')
  assert.equal(banner.textContent, 'Automatic rescue is off. Reminders and Redeem now still work.')
  assert.match(p.querySelector('[data-rr-footer]').textContent, /automatic rescue off/)
})

test('D-023: pt-BR says it too', { skip }, async () => {
  const st = twoLines()
  st.auto_redeem_enabled = false
  st.settings.auto_redeem_enabled = false
  const p = await mount(st, { locale: 'pt-br' })
  assert.match(p.querySelector('[data-rr-banner]').textContent, /^O resgate automático está desligado\./)
  assert.match(p.querySelector('[data-rr-footer]').textContent, /resgate automático desligado/)
})

test('D-023: on by default means no banner and no footer note', { skip }, async () => {
  const p = await mount(twoLines())
  absent(p.querySelector('[data-rr-banner]'))
  assert.doesNotMatch(p.querySelector('[data-rr-footer]').textContent, /automatic rescue off/)
})

test('D-023: Redeem now is still offered while auto-redeem is off', { skip }, async () => {
  const st = twoLines()
  st.auto_redeem_enabled = false
  st.settings.auto_redeem_enabled = false
  const p = await mount(st)
  assert.equal(p.querySelector('[data-rr-credit="cr_b2"] [data-rr-redeem]').textContent, 'Redeem now')
})
