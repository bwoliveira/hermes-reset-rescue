// StatusDTO fixtures shaped like reset_rescue/dto.py (A§5). Times are UTC "Z".
// Labels arrive masked, the default (D-013); committed fixtures stay masked.
// AccountDTO.quota is the D-017 summary the UI asks L1 for:
// { plan_type, five_hour_remaining_pct, weekly_remaining_pct } or null.
export const NOW = Date.parse('2026-11-08T12:00:00Z')

export function iso(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

const H = 3600_000

export function credit(key, { expiresIn, fireIn = null, state = 'available', title = 'Rate-limit reset', jobState = 'armed' } = {}) {
  return {
    credit_key: key,
    title,
    reset_type: 'codex',
    granted_at: iso(NOW - 48 * H),
    expires_at: expiresIn == null ? null : iso(NOW + expiresIn * H),
    state,
    plan: fireIn == null ? 'beyond_horizon' : 'scheduled',
    primary: fireIn == null ? null : { role: 'primary', fire_at: iso(NOW + fireIn * H), job_state: jobState },
    backup: fireIn == null ? null : { role: 'backup', fire_at: iso(NOW + fireIn * H + 6 * 60_000), job_state: jobState },
    last_result: null,
    can_redeem_now: state === 'available'
  }
}

export function account(key, label, credits, extra = {}) {
  return {
    account_key: key,
    label,
    included: true,
    reachable: true,
    last_error_code: null,
    last_checked_at: iso(NOW - H),
    quota: null,
    credits,
    ...extra
  }
}

export function status({ accounts = [], failed = 0, dryRun = false, problem = null, armed = true, lastScan = iso(NOW - H), settings = {}, language = 'auto' } = {}) {
  const banked = accounts.flatMap(a => a.credits).filter(c => c.state === 'available' || c.state === 'pending')
  const fires = accounts
    .flatMap(a => a.credits)
    .flatMap(c => [c.primary, c.backup])
    .filter(s => s && s.fire_at)
    .map(s => s.fire_at)
    .sort()
  return {
    version: '0.1.0',
    schema_version: 1,
    source_kind: 'pool',
    coverage_note_key: 'coverage.pool',
    dry_run: dryRun,
    language,
    settings: {
      scan_interval_hours: 12,
      lead_minutes: 10,
      backup_lead_minutes: 4,
      notify_target_display: 'telegram',
      excluded_accounts_count: 0,
      ...settings
    },
    schedule: { armed, scan_job: null, next_scan_at: null, last_scan_at: lastScan, last_scan_outcome: 'ok', wrappers: [], problem_code: problem },
    accounts,
    attention: { failed, pending: banked.length, next_fire_at: fires[0] ?? null }
  }
}

/** Two accounts with banked resets, one without, out of order on purpose.
 *  The soonest credit (cr_b1) expires 9.6 h from NOW: inside the 24 h warning. */
export function mixed() {
  return status({
    accounts: [
      account('acct_b', 'b***@e***.com', [credit('cr_b2', { expiresIn: 36, fireIn: 36 - (10 / 60) /* synthetic: the planner's slot inside the 48 h horizon */ }), credit('cr_b1', { expiresIn: 9.6, fireIn: 9.5 })], {
        quota: { plan_type: 'plus', five_hour_remaining_pct: 82.4, weekly_remaining_pct: 40 }
      }),
      account('acct_c', 'c***@e***.com', [credit('cr_c1', { expiresIn: 30, state: 'redeemed' })]),
      account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40, fireIn: 39.8 })])
    ]
  })
}

/** Banked credits all more than 24 h away: no warning. */
export function calm() {
  return status({
    accounts: [
      account('acct_b', 'b***@e***.com', [credit('cr_b1', { expiresIn: 30, fireIn: 29.8 })]),
      account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40, fireIn: 39.8 })])
    ]
  })
}

// D-030: per-account overrides. AccountDTO gains `monitored`, `overrides` (only
// the fields set) and `effective`; GET /settings lists the same accounts as
// `[{ account_key, label, overrides, effective }]`.
export const GLOBAL_EFFECTIVE = { auto_redeem_enabled: true, early_redeem_enabled: false, early_redeem_remaining_pct: 10, reminders_enabled: true }

/** The AccountDTO extras for `overrides` on top of the global settings. */
export function overridden(overrides = {}, { monitored } = {}) {
  const effective = { ...GLOBAL_EFFECTIVE }
  for (const [k, v] of Object.entries(overrides)) if (k in effective) effective[k] = v
  const watched = monitored ?? (overrides.monitored === undefined ? true : overrides.monitored)
  return { monitored: watched, overrides: { ...overrides }, effective }
}

/** Four accounts: a plain one, one with Auto off, one with Early on, one paused. */
export function perAccount() {
  return status({
    settings: { early_redeem_enabled: false, early_redeem_remaining_pct: 10 },
    accounts: [
      account('acct_a', 'a***@e***.com', [credit('cr_a1', { expiresIn: 40, fireIn: 39.8 })], overridden()),
      account('acct_b', 'b***@e***.com', [credit('cr_b1', { expiresIn: 30 })], overridden({ auto_redeem_enabled: false })),
      account('acct_c', 'c***@e***.com', [], overridden({ early_redeem_enabled: true, early_redeem_remaining_pct: 20 })),
      // Paused, with the soonest reset: still listed after the others.
      account('acct_d', 'd***@e***.com', [credit('cr_d1', { expiresIn: 5 })], overridden({ monitored: false }))
    ]
  })
}

/** GET /settings with the D-030 accounts list for `st` (a StatusDTO). */
export function settingsWithAccounts(fields, st) {
  return {
    fields,
    account_overrides: Object.fromEntries(st.accounts.filter(a => Object.keys(a.overrides ?? {}).length).map(a => [a.account_key, { label: a.label, ...a.overrides }])),
    accounts: st.accounts.map(a => ({ account_key: a.account_key, label: a.label, overrides: a.overrides, effective: a.effective }))
  }
}
