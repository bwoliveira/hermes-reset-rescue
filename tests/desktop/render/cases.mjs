// Fixture data for the real-browser layout test (popup-layout.test.mjs).
// Labels are neutral masked placeholders; no real address appears here.
import { NOW, account, credit, iso, overridden, status } from '../fixtures.mjs'

export const LABELS = ['a***@e***.com', 'b***@e***.com', 'c***@e***.com', 'd***@e***.com', 'e***@e***.com', 'f***@e***.com']

// The widest quota line (D-035): both windows at three digits plus the weekly
// reset time, so the layout cases measure the longest text the line can carry.
const QUOTA = { plan_type: 'team', five_hour_remaining_pct: 100, weekly_remaining_pct: 100, weekly_resets_at: iso(NOW + (5 * 24 + 8.5) * 3600_000) }

/** Accounts whose resets are all scheduled, so each reset shows its two lines
 *  (the clock/remaining line and the rescue line, D-027). */
function accounts(count, creditsFor) {
  return Array.from({ length: count }, (_, i) => {
    const n = creditsFor(i)
    const credits = Array.from({ length: n }, (__, j) =>
      credit(`cr_${i}_${j}`, { expiresIn: 40 + j * 5 + i, fireIn: 20 + j * 3 + i })
    )
    return account(`acct_${i}`, LABELS[i], credits, { quota: QUOTA })
  })
}

/** (a) five accounts, two resets each. */
export const CASE_A = status({ accounts: accounts(5, () => 2) })
/** (b) five accounts, one of them with three resets. */
export const CASE_B = status({ accounts: accounts(5, i => (i === 0 ? 3 : 2)) })
/** (c) six accounts, two resets each. */
export const CASE_C = status({ accounts: accounts(6, () => 2) })

/** (D-030) six accounts, two resets each, with per-account overrides: Auto
 *  off, Early on with its own threshold, paused, and two changes (Custom). */
const OVERRIDES = [{}, { auto_redeem_enabled: false }, { early_redeem_enabled: true, early_redeem_remaining_pct: 20 }, { monitored: false }, { auto_redeem_enabled: false, reminders_enabled: false }, {}]
export const CASE_D = status({
  accounts: accounts(6, () => 2).map((a, i) => ({ ...a, ...overridden(OVERRIDES[i]) }))
})

/** (D-031 g) one account with two resets. */
export const CASE_G = status({ accounts: accounts(1, () => 2) })
/** (D-031 h) two accounts with two resets each. */
export const CASE_H = status({ accounts: accounts(2, () => 2) })
/** (D-031 i) no account at all. */
export const CASE_I = status({ accounts: [] })

export const CASES = { a: CASE_A, b: CASE_B, c: CASE_C, d: CASE_D, dpt: CASE_D, g: CASE_G, h: CASE_H, i: CASE_I }
/** The Desktop locale per case; the rest run in English. */
export const LOCALES = { dpt: 'pt-br' }

export const SETTINGS = {
  fields: [
    { key: 'scan_interval_hours', type: 'int', value: 12, min: 1, max: 24 },
    { key: 'lead_minutes', type: 'int', value: 10, min: 5, max: 240 },
    { key: 'reminder_1_hours', type: 'int', value: 24, min: 0, max: 168 },
    { key: 'reminder_2_hours', type: 'int', value: 1, min: 0, max: 72 },
    { key: 'notify_target', type: 'str', value: '' },
    { key: 'auto_redeem_enabled', type: 'bool', value: true },
    { key: 'early_redeem_enabled', type: 'bool', value: false },
    { key: 'early_redeem_remaining_pct', type: 'int', value: 0, min: 0, max: 100 },
    { key: 'excluded_accounts', type: 'str', value: '' },
    { key: 'mask_labels', type: 'bool', value: true },
    { key: 'language', type: 'enum', value: 'auto', choices: ['auto', 'en', 'pt-BR'] },
    { key: 'dry_run', type: 'bool', value: false }
  ]
}
