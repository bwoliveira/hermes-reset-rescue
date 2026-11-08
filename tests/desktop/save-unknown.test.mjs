// D-033 (review r7 P1-01, P1-02): an unknown save result is never a failure,
// and a queued settings intent never reaches another Hermes profile.
//
// P1-01: a 504 at 12 s, a POST with no reply until 60 s, or an early 504
// followed by reads that cannot reach Hermes all ended with the row reverted
// to the old value and "Not saved", while the backend wrote the intent a
// moment later. Contract: "Not saved" only for a definite HTTP refusal; an
// unknown result keeps the intent on screen with "Checking…" and keeps reading
// GET /settings (2 s, then every 5 s, wall clock) while the popup is open. After
// 60 s without an answer the row reads "Not confirmed yet: reopen the popup to
// check". A late POST reply is still applied. Reopening the popup reads
// GET /settings and shows the engine.
//
// P1-02: a Default intent queued behind a POST in flight went out after a
// switch to Work, and Work's backend took it. Contract: every queued write
// carries the profile it was clicked in; a write whose profile is no longer
// active is not sent (nor any other queued write of that profile), and a reply
// or a read for an inactive profile changes nothing on screen.
import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'

import { NOW, perAccount, settingsWithAccounts } from './fixtures.mjs'
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
let writer = null
afterEach(async () => {
  mock.timers.reset()
  writer?.resolve()
  if (h) await h.unmount()
  h = null
  writer = null
})

const busy = () => Object.assign(new Error('504: {"error":"busy"}'), { status: 504 })
const offline = () => new Error('network disconnected')
const pop = () => h.q('[data-slot="popover-content"]')
const toggle = (key = 'auto_redeem_enabled') => pop().querySelector(`[data-rr-setting="${key}"] [data-rr-toggle]`)
const pressed = key => toggle(key).getAttribute('aria-pressed')
const note = (key = 'auto_redeem_enabled') =>
  pop().querySelector(`[data-rr-setting="${key}"]`)?.querySelector('[data-rr-saved], [data-rr-error]')?.textContent ?? null
const noNotSaved = msg => assert.ok(!/Not saved|Não salvou/.test(pop().textContent), msg)

async function micro(n = 25) {
  for (let i = 0; i < n; i += 1) await h.act(async () => {})
}
async function tick(ms) {
  await h.act(async () => mock.timers.tick(ms))
  await micro()
}
/** Wall clock and timers move together (the reconcile deadline is Date-based). */
const clock = () => mock.timers.enable({ apis: ['setTimeout', 'Date'], now: NOW })

/**
 * The global Auto toggle against a stateful engine. The POST changes the state
 * only when `writer` resolves (the shielded write that keeps running). While
 * it runs, GET /settings fails with `readError`.
 */
async function setup({ post504 = false, late504 = false, readError = null } = {}) {
  let auto = false
  let writing = false
  writer = deferred()
  const status = () => {
    const s = perAccount()
    s.settings.auto_redeem_enabled = auto
    for (const a of s.accounts) {
      a.overrides = {}
      a.effective.auto_redeem_enabled = auto
    }
    return s
  }
  const payload = () => settingsWithAccounts([{ key: 'auto_redeem_enabled', type: 'bool', value: auto }], status())
  h = await mountPlugin({
    routes: {
      'GET /status': status,
      'GET /settings': () => {
        if (writing && readError) throw readError()
        return payload()
      },
      'POST /settings': async body => {
        writing = true
        const write = writer.promise.then(() => {
          auto = body.value
          writing = false
          return { ...body, status: status() }
        })
        if (post504) {
          void write
          throw busy()
        }
        if (late504) {
          void write
          await new Promise(resolve => setTimeout(resolve, 12_000))
          throw busy()
        }
        return write
      }
    }
  })
  await h.flush()
  await openSettings()
  return { auto: () => auto }
}

async function openSettings() {
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
}

async function reopen() {
  await h.click(h.q('[data-rr-chip]')) // close
  await h.flush()
  await openSettings()
}

// ------------------------------------------------------------- P1-01

test('D-033 r7: a 504 at 12 s with busy reads keeps On / Checking…, then a silent row once the write lands', { skip }, async () => {
  const e = await setup({ late504: true, readError: busy })
  clock()
  await h.click(toggle())
  await tick(10_000)
  assert.equal(note(), 'Checking…')
  await tick(2_000) // the 504 arrives; the reconcile read answers busy
  assert.equal(note(), 'Checking…', 'a 504 is unknown, not a failure')
  assert.equal(pressed(), 'true', 'the intent stays on screen')
  noNotSaved('no Not saved at 12 s')
  await tick(1_000)
  writer.resolve() // the shielded write lands at 13 s
  await micro()
  assert.equal(e.auto(), true)
  await tick(2_000) // the next read answers
  assert.equal(note(), null, 'confirmed: the row goes quiet, no Saved')
  assert.equal(pressed(), 'true')
  noNotSaved('never Not saved')
  mock.timers.reset()
  await reopen()
  assert.equal(note(), null, 'a fresh tab after the reopen')
  assert.equal(pressed(), 'true', 'after a remount the toggle equals the engine')
})

test('D-033 r7: no reply until 60 s, busy reads: Not confirmed yet with the intent kept; the late reply is applied', { skip }, async () => {
  const e = await setup({ readError: busy })
  clock()
  await h.click(toggle())
  await tick(10_000)
  assert.equal(note(), 'Checking…')
  await tick(50_000)
  assert.equal(e.auto(), false, 'the engine has not written yet')
  assert.equal(note(), 'Not confirmed yet: reopen the popup to check')
  assert.equal(pressed(), 'true', 'the old value is not presented as saved')
  noNotSaved('no Not saved at 60 s')
  await tick(1_000)
  writer.resolve() // the write lands at 61 s and the POST finally answers
  await micro()
  await tick(5_000)
  assert.equal(e.auto(), true)
  assert.equal(pressed(), 'true')
  assert.equal(note(), null, 'the late reply is applied; no Saved')
  noNotSaved('never Not saved')
  mock.timers.reset()
  await reopen()
  assert.equal(note(), null, 'a fresh tab after the reopen')
  assert.equal(pressed(), 'true')
})

test('D-033 r7: an early 504 and reads that cannot reach Hermes: never Not saved, a silent row once a read answers', { skip }, async () => {
  const e = await setup({ post504: true, readError: offline })
  clock()
  await h.click(toggle())
  await micro()
  assert.equal(note(), null, 'silent before 3 s')
  await tick(3_000)
  assert.equal(note(), 'Checking…', 'unknown and not settled at 3 s')
  for (let i = 0; i < 14; i += 1) await tick(5_000) // 70 s of failed reads
  assert.equal(note(), 'Not confirmed yet: reopen the popup to check')
  assert.equal(pressed(), 'true')
  noNotSaved('no Not saved after the window')
  writer.resolve()
  await micro()
  await tick(5_000) // reading goes on while the popup is open
  assert.equal(e.auto(), true)
  assert.equal(pressed(), 'true')
  assert.equal(note(), null, 'confirmed: no Saved')
  mock.timers.reset()
  await reopen()
  assert.equal(note(), null, 'a fresh tab after the reopen')
  assert.equal(pressed(), 'true')
})

test('D-033 r7: the 60 s window is wall clock: slow failing reads still end in Not confirmed yet', { skip }, async () => {
  // Each read takes 7.2 s to fail; counting sleeps only kept Checking… past 60 s.
  let auto = false
  writer = deferred()
  const status = () => {
    const s = perAccount()
    s.settings.auto_redeem_enabled = auto
    return s
  }
  h = await mountPlugin({
    routes: {
      'GET /status': status,
      'GET /settings': async () => {
        if (!armed) return settingsWithAccounts([{ key: 'auto_redeem_enabled', type: 'bool', value: auto }], status())
        await new Promise(resolve => setTimeout(resolve, 7_200))
        throw busy()
      },
      'POST /settings': () => {
        throw busy()
      }
    }
  })
  let armed = false
  await h.flush()
  await openSettings()
  armed = true
  clock()
  await h.click(toggle())
  for (let i = 0; i < 7; i += 1) {
    await tick(7_200)
    await tick(2_000)
  }
  assert.equal(note(), 'Not confirmed yet: reopen the popup to check', 'past 60 s of wall clock')
  assert.equal(pressed(), 'true')
})

test('D-033 r7: a definite HTTP 422 is still Not saved at once', { skip }, async () => {
  h = await mountPlugin({
    routes: {
      'GET /status': perAccount,
      'GET /settings': () => settingsWithAccounts([{ key: 'auto_redeem_enabled', type: 'bool', value: false }], perAccount()),
      'POST /settings': () => {
        throw Object.assign(new Error('422: {"error":"invalid_setting","detail":"That value is not allowed."}'), { status: 422 })
      }
    }
  })
  await h.flush()
  await openSettings()
  await h.click(toggle())
  await h.flush()
  assert.equal(note(), 'Not saved: That value is not allowed.')
  assert.equal(pressed(), 'false')
})

test('D-033 r7: pt-BR reads Ainda não confirmado after 60 s without an answer', { skip }, async () => {
  await (async () => {
    writer = deferred()
    const status = () => perAccount()
    h = await mountPlugin({
      locale: 'pt-br',
      routes: {
        'GET /status': status,
        'GET /settings': () => {
          if (armed) throw busy()
          return settingsWithAccounts([{ key: 'auto_redeem_enabled', type: 'bool', value: false }], status())
        },
        'POST /settings': () => writer.promise
      }
    })
  })()
  let armed = false
  await h.flush()
  await openSettings()
  armed = true
  clock()
  await h.click(toggle())
  await tick(10_000)
  assert.equal(note(), 'Confirmando…')
  await tick(50_000)
  assert.equal(note(), 'Ainda não confirmado: reabra o popup para conferir')
  assert.equal(pressed(), 'true')
  noNotSaved('no Não salvou')
})

test('D-033 r7: the new strings exist in both bundles', { skip }, async () => {
  const { STRINGS } = await loadBundle()
  assert.equal(STRINGS.en.settings.unconfirmed, 'Not confirmed yet: reopen the popup to check')
  assert.equal(STRINGS['pt-BR'].settings.unconfirmed, 'Ainda não confirmado: reabra o popup para conferir')
})

// ------------------------------------------------------------- P1-02

test('D-033 r7: a Default intent queued behind a POST is not sent after a switch to Work, and the late Default reply changes nothing', { skip }, async () => {
  const first = deferred()
  const records = []
  const byProfile = {
    default: { auto_redeem_enabled: false, early_redeem_enabled: false },
    work: { auto_redeem_enabled: false, early_redeem_enabled: false }
  }
  // Like the real pluginRest: the profile is read when the request is made.
  const owner = () => h?.sdk.host.state.profile.get() ?? 'default'
  const status = profile => {
    const st = perAccount()
    Object.assign(st.settings, byProfile[profile])
    for (const a of st.accounts) {
      a.overrides = {}
      a.effective = { ...a.effective, ...byProfile[profile] }
    }
    return st
  }
  const settings = profile =>
    settingsWithAccounts(
      Object.entries(byProfile[profile]).map(([key, value]) => ({ key, value, type: 'bool' })),
      status(profile)
    )
  h = await mountPlugin({
    routes: {
      'GET /status': () => status(owner()),
      'GET /settings': () => settings(owner()),
      'POST /settings': async body => {
        const profile = owner()
        records.push({ profile, ...body })
        if (records.length === 1) await first.promise
        byProfile[profile][body.key] = body.value
        return { ...body, status: status(profile) }
      }
    }
  })
  await h.flush()
  await openSettings()
  await h.click(toggle('early_redeem_enabled')) // Default: in flight, held
  await h.click(toggle('auto_redeem_enabled')) // Default: queued
  assert.deepEqual(records, [{ profile: 'default', key: 'early_redeem_enabled', value: true }])
  await h.setProfile('work')
  await h.flush()
  first.resolve() // the Default request answers while Work is active
  await h.flush(8)
  assert.deepEqual(records, [{ profile: 'default', key: 'early_redeem_enabled', value: true }], 'Work receives no POST, Default nothing more')
  assert.equal(byProfile.work.auto_redeem_enabled, false, 'Work unchanged')
  assert.equal(byProfile.work.early_redeem_enabled, false, 'Work unchanged')
  assert.equal(byProfile.default.auto_redeem_enabled, false, 'the queued Default intent was dropped')
  assert.equal(pressed('auto_redeem_enabled'), 'false', 'Work shows its own value')
  assert.equal(pressed('early_redeem_enabled'), 'false', 'the late Default reply is not shown in Work')
  absent(pop().querySelector('[data-rr-setting="early_redeem_enabled"] [data-rr-saved]'), 'no Saved note in Work')
  absent(pop().querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-saved]'), 'no Saving note in Work')
  // Back to Default: GET /settings is the truth, nothing stale.
  await h.setProfile('default')
  await h.flush(5)
  assert.equal(pressed('early_redeem_enabled'), 'true', 'the write that was sent landed in Default')
  assert.equal(pressed('auto_redeem_enabled'), 'false', 'the dropped intent is not shown')
  absent(pop().querySelector('[data-rr-setting="auto_redeem_enabled"] [data-rr-saved]'), 'no stale note')
  assert.equal(records.length, 1)
})

test('D-033 r7: the save queue drops every queued job of a profile that is no longer active, and keys by profile', { skip }, async () => {
  const { createSaveQueue, ownerKey } = await loadBundle()
  // D-033 r8: owners are (connectionId, profile); here one connection.
  const DEFAULT = ownerKey('local', 'default')
  const WORK = ownerKey('local', 'work')
  const name = { [DEFAULT]: 'default', [WORK]: 'work' }
  let active = DEFAULT
  const sent = []
  const gate = deferred()
  const ctx = {
    rest: async (path, opts) => {
      sent.push({ profile: name[active], path, body: opts.body })
      if (sent.length === 1) await gate.promise
      return { ok: true }
    }
  }
  const q = createSaveQueue(ctx, { owner: () => active })
  const outcomes = []
  const job = (owner, target, value) => ({ owner, target, path: '/settings', body: { key: target, value }, onOutcome: o => outcomes.push([name[owner], target, o.kind]) })
  q.submit(job(DEFAULT, 'a', 1)) // in flight
  q.submit(job(DEFAULT, 'b', 2))
  q.submit(job(WORK, 'b', 3)) // same target, other profile: does not replace it
  q.submit(job(DEFAULT, 'c', 4))
  active = WORK
  gate.resolve()
  for (let i = 0; i < 20; i += 1) await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(
    sent.map(s => [s.profile, s.body.key, s.body.value]),
    [
      ['default', 'a', 1],
      ['work', 'b', 3]
    ]
  )
  assert.ok(outcomes.some(([o, t, k]) => o === 'default' && t === 'b' && k === 'dropped'))
  assert.ok(outcomes.some(([o, t, k]) => o === 'default' && t === 'c' && k === 'dropped'))
  assert.ok(!outcomes.some(([, , k]) => k === 'superseded'), 'different profiles never coalesce')
})
