// T-26: the harness loads the real desktop/plugin.js against the stubbed SDK.
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { after, test } from 'node:test'

import { mixed } from './fixtures.mjs'
import { absent, closeDom, harnessAvailability, liveMounts, mountPlugin, REPO } from './harness.mjs'

const avail = harnessAvailability()
after(() => closeDom())

test('plugin registers exactly one status-bar-right contribution and renders it', { skip: avail.skip }, async () => {
  const h = await mountPlugin({ routes: { 'GET /status': new Promise(() => {}) } })
  try {
    assert.equal(h.plugin.id, 'reset-rescue')
    assert.equal(h.registered.length, 1)
    assert.equal(h.chip.area, 'statusBar.right')
    assert.equal(typeof h.chip.render, 'function')
    assert.ok(h.q('[data-rr-chip]'), 'chip rendered')
    assert.ok(h.text().includes('Reset Rescue'))
  } finally {
    await h.unmount()
  }
})

test('plugin ships opt-in and registers its own i18n bundles', { skip: avail.skip }, async () => {
  const h = await mountPlugin({ routes: { 'GET /status': new Promise(() => {}) } })
  try {
    assert.equal(h.plugin.defaultEnabled, false)
    assert.equal(h.ctx.i18n.t('chip.idle'), 'Reset Rescue')
  } finally {
    await h.unmount()
  }
})

// ---------------------------------------------------------------- teardown safety
// A failing assertion must not leave a mounted Popover with live intervals
// re-rendering after the test, and must not format a React element.

let leaked = null
test('teardown: a test that throws after mounting (expected failure)', { skip: avail.skip, expectFailure: true }, async () => {
  leaked = await mountPlugin({ routes: { 'GET /status': () => mixed() } })
  await leaked.flush()
  await leaked.click(leaked.q('[data-rr-chip]'))
  await leaked.flush()
  assert.ok(leaked.activeTimers() > 0, 'the open Popover polls')
  assert.equal(1, 2, 'deliberate failure while mounted')
})

test('teardown: the root afterEach unmounted it and cleared every timer', { skip: avail.skip }, () => {
  assert.ok(leaked, 'the previous test mounted')
  assert.equal(leaked.activeTimers(), 0)
  assert.ok(leaked.tickers.length > 0 && leaked.tickers.every(t => !t.live))
  assert.equal(leaked.container.isConnected, false)
  assert.equal(liveMounts(), 0)
})

test('absent(): a found element fails fast with a short message', { skip: avail.skip }, async () => {
  const h = await mountPlugin({ routes: { 'GET /status': () => mixed() } })
  await h.flush()
  const el = h.q('[data-rr-chip]')
  assert.ok(Object.keys(el).some(k => k.startsWith('__react')), 'React expandos are what blow up inspect()')
  absent(null)
  let message = ''
  try {
    absent(el, 'chip')
  } catch (e) {
    message = e.message
  }
  assert.match(message, /^chip: expected no element, found <button>/)
  assert.ok(message.length < 400, `short message (${message.length} chars)`)
})

test('no desktop test hands a DOM query result to assert.equal/deepEqual (use absent())', () => {
  const dir = join(REPO, 'tests', 'desktop')
  const bad = /assert\.(?:not)?(?:equal|strictEqual|deepEqual|deepStrictEqual)\(\s*(?:(?:pop|chip|dialog)\(\)|[\w.]*(?:querySelector|\bq)\([^()]*\))\s*,/
  const hits = []
  for (const name of readdirSync(dir).filter(n => n.endsWith('.test.mjs'))) {
    readFileSync(join(dir, name), 'utf8')
      .split('\n')
      .forEach((line, i) => {
        if (bad.test(line)) hits.push(`${name}:${i + 1}: ${line.trim()}`)
      })
  }
  assert.deepEqual(hits, [])
})

test('runner: every desktop node --test goes through the bounded scripts/node_test.sh', () => {
  const runner = readFileSync(join(REPO, 'scripts', 'node_test.sh'), 'utf8')
  assert.match(runner, /ulimit -v "\$VMEM_KB"/)
  assert.match(runner, /node --max-old-space-size=1024 --test --test-timeout=60000/)
  assert.match(runner, /timeout --kill-after=10 "\$TIMEOUT_S"/)
  for (const script of ['test.sh', 'check.sh']) {
    const body = readFileSync(join(REPO, 'scripts', script), 'utf8')
    const raw = body.split('\n').filter(l => /\bnode --test\b/.test(l) && !/^\s*(#|echo |banner )/.test(l))
    assert.deepEqual(raw, [], `${script} runs node --test unbounded`)
    assert.match(body, /bash scripts\/node_test\.sh/)
  }
})
