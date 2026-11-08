// D-034: helpers for Settings > Per account, the account list (accordion).
// One row per account ([data-rr-account-row]); its header button
// ([data-rr-account-toggle]) expands it; an expanded account shows four options
// ([data-rr-option=<field>], data-rr-state global|on|off), each an SDK
// SegmentedControl whose three buttons are Default, On, Off in that order.
// Only the clicks go through `h.click` (no timers), so these work under
// mocked timers too.

export const SEGMENTS = ['default', 'on', 'off']

export const accountRow = (h, key) => h.q(`[data-rr-account-row="${key}"]`)

/** The open option element, or null while the account is collapsed. */
export const optionEl = (h, key, field) => h.q(`[data-rr-account-row="${key}"] [data-rr-option="${field}"]`)

/** Expands `key` unless it is already open (accordion: closes the open one). */
export async function expand(h, key) {
  const row = accountRow(h, key)
  if (!row) throw new Error(`no account row ${key}`)
  if (row.dataset.rrExpanded !== 'true') await h.click(row.querySelector('[data-rr-account-toggle]'))
  return accountRow(h, key)
}

/** The option of `key`, expanding the account first. */
export async function option(h, key, field) {
  await expand(h, key)
  const el = optionEl(h, key, field)
  if (!el) throw new Error(`no option ${field} on ${key}`)
  return el
}

/** The three segment buttons of an option element, Default/On/Off. */
export const segmentsOf = el => [...el.querySelectorAll('[data-rr-segments] button')]

/** One click on a segment ('default' | 'on' | 'off'). */
export async function pick(h, key, field, choice) {
  const el = await option(h, key, field)
  const i = SEGMENTS.indexOf(choice)
  if (i < 0) throw new Error(`unknown segment ${choice}`)
  await h.click(segmentsOf(el)[i])
}

/** 'global' | 'on' | 'off' for an option, expanding the account first. */
export async function stateOf(h, key, field) {
  return (await option(h, key, field)).dataset.rrState
}

/** The save note text on an account row (open or collapsed), or null. */
export const rowNote = (h, key) => accountRow(h, key)?.querySelector('[data-rr-saved], [data-rr-error]')?.textContent ?? null
