// T-26: static checks on desktop/plugin.js (catalog rule 8 + UI style rules).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import { PLUGIN_PATH } from './harness.mjs'
import { checkSource, importsOf, stripComments } from './static-checks.mjs'

const SRC = readFileSync(PLUGIN_PATH, 'utf8')
const ids = src => checkSource(src).map(v => v.id)

test('plugin.js passes every static rule', () => {
  assert.deepEqual(checkSource(SRC), [])
})

test('plugin.js imports only the SDK, react and react/jsx-runtime', () => {
  const specs = importsOf(SRC)
  assert.ok(specs.includes('@hermes/plugin-sdk'))
  for (const s of specs) assert.ok(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(s), s)
})

test('plugin.js parses as an ES module without JSX', async () => {
  const { spawnSync } = await import('node:child_process')
  const r = spawnSync(process.execPath, ['--input-type=module', '--check'], { input: SRC, encoding: 'utf8' })
  assert.equal(r.status, 0, r.stderr)
})

// Each fixture must be caught by its rule: proves the checker bites.
// The fixtures are inert strings fed to a text scanner; nothing here is executed.
const BAD = {
  import: "import x from 'lodash'",
  'import-reexport': "export { a } from './other.js'",
  require: "const fs = require('fs')",
  eval: "eval('1')",
  'new-function': "const f = new Function('return 1')",
  'dynamic-import': "await import('@hermes/plugin-sdk')",
  'dom-access': 'document.body.append(x)',
  'dom-window': 'window.location.hash = "#/x"',
  'dom-api': 'el.innerHTML = s',
  'raw-network': "fetch('/api/x')",
  socket: "ctx.socket('/events', fn)",
  'key-capture': "ctx.addEventListener(target, 'keydown', fn)",
  'raw-listener': "el.addEventListener('click', fn)",
  'color-hex': "const c = { color: '#ff0000' }",
  'color-short-hex': "className: 'text-[#fff]'",
  'color-fn': "style: { color: 'rgb(1, 2, 3)' }",
  'color-mix': "style: { background: 'color-mix(in srgb, var(--a) 50%, transparent)' }",
  'color-palette-class': "className: 'text-red-500'",
  italic: "className: 'italic'",
  monospace: "className: 'font-mono'",
  pill: "className: 'rounded-full px-2'",
  cream: "style: { background: 'ivory' }",
  cut: "status.settings_warnings.map(w => w)",
  'cut-test-notice': "ctx.rest('/test-notice', {})",
  'cut-include': "ctx.rest('/accounts/include', {})",
  'cut-rehearse': "ctx.rest('/rehearse', {})",
  'cut-ensure': "ctx.rest('/ensure', { method: 'POST' })",
  'cut-switch': "jsx(Switch, { checked })"
}
const EXPECT = {
  'import-reexport': 'import',
  'dom-window': 'dom-access',
  'color-short-hex': 'color-hex',
  'color-mix': 'color-fn',
  'cut-test-notice': 'cut',
  'cut-include': 'cut',
  'cut-rehearse': 'cut',
  'cut-ensure': 'cut',
  'cut-switch': 'cut'
}

for (const [name, src] of Object.entries(BAD)) {
  test(`static checker catches ${name}`, () => {
    const want = EXPECT[name] ?? name
    assert.ok(ids(src).includes(want), `${name}: got ${JSON.stringify(ids(src))}`)
  })
}

test('plugin.js calls only its own backend routes', () => {
  const code = stripComments(SRC)
  const paths = [...code.matchAll(/ctx\.rest\(\s*['"`]([^'"`]+)['"`]/g)].map(m => m[1].split('?')[0]).sort()
  // D-025 added the settings pair; every path stays inside the plugin's namespace.
  assert.deepEqual([...new Set(paths)], ['/history', '/redeem', '/scan', '/settings', '/status'])
  assert.match(code, /ctx\.rest\(`\/history\?limit=\$\{HISTORY_LIMIT\}`/)
})

test('Redeem now goes through the SDK ConfirmDialog with a literal confirm: true', () => {
  assert.match(SRC, /jsx\(ConfirmDialog,/)
  assert.match(SRC, /body: \{ credit_key: credit\.credit_key, confirm: true \}/)
  // The dialog is a sibling of the Popover, never inside PopoverContent (spike S8).
  const content = SRC.slice(SRC.indexOf('jsx(PopoverContent'), SRC.indexOf('jsx(ConfirmDialog'))
  assert.ok(!content.includes('ConfirmDialog'))
})

test('no external URL and no openExternal call (D-021 removed the usage link)', () => {
  const code = stripComments(SRC)
  const urls = code.match(/https?:\/\/[^'"`\s]+/g) ?? []
  assert.deepEqual(urls, [])
  assert.ok(!/openExternal|CODEX_USAGE_URL|openUsage/.test(code))
})

test('static checker ignores comments and allows theme variables', () => {
  const ok = [
    "// eval('x') and document.body in a comment",
    '/* #ff0000 rgb(1,2,3) */',
    "const cls = 'text-(--ui-text-tertiary) bg-(--ui-bg-elevated) text-destructive'",
    "import { host } from '@hermes/plugin-sdk'",
    "import { useState } from 'react'"
  ].join('\n')
  assert.deepEqual(checkSource(ok), [])
})
