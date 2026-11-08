// D-021: the remaining-time suffix on each reset line. Largest whole unit,
// floored; singular and plural in both bundles.
import assert from 'node:assert/strict'
import { after, test } from 'node:test'

import { closeDom, harnessAvailability, loadBundle } from './harness.mjs'

const avail = harnessAvailability()
const skip = avail.skip
after(() => closeDom())

const MIN = 60_000
const HOUR = 60 * MIN
const DAY = 24 * HOUR

async function render(lang, ms) {
  const { STRINGS, remainingText } = await loadBundle()
  assert.equal(typeof remainingText, 'function', 'plugin.js exports remainingText')
  const t = (key, ...args) => {
    const node = key.split('.').reduce((o, p) => o?.[p], STRINGS[lang])
    return typeof node === 'function' ? node(...args) : node
  }
  return remainingText(ms, t)
}

const CASES = [
  // [ms, en, pt-BR]
  [30 * DAY, '(30 days)', '(30 dias)'],
  [29.9 * DAY, '(29 days)', '(29 dias)'],
  [DAY, '(1 day)', '(1 dia)'],
  [DAY - 1, '(23 hours)', '(23 horas)'],
  [12 * HOUR, '(12 hours)', '(12 horas)'],
  [HOUR, '(1 hour)', '(1 hora)'],
  [HOUR - 1, '(59 minutes)', '(59 minutos)'],
  [30 * MIN, '(30 minutes)', '(30 minutos)'],
  [MIN, '(1 minute)', '(1 minuto)'],
  [MIN - 1, '(less than 1 minute)', '(menos de 1 minuto)'],
  [0, '(less than 1 minute)', '(menos de 1 minuto)'],
  [-5 * MIN, '(less than 1 minute)', '(menos de 1 minuto)']
]

for (const [ms, en, pt] of CASES) {
  test(`remainingText(${ms}) → en ${en} · pt-BR ${pt}`, { skip }, async () => {
    assert.equal(await render('en', ms), en)
    assert.equal(await render('pt-BR', ms), pt)
  })
}

test('remainingText: no suffix without a finite time', { skip }, async () => {
  assert.equal(await render('en', NaN), null)
  assert.equal(await render('en', Infinity), null)
})
