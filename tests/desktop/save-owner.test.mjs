// D-033 (review r8 P1): the owner of a settings write is the full
// (connectionId, profile) pair, not the profile name.
//
// Two registry sources can both run a profile called `default`. Switching
// from A/default to B/default changes host.state.connectionId and leaves
// host.state.profile at 'default'. With a name-only owner, a write clicked in
// A and queued behind a POST in flight went out after the switch and the host
// routed it to B (pluginRest reads the scope when the request is made): B's
// Auto turned On without a click there. Contract: a queued write whose full
// owner is no longer active is not sent, a late reply or read for A changes
// nothing in B, and the Settings tab remounts on a connection change alone.
// On a host without host.state.connectionId the owner cannot be compared, so
// a write that has to wait in the queue is never sent.
import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'

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
let gate = null
afterEach(async () => {
  gate?.resolve()
  if (h) await h.unmount()
  h = null
  gate = null
})

const pop = () => h.q('[data-slot="popover-content"]')
const toggle = key => pop().querySelector(`[data-rr-setting="${key}"] [data-rr-toggle]`)
const pressed = key => toggle(key).getAttribute('aria-pressed')
const note = key => pop().querySelector(`[data-rr-setting="${key}"]`)?.querySelector('[data-rr-saved], [data-rr-error]')?.textContent ?? null

async function openSettings() {
  await h.click(h.q('[data-rr-chip]'))
  await h.flush()
  await h.click(pop().querySelector('[data-rr-settings]'))
  await h.flush()
}

/**
 * Two sources, both on profile `default`, both Auto off. Like the real
 * pluginRest, every request goes to the source active when it is made. The
 * first write is held until `gate` resolves.
 */
async function twoSources() {
  gate = deferred()
  const states = {
    'source-a': { auto_redeem_enabled: false, early_redeem_enabled: false },
    'source-b': { auto_redeem_enabled: false, early_redeem_enabled: false }
  }
  const writes = []
  const reads = []
  const where = () => `${h.sdk.host.state.connectionId.get()}/${h.sdk.host.state.profile.get()}`
  const source = () => h.sdk.host.state.connectionId.get()
  const status = c => {
    const s = perAccount()
    Object.assign(s.settings, states[c])
    for (const a of s.accounts) {
      a.overrides = {}
      a.effective = { ...a.effective, ...states[c] }
    }
    return s
  }
  const payload = c =>
    settingsWithAccounts(
      Object.entries(states[c]).map(([key, value]) => ({ key, value, type: 'bool' })),
      status(c)
    )
  const write = async (path, body, apply) => {
    const c = source()
    writes.push({ to: where(), path, body })
    if (writes.length === 1) await gate.promise
    apply(c)
    return c
  }
  h = await mountPlugin({
    connection: 'source-a',
    routes: {
      'GET /status': () => status(source()),
      'GET /settings': () => {
        reads.push(where())
        return payload(source())
      },
      'POST /settings': async body => {
        const c = await write('/settings', body, c => {
          states[c][body.key] = body.value
        })
        return { ...body, status: status(c) }
      },
      'POST /settings/reset': async body => {
        const c = await write('/settings/reset', body, c => {
          Object.assign(states[c], { auto_redeem_enabled: true, early_redeem_enabled: false })
        })
        return payload(c)
      }
    }
  })
  await h.flush()
  await openSettings()
  return { states, writes, reads }
}

const FIRST = { to: 'source-a/default', path: '/settings', body: { key: 'early_redeem_enabled', value: true } }

for (const operation of ['Auto=On', 'Restore defaults']) {
  test(`D-033 r8: ${operation} queued on A/default is not sent after a switch to B/default`, { skip }, async () => {
    const { states, writes, reads } = await twoSources()
    await h.click(toggle('early_redeem_enabled')) // A: in flight, held
    if (operation === 'Auto=On') await h.click(toggle('auto_redeem_enabled'))
    else {
      await h.click(pop().querySelector('[data-rr-reset-defaults]'))
      await h.click(pop().querySelector('[data-rr-reset-defaults]'))
    }
    assert.deepEqual(writes, [FIRST], 'the second write is queued')
    await h.setOwner('source-b') // same profile name, other source
    assert.equal(h.sdk.host.state.profile.get(), 'default')
    await h.flush()
    gate.resolve() // A's request answers while B is active
    await h.flush(10)
    assert.deepEqual(writes, [FIRST], 'B receives no POST')
    assert.deepEqual(states['source-b'], { auto_redeem_enabled: false, early_redeem_enabled: false }, 'B unchanged')
    assert.equal(states['source-a'].auto_redeem_enabled, false, 'the queued A intent was dropped')
    assert.ok(reads.includes('source-b/default'), 'the tab re-read GET /settings from B')
    assert.equal(pressed('auto_redeem_enabled'), 'false', 'B shows its own Auto')
    assert.equal(pressed('early_redeem_enabled'), 'false', "A's reply is not shown in B")
    absent(pop().querySelector('[data-rr-setting] [data-rr-saved]'), 'no Saved/Saving note in B')
    absent(pop().querySelector('[data-rr-reset-note] [data-rr-saved]'), 'no reset note in B')
  })
}

test("D-033 r8: a late reply from A/default changes nothing on B/default's screen", { skip }, async () => {
  const { states, writes } = await twoSources()
  await h.click(toggle('auto_redeem_enabled')) // A: in flight, held
  assert.deepEqual(writes, [{ to: 'source-a/default', path: '/settings', body: { key: 'auto_redeem_enabled', value: true } }])
  await h.setOwner('source-b')
  await h.flush()
  assert.equal(pressed('auto_redeem_enabled'), 'false', 'B shows its own value, not the A intent')
  gate.resolve()
  await h.flush(10)
  assert.equal(writes.length, 1, 'B receives no POST')
  assert.equal(states['source-a'].auto_redeem_enabled, true, 'the A write landed in A')
  assert.equal(states['source-b'].auto_redeem_enabled, false, 'B unchanged')
  assert.equal(pressed('auto_redeem_enabled'), 'false', "the late A reply is not applied in B's tab")
  assert.equal(note('auto_redeem_enabled'), null, 'no Saved note in B')
  // Back to A: GET /settings is the truth.
  await h.setOwner('source-a')
  await h.flush(5)
  assert.equal(pressed('auto_redeem_enabled'), 'true', 'A shows the write that landed')
  assert.equal(writes.length, 1)
})

test('D-033 r8: a click on B/default right after the switch is sent to B, A queued intents are not', { skip }, async () => {
  const { states, writes } = await twoSources()
  await h.click(toggle('early_redeem_enabled')) // A: held
  await h.click(toggle('auto_redeem_enabled')) // A: queued
  await h.setOwner('source-b')
  await h.flush()
  await h.click(toggle('auto_redeem_enabled')) // B: explicit click
  gate.resolve()
  await h.flush(10)
  assert.deepEqual(writes, [FIRST, { to: 'source-b/default', path: '/settings', body: { key: 'auto_redeem_enabled', value: true } }])
  assert.equal(states['source-a'].auto_redeem_enabled, false)
  assert.equal(states['source-b'].auto_redeem_enabled, true)
  assert.equal(pressed('auto_redeem_enabled'), 'true')
  assert.equal(note('auto_redeem_enabled'), null, 'no Saved')
})

test('D-033 r8: without host.state.connectionId a write that must wait in the queue is never sent', { skip }, async () => {
  const { sdk } = await loadBundle()
  const atom = sdk.host.state.connectionId
  delete sdk.host.state.connectionId // an older host: no connection atom
  try {
    gate = deferred()
    const state = { auto_redeem_enabled: false, early_redeem_enabled: false }
    const writes = []
    const status = () => {
      const s = perAccount()
      Object.assign(s.settings, state)
      for (const a of s.accounts) {
        a.overrides = {}
        a.effective = { ...a.effective, ...state }
      }
      return s
    }
    h = await mountPlugin({
      routes: {
        'GET /status': status,
        'GET /settings': () => settingsWithAccounts(Object.entries(state).map(([key, value]) => ({ key, value, type: 'bool' })), status()),
        'POST /settings': async body => {
          writes.push(body)
          if (writes.length === 1) await gate.promise
          state[body.key] = body.value
          return { ...body, status: status() }
        }
      }
    })
    await h.flush()
    await openSettings()
    await h.click(toggle('early_redeem_enabled')) // nothing ahead: sent at click time
    await h.click(toggle('auto_redeem_enabled')) // queued: its owner cannot be compared
    gate.resolve()
    await h.flush(10)
    assert.deepEqual(writes, [{ key: 'early_redeem_enabled', value: true }], 'the queued write is not sent')
    assert.equal(state.auto_redeem_enabled, false)
    assert.equal(pressed('early_redeem_enabled'), 'true')
    assert.equal(note('early_redeem_enabled'), null, 'no Saved')
    assert.equal(pressed('auto_redeem_enabled'), 'false', 'the unsent intent is not shown')
    assert.equal(note('auto_redeem_enabled'), null, 'no Saving… left behind')
  } finally {
    sdk.host.state.connectionId = atom
  }
})

test('D-033 r8: the save queue keys and drops by the full owner, and never sends a queued job with an unknown owner', { skip }, async () => {
  const { createSaveQueue, ownerKey } = await loadBundle()
  assert.equal(typeof ownerKey, 'function', 'ownerKey is exported')
  const A = ownerKey('source-a', 'default')
  const B = ownerKey('source-b', 'default')
  assert.notEqual(A, B, 'same profile name, different sources: different owners')
  let active = A
  const sent = []
  const hold = deferred()
  const ctx = {
    rest: async (path, opts) => {
      sent.push([active, opts.body.key, opts.body.value])
      if (sent.length === 1) await hold.promise
      return { ok: true }
    }
  }
  const q = createSaveQueue(ctx, { owner: () => active })
  const outcomes = []
  const job = (owner, target, value) => ({ owner, target, path: '/settings', body: { key: target, value }, onOutcome: o => outcomes.push([owner, target, o.kind]) })
  q.submit(job(A, 'a', 1)) // in flight
  q.submit(job(A, 'b', 2))
  q.submit(job(B, 'b', 3)) // same target and profile name, other source: does not replace it
  q.submit(job(A, 'c', 4))
  active = B
  hold.resolve()
  for (let i = 0; i < 20; i += 1) await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(sent, [
    [A, 'a', 1],
    [B, 'b', 3]
  ])
  assert.ok(outcomes.some(([o, t, k]) => o === A && t === 'b' && k === 'dropped'))
  assert.ok(outcomes.some(([o, t, k]) => o === A && t === 'c' && k === 'dropped'))
  assert.ok(!outcomes.some(([, , k]) => k === 'superseded'), 'different sources never coalesce')

  // An owner that cannot be compared (profile only: the host has no
  // connection atom): sent when nothing is ahead of it, dropped when queued.
  const unknown = 'default'
  active = unknown
  sent.length = 0
  outcomes.length = 0
  const hold2 = deferred()
  const ctx2 = {
    rest: async (path, opts) => {
      sent.push([active, opts.body.key, opts.body.value])
      if (sent.length === 1) await hold2.promise
      return { ok: true }
    }
  }
  const q2 = createSaveQueue(ctx2, { owner: () => active })
  q2.submit(job(unknown, 'a', 1)) // nothing ahead: sent at click time
  q2.submit(job(unknown, 'b', 2)) // queued: its owner cannot be compared
  hold2.resolve()
  for (let i = 0; i < 20; i += 1) await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(sent, [[unknown, 'a', 1]], 'the queued job with an unknown owner is not sent')
  assert.ok(outcomes.some(([, t, k]) => t === 'b' && k === 'dropped'))
})
