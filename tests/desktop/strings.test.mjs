// T-27a: UI string bundles. en is the source; pt-BR has the same keys and the
// same interpolator arity; plugin.js carries no user-visible literal outside them.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { after, test } from 'node:test'

import { closeDom, harnessAvailability, loadBundle, PLUGIN_PATH } from './harness.mjs'
import { stripComments } from './static-checks.mjs'

const avail = harnessAvailability()
const SRC = readFileSync(PLUGIN_PATH, 'utf8')
after(() => closeDom())

function leaves(tree, prefix = '') {
  const out = {}
  for (const [k, v] of Object.entries(tree)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') Object.assign(out, leaves(v, key))
    else out[key] = typeof v === 'function' ? `fn/${v.length}` : typeof v
  }
  return out
}

test('en and pt-BR bundles have identical keys and arities', { skip: avail.skip }, async () => {
  const { STRINGS } = await loadBundle()
  assert.deepEqual(Object.keys(STRINGS).sort(), ['en', 'pt-BR'])
  assert.deepEqual(leaves(STRINGS['pt-BR']), leaves(STRINGS.en))
})

test('every bundle leaf is a non-empty string or an interpolator returning one', { skip: avail.skip }, async () => {
  const { STRINGS } = await loadBundle()
  for (const lang of ['en', 'pt-BR']) {
    for (const [key, kind] of Object.entries(leaves(STRINGS[lang]))) {
      const v = key.split('.').reduce((o, p) => o[p], STRINGS[lang])
      const s = typeof v === 'function' ? v(...Array.from({ length: v.length }, (_, i) => `<${i}>`)) : v
      assert.equal(typeof s, 'string', `${lang}:${key} (${kind})`)
      assert.ok(s.trim().length > 0, `${lang}:${key} is empty`)
      assert.ok(!/undefined|NaN|\[object/.test(s), `${lang}:${key} renders ${s}`)
    }
  }
})

test('pt-BR is actually translated (most leaves differ from en)', { skip: avail.skip }, async () => {
  const { STRINGS } = await loadBundle()
  const en = leaves(STRINGS.en)
  let same = 0
  let total = 0
  for (const key of Object.keys(en)) {
    const a = key.split('.').reduce((o, p) => o[p], STRINGS.en)
    const b = key.split('.').reduce((o, p) => o[p], STRINGS['pt-BR'])
    if (typeof a === 'string') {
      total += 1
      if (a === b) same += 1
    }
  }
  // Brand name and a few technical words may stay identical.
  assert.ok(same <= Math.max(3, total * 0.15), `${same}/${total} pt-BR strings equal en`)
})

test('settings descriptions show the defaults and masking uses a reserved example', { skip: avail.skip }, async () => {
  const { STRINGS } = await loadBundle()
  for (const lang of ['en', 'pt-BR']) {
    const settings = STRINGS[lang].settings
    for (const key of Object.keys(settings.labels)) {
      assert.ok(settings.help[key], `${lang}:${key} has help`)
    }
    for (const key of ['scan_interval_hours', 'lead_minutes', 'reminder_1_hours', 'reminder_2_hours', 'auto_redeem_enabled', 'early_redeem_enabled', 'early_redeem_remaining_pct', 'excluded_accounts', 'mask_labels', 'language', 'dry_run']) {
      assert.match(settings.help[key], lang === 'en' ? /default/i : /padrão/i, `${lang}:${key} explains the default`)
    }
    assert.match(settings.maskExample, /example@example\.com → e\*\*\*@e\*\*\*\.com/)
    assert.match(settings.maskExample, lang === 'en' ? /default/i : /padrão/i)
  }
})

test('plural forms and immediate redemption wording are clear in both languages', { skip: avail.skip }, async () => {
  const { STRINGS } = await loadBundle()
  assert.equal(STRINGS.en.account.count(1), '1 reset')
  assert.equal(STRINGS.en.account.count(2), '2 resets')
  assert.equal(STRINGS['pt-BR'].remaining.days(1), '(1 dia)')
  assert.equal(STRINGS['pt-BR'].remaining.days(2), '(2 dias)')
  assert.match(STRINGS.en.redeem.description('ACCOUNT', 'DATE'), /spends one banked reset on ACCOUNT now/)
  assert.match(STRINGS['pt-BR'].redeem.description('ACCOUNT', 'DATE'), /gasta agora um reset guardado de ACCOUNT/)
  assert.match(STRINGS.en.redeem.description('ACCOUNT', 'DATE'), /DATE/)
  assert.match(STRINGS['pt-BR'].redeem.description('ACCOUNT', 'DATE'), /DATE/)
  assert.equal(STRINGS.en.quota.weekly(30), 'Weekly: 30% left')
  assert.equal(STRINGS['pt-BR'].quota.weekly(30), 'Semana: 30% livre')
  assert.equal(STRINGS['pt-BR'].quota.fiveHour(100), '5 h: 100% livre')
})

test('no user-visible literal outside the bundles (source scan)', () => {
  const begin = SRC.indexOf('// @strings-begin')
  const end = SRC.indexOf('// @strings-end')
  assert.ok(begin >= 0 && end > begin, 'string bundle markers present')
  const outside = stripComments(SRC.slice(0, begin) + SRC.slice(end))
  // Props that put text on screen or in assistive tech must never get a literal.
  const re = /\b(children|label|title|placeholder|description|confirmLabel|cancelLabel|busyLabel|doneLabel|message|'aria-label'|"aria-label")\s*:\s*(['"`])((?:(?!\2).)*)\2/g
  const allowed = new Set(['Reset Rescue']) // registration-time plugin `name` (inventory label)
  const bad = []
  let m
  while ((m = re.exec(outside))) {
    if (/[A-Za-zÀ-ÿ]/.test(m[3]) && !allowed.has(m[3])) bad.push(m[0])
  }
  // Text passed positionally to jsx('span', ..., 'literal') is also forbidden.
  const textNodes = outside.match(/jsxs?\(\s*['"][a-z]+['"]\s*,\s*['"`][^'"`]*[A-Za-z][^'"`]*['"`]/g) ?? []
  assert.deepEqual([...bad, ...textNodes], [])
})
