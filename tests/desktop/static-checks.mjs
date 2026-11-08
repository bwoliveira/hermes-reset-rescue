// Static checks for desktop/plugin.js (catalog rule 8 + the maintainer's UI rules).
// Pure functions over the source text so the tests can feed known-bad fixtures.

export const ALLOWED_IMPORTS = new Set(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])

// Strip comments so prose in comments never trips a rule. Keeps string literals.
export function stripComments(src) {
  let out = ''
  let i = 0
  let quote = null
  while (i < src.length) {
    const c = src[i]
    const n = src[i + 1]
    if (quote) {
      out += c
      if (c === '\\') {
        out += n ?? ''
        i += 2
        continue
      }
      if (c === quote) quote = null
      i += 1
      continue
    }
    if (c === '"' || c === "'" || c === '`') {
      quote = c
      out += c
      i += 1
      continue
    }
    if (c === '/' && n === '/') {
      while (i < src.length && src[i] !== '\n') i += 1
      continue
    }
    if (c === '/' && n === '*') {
      i += 2
      while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) {
        if (src[i] === '\n') out += '\n'
        i += 1
      }
      i += 2
      continue
    }
    out += c
    i += 1
  }
  return out
}

const RULES = [
  { id: 'eval', re: /\beval\s*\(/ },
  { id: 'new-function', re: /\bnew\s+Function\b|\bFunction\s*\(/ },
  { id: 'dynamic-import', re: /\bimport\s*\(/ },
  { id: 'dom-access', re: /\b(document|window|globalThis|self|navigator|localStorage|sessionStorage)\s*[.[]/ },
  { id: 'dom-api', re: /\b(querySelector(All)?|getElementById|getElementsBy\w+|innerHTML|outerHTML|insertAdjacentHTML|createElement|appendChild|MutationObserver)\b/ },
  { id: 'raw-network', re: /\b(fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(|\bnew\s+(XMLHttpRequest|WebSocket|EventSource)\b/ },
  { id: 'socket', re: /\bctx\.socket\b|\.socket\s*\(/ },
  { id: 'gateway-shell', re: /shell\.exec/ },
  { id: 'key-capture', re: /\b(keydown|keyup|keypress|onKeyDown|onKeyUp|onKeyPress)\b/ },
  { id: 'raw-listener', re: /\baddEventListener\s*\(/ },
  { id: 'color-hex', re: /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{1,5})?\b(?![-\w])/ },
  { id: 'color-fn', re: /\b(rgba?|hsla?|oklch|oklab|lab|lch|hwb|color-mix)\s*\(/ },
  {
    id: 'color-palette-class',
    re: /\b(?:text|bg|border|ring|fill|stroke|from|to|via|outline|decoration)-(?:red|green|blue|yellow|gray|grey|slate|zinc|neutral|stone|orange|amber|lime|emerald|teal|cyan|sky|indigo|violet|purple|fuchsia|pink|rose|white|black)(?:-\d{2,3})?\b/
  },
  { id: 'italic', re: /\bitalic\b|fontStyle/ },
  { id: 'monospace', re: /\bfont-mono\b|monospace|fontFamily/ },
  { id: 'pill', re: /\brounded-full\b|\bBadge\b|border-radius\s*:\s*9999/ },
  { id: 'cream', re: /\b(cream|ivory|beige|linen|oldlace|floralwhite|seashell|antiquewhite|bisque)\b/i },
  // Still cut (T-31 and the route trims): the warnings block, the test-notice
  // button, the include switch, rehearsal and a separate /ensure call (the
  // scan route ensures first).
  {
    id: 'cut',
    re: /settings_warnings|test[_-]notice|\/accounts\/include|\/rehearse|['"`]\/ensure\b|\bSwitch\b/
  }
]

export function importsOf(src) {
  const code = stripComments(src)
  const specs = []
  const re = /\bimport\s+(?:[\w*{}\s,]+\s+from\s+)?['"]([^'"]+)['"]|\bexport\s+[\w*{}\s,]+\s+from\s+['"]([^'"]+)['"]/g
  let m
  while ((m = re.exec(code))) specs.push(m[1] ?? m[2])
  return specs
}

export function checkSource(src) {
  const code = stripComments(src)
  const violations = []
  for (const spec of importsOf(src)) {
    if (!ALLOWED_IMPORTS.has(spec)) violations.push({ id: 'import', detail: spec })
  }
  if (/\brequire\s*\(/.test(code)) violations.push({ id: 'require', detail: 'require(' })
  const lines = code.split('\n')
  for (const rule of RULES) {
    lines.forEach((line, idx) => {
      if (rule.re.test(line)) violations.push({ id: rule.id, detail: `line ${idx + 1}: ${line.trim().slice(0, 120)}` })
    })
  }
  return violations
}
