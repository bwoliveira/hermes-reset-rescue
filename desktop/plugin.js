/**
 * Reset Rescue — Desktop half.
 *
 * One status-bar chip (STATUSBAR_AREAS.right) that opens a Popover listing the
 * Codex accounts in the Hermes pool that hold banked reset credits: how many,
 * and each expiry with its rescue time, in the Desktop's local time zone.
 * Per account a compact quota line (plan, 5 h and weekly remaining) sits
 * under the banked resets (D-017). Per credit a Redeem now with a
 * confirmation (D-015). A History view (D-014), a sort control (D-018), Scan
 * now, a Settings link and a one-line footer.
 *
 * Labels are shown exactly as the StatusDTO sends them: the backend masks them
 * unless the user turned `mask_labels` off (D-013). Strings come from the
 * bundles registered through ctx.i18n; the `language` setting (auto, en,
 * pt-BR) picks the bundle, auto follows the Desktop (D-016).
 *
 * Talks only to its own backend namespace through ctx.rest
 * (/api/plugins/reset-rescue/: GET /status, POST /scan, POST /redeem,
 * GET /history). Plain ESM, no JSX, three imports only.
 */
import {
  Button,
  ConfirmDialog,
  GlyphSpinner,
  host,
  Popover,
  PopoverContent,
  PopoverTrigger,
  STATUSBAR_AREAS,
  useI18n,
  usePluginI18n,
  useValue
} from '@hermes/plugin-sdk'
// D-034: SegmentedControl and Tip are read off the namespace, so a host
// without them still loads the plugin (a missing named import would not).
import * as SDK from '@hermes/plugin-sdk'
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'reset-rescue'

/** Poll cadence: every minute while the Popover is open, every 5 min for the chip. */
export const POLL_OPEN_MS = 60_000
export const POLL_CLOSED_MS = 300_000
const STATUS_TIMEOUT_MS = 10_000
/** /scan reads every account and may wait for the run lock; /redeem reads back. */
const ACTION_TIMEOUT_MS = 45_000
export const HISTORY_LIMIT = 20
/** A rescue this close shows its clock time; a credit expiring this close warns. */
const SOON_MS = 24 * 60 * 60 * 1000
const MINUTE_MS = 60_000
const HOUR_MS = 60 * MINUTE_MS
const DAY_MS = 24 * HOUR_MS
/** The remaining-time suffix refreshes this often, only while the Popover is open. */
export const TICK_MS = 30_000
/** D-025: a number/text edit saves this long after the last keystroke, or on blur. */
export const SAVE_DEBOUNCE_MS = 600
/** D-024, D-027 and D-029: the popover's one height, in px, measured at runtime.
 *
 *  When the popover opens, and again when the text changes (language, status)
 *  or the window resizes, the Chip renders one hidden account block with the
 *  same AccountSection component (two scheduled resets and a quota line), plus
 *  the header in its Accounts-tab form, the sort row and the footer, and reads
 *  their heights (D-030: the header is the sample's, so every tab gets the
 *  same height even where the live header differs). {@link popoverHeightFor} adds
 *  them up, so five full accounts fit with no scroll on any machine, while a
 *  third reset or a sixth account scrolls inside the box. The content box also
 *  caps at calc(100vh - 80px), so a short window never gets a taller popover.
 *
 *  ROW_LAYOUT holds the fallback used before the first measurement (and in an
 *  environment without a layout engine, such as jsdom). The padding and gap are
 *  CSS constants of S.inner, so they are not measured. */
export const ROW_LAYOUT = {
  header: 47,
  sort: 22,
  footer: 30,
  account: 144,
  padding: 14,
  gap: 12
}

/** The popover height for `accounts` full accounts, from `sizes` (px).
 *
 *  The column holds the header, the sort row, `accounts` blocks and the footer:
 *  count + 3 items with count + 2 gaps between them, inside the inner padding
 *  on both sides. The sum rounds up, so the content never falls short by a
 *  fraction of a pixel. */
export function popoverHeightFor(accounts, sizes = ROW_LAYOUT) {
  const count = Math.max(0, Math.trunc(accounts))
  const { header, sort, footer, account, padding, gap } = { ...ROW_LAYOUT, ...sizes }
  return Math.ceil(padding * 2 + header + sort + footer + count * account + gap * (count + 2))
}

export const POPOVER_HEIGHT = popoverHeightFor(5)

/** D-031: the popup fits its content between a floor and the D-029 cap.
 *
 *  natural = the Accounts view as it renders (header, sort row, the listed
 *  accounts, footer), read from a hidden copy of that view; floor = the header,
 *  the first four global setting rows and the footer, read from a hidden copy
 *  of those rows, so the Settings tab is usable with one account; cap = five
 *  full accounts ({@link popoverHeightFor}). Both copies are rendered whatever
 *  the open tab, so the height follows the account list, the language, the
 *  masking and the window, never the tab: every tab has the Accounts height
 *  and a longer tab scrolls inside. FLOOR_FALLBACK is the floor before the
 *  first measurement (and in jsdom). */
export const FLOOR_FALLBACK = 360
/** The settings the floor sample draws, in config_schema order. */
const FLOOR_FIELDS = ['scan_interval_hours', 'lead_minutes', 'reminder_1_hours', 'reminder_2_hours']

/** clamp(natural, floor, cap), rounded up; the cap wins over the floor. */
export function clampHeight(natural, floor, cap) {
  return Math.min(cap, Math.max(floor, Math.ceil(natural)))
}

/** The floor from measured sizes: `settings` is the four rows' height. */
export function floorHeightFor(sizes) {
  if (!(sizes?.settings > 0)) return FLOOR_FALLBACK
  const { header, footer, settings, padding, gap } = { ...ROW_LAYOUT, ...sizes }
  return Math.ceil(padding * 2 + header + settings + footer + gap * 2)
}

/** The natural height from measured sizes: `body` is everything under the
 *  header (the list and the footer, with their gaps). */
export function naturalHeightFor(sizes) {
  const { header, body, padding, gap } = { ...ROW_LAYOUT, ...sizes }
  return Math.ceil(padding * 2 + header + gap + body)
}

/** The plugin's schema form in Desktop settings (spike S8).
 *
 *  D-025 moved the popup's own Settings control into a tab inside the popup,
 *  so nothing in the popup navigates here any more. The URL builder stays
 *  exported because it is the documented deep link to Hermes' form, which
 *  keeps working (in English) for anyone who prefers it.
 */
export function settingsPath(profile) {
  const base = '/settings?tab=plugins&agent=reset-rescue'
  return profile ? `${base}&profile=${encodeURIComponent(profile)}` : base
}
export const SETTINGS_PATH = settingsPath(null)
export const SORTS = ['soonest', 'account']

const BANKED_STATES = new Set(['available', 'pending'])
const DONE_OUTCOMES = new Set(['redeemed', 'already_redeemed'])
/** Activity-log action (store.HISTORY_ACTIONS, D-014) → bundle key. */
const HISTORY_ACTIONS = {
  scan: 'scan',
  schedule: 'schedule',
  reminder: 'reminder',
  remind: 'reminder',
  redemption: 'redemption',
  fire: 'fire',
  refresh: 'refresh',
  failure: 'failure',
  quota_check: 'quotaCheck',
  notice: 'notice',
  heal: 'maintenance',
  gc: 'maintenance',
  uninstall: 'maintenance'
}
const HISTORY_OUTCOMES = new Set([
  'redeemed',
  'already_redeemed',
  'nothing_to_reset',
  'vanished',
  'failed',
  'unconfirmed',
  'dry_run',
  'ok',
  'scheduled',
  'sent',
  'checked'
])

// @strings-begin
// English is the source bundle; pt-BR must carry the same keys and arities.
const EN = {
  sep: ' · ',
  chip: {
    idle: 'Reset Rescue',
    banked: n => `Resets: ${n}`,
    bankedNext: (n, when) => `Resets: ${n} · Next rescue ${when}`,
    expiring: (n, when) => `Resets: ${n} · Expires ${when}`,
    failed: 'Reset failed',
    scanning: 'Checking…'
  },
  chipTip: {
    loading: 'Reset Rescue: loading',
    unreachable: 'Reset Rescue: could not load the status',
    none: 'Reset Rescue: no banked reset credits',
    banked: n => (n === 1 ? 'Reset Rescue: 1 banked reset credit' : `Reset Rescue: ${n} banked reset credits`),
    bankedNext: (n, when) => `Reset Rescue: ${n === 1 ? '1 banked reset credit' : `${n} banked reset credits`}, next rescue ${when}`,
    expiring: when => `Reset Rescue: a reset credit expires within 24 hours (${when})`,
    failed: 'Reset Rescue: an account could not be read or a rescue failed',
    scanning: 'Reset Rescue: checking your Codex accounts'
  },
  popover: {
    title: 'Reset Rescue',
    scanNow: 'Check now',
    settings: 'Settings',
    history: 'History',
    back: 'Back',
    checked: when => `Checked ${when}`,
    neverChecked: 'Not checked yet',
    loading: 'Loading…',
    unreachable: 'Could not load the Reset Rescue status. It will try again automatically.'
  },
  sort: {
    soonest: 'Soonest expiry first',
    account: 'By account'
  },
  banner: {
    dryRun: 'Dry run: checks and reminders still run, but no reset is spent.',
    autoOff: 'Automatic rescue is off. Reminders and Redeem now still work.',
    signIn: n => (n === 1 ? 'Could not read 1 account. Sign in to it again in Hermes.' : `Could not read ${n} accounts. Sign in to them again in Hermes.`),
    setup: 'Rescue schedules are not set up yet. Use Check now to try again.'
  },
  scan: {
    busy: 'A check or rescue is already running. Try again in a minute.',
    failed: 'The check did not finish. Try again in a minute.'
  },
  empty: {
    scanning: (accounts, hours) =>
      `No banked Codex resets right now — checking ${accounts === 1 ? '1 account' : `${accounts} accounts`} every ${hours} h.`,
    plain: 'No banked Codex resets right now.',
    noAccount: 'No Codex account in Hermes — run hermes auth add openai-codex.'
  },
  account: {
    count: n => (n === 1 ? '1 reset' : `${n} resets`),
    signIn: 'Sign in again in Hermes to keep this rescue working.'
  },
  quota: {
    fiveHour: n => `5 h: ${n}% left`,
    weekly: n => `Weekly: ${n}% left`
  },
  credit: {
    expires: when => `Expires ${when}`,
    rescue: when => `Rescue ${when}`,
    noExpiry: 'Expiry unknown',
    redeemNow: 'Redeem now'
  },
  remaining: {
    days: n => (n === 1 ? '(1 day)' : `(${n} days)`),
    hours: n => (n === 1 ? '(1 hour)' : `(${n} hours)`),
    minutes: n => (n === 1 ? '(1 minute)' : `(${n} minutes)`),
    underMinute: '(less than 1 minute)'
  },
  redeem: {
    title: 'Redeem this reset now?',
    description: (label, expires) =>
      `This spends one banked reset on ${label} now and resets its Codex limits. The reset expires ${expires}.`,
    soon: 'soon',
    confirm: 'Redeem now',
    busy: 'Redeeming…',
    cancel: 'Cancel',
    done: label => `Reset redeemed on ${label}.`,
    dryRun: 'Dry run: nothing was spent.',
    failed: 'The reset was not redeemed. Please try again.'
  },
  history: {
    empty: 'No activity yet.',
    unreachable: 'Could not load the history.',
    action: {
      scan: 'Check',
      schedule: 'Rescue scheduled',
      reminder: 'Reminder',
      redemption: 'Redemption',
      fire: 'Rescue',
      refresh: 'Sign-in renewal',
      failure: 'Problem',
      quotaCheck: 'Usage check',
      notice: 'Notice',
      maintenance: 'Maintenance',
      other: 'Activity'
    },
    outcome: {
      redeemed: 'redeemed',
      already_redeemed: 'already redeemed',
      nothing_to_reset: 'nothing to reset',
      vanished: 'no longer listed',
      failed: 'failed',
      unconfirmed: 'not confirmed',
      dry_run: 'dry run',
      ok: 'done',
      scheduled: 'scheduled',
      sent: 'sent',
      checked: 'checked'
    }
  },
  footer: {
    scans: hours => `Checks every ${hours} h`,
    notices: target => `notices → ${target}`,
    early: threshold => `Early rescue ≤ ${threshold}%`,
    autoOff: 'automatic rescue off',
    dryRun: 'Dry run'
  },
  settings: {
    title: 'Settings',
    hint: 'Changes save as you make them.',
    failed: 'Not saved',
    notSaved: reason => `Not saved: ${reason}`, // D-033
    checking: 'Checking…', // D-033
    unconfirmed: 'Not confirmed yet: reopen the popup to check', // D-033
    loadFailed: 'Could not load the settings.',
    maskExample: 'Hides part of each email address, including the domain, for example example@example.com → e***@e***.com. On by default.',
    on: 'On',
    off: 'Off',
    labels: {
      scan_interval_hours: 'Scan interval (hours)',
      lead_minutes: 'Rescue time (minutes before expiry)',
      reminder_1_hours: 'First reminder (hours before expiry)',
      reminder_2_hours: 'Second reminder (hours before expiry)',
      notify_target: 'Reminder destination',
      auto_redeem_enabled: 'Redeem automatically before expiry',
      early_redeem_enabled: 'Redeem early when weekly usage is nearly full',
      early_redeem_remaining_pct: 'Early rescue threshold (% left)',
      excluded_accounts: 'Excluded accounts',
      mask_labels: 'Hide account email addresses',
      language: 'Language',
      dry_run: 'Dry run'
    },
    help: {
      scan_interval_hours: 'Checks accounts every 1–24 hours. Default: 12 hours.',
      lead_minutes: 'Sets the rescue time 5–240 minutes before expiry. Default: 10 minutes; a backup attempt is automatic.',
      reminder_1_hours: 'Sends the first reminder 0–168 hours before expiry; 0 turns it off. Default: 24 hours.',
      reminder_2_hours: 'Sends the second reminder 0–72 hours before expiry; 0 turns it off. Default: 1 hour.',
      notify_target: 'Leave empty to use the Hermes default channel. Or enter platform, platform:chat or platform:chat:thread.',
      auto_redeem_enabled: 'On by default. When off, no automatic rescues are scheduled; reminders and Redeem now still work.',
      early_redeem_enabled: 'When weekly usage left is at or below the threshold, redeems the reset that expires soonest. Off by default.',
      early_redeem_remaining_pct: 'Sets the weekly usage left that triggers early rescue, from 0–100%. Default: 0%.',
      excluded_accounts: 'Enter account keys or hidden labels separated by commas. Empty by default, so all accounts are included.',
      mask_labels: 'Hides part of each email address, including the domain, in the popup and notices. On by default; files and logs stay masked.',
      language: 'Sets the language for the popup and notices. By default, it follows Hermes.',
      dry_run: 'Checks and schedules rescues without spending resets. Off by default.'
    },
    choices: {
      auto: 'Automatic (follow Hermes)'
    }
  },
  // D-027
  noResets: 'No resets',
  // D-030: per-account settings
  scope: {
    label: 'Apply settings to',
    all: 'All accounts',
    account: 'Per account'
  },
  perAccount: {
    tips: {
      monitored: 'When Off, the account is paused: no checks, no automatic rescues and no notices. Default follows Excluded accounts.', // D-034
      auto_redeem_enabled: "Redeems this account's resets automatically before they expire. When Off, Redeem now still works.", // D-034
      early_redeem_enabled: "Redeems a reset early when this account's weekly quota left reaches the threshold. Needs auto-redeem on.", // D-034
      reminders_enabled: 'Sends the reminder notices for this account. The reminder hours are the same for every account.' // D-034
    },
    resetTip: "Removes this account's own settings, so it follows All accounts again.",
    empty: 'No accounts in the Hermes pool yet.',
    // D-030: single-account rule
    singleNotice: 'This account has its own settings',
    singleReset: 'Use global settings',
    // D-034: the account list
    options: {
      auto_redeem_enabled: 'Auto-redeem expiring credits', // D-034
      early_redeem_enabled: 'Early redeem', // D-034
      reminders_enabled: 'Reminders', // D-034
      monitored: 'Watch this account' // D-034
    },
    segment: {
      default: value => `Default (${value})`, // D-034
      on: 'On', // D-034
      off: 'Off' // D-034
    },
    followsGlobal: 'Follows global', // D-034
    customCount: n => (n === 1 ? '1 custom setting' : `${n} custom settings`), // D-034
    paused: 'Paused', // D-034
    thresholdLead: 'when weekly quota left ≤', // D-034
    thresholdUnit: '%', // D-034
    resetAccount: 'Reset this account' // D-034
  },
  tag: {
    autoOff: 'Auto off',
    earlyOn: 'Early on',
    paused: 'Paused',
    custom: 'Custom',
    tip: 'This account has its own settings. Click to open them in Settings > Per account.'
  },
  // D-033: why a save failed, and the two reset buttons
  saveError: {
    timeout: 'no reply in 10 s', // D-033
    notFound: 'route not found (HTTP 404)', // D-033
    http: code => `HTTP ${code}`, // D-033
    offline: 'no connection to Hermes' // D-033
  },
  reset: {
    defaults: 'Restore defaults', // D-033
    defaultsTip: 'Puts every option on this tab back to its default. Reminder destination, excluded accounts and per-account settings stay as they are.', // D-033
    allAccounts: 'Reset all accounts to global', // D-033
    allAccountsTip: "Clears every account's own settings and watches every account again, so all of them follow All accounts.", // D-033
    confirm: 'Confirm', // D-033
    confirmTip: 'Click again within 5 seconds to confirm.' // D-033
  }
}

const PT_BR = {
  sep: ' · ',
  chip: {
    idle: 'Reset Rescue',
    banked: n => `Resets: ${n}`,
    bankedNext: (n, when) => `Resets: ${n} · Próximo resgate ${when}`,
    expiring: (n, when) => `Resets: ${n} · Vence ${when}`,
    failed: 'Falha no reset',
    scanning: 'Verificando…'
  },
  chipTip: {
    loading: 'Reset Rescue: carregando',
    unreachable: 'Reset Rescue: não foi possível carregar o status',
    none: 'Reset Rescue: nenhum crédito de reset guardado',
    banked: n => (n === 1 ? 'Reset Rescue: 1 crédito de reset guardado' : `Reset Rescue: ${n} créditos de reset guardados`),
    bankedNext: (n, when) => `Reset Rescue: ${n === 1 ? '1 crédito de reset guardado' : `${n} créditos de reset guardados`}, próximo resgate ${when}`,
    expiring: when => `Reset Rescue: um crédito de reset vence em menos de 24 horas (${when})`,
    failed: 'Reset Rescue: uma conta não pôde ser lida ou um resgate falhou',
    scanning: 'Reset Rescue: verificando suas contas do Codex'
  },
  popover: {
    title: 'Reset Rescue',
    scanNow: 'Verificar agora',
    settings: 'Configurações',
    history: 'Histórico',
    back: 'Voltar',
    checked: when => `Verificado ${when}`,
    neverChecked: 'Ainda não verificado',
    loading: 'Carregando…',
    unreachable: 'Não foi possível carregar o status do Reset Rescue. A tentativa será repetida automaticamente.'
  },
  sort: {
    soonest: 'Vencimento mais próximo',
    account: 'Por conta'
  },
  banner: {
    dryRun: 'Simulação: as verificações e os lembretes continuam, mas nenhum reset é gasto.',
    autoOff: 'O resgate automático está desligado. Os lembretes e a opção Resgatar agora continuam funcionando.',
    signIn: n => (n === 1 ? 'Não foi possível ler 1 conta. Entre nela de novo no Hermes.' : `Não foi possível ler ${n} contas. Entre nelas de novo no Hermes.`),
    setup: 'Os resgates ainda não estão agendados. Use Verificar agora para tentar novamente.'
  },
  scan: {
    busy: 'Já há uma verificação ou resgate em andamento. Tente de novo em um minuto.',
    failed: 'A verificação não terminou. Tente de novo em um minuto.'
  },
  empty: {
    scanning: (accounts, hours) =>
      `Nenhum reset do Codex guardado agora — verificando ${accounts === 1 ? '1 conta' : `${accounts} contas`} a cada ${hours} h.`,
    plain: 'Nenhum reset do Codex guardado agora.',
    noAccount: 'Nenhuma conta do Codex no Hermes — execute hermes auth add openai-codex.'
  },
  account: {
    count: n => (n === 1 ? '1 reset' : `${n} resets`),
    signIn: 'Entre de novo no Hermes para manter este resgate funcionando.'
  },
  quota: {
    fiveHour: n => `5 h: ${n}% livre`,
    weekly: n => `Semana: ${n}% livre`
  },
  credit: {
    expires: when => `Vence ${when}`,
    rescue: when => `Resgate ${when}`,
    noExpiry: 'Validade desconhecida',
    redeemNow: 'Resgatar agora'
  },
  remaining: {
    days: n => (n === 1 ? '(1 dia)' : `(${n} dias)`),
    hours: n => (n === 1 ? '(1 hora)' : `(${n} horas)`),
    minutes: n => (n === 1 ? '(1 minuto)' : `(${n} minutos)`),
    underMinute: '(menos de 1 minuto)'
  },
  redeem: {
    title: 'Resgatar este reset agora?',
    description: (label, expires) =>
      `Esta ação gasta agora um reset guardado de ${label} e zera os limites do Codex. O reset vence ${expires}.`,
    soon: 'em breve',
    confirm: 'Resgatar agora',
    busy: 'Resgatando…',
    cancel: 'Cancelar',
    done: label => `Reset resgatado em ${label}.`,
    dryRun: 'Simulação: nada foi gasto.',
    failed: 'O reset não foi resgatado. Tente novamente.'
  },
  history: {
    empty: 'Nenhuma atividade ainda.',
    unreachable: 'Não foi possível carregar o histórico.',
    action: {
      scan: 'Verificação',
      schedule: 'Resgate agendado',
      reminder: 'Lembrete',
      redemption: 'Resgate',
      fire: 'Resgate',
      refresh: 'Renovação do acesso',
      failure: 'Problema',
      quotaCheck: 'Verificação de uso',
      notice: 'Aviso',
      maintenance: 'Manutenção',
      other: 'Atividade'
    },
    outcome: {
      redeemed: 'resgatado',
      already_redeemed: 'já resgatado',
      nothing_to_reset: 'nada a zerar',
      vanished: 'não aparece mais',
      failed: 'falhou',
      unconfirmed: 'não confirmado',
      dry_run: 'simulação',
      ok: 'feito',
      scheduled: 'agendado',
      sent: 'enviado',
      checked: 'verificado'
    }
  },
  footer: {
    scans: hours => `Verifica a cada ${hours} h`,
    notices: target => `avisos → ${target}`,
    early: threshold => `Resgate antecipado ≤ ${threshold}%`,
    autoOff: 'resgate automático desligado',
    dryRun: 'Simulação'
  },
  settings: {
    title: 'Configurações',
    hint: 'As mudanças são salvas na hora.',
    failed: 'Não salvo',
    notSaved: reason => `Não salvou: ${reason}`, // D-033
    checking: 'Confirmando…', // D-033
    unconfirmed: 'Ainda não confirmado: reabra o popup para conferir', // D-033
    loadFailed: 'Não foi possível carregar as configurações.',
    maskExample: 'Oculta parte de cada e-mail, inclusive o domínio. Exemplo: example@example.com → e***@e***.com. Ligado por padrão.',
    on: 'Ligado',
    off: 'Desligado',
    labels: {
      scan_interval_hours: 'Intervalo da verificação (horas)',
      lead_minutes: 'Antecedência do resgate (minutos antes de vencer)',
      reminder_1_hours: 'Primeiro lembrete (horas antes de vencer)',
      reminder_2_hours: 'Segundo lembrete (horas antes de vencer)',
      notify_target: 'Destino dos lembretes',
      auto_redeem_enabled: 'Resgatar automaticamente antes de vencer',
      early_redeem_enabled: 'Resgatar antes quando o uso semanal estiver quase no limite',
      early_redeem_remaining_pct: 'Limite do resgate antecipado (% livre)',
      excluded_accounts: 'Contas excluídas',
      mask_labels: 'Ocultar e-mails das contas',
      language: 'Idioma',
      dry_run: 'Simulação'
    },
    help: {
      scan_interval_hours: 'Verifica as contas a cada 1–24 horas. Padrão: 12 horas.',
      lead_minutes: 'Define o horário do resgate de 5 a 240 minutos antes do vencimento. Padrão: 10 minutos; uma nova tentativa é automática.',
      reminder_1_hours: 'Envia o primeiro lembrete de 0 a 168 horas antes do vencimento; 0 desliga. Padrão: 24 horas.',
      reminder_2_hours: 'Envia o segundo lembrete de 0 a 72 horas antes do vencimento; 0 desliga. Padrão: 1 hora.',
      notify_target: 'Deixe vazio para usar o canal padrão do Hermes. Ou informe plataforma, plataforma:chat ou plataforma:chat:thread.',
      auto_redeem_enabled: 'Ligado por padrão. Desligado, não agenda resgates automáticos; os lembretes e a opção Resgatar agora continuam funcionando.',
      early_redeem_enabled: 'Quando o uso semanal livre está no limite definido ou abaixo dele, resgata o reset que vence primeiro. Desligado por padrão.',
      early_redeem_remaining_pct: 'Define o percentual de uso semanal livre que aciona o resgate antecipado, de 0 a 100%. Padrão: 0%.',
      excluded_accounts: 'Informe chaves ou rótulos ocultos das contas, separados por vírgula. Vazio por padrão, inclui todas as contas.',
      mask_labels: 'Oculta parte de cada e-mail, inclusive o domínio, na janela e nos avisos. Ligado por padrão; arquivos e registros continuam protegidos.',
      language: 'Define o idioma da janela e dos avisos. Por padrão, segue o Hermes.',
      dry_run: 'Verifica e agenda resgates sem gastar resets. Desligado por padrão.'
    },
    choices: {
      auto: 'Automático (seguir o Hermes)'
    }
  },
  // D-027
  noResets: 'Sem resets',
  // D-030: per-account settings
  scope: {
    label: 'Aplicar configurações a',
    all: 'Todas as contas',
    account: 'Por conta'
  },
  perAccount: {
    tips: {
      monitored: 'Em Desligado, a conta fica pausada: sem verificações, sem resgates automáticos e sem avisos. Padrão segue Contas excluídas.', // D-034
      auto_redeem_enabled: 'Resgata automaticamente os resets desta conta antes de vencerem. Em Desligado, Resgatar agora continua funcionando.', // D-034
      early_redeem_enabled: 'Resgata um reset antes do vencimento quando a cota semanal restante desta conta chega ao limite. Precisa do resgate automático ligado.', // D-034
      reminders_enabled: 'Envia os avisos de lembrete desta conta. Os horários dos lembretes são os mesmos para todas as contas.' // D-034
    },
    resetTip: 'Remove as configurações próprias desta conta, que volta a seguir Todas as contas.',
    empty: 'Ainda não há contas no pool do Hermes.',
    // D-030: the single-account rule
    singleNotice: 'Esta conta tem ajustes próprios',
    singleReset: 'Usar configurações globais',
    // D-034: the account list
    options: {
      auto_redeem_enabled: 'Resgate automático (créditos vencendo)', // D-034
      early_redeem_enabled: 'Resgate antecipado', // D-034
      reminders_enabled: 'Lembretes', // D-034
      monitored: 'Monitorar esta conta' // D-034
    },
    segment: {
      default: value => `Padrão (${value})`, // D-034
      on: 'Ligado', // D-034
      off: 'Desligado' // D-034
    },
    followsGlobal: 'Segue o global', // D-034
    customCount: n => (n === 1 ? '1 ajuste' : `${n} ajustes`), // D-034
    paused: 'Pausada', // D-034
    thresholdLead: 'quando a cota semanal restante ≤', // D-034
    thresholdUnit: '%', // D-034
    resetAccount: 'Restaurar esta conta' // D-034
  },
  tag: {
    autoOff: 'Auto desligado',
    earlyOn: 'Antecipado ligado',
    paused: 'Pausada',
    custom: 'Personalizada',
    tip: 'Esta conta tem configurações próprias. Clique para abri-las em Configurações > Por conta.'
  },
  // D-033: why a save failed, and the two reset buttons
  saveError: {
    timeout: 'sem resposta em 10 s', // D-033
    notFound: 'rota não encontrada (HTTP 404)', // D-033
    http: code => `HTTP ${code}`, // D-033
    offline: 'sem conexão com o Hermes' // D-033
  },
  reset: {
    defaults: 'Restaurar padrões', // D-033
    defaultsTip: 'Volta todas as opções desta aba ao padrão. O destino dos lembretes, as contas excluídas e os ajustes por conta continuam como estão.', // D-033
    allAccounts: 'Voltar todas para o global', // D-033
    allAccountsTip: 'Apaga os ajustes próprios de todas as contas e volta a monitorar todas, que passam a seguir Todas as contas.', // D-033
    confirm: 'Confirmar', // D-033
    confirmTip: 'Clique de novo em até 5 segundos para confirmar.' // D-033
  }
}

// The Desktop's locale ids are lowercase BCP-47 ('pt-br') and bundles are
// matched by exact id, so pt-BR is registered under both spellings.
export const STRINGS = { en: EN, 'pt-BR': PT_BR }
const REGISTERED = { en: EN, 'pt-BR': PT_BR, 'pt-br': PT_BR }

// Inventory metadata is read once at load, before any locale is known: English only.
const META = {
  name: 'Reset Rescue',
  description: 'Redeems expiring Codex reset credits for every account in your Hermes pool.'
}
// @strings-end

// ---------------------------------------------------------------- language

/** The Desktop's own language: pt-* gives pt-BR, else English. */
export function hostLanguage(hostLocale) {
  return /^pt([-_]|$)/i.test(String(hostLocale ?? '')) ? 'pt-BR' : 'en'
}

/** The plugin's `language` setting wins when it names a bundle; `auto` (or
 *  anything else) follows the Desktop (D-016). */
export function pickLanguage(hostLocale, setting) {
  return setting === 'en' || setting === 'pt-BR' ? setting : hostLanguage(hostLocale)
}

function resolveIn(bundle, key, args) {
  let node = bundle
  for (const part of key.split('.')) {
    if (node == null || typeof node !== 'object') return key
    node = node[part]
  }
  if (typeof node === 'function') return node(...args)
  return typeof node === 'string' ? node : key
}

/** The host's scoped translator when the Desktop already shows `lang`; else a
 *  read of the same bundle registered through ctx.i18n. */
function useTranslator(lang) {
  const hostT = usePluginI18n(ID)
  const { locale } = useI18n()
  return lang === hostLanguage(locale) ? hostT : (key, ...args) => resolveIn(STRINGS[lang], key, args)
}

// ---------------------------------------------------------------- time

function toDate(iso) {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

function epoch(iso) {
  return toDate(iso)?.getTime() ?? Infinity
}

function isoOf(ms) {
  return new Date(ms).toISOString()
}

/** "Sun, Nov 8, 18:30" in the Desktop's own time zone (no timeZone option). */
/** D-021: time left as the largest whole unit, floored: "(3 days)", "(1 hour)". */
export function remainingText(ms, t) {
  if (!Number.isFinite(ms)) return null
  if (ms >= DAY_MS) return t('remaining.days', Math.floor(ms / DAY_MS))
  if (ms >= HOUR_MS) return t('remaining.hours', Math.floor(ms / HOUR_MS))
  if (ms >= MINUTE_MS) return t('remaining.minutes', Math.floor(ms / MINUTE_MS))
  return t('remaining.underMinute')
}

export function formatWhen(iso, lang) {
  const d = toDate(iso)
  if (!d) return null
  return new Intl.DateTimeFormat(lang, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23'
  }).format(d)
}

/** "18:30" in the Desktop's own time zone. */
export function formatTime(iso, lang) {
  const d = toDate(iso)
  if (!d) return null
  return new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d)
}

/** "Nov 9" in the Desktop's own time zone. */
export function formatDay(iso, lang) {
  const d = toDate(iso)
  if (!d) return null
  return new Intl.DateTimeFormat(lang, { month: 'short', day: 'numeric' }).format(d)
}

function sameLocalDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** Clock time today, else date and time. */
function formatRecent(iso, lang) {
  const d = toDate(iso)
  if (!d) return null
  return sameLocalDay(d, new Date(Date.now())) ? formatTime(iso, lang) : formatWhen(iso, lang)
}

// ---------------------------------------------------------------- view model

function isBanked(credit) {
  // A banked-only StatusDTO may omit the state; anything it lists is banked.
  return credit != null && (credit.state == null || BANKED_STATES.has(credit.state))
}

function liveSlot(slot) {
  return slot && slot.fire_at && slot.job_state !== 'removed' && slot.job_state !== 'fired' ? slot.fire_at : null
}

/** When a banked credit will be rescued: its armed primary, else its armed
 *  backup. Only a scheduled slot books a rescue; a credit without one (beyond
 *  the 48 h horizon, or with every slot fired or removed) has no rescue time. */
export function rescueAt(credit) {
  if (credit?.rescue_at) return credit.rescue_at
  return liveSlot(credit?.primary) ?? liveSlot(credit?.backup) ?? null
}

/** A rescue is booked only for an account that is watched and has auto-redeem
 *  on. A paused or auto-off account keeps its scheduled slots out of the popup. */
export function rescueBooked(account) {
  return !isPaused(account) && account?.effective?.auto_redeem_enabled !== false
}

/** Accounts with at least one banked credit; credits soonest expiry first.
 *  `soonest` orders accounts by their soonest expiry, `account` by label. */
export function bankedAccounts(status, sort = 'soonest') {
  const out = []
  for (const account of status?.accounts ?? []) {
    const credits = (account.credits ?? []).filter(isBanked).sort((a, b) => epoch(a.expires_at) - epoch(b.expires_at))
    if (credits.length) out.push({ account, credits })
  }
  const bySoonest = (a, b) => epoch(a.credits[0].expires_at) - epoch(b.credits[0].expires_at)
  if (sort === 'account') {
    return out.sort((a, b) => String(a.account.label ?? '').localeCompare(String(b.account.label ?? '')) || bySoonest(a, b))
  }
  return out.sort(bySoonest)
}

/** D-027: every pool account, banked credits first. An account with no banked
 *  reset is listed with an empty `credits` list. Soonest: accounts with resets
 *  by soonest expiry, then the rest by label. `account`: by label. */
export function listedAccounts(status, sort = 'soonest') {
  const out = (status?.accounts ?? []).map(account => ({
    account,
    credits: (account.credits ?? []).filter(isBanked).sort((a, b) => epoch(a.expires_at) - epoch(b.expires_at))
  }))
  const byLabel = (a, b) => String(a.account.label ?? '').localeCompare(String(b.account.label ?? ''))
  const bySoonest = (a, b) => {
    const ea = a.credits.length ? epoch(a.credits[0].expires_at) : Infinity
    const eb = b.credits.length ? epoch(b.credits[0].expires_at) : Infinity
    if (ea !== eb) return ea < eb ? -1 : 1
    return byLabel(a, b)
  }
  out.sort(sort === 'account' ? (a, b) => byLabel(a, b) || bySoonest(a, b) : bySoonest)
  // D-030: paused accounts go after the others, in the same order.
  return [...out.filter(g => !isPaused(g.account)), ...out.filter(g => isPaused(g.account))]
}

// ---------------------------------------------------------------- D-030

const THRESHOLD_FIELD = 'early_redeem_remaining_pct'

/** A paused account is one the engine does not watch (`monitored: false`). */
export function isPaused(account) {
  return account?.monitored === false
}

/** The overrides an account carries: only the fields set, without `label`. */
export function overridesOf(account) {
  const out = {}
  const raw = account?.overrides
  if (!raw || typeof raw !== 'object') return out
  for (const [k, v] of Object.entries(raw)) if (k !== 'label' && v !== null && v !== undefined) out[k] = v
  return out
}

/** The Accounts-tab tag for an account (D-030): paused, autoOff, earlyOn,
 *  custom when several things differ, or null with no override. */
export function accountTag(account) {
  if (isPaused(account)) return 'paused'
  const o = overridesOf(account)
  const keys = Object.keys(o)
  if (!keys.length) return null
  if (keys.length === 1 && o.auto_redeem_enabled === false) return 'autoOff'
  if (o.early_redeem_enabled === true && keys.every(k => k === 'early_redeem_enabled' || k === THRESHOLD_FIELD)) return 'earlyOn'
  return 'custom'
}

/** D-030 single-account rule: 'multi' shows the switch and the tags; 'single'
 *  (one account that still has overrides) shows only the reset notice; 'off'
 *  shows neither. The API decides when it can (status first, then GET
 *  /settings `meta`): `per_account_available`, then `pool_account_count` (the
 *  pool, paused accounts included). `account_count` is not used: it leaves
 *  paused accounts out, so it would hide the links that watch them again.
 *  Without either, the listed accounts (paused included) decide. */
export function perAccountMode(status, accounts = status?.accounts ?? [], meta = {}) {
  const pick = key => (status?.[key] !== undefined && status?.[key] !== null ? status[key] : meta?.[key])
  const list = Array.isArray(accounts) ? accounts : []
  const customised = list.some(a => Object.keys(overridesOf(a)).length > 0)
  const pool = pick('pool_account_count')
  const n = Number.isFinite(pool) ? pool : list.length
  const flag = pick('per_account_available')
  const available = typeof flag === 'boolean' ? flag : n >= 2 || customised
  if (!available) return 'off'
  if (n >= 2) return 'multi'
  return list.length === 1 && customised ? 'single' : 'off'
}

/** Soonest rescue still ahead across every banked credit. */
export function nextRescue(status, nowMs = Date.now()) {
  let best = Infinity
  for (const group of bankedAccounts(status)) {
    if (!rescueBooked(group.account)) continue
    for (const credit of group.credits) {
      const at = epoch(rescueAt(credit))
      if (at >= nowMs && at < best) best = at
    }
  }
  return best === Infinity ? null : isoOf(best)
}

/** Soonest banked expiry within 24 h that has not passed, or null. */
export function expiringSoon(status, nowMs = Date.now()) {
  let best = Infinity
  for (const group of bankedAccounts(status)) {
    const at = epoch(group.credits[0].expires_at)
    if (at >= nowMs && at - nowMs < SOON_MS && at < best) best = at
  }
  return best === Infinity ? null : isoOf(best)
}

/** No Codex account to scan: an empty state, not a failure. */
const NO_ACCOUNT_CODES = new Set(['no_account', 'no_source'])
/** Transient: the next poll clears them. */
const TRANSIENT_CODES = new Set(['busy', 'lock_timeout'])

function problemCode(status) {
  return status?.schedule?.problem_code ?? null
}

function noAccount(status) {
  return NO_ACCOUNT_CODES.has(problemCode(status)) || status?.account_count === 0
}

function setupProblem(status) {
  const code = problemCode(status)
  return Boolean(code) && !NO_ACCOUNT_CODES.has(code) && !TRANSIENT_CODES.has(code)
}

function failedAccounts(status) {
  return Number(status?.attention?.failed ?? 0) || 0
}

/** Chip state: scanning, then failed, then a credit expiring within 24 h,
 *  then the banked count with the next rescue, then zero. */
export function chipState(status, phase, { scanning = false, nowMs = Date.now() } = {}) {
  if (scanning) return { kind: 'scanning' }
  if (!status) return { kind: phase === 'error' ? 'unreachable' : 'loading' }
  if (failedAccounts(status) > 0 || setupProblem(status)) return { kind: 'failed' }
  const banked = bankedAccounts(status).reduce((n, g) => n + g.credits.length, 0)
  if (!banked) return { kind: 'none' }
  const expiring = expiringSoon(status, nowMs)
  if (expiring) return { kind: 'expiring', banked, at: expiring }
  const at = nextRescue(status, nowMs)
  if (!at) return { kind: 'banked', banked }
  return { kind: 'bankedNext', banked, at, soon: epoch(at) - nowMs < SOON_MS }
}

/** How many accounts the scan covers, or null when the status cannot say.
 *  A banked-only status may list no account at all, so an empty list is not
 *  "no account": only `account_count` or a no-account problem says that. */
export function accountCount(status) {
  if (typeof status?.account_count === 'number') return status.account_count
  const accounts = (status?.accounts ?? []).filter(a => a.included !== false)
  return accounts.length ? accounts.length : null
}

function percent(value) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(Math.min(100, Math.max(0, value))) : null
}

/** "Plus · 5 h 82% left · weekly 40% left" from AccountDTO.quota, or null when
 *  the backend sent no reading (D-017). The plan is shown as the backend
 *  reports it, first letter raised. */
export function quotaLine(account, t) {
  const quota = account?.quota
  if (!quota || typeof quota !== 'object') return null
  const parts = []
  const plan = typeof quota.plan_type === 'string' ? quota.plan_type.trim().replace(/_/g, ' ') : ''
  if (plan) parts.push(plan.charAt(0).toUpperCase() + plan.slice(1))
  const five = percent(quota.five_hour_remaining_pct)
  if (five !== null) parts.push(t('quota.fiveHour', five))
  const weekly = percent(quota.weekly_remaining_pct)
  if (weekly !== null) parts.push(t('quota.weekly', weekly))
  return parts.length ? parts.join(t('sep')) : null
}

// ---------------------------------------------------------------- data

function isBusyError(err) {
  return err?.statusCode === 409 || err?.status === 409 || /\b409\b|\bbusy\b/.test(String(err?.message ?? ''))
}

/**
 * D-033 (review r8 P1): who owns the popup's data and its settings writes:
 * the full (connectionId, profile) pair of the active gateway. Two registry
 * sources can both run a profile called `default`, and the host routes each
 * request to the source and profile active when it is made, so the profile
 * name alone cannot tell them apart.
 *
 * A host without `host.state.connectionId` gives the profile name alone: an
 * owner that cannot be compared (see `ownerKnown`). The queue never sends a
 * job with such an owner that had to wait behind another write.
 */
export function ownerKey(connectionId, profile) {
  return `${connectionId ?? ''}\u0000${profile}`
}

/** True when `owner` carries a connection, so it can be compared. */
export function ownerKnown(owner) {
  return typeof owner === 'string' && owner.includes('\u0000')
}

/** The owner active now (see `ownerKey`). */
export function currentOwner() {
  const profile = host.state.profile.get()
  return host.state.connectionId ? ownerKey(host.state.connectionId.get(), profile) : profile
}

/** `currentOwner()` as a hook: re-renders when the connection or the profile
 *  changes, alone or together. */
function useOwner() {
  const profile = useValue(host.state.profile)
  // Always two subscriptions, so the hook order never depends on the host.
  const connectionId = useValue(host.state.connectionId ?? host.state.profile)
  return host.state.connectionId ? ownerKey(connectionId, profile) : profile
}

/** Owner-keyed polling of /status, plus Scan now (which also arms the owned
 *  jobs). Data is cleared on an owner change (connection or profile) and
 *  responses that belong to an older owner or request are dropped. `profile`
 *  below is that owner (D-033 r8), not the bare profile name. */
function useRescue(ctx, open) {
  const profile = useOwner()
  const [state, setState] = useState({ status: null, phase: 'loading', profile })
  const [scan, setScan] = useState({ busy: false, note: null, profile })
  const seq = useRef(0)
  const scanned = useRef(new Set())
  const current = useRef(profile)
  current.current = profile

  const accept = useCallback((status, owner) => {
    if (owner !== current.current) return
    seq.current += 1 // older polls in flight are now stale
    setState({ status, phase: 'ready', profile: owner })
  }, [])

  const runScan = useCallback(async () => {
    const owner = current.current
    scanned.current.add(owner)
    setScan({ busy: true, note: null, profile: owner })
    try {
      const status = await ctx.rest('/scan', { method: 'POST', body: {}, timeoutMs: ACTION_TIMEOUT_MS })
      accept(status, owner)
      if (owner === current.current) setScan({ busy: false, note: null, profile: owner })
    } catch (err) {
      if (owner === current.current) setScan({ busy: false, note: isBusyError(err) ? 'busy' : 'failed', profile: owner })
    }
  }, [ctx, accept])

  const load = useCallback(() => {
    const mine = ++seq.current
    const owner = profile
    ctx.rest('/status', { timeoutMs: STATUS_TIMEOUT_MS }).then(
      status => {
        if (mine !== seq.current || owner !== current.current) return
        setState({ status, phase: 'ready', profile: owner })
        // Zero setup: the first status with no armed jobs and no known
        // problem scans once, and the scan arms the jobs.
        const schedule = status?.schedule
        if (schedule && schedule.armed === false && !schedule.problem_code && !scanned.current.has(owner)) runScan()
      },
      () => {
        if (mine === seq.current && owner === current.current) setState({ status: null, phase: 'error', profile: owner })
      }
    )
  }, [ctx, profile, runScan])

  useEffect(() => {
    load()
    return ctx.setInterval(load, open ? POLL_OPEN_MS : POLL_CLOSED_MS)
  }, [ctx, load, open])

  // P3: a save from the Settings form emits `plugin.reset-rescue.settings.changed`
  // (hermes_cli/plugin_events.py). Reloading here is what makes the change
  // visible on the next render instead of on the next poll — the backend
  // already rebuilds its service per request, so nothing else is cached.
  useEffect(() => {
    if (typeof ctx.onEvent !== 'function') return undefined
    return ctx.onEvent('plugin.reset-rescue.settings.changed', () => load())
  }, [ctx, load])

  // A render right after a profile switch must not show the old profile's data.
  const view = state.profile === profile ? state : { status: null, phase: 'loading', profile }
  const scanView = scan.profile === profile ? scan : { busy: false, note: null, profile }
  return { ...view, scan: scanView, runScan, reload: load, adopt: accept, profile }
}

/** The History view's data: fetched each time the view opens. */
function useHistory(ctx) {
  const [state, setState] = useState({ items: null, phase: 'loading' })
  useEffect(() => {
    let live = true
    ctx.rest(`/history?limit=${HISTORY_LIMIT}`, { timeoutMs: STATUS_TIMEOUT_MS }).then(
      body => live && setState({ items: Array.isArray(body?.items) ? body.items : [], phase: 'ready' }),
      () => live && setState({ items: null, phase: 'error' })
    )
    return () => {
      live = false
    }
  }, [ctx])
  return state
}

/** The clock for the remaining-time suffix: ticks every 30 s while open only. */
function useNow(ctx, open) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!open) return undefined
    setNow(Date.now())
    return ctx.setInterval(() => setNow(Date.now()), TICK_MS)
  }, [ctx, open])
  return now
}

// ---------------------------------------------------------------- styles

const S = {
  // D-022: a fixed, comfortable width. D-021 sized the popover to its longest
  // line (`width: max-content`), which made it as wide as the longest reset
  // string; the rows now stack in two lines and clip instead.
  content: { width: 400, minWidth: 380, maxWidth: '90vw', maxHeight: 'calc(100vh - 80px)', padding: 0, background: 'var(--ui-bg-elevated)', color: 'var(--ui-text-primary)' },
  // D-024: the height is fixed on the content box, so this is the only part
  // that scrolls. `flex: 1 1 auto` + `minHeight: 0` is what lets it shrink
  // inside the fixed-height column instead of growing the popover.
  scroll: { height: '100%', overflowY: 'auto', position: 'relative' },
  inner: { display: 'flex', flexDirection: 'column', gap: 12, padding: 14, minHeight: '100%', boxSizing: 'border-box' },
  // D-029: the hidden measuring sample sits over the scroll box's own width
  // (left and right 14 px, the inner padding), so it wraps like the real rows.
  // The box is zero high and clipped, so the sample adds nothing to the scroll height.
  measure: { position: 'absolute', top: 0, left: 14, right: 14, height: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column', gap: 12, visibility: 'hidden', pointerEvents: 'none' },

  header: { display: 'flex', flexDirection: 'column', gap: 4 },
  headRow: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  actions: { display: 'flex', alignItems: 'center', gap: 6 },
  title: { fontSize: 14, fontWeight: 600, margin: 0 },
  muted: { fontSize: 12, color: 'var(--ui-text-tertiary)' },
  secondary: { fontSize: 12, color: 'var(--ui-text-secondary)' },
  account: { display: 'flex', flexDirection: 'column', gap: 6, paddingTop: 10, borderTop: '1px solid var(--ui-stroke-secondary)' },
  accountHead: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 },
  label: { fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  quota: { fontSize: 11, color: 'var(--ui-text-tertiary)' },
  when: { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '100%' },
  credit: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8, fontSize: 12, color: 'var(--ui-text-secondary)', paddingLeft: 10 },
  // D-022: the two-line block. `minWidth: 0` lets it shrink inside the flex
  // row so the ellipsis fires instead of widening the popover.
  creditLines: { display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0, flex: '1 1 auto' },
  rescueLine: { fontSize: 11, color: 'var(--ui-text-tertiary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '100%' },
  destructive: { color: 'var(--color-destructive)' },
  line: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--ui-text-tertiary)' },
  row: { display: 'flex', alignItems: 'center', gap: 8 },
  para: { fontSize: 12, color: 'var(--ui-text-tertiary)', margin: 0 },
  list: { display: 'flex', flexDirection: 'column', gap: 6, margin: 0, padding: 0, listStyle: 'none' },
  footer: { fontSize: 12, color: 'var(--ui-text-tertiary)', paddingTop: 10, borderTop: '1px solid var(--ui-stroke-secondary)' },
  // D-025: the settings tab. One row per key: label + control on the first
  // line, the muted help text under it, and a small save note on the right.
  setting: { display: 'flex', flexDirection: 'column', gap: 3, paddingTop: 10, borderTop: '1px solid var(--ui-stroke-secondary)' },
  settingHead: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  settingLabel: { fontSize: 12, fontWeight: 500, color: 'var(--ui-text-primary)' },
  settingControl: { display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 },
  settingHint: { fontSize: 11, color: 'var(--ui-text-tertiary)', margin: 0 },
  settingNote: { fontSize: 11, color: 'var(--ui-text-tertiary)' },
  toggle: { fontSize: 11, lineHeight: '16px', padding: '0 6px', minWidth: 34 },
  number: { width: 64, fontSize: 12, padding: '1px 4px', color: 'var(--ui-text-primary)', background: 'var(--ui-bg-quaternary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 3 },
  text: { width: 190, fontSize: 12, padding: '1px 4px', color: 'var(--ui-text-primary)', background: 'var(--ui-bg-quaternary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 3 },
  select: { fontSize: 12, padding: '1px 4px', color: 'var(--ui-text-primary)', background: 'var(--ui-bg-quaternary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 3 },
  // D-030: the scope control and the Accounts-tab tag. D-034: the per-account
  // list (one row per account, accordion) and its options.
  scope: { display: 'flex', alignItems: 'center', gap: 4, paddingTop: 10 },
  ellipsis: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  link: { fontSize: 11, lineHeight: '14px', padding: 0, minWidth: 0, color: 'var(--ui-text-secondary)' },
  resetFooter: { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, paddingTop: 10 },
  singleNotice: { display: 'flex', alignItems: 'center', gap: 6, minWidth: 0, paddingTop: 10, whiteSpace: 'nowrap' },
  acctRow: { display: 'flex', flexDirection: 'column', minWidth: 0, borderTop: '1px solid var(--ui-stroke-secondary)' },
  acctHead: { display: 'flex', alignItems: 'center', gap: 8, width: '100%', minWidth: 0, padding: '8px 0', margin: 0, border: 0, background: 'transparent', color: 'inherit', font: 'inherit', textAlign: 'left', cursor: 'pointer' },
  acctLabel: { flex: '0 1 auto', fontSize: 12, fontWeight: 500, color: 'var(--ui-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  acctSummary: { flex: '1 1 auto', fontSize: 11, color: 'var(--ui-text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  chevron: { flexShrink: 0, color: 'var(--ui-text-tertiary)', transition: 'transform 100ms' },
  acctNote: { display: 'flex', minWidth: 0, paddingBottom: 6 },
  acctDetail: { display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0, padding: '0 0 10px 0' },
  option: { display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4, minWidth: 0 },
  // An option on Default reads muted; an account's own setting reads strong.
  optionInherited: { fontSize: 12, fontWeight: 400, color: 'var(--ui-text-tertiary)' },
  optionOwn: { fontSize: 12, fontWeight: 600, color: 'var(--ui-text-primary)' },
  segments: { display: 'flex', maxWidth: '100%', minWidth: 0 },
  segFallback: { display: 'inline-flex', alignItems: 'center', gap: 2 },
  threshold: { display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 6, minWidth: 0, paddingLeft: 10 },
  paused: { opacity: 0.55 },
  tag: { fontSize: 11, lineHeight: '15px', padding: '0 4px', minWidth: 0, flexShrink: 0, color: 'var(--ui-text-tertiary)', fontWeight: 400 },
  nameLine: { display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }
}

// ---------------------------------------------------------------- views

function Loading({ t, measure = false }) {
  return jsxs('div', { ...attrs(measure, { 'data-rr-loading': 'true' }), style: S.line, children: [jsx(GlyphSpinner, {}), jsx('span', { children: t('popover.loading') })] })
}

/** Data attributes for the page and its tests. The hidden measuring sample
 *  (measure = true) carries none, so it never counts as an account or a row. */
function attrs(measure, props) {
  return measure ? {} : props
}

/** D-034: `child` in an SDK Tip; the hidden measuring sample gets none. */
function withTip(measure, label, child, key) {
  return measure ? child : jsx(Tip, { label, children: child }, key)
}

function CreditRow({ credit, booked, now, lang, t, onRedeem, measure = false }) {
  const expires = formatWhen(credit.expires_at, lang)
  const rescue = formatWhen(booked ? rescueAt(credit) : null, lang)
  const left = expires ? remainingText(epoch(credit.expires_at) - now, t) : null
  // D-022: two tidy lines. Line 1 is the expiry with the time-left suffix
  // (\"Vence qua., 11 de nov., 09:00 (3 dias)\"), line 2 the muted rescue time.
  // The old one-line form joined them and made the popover as wide as the
  // longest string; the fixed width below clips instead.
  const first = expires ? t('credit.expires', expires) : t('credit.noExpiry')
  const line = left ? `${first} ${left}` : first
  const canRedeem = credit.can_redeem_now !== false && credit.state !== 'pending'
  return jsxs('div', {
    ...attrs(measure, { 'data-rr-credit': credit.credit_key, 'data-rr-rescue': rescue ? 'scheduled' : 'none' }),
    style: S.credit,
    children: [
      jsxs('div', {
        style: S.creditLines,
        children: [
          jsx('span', { ...attrs(measure, { 'data-rr-when': 'true' }), style: S.when, children: line }),
          rescue
            ? jsx('span', { ...attrs(measure, { 'data-rr-rescue-at': 'true' }), style: S.rescueLine, children: t('credit.rescue', rescue) })
            : null
        ]
      }),
      canRedeem
        ? jsx(Button, {
            variant: 'text',
            size: 'micro',
            ...attrs(measure, { 'data-rr-redeem': credit.credit_key }),
            onClick: () => onRedeem(credit),
            children: t('credit.redeemNow')
          })
        : null
    ]
  })
}

function AccountSection({ group, now, lang, t, onRedeem, onTag, measure = false }) {
  const { account, credits } = group
  const booked = rescueBooked(account)
  const quota = quotaLine(account, t)
  // D-030: an account with its own settings carries a small muted tag that
  // opens Settings > Per account on its row; a paused account is greyed.
  const tag = !onTag ? null : accountTag(account)
  const paused = !measure && isPaused(account)
  return jsxs('section', {
    ...attrs(measure, { 'data-rr-account': account.account_key, 'data-rr-paused': paused ? 'true' : undefined }),
    style: paused ? { ...S.account, ...S.paused } : S.account,
    children: [
      jsxs('div', {
        style: S.accountHead,
        children: [
          jsxs('div', {
            style: S.nameLine,
            children: [
              // D-013: the label exactly as the DTO sends it (masked by default).
              // D-034: the full label in an SDK Tip (it clips at 400 px), never a native title.
              withTip(measure, account.label, jsx('span', { ...attrs(measure, { 'data-rr-label': 'true' }), style: S.label, children: account.label }), 'label'),
              tag
                ? jsx(
                    Tip,
                    {
                      label: t('tag.tip'),
                      children: jsx(Button, {
                        variant: 'text',
                        size: 'micro',
                        ...attrs(measure, { 'data-rr-tag': tag }),
                        style: S.tag,
                        onClick: () => onTag?.(account.account_key),
                        children: t(`tag.${tag}`)
                      })
                    },
                    'tag'
                  )
                : null
            ]
          }),
          credits.length
            ? jsx('span', { ...attrs(measure, { 'data-rr-count': 'true' }), style: S.muted, children: t('account.count', credits.length) })
            : null
        ]
      }),
      account.reachable === false
        ? jsx('span', {
            ...attrs(measure, { 'data-rr-signin': 'true' }),
            style: { ...S.muted, ...S.destructive },
            children: t('account.signIn')
          })
        : null,
      // D-027: an account with no banked reset says so in a muted line.
      credits.length
        ? null
        : jsx('span', { ...attrs(measure, { 'data-rr-no-resets': 'true' }), style: S.muted, children: t('noResets') }),
      ...credits.map(credit =>
        jsx(CreditRow, { credit, booked, now, lang, t, onRedeem: c => onRedeem(c, account), measure }, credit.credit_key)
      ),
      quota ? jsx('span', { ...attrs(measure, { 'data-rr-quota': 'true' }), style: S.quota, children: quota }) : null
    ]
  })
}

function bannerOf(status, t) {
  if (!status) return null
  const failed = failedAccounts(status)
  if (failed > 0) return { kind: 'signIn', text: t('banner.signIn', failed), error: true }
  if (setupProblem(status)) return { kind: 'setup', text: t('banner.setup'), error: true }
  if (status.dry_run) return { kind: 'dryRun', text: t('banner.dryRun'), error: false }
  // D-023: the user turned automatic redemption off; say so, do not alarm.
  if (status.auto_redeem_enabled === false) return { kind: 'autoOff', text: t('banner.autoOff'), error: false }
  return null
}

function emptyText(status, t) {
  if (noAccount(status)) return t('empty.noAccount')
  const count = accountCount(status)
  const hours = status?.settings?.scan_interval_hours
  if (count > 0 && hours > 0) return t('empty.scanning', count, hours)
  return t('empty.plain')
}

function footerText(status, t) {
  const settings = status?.settings ?? {}
  const parts = []
  if (settings.scan_interval_hours > 0) parts.push(t('footer.scans', settings.scan_interval_hours))
  if (settings.notify_target_display) parts.push(t('footer.notices', settings.notify_target_display))
  // `typeof NaN === 'number'`: a finite check is what keeps a malformed
  // payload from rendering "Early rescue ≤ NaN%".
  const threshold = settings.early_redeem_remaining_pct
  if (settings.early_redeem_enabled === true && Number.isFinite(threshold)) parts.push(t('footer.early', threshold))
  if (settings.auto_redeem_enabled === false) parts.push(t('footer.autoOff'))
  if (status?.dry_run) parts.push(t('footer.dryRun'))
  return parts.join(t('sep'))
}

function Header({ status, scan, view, setView, onScan, onSettings, lang, t, measure = false }) {
  const banner = bannerOf(status, t)
  const checked = formatRecent(status?.schedule?.last_scan_at, lang)
  const inHistory = view === 'history'
  const inSettings = view === 'settings'
  return jsxs('header', {
    style: S.header,
    children: [
      jsxs('div', {
        style: S.headRow,
        children: [
          jsx('h2', { style: S.title, children: inSettings ? t('settings.title') : t('popover.title') }),
          jsxs('div', {
            style: S.actions,
            children: [
              jsx(Button, {
                variant: 'secondary',
                size: 'xs',
                ...attrs(measure, { 'data-rr-scan': scan.busy ? 'busy' : 'idle' }),
                loading: scan.busy,
                disabled: scan.busy,
                onClick: onScan,
                children: t('popover.scanNow')
              }),
              inSettings
                ? null
                : jsx(Button, {
                    variant: 'text',
                    size: 'micro',
                    ...attrs(measure, { 'data-rr-view': inHistory ? 'back' : 'history' }),
                    onClick: () => setView(inHistory ? 'list' : 'history'),
                    children: inHistory ? t('popover.back') : t('popover.history')
                  }),
              // D-025: the tab lives inside the popup; the same control goes
              // back, so the popover keeps one shape on every view.
              jsx(Button, {
                variant: 'text',
                size: 'micro',
                ...attrs(measure, { 'data-rr-settings': inSettings ? 'back' : 'open' }),
                onClick: () => (inSettings ? setView('list') : onSettings()),
                children: inSettings ? t('popover.back') : t('popover.settings')
              })
            ]
          })
        ]
      }),
      status
        ? jsx('span', { ...attrs(measure, { 'data-rr-checked': 'true' }), style: S.muted, children: checked ? t('popover.checked', checked) : t('popover.neverChecked') })
        : null,
      banner
        ? jsx('span', { ...attrs(measure, { 'data-rr-banner': banner.kind }), style: banner.error ? { ...S.muted, ...S.destructive } : S.secondary, children: banner.text })
        : null,
      scan.note ? jsx('span', { ...attrs(measure, { 'data-rr-scan-note': scan.note }), style: S.secondary, children: t(`scan.${scan.note}`) }) : null
    ]
  })
}

function SortControl({ sort, setSort, t, measure = false }) {
  return jsx('div', {
    ...attrs(measure, { 'data-rr-sort': sort }),
    style: S.row,
    children: SORTS.map(id =>
      jsx(
        Button,
        {
          variant: id === sort ? 'textStrong' : 'text',
          size: 'micro',
          'aria-pressed': id === sort ? 'true' : 'false',
          ...attrs(measure, { 'data-rr-sort-option': id }),
          onClick: () => setSort(id),
          children: t(`sort.${id}`)
        },
        id
      )
    )
  })
}

function historyText(item, labels, lang, t) {
  const action = Object.hasOwn(HISTORY_ACTIONS, item.action) ? HISTORY_ACTIONS[item.action] : 'other'
  const parts = [formatRecent(item.at, lang), t(`history.action.${action}`)]
  const label = item.account_key ? labels.get(item.account_key) : null
  if (label) parts.push(label)
  if (HISTORY_OUTCOMES.has(item.outcome)) parts.push(t(`history.outcome.${item.outcome}`))
  return parts.filter(Boolean).join(t('sep'))
}

function HistoryView({ ctx, status, lang, t }) {
  const { items, phase } = useHistory(ctx)
  if (phase === 'error') return jsx('p', { 'data-rr-history-error': 'true', style: S.para, children: t('history.unreachable') })
  if (!items) return jsx(Loading, { t })
  if (!items.length) return jsx('p', { 'data-rr-history-empty': 'true', style: S.para, children: t('history.empty') })
  const labels = new Map((status?.accounts ?? []).map(a => [a.account_key, a.label]))
  return jsx('ul', {
    'data-rr-history': 'true',
    style: S.list,
    children: items.map((item, i) =>
      jsx('li', { 'data-rr-history-item': item.action || 'other', style: S.secondary, children: historyText(item, labels, lang, t) }, `${item.at}-${i}`)
    )
  })
}

/** D-025: one setting row. The control saves its own change; there is no
 *  Save button anywhere in the tab. */
function SettingRow({ field, note, onSave, t, measure = false }) {
  const [text, setText] = useState(() => (field.value == null ? '' : String(field.value)))
  const timer = useRef(null)
  const save = useCallback(value => onSave(field.key, value), [field.key, onSave])

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])
  // D-033 (review r6): a reconcile or a refused save changes the value under
  // the input; follow it unless a keystroke is still waiting to be saved.
  useEffect(() => {
    if (!timer.current) setText(field.value == null ? '' : String(field.value))
  }, [field.value])

  const debounced = useCallback(
    value => {
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => {
        timer.current = null
        save(value)
      }, SAVE_DEBOUNCE_MS)
    },
    [save]
  )

  const flush = useCallback(
    value => {
      if (timer.current) clearTimeout(timer.current)
      timer.current = null
      save(value)
    },
    [save]
  )

  const noteEl = jsx(SaveNote, { note, t })

  let control
  if (field.type === 'bool') {
    control = jsx(Button, {
      variant: field.value ? 'secondary' : 'text',
      size: 'micro',
      ...attrs(measure, { 'data-rr-toggle': field.key }),
      'aria-pressed': field.value ? 'true' : 'false',
      style: S.toggle,
      onClick: () => save(!field.value),
      children: field.value ? t('settings.on') : t('settings.off')
    })
  } else if (field.type === 'int') {
    control = jsx('input', {
      type: 'number',
      ...attrs(measure, { 'data-rr-number': field.key }),
      min: field.min,
      max: field.max,
      step: 1,
      value: text,
      style: S.number,
      onChange: event => {
        const next = event.target.value
        setText(next)
        // An empty or half-typed field is not a save: wait for a number.
        if (/^\d+$/.test(next)) debounced(Number(next))
      },
      onBlur: () => {
        if (/^\d+$/.test(text) && Number(text) !== field.value) flush(Number(text))
      }
    })
  } else if (field.type === 'enum') {
    control = jsx('select', {
      ...attrs(measure, { 'data-rr-select': field.key }),
      value: field.value,
      style: S.select,
      onChange: event => save(event.target.value),
      children: (field.choices ?? []).map(choice =>
        jsx('option', { value: choice, children: choice === 'auto' ? t('settings.choices.auto') : choice }, choice)
      )
    })
  } else {
    control = jsx('input', {
      type: 'text',
      ...attrs(measure, { 'data-rr-text': field.key }),
      value: text,
      style: S.text,
      onChange: event => {
        setText(event.target.value)
        debounced(event.target.value)
      },
      onBlur: () => {
        if (text !== String(field.value ?? '')) flush(text)
      }
    })
  }

  return jsxs('div', {
    ...attrs(measure, { 'data-rr-setting': field.key }),
    style: S.setting,
    children: [
      jsxs('div', {
        style: S.settingHead,
        children: [
          jsx('span', { ...attrs(measure, { 'data-rr-setting-label': 'true' }), style: S.settingLabel, children: t(`settings.labels.${field.key}`) }),
          jsxs('span', { style: S.settingControl, children: [noteEl, control] })
        ]
      }),
      jsx('p', { ...attrs(measure, { 'data-rr-setting-hint': 'true' }), style: S.settingHint, children: field.key === 'mask_labels' ? t('settings.maskExample') : t(`settings.help.${field.key}`) })
    ]
  })
}

/** The inline note of a setting or an account row. A click applies at once and
 *  says nothing (Hermes Desktop's own settings): the row stays silent while a
 *  write runs (`saving`) and after it lands (`saved`). Text appears only when
 *  something is wrong: Checking… once a write has not settled after
 *  SAVE_QUIET_MS (`late`), Not confirmed yet, or Not saved: <reason>. */
function SaveNote({ note, t }) {
  if (!note || note.kind === 'saving' || note.kind === 'saved' || (note.kind === 'checking' && !note.late)) return null
  const failed = note.kind === 'failed'
  return jsx('span', {
    'data-rr-saved': failed ? undefined : note.kind,
    'data-rr-error': failed ? 'true' : undefined,
    style: failed ? { ...S.settingNote, ...S.destructive, ...S.ellipsis } : { ...S.settingNote, ...S.ellipsis },
    children: failed && note.reason ? t('settings.notSaved', note.reason) : t(`settings.${note.kind}`)
  })
}

/** D-030: the two-option control at the top of the Settings tab. */
function ScopeControl({ scope, setScope, t }) {
  return jsx('div', {
    'data-rr-scope': scope,
    role: 'group',
    'aria-label': t('scope.label'),
    style: S.scope,
    children: ['all', 'account'].map(id =>
      jsx(
        Button,
        {
          variant: id === scope ? 'secondary' : 'text',
          size: 'xs',
          'aria-pressed': id === scope ? 'true' : 'false',
          'data-rr-scope-option': id,
          onClick: () => setScope(id),
          children: t(`scope.${id}`)
        },
        id
      )
    )
  })
}

/** One account entry from GET /settings `accounts` (or the StatusDTO account). */
function accountEntry(raw, statusAccount) {
  const base = statusAccount ?? {}
  return {
    account_key: raw.account_key,
    label: raw.label ?? base.label ?? '',
    excluded: raw.excluded ?? base.excluded,
    overrides: overridesOf(raw.overrides ? raw : base),
    effective: { ...(base.effective ?? {}), ...(raw.effective ?? {}) },
    monitored: raw.monitored ?? base.monitored,
    // D-034: the engine's `monitored` includes the account's own Watch value;
    // it is the inherited one only when it was computed without one.
    monitoredInherited: overridesOf(raw.monitored !== undefined ? raw : base).monitored === undefined
  }
}

/** D-034: the account keys and masked labels in Excluded accounts. */
function excludedList(value) {
  const items = Array.isArray(value) ? value : typeof value === 'string' ? value.split(',') : []
  return items.map(item => String(item).trim()).filter(Boolean)
}

/** D-028/D-034: whether Excluded accounts names this account. The engine decides
 *  it (one matcher for account keys and masked labels) and reports it per
 *  account as `excluded`, so the popup never re-masks a label. An engine without
 *  the flag gets the literal check on the key or the masked label it sent. */
export function isExcluded(entry, excluded) {
  if (typeof entry.excluded === 'boolean') return entry.excluded
  if (!Array.isArray(excluded) || !excluded.length) return false
  return excluded.includes(entry.account_key) || excluded.includes(entry.label)
}

/** The global values a Default segment falls back to, from the Settings
 *  fields (the tab's own, pending global saves included). */
/** The inherited value of Reminders when no global one is given. The Settings
 *  form has no global reminders field (D-034: the hours are shared, the switch
 *  lives per account); the account default is On (reset_rescue/settings.py). */
const GLOBAL_REMINDERS_DEFAULT = true

function globalsOf(fields, settings) {
  const globals = {}
  const bounds = { min: 0, max: 100 }
  for (const field of fields ?? []) {
    if (field.key === THRESHOLD_FIELD) {
      globals[THRESHOLD_FIELD] = field.value
      if (Number.isFinite(field.min)) bounds.min = field.min
      if (Number.isFinite(field.max)) bounds.max = field.max
    } else if (field.type === 'bool') globals[field.key] = field.value
    else if (field.key === 'excluded_accounts') globals.excluded = excludedList(field.value)
  }
  if (!Number.isFinite(globals[THRESHOLD_FIELD])) globals[THRESHOLD_FIELD] = settings?.[THRESHOLD_FIELD]
  if (typeof globals.reminders_enabled !== 'boolean') globals.reminders_enabled = GLOBAL_REMINDERS_DEFAULT
  return { globals, bounds }
}

/** An account with the own values of `clear` dropped: what it shows at once,
 *  before the engine answers. Each cleared field takes the inherited global
 *  value, so a Default label and the threshold follow the global (r10). */
function clearedEntry(entry, clear, globals) {
  const overrides = { ...entry.overrides }
  const effective = { ...entry.effective }
  let { monitored, monitoredInherited } = entry
  for (const field of clear) {
    delete overrides[field]
    if (field === 'monitored') {
      monitored = !isExcluded(entry, globals.excluded)
      monitoredInherited = true
    } else if (globals[field] !== undefined && globals[field] !== null) effective[field] = globals[field]
  }
  return { ...entry, overrides, effective, monitored, monitoredInherited }
}

/** Reset all accounts (POST /settings/account/reset-all) clears every override
 *  and the excluded_accounts list (plugin_api.py _reset_settings), so each
 *  account follows the globals at once: no own values, not excluded, watched. */
function resetAllView(view) {
  const fields = view.fields.map(f => (f.key === 'excluded_accounts' ? { ...f, value: '' } : f))
  const cleared = { ...view, fields }
  const { globals } = globalsOf(fields)
  const entries = view.entries.map(e => ({
    ...clearedEntry(e, Object.keys(e.overrides), globals),
    excluded: false,
    monitored: true,
    monitoredInherited: true
  }))
  return { ...cleared, entries }
}

/** The value an account gets for `field` when the cell says Global: the
 *  engine's effective value while no override is set, else the global one. */
function inheritedOf(entry, field, globals) {
  const free = entry.overrides[field] === undefined
  if (field === 'monitored') {
    // Global Watch follows Excluded accounts; the engine reports the result
    // when it computed it without the account's own value (D-034).
    if (free && entry.monitoredInherited && typeof entry.monitored === 'boolean') return entry.monitored
    return !isExcluded(entry, globals.excluded)
  }
  if (free && typeof entry.effective[field] === 'boolean') return entry.effective[field]
  if (typeof globals[field] === 'boolean') return globals[field]
  return field !== 'early_redeem_enabled'
}

/** Watched or paused, after the account's own Watch override. */
function watchedOf(entry, globals) {
  const own = entry.overrides.monitored
  return typeof own === 'boolean' ? own : inheritedOf(entry, 'monitored', globals)
}

/** D-034: the four options of an expanded account, in display order. */
export const ACCOUNT_OPTIONS = ['auto_redeem_enabled', 'early_redeem_enabled', 'reminders_enabled', 'monitored']

/** D-034: the segment a value shows, and the value a segment picks. */
const SEGMENT_OF = value => (value === true ? 'on' : value === false ? 'off' : 'default')
const VALUE_OF = { default: null, on: true, off: false }

/** D-034: three plain buttons with the SegmentedControl semantics, for a host
 *  whose SDK has no SegmentedControl (detected once, at load). */
export function SegmentedFallback({ options, value, onChange, disabled = false }) {
  return jsx('div', {
    style: S.segFallback,
    children: options.map(({ id, label }) =>
      jsx(
        Button,
        {
          variant: id === value ? 'secondary' : 'text',
          size: 'micro',
          'aria-pressed': id === value ? 'true' : 'false',
          disabled,
          style: S.toggle,
          onClick: () => onChange(id),
          children: label
        },
        id
      )
    )
  })
}

/** Without the SDK Tip a label simply has no tooltip (never a native title). */
function TipFallback({ children }) {
  return children ?? null
}

const SegmentedControl = SDK.SegmentedControl ?? SegmentedFallback
const Tip = SDK.Tip ?? TipFallback
/** D-034: which SDK pieces this host provides (the rest use the fallbacks). */
export const SDK_UI = { segmented: SDK.SegmentedControl != null, tip: SDK.Tip != null }

function ChevronIcon({ open }) {
  return jsx('svg', {
    'aria-hidden': 'true',
    viewBox: '0 0 12 12',
    width: 12,
    height: 12,
    style: { ...S.chevron, transform: open ? 'rotate(90deg)' : 'none' },
    children: jsx('path', { d: 'M4.5 2.5 8 6l-3.5 3.5', fill: 'none', stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round', strokeLinejoin: 'round' })
  })
}

/** D-034: one option of an expanded account: its label (SDK Tip with the
 *  one-sentence description) and a SegmentedControl Default (inherited) /
 *  On / Off. One click picks the final value; no cycling. */
function AccountOption({ entry, field, globals, onSave, t }) {
  const own = entry.overrides[field]
  const segment = SEGMENT_OF(own)
  const inherited = inheritedOf(entry, field, globals)
  const label = t(`perAccount.options.${field}`)
  const options = [
    { id: 'default', label: t('perAccount.segment.default', inherited ? t('perAccount.segment.on') : t('perAccount.segment.off')) },
    { id: 'on', label: t('perAccount.segment.on') },
    { id: 'off', label: t('perAccount.segment.off') }
  ]
  return jsxs('div', {
    'data-rr-option': field,
    'data-rr-state': segment === 'default' ? 'global' : segment,
    'data-rr-inherited': inherited ? 'on' : 'off',
    role: 'group',
    'aria-label': label,
    style: S.option,
    children: [
      jsx(
        Tip,
        {
          label: t(`perAccount.tips.${field}`),
          children: jsx('span', { 'data-rr-option-label': segment === 'default' ? 'inherited' : 'own', style: segment === 'default' ? S.optionInherited : S.optionOwn, children: label })
        },
        'label'
      ),
      jsx(
        'div',
        {
          'data-rr-segments': 'true',
          style: S.segments,
          children: jsx(SegmentedControl, {
            options,
            value: segment,
            sizing: 'content',
            onChange: id => {
              if (!(id in VALUE_OF) || id === segment) return
              onSave(entry.account_key, field, VALUE_OF[id])
            }
          })
        },
        'segments'
      )
    ]
  })
}

/** D-034: the early threshold of an account, shown while early redeem is
 *  effectively on. Empty = Default (the global value is the placeholder);
 *  saved 600 ms after the last keystroke or on blur. */
function ThresholdInput({ entry, globals, bounds, onSave, t }) {
  const own = entry.overrides[THRESHOLD_FIELD]
  const globalValue = own === undefined && Number.isFinite(entry.effective[THRESHOLD_FIELD]) ? entry.effective[THRESHOLD_FIELD] : globals[THRESHOLD_FIELD]
  const [text, setText] = useState(() => (own === undefined ? '' : String(own)))
  const timer = useRef(null)
  const save = useCallback(value => onSave(entry.account_key, THRESHOLD_FIELD, value), [entry.account_key, onSave])
  const cancel = () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
  }
  useEffect(() => cancel, [])
  // A reconcile or a refused save changes the value under the input; follow
  // it unless a keystroke is still waiting to be saved.
  useEffect(() => {
    if (!timer.current) setText(own === undefined ? '' : String(own))
  }, [own])
  const intent = value => {
    if (value === '') return own === undefined ? undefined : null
    if (!/^\d+$/.test(value) || Number(value) < bounds.min || Number(value) > bounds.max) return undefined
    return Number(value) === own ? undefined : Number(value)
  }
  const lead = t('perAccount.thresholdLead')
  return jsxs('label', {
    'data-rr-threshold': 'true',
    style: S.threshold,
    children: [
      jsx('span', { style: own === undefined ? S.optionInherited : S.optionOwn, children: lead }, 'lead'),
      jsx(
        'input',
        {
          type: 'number',
          'data-rr-number': THRESHOLD_FIELD,
          'aria-label': lead,
          min: bounds.min,
          max: bounds.max,
          step: 1,
          value: text,
          placeholder: Number.isFinite(globalValue) ? String(globalValue) : undefined,
          style: { ...S.number, width: 52 },
          onChange: event => {
            const next = event.target.value
            setText(next)
            cancel()
            const value = intent(next)
            if (value === undefined) return
            timer.current = setTimeout(() => {
              timer.current = null
              save(value)
            }, SAVE_DEBOUNCE_MS)
          },
          onBlur: () => {
            if (!timer.current) return
            cancel()
            const value = intent(text)
            if (value !== undefined) save(value)
          }
        },
        'n'
      ),
      jsx('span', { children: t('perAccount.thresholdUnit') }, 'unit')
    ]
  })
}

/** D-034: one account of the list. Collapsed: label, summary (Follows global
 *  or N custom settings, plus Paused) and a chevron; a click expands it. The
 *  save note stays on the row whether it is open or not. */
function AccountItem({ entry, expanded, focused, onToggle, note, globals, bounds, onSave, onReset, t }) {
  const paused = !watchedOf(entry, globals)
  const count = Object.keys(entry.overrides).length
  const summary = [count ? t('perAccount.customCount', count) : t('perAccount.followsGlobal')]
  if (paused) summary.push(t('perAccount.paused'))
  const early = entry.overrides.early_redeem_enabled ?? inheritedOf(entry, 'early_redeem_enabled', globals)
  // An account opened from an Accounts-tab tag scrolls into view inside the box.
  const reveal = useCallback(node => {
    if (node && focused) node.scrollIntoView?.({ block: 'nearest' })
  }, [focused])
  return jsxs('div', {
    ref: reveal,
    'data-rr-account-row': entry.account_key,
    'data-rr-paused': paused ? 'true' : undefined,
    'data-rr-expanded': expanded ? 'true' : 'false',
    style: S.acctRow,
    children: [
      jsxs(
        'button',
        {
          type: 'button',
          'data-rr-account-toggle': 'true',
          'aria-expanded': expanded ? 'true' : 'false',
          style: S.acctHead,
          onClick: () => onToggle(entry.account_key),
          children: [
            jsx('span', { 'data-rr-row-label': 'true', style: paused ? { ...S.acctLabel, ...S.paused } : S.acctLabel, children: entry.label }, 'label'),
            jsx('span', { 'data-rr-summary': 'true', style: S.acctSummary, children: summary.join(t('sep')) }, 'summary'),
            jsx(ChevronIcon, { open: expanded }, 'chevron')
          ]
        },
        'head'
      ),
      note ? jsx('div', { style: S.acctNote, children: jsx(SaveNote, { note, t }) }, 'note') : null,
      expanded
        ? jsxs(
            'div',
            {
              'data-rr-account-detail': 'true',
              style: S.acctDetail,
              children: [
                ...ACCOUNT_OPTIONS.flatMap(field => [
                  jsx(AccountOption, { entry, field, globals, onSave, t }, field),
                  field === 'early_redeem_enabled' && early ? jsx(ThresholdInput, { entry, globals, bounds, onSave, t }, 'threshold') : null
                ]),
                // Always inside the account; disabled with nothing to reset (r10).
                jsx(
                  Tip,
                  {
                    label: t('perAccount.resetTip'),
                    children: jsx(Button, {
                      variant: 'text',
                      size: 'micro',
                      'data-rr-account-reset': 'true',
                      disabled: !count,
                      style: { ...S.link, alignSelf: 'flex-start' },
                      onClick: () => {
                        if (count) onReset(entry.account_key)
                      },
                      children: t('perAccount.resetAccount')
                    })
                  },
                  'reset'
                )
              ]
            },
            'detail'
          )
        : null
    ]
  })
}

/** The accounts in label order, paused accounts last (D-030, kept by D-034). */
function accountOrder(entries, globals) {
  const paused = e => !watchedOf(e, globals)
  const sorted = [...entries].sort((a, b) => String(a.label).localeCompare(String(b.label)))
  return [...sorted.filter(e => !paused(e)), ...sorted.filter(paused)]
}

// D-033: a reset button waits this long for its second click.
const CONFIRM_WINDOW_MS = 5_000
// D-033 (review r6): a settings write with no reply after SAVE_DEADLINE_MS
// has an unknown result, not a failed one: the server may still write it. The
// row reads Checking… (from SAVE_QUIET_MS on) and the queue waits for the reply up to SAVE_CAP_MS.
// Only a 2xx reply or an answered GET /settings confirms it (review r7).
const SAVE_DEADLINE_MS = 10_000
const SAVE_CAP_MS = 60_000
// A click shows its value at once and no note. Only a write that has not
// settled SAVE_QUIET_MS after its click (still in the queue, in flight, or its
// result unknown) shows the quiet Checking… note on its row.
const SAVE_QUIET_MS = 3_000
// D-033 (review r7): a write whose result is unknown is checked with
// GET /settings for as long as the popup shows it: first after
// RECONCILE_FIRST_MS, then every RECONCILE_RETRY_MS. SAVE_CAP_MS of wall clock
// after the send (Date.now, request time included) with no answer, the row
// reads Not confirmed yet and the checks go on.
const RECONCILE_FIRST_MS = 2_000
const RECONCILE_RETRY_MS = 5_000

/** The HTTP status of a failed `ctx.rest`, or 0 when there was no answer. The
 *  bridge rejects with `<status>: <body>`. */
function httpStatus(err) {
  const text = String(err?.message ?? '')
  return Number(err?.status ?? err?.statusCode) || Number(/\b([45]\d\d)\b/.exec(text)?.[1]) || 0
}

/** D-033 (review r6): true when a failed write may still have been applied:
 *  the client deadline, no HTTP answer at all, or a 504 (the server answers
 *  504 while its write keeps running). */
function outcomeUnknown(err) {
  if (err?.rrTimeout) return true
  const status = httpStatus(err)
  return !status || status === 504
}

const deadlineError = () => Object.assign(new Error('deadline'), { rrTimeout: true })

/** D-033: the short reason a save failed. A JSON body `{error, detail}` gives
 *  the detail. E-mail addresses are masked and the text is capped. */
function failReason(err, t) {
  if (err?.rrTimeout) return t('saveError.timeout')
  const text = String(err?.message ?? '')
  let detail = null
  const brace = text.indexOf('{')
  if (brace >= 0) {
    try {
      const body = JSON.parse(text.slice(brace))
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      detail = null
    }
  }
  const status = httpStatus(err)
  if (detail && detail.trim() && detail !== 'Not Found') {
    const clean = detail.replace(/[^\s@]+@[^\s@]+/g, '***').replace(/\s+/g, ' ').trim()
    return clean.length > 80 ? `${clean.slice(0, 79)}…` : clean
  }
  if (status === 404) return t('saveError.notFound')
  if (status) return t('saveError.http', status)
  return t('saveError.offline')
}

/**
 * D-033 (review r6 P1-01, P1-02): the one queue for every settings write of
 * the popup: per-account options and account resets, the single-account notice, the
 * global rows and both reset buttons. `/redeem` and `/scan` stay outside it.
 *
 * At most one POST is in flight, in click order. A target (one cell, one
 * global key, one reset) keeps only its latest queued intent: a newer one
 * replaces it and moves to the end. Each job's `onOutcome` receives, in turn:
 *
 *   checking    no reply after SAVE_DEADLINE_MS; the request is still open
 *   ok          the reply, `{ result, late }` (`late` after checking)
 *   error       a definite HTTP refusal, `{ err }`
 *   unknown     no reply within SAVE_CAP_MS, or an answer that cannot tell
 *               (no HTTP status, 504): `{ err, sentAt }`
 *   reply       an answer that came after `unknown`, `{ reply }`; the
 *               request's handler stays attached, so a late write is applied
 *   superseded  a newer intent for the same target replaced it unsent
 *   dropped     its owner was no longer active when its turn came, or it
 *               waited and its owner cannot be compared
 *
 * The queue waits for the promise `onOutcome` returns, so the check of an
 * unknown write ends (or times out) before the next write goes out. Nothing
 * is retried.
 *
 * D-033 (review r7 P1-02, r8 P1): every job carries `owner`, the full
 * (connectionId, profile) it was clicked in (`ownerKey`); jobs coalesce per
 * (owner, target). The host routes each request to the connection and profile
 * active when it is made (pluginRest's profileScoped()/connectionScoped()), so
 * a job whose owner is no longer active is not sent, and neither is any other
 * queued job of that owner. A job that waited behind another write and whose
 * owner cannot be compared (no connection: `ownerKnown`) is not sent either.
 * `owner()` reads the active owner.
 */
export function createSaveQueue(ctx, { owner = currentOwner } = {}) {
  const queued = []
  const drainHooks = new Set()
  let running = false

  const deliver = async (job, outcome) => {
    try {
      await job.onOutcome?.(outcome)
    } catch {
      // A view that went away must not stall the queue.
    }
  }
  const timer = ms => {
    let handle = null
    const promise = new Promise(resolve => {
      handle = setTimeout(() => resolve(null), ms)
    })
    return { promise, stop: () => clearTimeout(handle) }
  }
  const sameSlot = (a, b) => a.owner === b.owner && a.target === b.target
  // A reply the client can act on, or `unknown` when it cannot tell.
  const settled = (job, reply, sentAt, late) => {
    if (reply.kind === 'error' && outcomeUnknown(reply.err)) return deliver(job, { kind: 'unknown', err: reply.err, sentAt })
    return deliver(job, reply.kind === 'ok' ? { ...reply, late } : reply)
  }

  async function send(job) {
    const sentAt = Date.now()
    let call
    try {
      call = Promise.resolve(ctx.rest(job.path, { method: 'POST', body: job.body, timeoutMs: SAVE_CAP_MS }))
    } catch (err) {
      call = Promise.reject(err)
    }
    const request = call.then(
      result => ({ kind: 'ok', result }),
      err => ({ kind: 'error', err })
    )
    const deadline = timer(SAVE_DEADLINE_MS)
    const quick = await Promise.race([request, deadline.promise])
    deadline.stop()
    if (quick) return settled(job, quick, sentAt, false)
    deliver(job, { kind: 'checking' })
    const cap = timer(SAVE_CAP_MS - SAVE_DEADLINE_MS)
    const late = await Promise.race([request, cap.promise])
    cap.stop()
    if (late) return settled(job, late, sentAt, true)
    // No reply in the cap. The answer, if one comes, still reaches the view.
    // The cap has passed by the timers even if the wall clock says less.
    request.then(reply => deliver(job, { kind: 'reply', reply }))
    return deliver(job, { kind: 'unknown', err: deadlineError(), sentAt: Math.min(sentAt, Date.now() - SAVE_CAP_MS) })
  }

  async function pump() {
    if (running) return
    running = true
    try {
      while (queued.length) {
        const job = queued.shift()
        if (job.owner !== undefined && (job.owner !== owner() || (job.waited && !ownerKnown(job.owner)))) {
          // Another owner is active (or this one cannot be told from another):
          // this job and every queued job of its owner stay unsent (the user
          // sees that owner's engine values when they return).
          const stale = [job]
          for (let i = queued.length - 1; i >= 0; i -= 1) if (queued[i].owner === job.owner) stale.push(...queued.splice(i, 1))
          for (const gone of stale) deliver(gone, { kind: 'dropped' })
          continue
        }
        await send(job)
      }
    } finally {
      running = false
    }
    for (const hook of [...drainHooks]) hook()
  }

  return {
    submit(job) {
      const at = queued.findIndex(j => sameSlot(j, job))
      if (at >= 0) deliver(queued.splice(at, 1)[0], { kind: 'superseded' })
      // `waited`: a write is ahead of it, so it goes out later than its click.
      queued.push({ ...job, waited: running })
      pump()
    },
    busy: () => running,
    onDrain(hook) {
      drainHooks.add(hook)
      return () => drainHooks.delete(hook)
    }
  }
}

const ACCOUNT_RESET_PATH = '/settings/account/reset'

/** D-030: the StatusDTO in a per-account reply (a {status} wrapper too), or null. */
function statusOf(result) {
  return Array.isArray(result?.accounts) ? result : Array.isArray(result?.status?.accounts) ? result.status : null
}

/** One account's override map with `field` set, or cleared when `value` is null. */
function withOverride(overrides, field, value) {
  const next = { ...overrides }
  if (value === null) delete next[field]
  else next[field] = value
  return next
}

/** The `account_key` row of a GET /settings payload, or null. */
function payloadAccount(body, key) {
  return Array.isArray(body?.accounts) ? body.accounts.find(a => a?.account_key === key) ?? null : null
}

/** D-034: Settings > Per account, a list of accounts (accordion: one open at
 *  a time). Every option saves through `POST /settings/account`
 *  {account_key, field, value}; null clears it. The saves themselves run in
 *  SettingsView, through the popup's save queue. */
function PerAccountView({ entries, globals, bounds, focus, notes, onSave, onReset, footer, t }) {
  const [open, setOpen] = useState(focus ?? null)
  const onToggle = useCallback(key => setOpen(prev => (prev === key ? null : key)), [])

  if (!entries.length) return jsx('p', { 'data-rr-account-list-empty': 'true', style: { ...S.para, paddingTop: 10 }, children: t('perAccount.empty') })
  return jsxs('div', {
    'data-rr-account-list': 'true',
    style: { display: 'flex', flexDirection: 'column', paddingTop: 8 },
    children: [
      ...entries.map(entry =>
        jsx(
          AccountItem,
          {
            entry,
            expanded: open === entry.account_key,
            focused: entry.account_key === focus,
            onToggle,
            note: notes[`acct:${entry.account_key}`],
            globals,
            bounds,
            onSave,
            onReset,
            t
          },
          entry.account_key
        )
      ),
      footer
    ]
  })
}

/** D-030: one account that still has its own settings (left over after the
 *  others were removed): a one-line notice whose link resets it to global. */
function SingleNotice({ entry, note, onReset, t }) {
  const busy = note?.kind === 'saving' || note?.kind === 'checking'
  return jsxs('div', {
    'data-rr-single-notice': entry.account_key,
    style: S.singleNotice,
    children: [
      jsx('span', { style: { ...S.settingHint, ...S.ellipsis }, children: t('perAccount.singleNotice') }, 'text'),
      jsx(
        Button,
        { variant: 'text', size: 'micro', 'data-rr-single-reset': 'true', style: S.link, disabled: busy, onClick: () => onReset(entry.account_key), children: t('perAccount.singleReset') },
        'reset'
      ),
      jsx(SaveNote, { note, t }, 'note')
    ]
  })
}

/** D-033: a text link that asks before it acts: the first click turns it into
 *  Confirm for CONFIRM_WINDOW_MS, a second click in that window fires. */
function ConfirmLink({ attr, label, tip, busy, onConfirm, t }) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return undefined
    const timer = setTimeout(() => setArmed(false), CONFIRM_WINDOW_MS)
    return () => clearTimeout(timer)
  }, [armed])
  return withTip(false, armed ? t('reset.confirmTip') : tip, jsx(Button, {
    variant: 'text',
    size: 'micro',
    [attr]: 'true',
    'data-rr-confirming': armed ? 'true' : undefined,
    disabled: busy,
    style: armed ? { ...S.link, ...S.destructive } : S.link,
    onClick: () => {
      if (busy) return
      if (!armed) {
        setArmed(true)
        return
      }
      setArmed(false)
      onConfirm()
    },
    children: armed ? t('reset.confirm') : label
  }))
}

/** D-033: the reset line at the bottom of a Settings tab, with its save note. */
function ResetFooter({ attr, label, tip, note, onConfirm, t }) {
  return jsxs('div', {
    'data-rr-reset-footer': 'true',
    style: S.resetFooter,
    children: [
      jsx(ConfirmLink, { attr, label, tip, busy: note?.kind === 'saving' || note?.kind === 'checking', onConfirm, t }, 'link'),
      jsx('span', { 'data-rr-reset-note': 'true', style: { minWidth: 0, display: 'flex' }, children: jsx(SaveNote, { note, t }) }, 'note')
    ]
  })
}

/** The `defaults` map of a GET /settings payload ({key: scalar}), or null
 *  when the engine sends none (older engines) or something else. */
function defaultsOf(body) {
  const raw = body?.defaults
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const out = {}
  for (const [key, value] of Object.entries(raw)) {
    if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) out[key] = value
  }
  return Object.keys(out).length ? out : null
}

/** A Settings view with Restore defaults applied: every Global row named in
 *  `defaults` at its default, and every account that inherits it following. */
function withDefaults(view, defaults) {
  const has = key => Object.prototype.hasOwnProperty.call(defaults, key)
  return {
    ...view,
    fields: view.fields.map(field => (has(field.key) ? { ...field, value: defaults[field.key] } : field)),
    entries: view.entries.map(entry => {
      const inherited = Object.keys(entry.effective ?? {}).filter(key => has(key) && entry.overrides?.[key] === undefined)
      if (!inherited.length) return entry
      const effective = { ...entry.effective }
      for (const key of inherited) effective[key] = defaults[key]
      return { ...entry, effective }
    })
  }
}

/** Notes still waiting for an answer (or not confirmed yet) survive a reset; the others go. */
function busyNotes(notes) {
  return Object.fromEntries(Object.entries(notes).filter(([, n]) => n?.kind === 'saving' || n?.kind === 'checking' || n?.kind === 'unconfirmed'))
}

/** D-025: the in-popup settings tab. Every change saves immediately through
 *  `POST /settings`, which writes with Hermes' own `save_plugin_settings`.
 *  D-030: a switch at the top picks All accounts (these rows) or Per account
 *  (the account list, D-034); `focus` opens Per account with that account expanded.
 *  D-033 (review r6): `fields` and `entries` hold what the engine last
 *  confirmed; `overlays` the intents not confirmed yet, in click order. What
 *  the tab shows is the first with the second applied on top. */
function SettingsView({ ctx, queue, status, profile, focus = null, onSaved, onReload = null, t }) {
  // D-033 (review r7 P1-02, r8 P1): the owner this tab shows, the full
  // (connectionId, profile). A click records it on its job; a reply or a read
  // is applied only while it is still the active one.
  const owner = profile
  const ownerActive = useCallback(() => owner === undefined || currentOwner() === owner, [owner])
  const [fields, setFields] = useState(null)
  const [entries, setEntries] = useState([])
  const [overlays, setOverlays] = useState([])
  const [phase, setPhase] = useState('loading')
  const [notes, setNotes] = useState({})
  const [scope, setScope] = useState(focus ? 'account' : 'all')
  const [meta, setMeta] = useState({})
  const [gen, setGen] = useState(0)
  // What Restore defaults writes, from GET /settings `defaults`; null on an
  // older engine, whose reset then changes the rows only when it answers.
  const defaultsRef = useRef(null)
  // Per intent: the newest seq of each note and target, and the last answered.
  const book = useRef({ seq: 0, note: {}, target: {}, answered: {} })
  // The StatusDTO accounts fill in what GET /settings leaves out (D-030).
  const statusRef = useRef(status)
  statusRef.current = status

  // GET /settings: the account entries and the pool numbers (D-030). `full`
  // also takes the fields; a refresh after a global save keeps the tab's own.
  const reads = useRef(0)
  const live = useRef(true)
  // D-033 (review r7 P1-01): the writes whose result is unknown, by intent seq.
  const checks = useRef(new Map())
  const wake = useRef(null)
  // The SAVE_QUIET_MS timers of the intents still unsettled.
  const quiet = useRef(new Set())
  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
      for (const handle of quiet.current) clearTimeout(handle)
      quiet.current.clear()
      // Nothing more to check here: let the queue go on.
      for (const c of checks.current.values()) c.release()
      checks.current.clear()
      wake.current?.()
    }
  }, [])
  // A GET /settings payload (also the reply of both D-033 resets).
  const adoptPayload = useCallback((body, full) => {
    const known = new Map((statusRef.current?.accounts ?? []).map(a => [a.account_key, a]))
    const raw = Array.isArray(body?.accounts) ? body.accounts : [...known.values()]
    setEntries(raw.filter(a => a && a.account_key).map(a => accountEntry(a, known.get(a.account_key))))
    setMeta({ per_account_available: body?.per_account_available, pool_account_count: body?.pool_account_count })
    defaultsRef.current = defaultsOf(body)
    if (full) setFields(Array.isArray(body?.fields) ? body.fields : [])
  }, [])
  const load = useCallback(
    full => {
      const mine = ++reads.current
      return ctx.rest('/settings', { timeoutMs: STATUS_TIMEOUT_MS }).then(
        body => {
          if (!live.current || mine !== reads.current || !ownerActive()) return
          adoptPayload(body, full)
          verify.current(body, mine)
        },
        () => {
          if (full && live.current && mine === reads.current && ownerActive()) setPhase('error')
        }
      )
    },
    [ctx, adoptPayload, ownerActive]
  )
  useEffect(() => {
    load(true)
  }, [load])

  // One GET /settings. `{ body }` on an answer, `{ err }` on a failed read, and
  // null when a newer read, an unmounted view or another active profile made
  // it stale. Any read still in flight is older than a newer one and is dropped.
  const readEngine = useCallback(async () => {
    const mine = ++reads.current
    const stale = () => !live.current || mine !== reads.current || !ownerActive()
    let body = null
    try {
      body = await ctx.rest('/settings', { timeoutMs: STATUS_TIMEOUT_MS })
    } catch (err) {
      return stale() ? null : { err }
    }
    if (stale()) return null
    if (!body) return { err: new Error('empty settings reply') }
    adoptPayload(body, true)
    verify.current(body, mine)
    onReload?.()
    return { body }
  }, [ctx, adoptPayload, onReload, ownerActive])

  // D-033 (review r6): the engine's own values. null when the read fails too.
  const reconcile = useCallback(async () => (await readEngine())?.body ?? null, [readEngine])

  // The note and the overlay of the intent `seq` of `job`.
  // Each note carries its intent's seq, and `late` once SAVE_QUIET_MS passed.
  const noteFor = (job, seq) => value => {
    if (book.current.note[job.note] !== seq) return
    setNotes(prev => {
      const late = prev[job.note]?.seq === seq && prev[job.note].late === true
      return { ...prev, [job.note]: value && { ...value, seq, late } }
    })
  }
  const dropFor = (job, seq) => () => setOverlays(prev => prev.filter(o => !(o.target === job.target && o.seq === seq)))
  const endCheck = seq => {
    const c = checks.current.get(seq)
    if (!c) return
    checks.current.delete(seq)
    c.release()
  }

  // D-033 (review r7 P1-01): only a 2xx reply or an answered GET /settings
  // confirms a write. Every answered read issued after an unknown write was
  // sent is compared with its intent; a read that differs proves nothing while
  // the write may still land, so the intent stays on screen.
  const verify = useRef(null)
  verify.current = (body, mine) => {
    for (const [seq, c] of [...checks.current]) {
      if (mine <= c.mark || c.job.matches?.(body) !== true) continue
      endCheck(seq)
      c.drop()
      c.note({ kind: 'saved' })
    }
  }
  // SAVE_CAP_MS of wall clock after the send: Not confirmed yet. The queue goes
  // on; the checks too, while the tab is open.
  const expire = seq => {
    const c = checks.current.get(seq)
    if (!c || c.expired) return
    c.expired = true
    c.release()
    if (book.current.target[c.job.target] !== seq) {
      checks.current.delete(seq) // a newer intent for the target took over
      return
    }
    c.note({ kind: 'unconfirmed' })
  }
  const looping = useRef(false)
  const runChecks = useRef(null)
  runChecks.current = async () => {
    if (looping.current) {
      wake.current?.()
      return
    }
    looping.current = true
    try {
      while (live.current && checks.current.size && ownerActive()) {
        await readEngine()
        if (!live.current || !checks.current.size) break
        const now = Date.now()
        for (const [seq, c] of checks.current) {
          if (now - c.sentAt >= SAVE_CAP_MS) expire(seq)
        }
        let first = false
        for (const c of checks.current.values()) if (c.reads++ === 0) first = true
        await new Promise(resolve => {
          const handle = setTimeout(resolve, first ? RECONCILE_FIRST_MS : RECONCILE_RETRY_MS)
          wake.current = () => {
            clearTimeout(handle)
            resolve()
          }
        })
        wake.current = null
      }
    } finally {
      looping.current = false
    }
  }

  // One answer of the save queue for the intent `seq` of `job`.
  const settle = useRef(null)
  settle.current = async (job, seq, outcome) => {
    const b = book.current
    const note = noteFor(job, seq)
    const drop = dropFor(job, seq)
    if (outcome.kind === 'superseded') return
    // D-033 (review r7 P1-02): an answer for an owner that is no longer active
    // changes nothing here (its tab shows GET /settings when it comes back).
    if (!ownerActive()) return
    if (outcome.kind === 'dropped') {
      // D-033 (review r8): not sent while this tab is still the owner (the
      // owner cannot be compared): nothing was written, so the intent goes.
      if (live.current) {
        drop()
        note(undefined)
      }
      return
    }
    const reply = outcome.kind === 'reply' ? outcome.reply : null
    if (!live.current) {
      // The tab closed: still keep the Accounts tab current.
      if (outcome.kind === 'ok') job.whenGone?.(outcome.result)
      else if (reply?.kind === 'ok') job.whenGone?.(reply.result)
      else if (outcome.kind === 'unknown') onReload?.()
      return
    }
    if (outcome.kind === 'checking') {
      note({ kind: 'checking' })
      return
    }
    if (reply) {
      // The answer of a write that had none in the cap. Applied unless a newer
      // intent for the same target exists.
      if (b.target[job.target] !== seq) {
        endCheck(seq)
        return
      }
      if (reply.kind === 'ok') {
        endCheck(seq)
        reads.current += 1
        job.adopt(reply.result)
        drop()
        note({ kind: 'saved' })
      } else if (!outcomeUnknown(reply.err)) {
        endCheck(seq)
        drop()
        note({ kind: 'failed', reason: failReason(reply.err, t) })
      }
      return
    }
    b.answered[job.target] = Math.max(b.answered[job.target] ?? 0, seq)
    if (outcome.kind === 'ok') {
      reads.current += 1 // a GET /settings still in flight is older than this reply
      job.adopt(outcome.result)
      drop()
      note({ kind: 'saved' })
      // A reply after Checking…: read the engine once more for the other rows.
      if (outcome.late) readEngine()
      return
    }
    if (outcome.kind === 'error') {
      // A definite refusal: back to what the engine last confirmed.
      drop()
      note({ kind: 'failed', reason: failReason(outcome.err, t) })
      return
    }
    // Unknown: the intent stays on screen with Checking… while the engine is
    // read. The queue waits until it is confirmed, refused, or the cap passed.
    note({ kind: 'checking' })
    let release = null
    const gate = new Promise(resolve => {
      release = resolve
    })
    const sentAt = Number.isFinite(outcome.sentAt) ? outcome.sentAt : Date.now()
    const c = { job, sentAt, mark: reads.current, reads: 0, expired: false, note, drop, timer: null }
    c.release = () => {
      clearTimeout(c.timer)
      release()
    }
    checks.current.set(seq, c)
    const left = sentAt + SAVE_CAP_MS - Date.now()
    if (left <= 0) expire(seq)
    else c.timer = setTimeout(() => expire(seq), left)
    runChecks.current()
    return gate
  }

  const enqueue = useCallback(
    job => {
      if (!ownerActive()) return // the tab is about to show another profile
      const b = book.current
      const seq = ++b.seq
      b.note[job.note] = seq
      b.target[job.target] = seq
      if (job.overlay) setOverlays(prev => [...prev.filter(o => o.target !== job.target), { target: job.target, seq, apply: job.overlay }])
      setNotes(prev => ({ ...prev, [job.note]: { kind: 'saving', seq, late: false } }))
      // Not settled after SAVE_QUIET_MS: the row reads Checking…, quietly.
      const handle = setTimeout(() => {
        quiet.current.delete(handle)
        if (!live.current) return
        setNotes(prev => {
          const n = prev[job.note]
          if (n?.seq !== seq || (n.kind !== 'saving' && n.kind !== 'checking')) return prev
          return { ...prev, [job.note]: { ...n, kind: 'checking', late: true } }
        })
      }, SAVE_QUIET_MS)
      quiet.current.add(handle)
      queue.submit({ owner, target: job.target, path: job.path, body: job.body, onOutcome: outcome => settle.current(job, seq, outcome) })
    },
    [queue, owner, ownerActive]
  )

  // D-033 (review r6): once the queue drains, a target whose newest intent has
  // no answer (or a tab that opened while writes were still running) re-reads
  // the engine.
  const openedBusy = useRef(queue.busy())
  useEffect(
    () =>
      queue.onDrain(() => {
        if (!live.current) return
        const b = book.current
        const behind = Object.keys(b.target).filter(target => (b.answered[target] ?? 0) < b.target[target])
        if (!behind.length && !openedBusy.current) return
        openedBusy.current = false
        for (const target of behind) b.answered[target] = b.target[target]
        reconcile().then(body => {
          if (body && live.current) setOverlays(prev => prev.filter(o => !behind.includes(o.target)))
        })
      }),
    [queue, reconcile]
  )

  // The global values the tab showed last, for a reply that names no account.
  const globalsRef = useRef({})
  // The globals an overlay clears to: the view's fields, pending saves included.
  const viewGlobals = view => globalsOf(view.fields, statusRef.current?.settings).globals

  // A per-account reply: take the row from it and keep the Accounts tab
  // current. The per-account reply carries {account_key, overrides, effective,
  // status}: its own `effective` wins, the status row fills in the rest.
  const adoptAccount = useCallback(
    (key, fallback) => result => {
      const fresh = statusOf(result)
      const back = fresh?.accounts.find(a => a.account_key === key)
      const own = result?.account_key === key && result.effective && typeof result.effective === 'object' ? result : null
      const next = e => {
        if (own) {
          const raw = { account_key: key, label: back?.label ?? e.label, overrides: own.overrides ?? back?.overrides ?? {}, effective: own.effective }
          return accountEntry(raw, back ?? {})
        }
        if (back) return accountEntry(back, back)
        return fallback ? fallback(e) : e
      }
      setEntries(prev => prev.map(e => (e.account_key !== key ? e : next(e))))
      if (fresh) onSaved(fresh)
    },
    [onSaved]
  )
  const accountGone = result => {
    const fresh = statusOf(result)
    if (fresh) onSaved(fresh)
  }

  const onCellSave = useCallback(
    (key, field, value) =>
      enqueue({
        target: `acct:${key}:${field}`,
        note: `acct:${key}`,
        path: '/settings/account',
        body: { account_key: key, field, value },
        // A cleared value shows the inherited global at once (r10).
        overlay: view => ({
          ...view,
          entries: view.entries.map(e =>
            e.account_key !== key ? e : value === null ? clearedEntry(e, [field], viewGlobals(view)) : { ...e, overrides: withOverride(e.overrides, field, value) }
          )
        }),
        adopt: adoptAccount(key, e => (value === null ? clearedEntry(e, [field], globalsRef.current) : { ...e, overrides: withOverride(e.overrides, field, value) })),
        whenGone: accountGone,
        matches: body => {
          const a = payloadAccount(body, key)
          if (!a) return undefined
          const own = overridesOf(a)[field]
          return value === null ? own === undefined : own === value
        }
      }),
    [enqueue, adoptAccount]
  )
  const accountReset = (key, note, overlay) => ({
    target: `acct-reset:${key}`,
    note,
    path: ACCOUNT_RESET_PATH,
    body: { account_key: key },
    overlay,
    adopt: adoptAccount(key, e => clearedEntry(e, Object.keys(e.overrides), globalsRef.current)),
    whenGone: accountGone,
    matches: body => {
      const a = payloadAccount(body, key)
      return a ? Object.keys(overridesOf(a)).length === 0 : undefined
    }
  })
  // Resets show every cleared value as the inherited global at once (r10).
  const clearRow = key => view => ({
    ...view,
    entries: view.entries.map(e => (e.account_key === key ? clearedEntry(e, Object.keys(e.overrides), viewGlobals(view)) : e))
  })
  const onRowReset = useCallback(key => enqueue(accountReset(key, `acct:${key}`, clearRow(key))), [enqueue, adoptAccount])
  const onSingleReset = useCallback(key => enqueue(accountReset(key, `single:${key}`, clearRow(key))), [enqueue, adoptAccount])

  const onSave = useCallback(
    (key, value) =>
      enqueue({
        target: `global:${key}`,
        note: key,
        path: '/settings',
        body: { key, value },
        // D-030: a Global cell shows the inherited value, so every account
        // without its own value follows the new global at once.
        overlay: view => ({
          fields: view.fields.map(field => (field.key === key ? { ...field, value } : field)),
          entries: view.entries.map(entry =>
            entry.overrides[key] === undefined && key in entry.effective ? { ...entry, effective: { ...entry.effective, [key]: value } } : entry
          )
        }),
        adopt: result => {
          const saved = result?.value ?? value
          setFields(prev => (prev ?? []).map(field => (field.key === key ? { ...field, value: saved } : field)))
          setEntries(prev =>
            prev.map(entry =>
              entry.overrides[key] === undefined && key in entry.effective ? { ...entry, effective: { ...entry.effective, [key]: saved } } : entry
            )
          )
          // Then the engine's view of every account from GET /settings.
          load(false)
          if (result?.status) onSaved(result.status)
        },
        whenGone: result => {
          if (result?.status) onSaved(result.status)
        },
        matches: body => {
          const field = Array.isArray(body?.fields) ? body.fields.find(f => f.key === key) : null
          return field ? String(field.value) === String(value) : undefined
        }
      }),
    [enqueue, onSaved, load]
  )

  // D-033: Restore defaults (Global tab) and Reset all accounts to global
  // (Per-account tab). Both answer with the GET /settings payload; the tab
  // re-reads from it and remounts its rows, so no input keeps an old value.
  const runReset = useCallback(
    (which, path) => {
      // Restore defaults: the engine's `defaults` as read at the click. Without
      // them (an older engine) its reply sets the values.
      const defaults = which === 'account' ? null : defaultsRef.current
      return enqueue({
        target: `reset:${which}`,
        note: `reset:${which}`,
        path,
        body: {},
        // Reset all: every account follows the global values at once.
        // Restore defaults: every Global row shows its default at once, and
        // every account without its own value follows it.
        overlay:
          which === 'account'
            ? view => resetAllView(view)
            : defaults
              ? view => withDefaults(view, defaults)
              : null,
        adopt: body => {
          if (Array.isArray(body?.accounts)) adoptPayload(body, false)
          else load(false)
          if (Array.isArray(body?.fields)) setFields(body.fields)
          setNotes(busyNotes)
          setGen(prev => prev + 1)
          if (Array.isArray(body?.status?.accounts)) onSaved(body.status)
          else onReload?.()
        },
        whenGone: () => onReload?.(),
        // Restore defaults: every field the engine named is at its default
        // (without `defaults`, only its reply can confirm it). Reset all: no
        // account keeps its own values.
        matches:
          which === 'account'
            ? body => (Array.isArray(body?.accounts) ? body.accounts.every(a => Object.keys(overridesOf(a)).length === 0) : undefined)
            : defaults
              ? body => (Array.isArray(body?.fields) ? body.fields.every(f => !(f?.key in defaults) || String(f.value) === String(defaults[f.key])) : undefined)
              : undefined
      })
    },
    [enqueue, adoptPayload, load, onSaved, onReload]
  )

  // What the tab shows: the confirmed values with the pending intents on top.
  const shown = fields ? overlays.reduce((view, o) => o.apply(view), { fields, entries }) : { fields, entries }

  // The global values a Global cell falls back to, from the tab's own fields.
  const { globals, bounds } = globalsOf(shown.fields, status?.settings)
  globalsRef.current = globals
  // Order once per load, so a row does not jump while it is edited.
  const [ordered, setOrdered] = useState(null)
  useEffect(() => {
    if (fields && ordered === null) setOrdered(accountOrder(shown.entries, globals).map(e => e.account_key))
  }, [fields, entries, ordered])
  const byKey = new Map(shown.entries.map(e => [e.account_key, e]))
  const rows = ordered
    ? [...ordered.map(k => byKey.get(k)).filter(Boolean), ...shown.entries.filter(e => !ordered.includes(e.account_key))]
    : accountOrder(shown.entries, globals)

  const mode = perAccountMode(status, shown.entries, meta)
  const perAccount = mode === 'multi' && scope === 'account'
  const resetFooter = which =>
    jsx(
      ResetFooter,
      which === 'account'
        ? { attr: 'data-rr-reset-all', label: t('reset.allAccounts'), tip: t('reset.allAccountsTip'), note: notes['reset:account'], onConfirm: () => runReset(which, '/settings/account/reset-all'), t }
        : { attr: 'data-rr-reset-defaults', label: t('reset.defaults'), tip: t('reset.defaultsTip'), note: notes['reset:all'], onConfirm: () => runReset(which, '/settings/reset'), t },
      `reset-${which}`
    )

  if (phase === 'error') return jsx('p', { 'data-rr-settings-error': 'true', style: S.para, children: t('settings.loadFailed') })
  if (!shown.fields) return jsx(Loading, { t })
  const single = mode === 'single' ? shown.entries[0] : null
  return jsxs('div', {
    'data-rr-settings-tab': 'true',
    style: { display: 'flex', flexDirection: 'column', gap: 0 },
    children: [
      jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 2 },
        // The popup header already reads Settings: no second title (r10).
        children: [jsx('span', { style: S.muted, children: t('settings.hint') })]
      }),
      // D-030 single-account rule: the switch only with 2+ accounts; one
      // account with leftover overrides gets the reset notice instead.
      mode === 'multi' ? jsx(ScopeControl, { scope, setScope, t }, 'scope') : null,
      single ? jsx(SingleNotice, { entry: single, note: notes[`single:${single.account_key}`], onReset: onSingleReset, t }, 'single') : null,
      ...(perAccount
        ? [
            jsx(
              PerAccountView,
              { entries: rows, globals, bounds, focus, notes, onSave: onCellSave, onReset: onRowReset, footer: resetFooter('account'), t },
              `accounts-${gen}`
            )
          ]
        : [...shown.fields.map(field => jsx(SettingRow, { field, note: notes[field.key], onSave, t }, `${field.key}-${gen}`)), resetFooter('all')])
    ]
  })
}

function ListView({ status, phase, sort, setSort, now, lang, t, onRedeem, onTag, measure = false }) {
  if (!status && phase !== 'error') return jsx(Loading, { t, measure })
  if (!status) return jsx('p', { ...attrs(measure, { 'data-rr-error': 'true' }), style: S.para, children: t('popover.unreachable') })
  const groups = listedAccounts(status, sort)
  if (!groups.length) return jsx('p', { ...attrs(measure, { 'data-rr-empty': 'true' }), style: S.para, children: emptyText(status, t) })
  const lead = status.settings?.lead_minutes
  return jsxs(Fragment, {
    children: [
      groups.length > 1 ? jsx(SortControl, { sort, setSort, t, measure }, 'sort') : null,
      // D-030: tags only where Settings > Per account exists (2+ accounts).
      ...groups.map(group =>
        jsx(AccountSection, { group, now, lang, t, onRedeem, onTag: perAccountMode(status) === 'multi' ? onTag : null, measure }, group.account.account_key)
      )
    ]
  })
}

function chipLabel(chip, lang, t) {
  if (chip.kind === 'scanning') return [jsx(GlyphSpinner, {}, 'spin'), t('chip.scanning')]
  if (chip.kind === 'failed') return t('chip.failed')
  if (chip.kind === 'none') return t('chip.banked', 0)
  if (chip.kind === 'banked') return t('chip.banked', chip.banked)
  if (chip.kind === 'expiring') return t('chip.expiring', chip.banked, formatTime(chip.at, lang))
  if (chip.kind === 'bankedNext') {
    return t('chip.bankedNext', chip.banked, chip.soon ? formatTime(chip.at, lang) : formatDay(chip.at, lang))
  }
  return t('chip.idle')
}

function chipTip(chip, lang, t) {
  if (chip.kind === 'banked') return t('chipTip.banked', chip.banked)
  if (chip.kind === 'bankedNext') return t('chipTip.bankedNext', chip.banked, formatWhen(chip.at, lang))
  if (chip.kind === 'expiring') return t('chipTip.expiring', formatWhen(chip.at, lang))
  return t(`chipTip.${chip.kind}`)
}

// D-032: the chip is a native status-bar item. It carries Hermes' own
// status-bar item class (STATUSBAR_ACTION_CLASS,
// apps/desktop/src/app/shell/statusbar-controls.tsx:30-31 — the SDK exports
// no component or constant for it), so its font size, weight, line height,
// family and colour come from Hermes' stylesheet and follow Hermes' text
// settings. No inline font or colour, and no SDK Button (its text-xs leading-4
// font-medium and ghost colour made the chip larger and brighter than its
// neighbours). The warning and failed looks are the classes native items use:
// an update item's text-primary (use-statusbar-items.tsx:116) and the Agents
// item's failed text-destructive (use-statusbar-items.tsx:626-629).
const STATUSBAR_ITEM_CLASS =
  'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground disabled:cursor-default disabled:opacity-45'
const CHIP_STATE_CLASS = { failed: 'text-destructive hover:text-destructive', expiring: 'text-primary hover:text-primary' }

function readSort(ctx) {
  const saved = ctx.storage?.get('sort', 'soonest')
  return SORTS.includes(saved) ? saved : 'soonest'
}

/** The one account the measuring sample draws: two scheduled, redeemable
 *  resets and a quota line. Its text is not what gets measured (every line is
 *  nowrap), only its shape, which matches a real full account. */
function probeGroup(now) {
  const at = ms => new Date(ms).toISOString()
  const reset = (key, expiresH, fireH) => ({
    credit_key: key,
    reset_type: 'codex',
    granted_at: at(now - DAY_MS),
    expires_at: at(now + expiresH * HOUR_MS),
    state: 'available',
    plan: 'scheduled',
    primary: { role: 'primary', fire_at: at(now + fireH * HOUR_MS), job_state: 'armed' },
    backup: null,
    last_result: null,
    can_redeem_now: true
  })
  return {
    account: {
      account_key: 'probe',
      label: '***',
      included: true,
      reachable: true,
      last_error_code: null,
      last_checked_at: null,
      quota: { plan_type: 'plus', five_hour_remaining_pct: 100, weekly_remaining_pct: 100 },
      credits: []
    },
    credits: [reset('probe_1', 40, 20), reset('probe_2', 45, 25)]
  }
}

/** The measured sizes of the column, or null when the page has no layout
 *  engine (jsdom reports 0 px), so the fallback in ROW_LAYOUT stays. */
function measureLayout(scroll) {
  // The scroll box holds the column first, then the three hidden samples: the
  // full-account sample (D-029), the Accounts-view copy and the floor rows
  // (D-031). Walking children avoids DOM queries. The header comes from the
  // sample too, in its Accounts-tab form (D-030): a pt-BR header with three
  // buttons wraps, one with two does not, so measuring the live header gave
  // each tab its own height (and a remeasure on any resize changed it under the user).
  const [, sample, list, floorRows] = scroll.children
  if (!sample || sample.children.length < 4) return null
  const [header, sort, account, footerLine] = sample.children
  // getBoundingClientRect is in visual px, but the popup height is written in
  // CSS px. Under a zoomed root (a larger Desktop text size) the two differ, so
  // the ratio of a rect width to its CSS width gives the factor to undo.
  const scale = sample.getBoundingClientRect().width / (sample.offsetWidth || 1) || 1
  const height = el => el.getBoundingClientRect().height / scale
  const sizes = {
    header: height(header),
    sort: height(sort),
    account: height(account),
    footer: height(footerLine)
  }
  if (!(sizes.header > 0 && sizes.account > 0)) return null
  // D-031: the list copy's body runs from the item under its header to the
  // last item (rect edges, so the gaps between items are included).
  if (list && list.children.length > 1) {
    const first = list.children[1].getBoundingClientRect().top
    const last = list.lastElementChild.getBoundingClientRect().bottom
    sizes.listHeader = height(list.children[0])
    sizes.body = (last - first) / scale
  }
  const rows = floorRows?.firstElementChild
  if (rows) sizes.settings = height(rows)
  return sizes
}

const LAYOUT_KEYS = ['header', 'sort', 'footer', 'account', 'listHeader', 'body', 'settings']

function sameLayout(a, b) {
  return Boolean(a && b && LAYOUT_KEYS.every(key => a[key] === b[key]))
}

function sizesOf(layout) {
  return layout ? { ...ROW_LAYOUT, ...layout } : ROW_LAYOUT
}

function Chip({ ctx }) {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState('list')
  // D-030: the account whose row Settings > Per account opens expanded.
  const [focus, setFocus] = useState(null)
  const [sort, setSortState] = useState(() => readSort(ctx))
  const [confirm, setConfirm] = useState(null)
  // D-033 (review r6): one save queue for the whole popup; it outlives the
  // Settings tab, so a write still running keeps its place after a switch.
  const queueRef = useRef(null)
  if (!queueRef.current) queueRef.current = createSaveQueue(ctx)
  const saveQueue = queueRef.current
  const { status, phase, scan, runScan, reload, adopt, profile } = useRescue(ctx, open)
  const now = useNow(ctx, open)
  const { locale } = useI18n()
  const lang = pickLanguage(locale, status?.language)
  const t = useTranslator(lang)
  const chip = chipState(status, phase, { scanning: scan.busy })
  const tip = chipTip(chip, lang, t)
  const footer = status ? footerText(status, t) : ''
  // D-029: the height comes from the measured column (see ROW_LAYOUT).
  const [layout, setLayout] = useState(null)
  const scrollRef = useRef(null)
  const remeasure = useCallback(() => {
    const node = scrollRef.current
    const next = node ? measureLayout(node) : null
    if (next) setLayout(prev => (sameLayout(prev, next) ? prev : next))
  }, [])
  // Runs after each commit that changes the sample's text (language, status),
  // so the height follows the new text before the popover is painted.
  const attachScroll = useCallback(
    node => {
      scrollRef.current = node
      if (node) remeasure()
    },
    [remeasure, lang, status, scan.note]
  )
  useEffect(() => {
    if (!open || typeof ResizeObserver === 'undefined') return undefined
    const node = scrollRef.current
    if (!node) return undefined
    // The scroll box follows the window (its max height is 100vh - 80 px), so a
    // resize shows up as a size change on it.
    const observer = new ResizeObserver(remeasure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [open, remeasure])
  // D-031: clamp(natural, floor, cap). Before the first measurement the
  // natural height is the ROW_LAYOUT formula for the listed accounts.
  const listed = status ? listedAccounts(status, sort).length : 0
  const cap = popoverHeightFor(5, sizesOf(layout))
  const natural = layout?.body > 0 ? naturalHeightFor({ header: layout.listHeader, body: layout.body }) : popoverHeightFor(listed, sizesOf(layout))
  const height = clampHeight(natural, floorHeightFor(layout), cap)
  // The hidden sample, same width as the rows. It is never interactive
  // (visibility hidden) and carries no data-rr attributes except its own.
  const measureSample = jsxs('div', {
    'data-rr-measure': 'true',
    'aria-hidden': 'true',
    style: S.measure,
    children: [
      jsx(Header, { status, scan, view: 'list', setView: () => {}, onScan: () => {}, onSettings: () => {}, lang, t, measure: true }),
      jsx(SortControl, { sort: 'soonest', setSort: () => {}, t, measure: true }),
      jsx(AccountSection, {
        group: probeGroup(now),
        now,
        lang,
        t,
        onRedeem: () => {},
        measure: true
      }),
      jsx('div', { style: S.footer, children: t('footer.scans', 12) })
    ]
  })
  // D-031: a hidden copy of the Accounts view (header, list, footer) for the
  // natural height, whatever tab is open, and the first four setting rows for
  // the floor. Neither carries data-rr attributes beyond its own marker.
  const listSample = jsxs('div', {
    'data-rr-measure-list': 'true',
    'aria-hidden': 'true',
    style: S.measure,
    children: [
      jsx(Header, { status, scan, view: 'list', setView: () => {}, onScan: () => {}, onSettings: () => {}, lang, t, measure: true }, 'header'),
      jsx(ListView, { status, phase, sort, setSort: () => {}, now, lang, t, onRedeem: () => {}, onTag: () => {}, measure: true }, 'list'),
      footer ? jsx('div', { style: S.footer, children: footer }, 'footer') : null
    ]
  })
  const floorSample = jsx('div', {
    'data-rr-measure-floor': 'true',
    'aria-hidden': 'true',
    style: S.measure,
    children: jsx('div', {
      style: { display: 'flex', flexDirection: 'column' },
      children: FLOOR_FIELDS.map(key =>
        jsx(SettingRow, { field: { key, type: 'int', value: status?.settings?.[key] ?? null }, note: null, onSave: () => {}, t, measure: true }, key)
      )
    })
  })

  const onOpenChange = useCallback(next => {
    setOpen(next)
    if (!next) setView('list')
  }, [])
  const setSort = useCallback(
    id => {
      setSortState(id)
      ctx.storage?.set('sort', id)
    },
    [ctx]
  )
  // D-025: Settings is a tab inside the popup, not a jump to Hermes' form.
  const onSettings = useCallback(() => {
    setFocus(null)
    setView('settings')
  }, [])
  // D-030: a tag on the Accounts tab opens Settings > Per account on its row.
  const onTag = useCallback(key => {
    setFocus(key)
    setView('settings')
  }, [])
  // A save returns the fresh StatusDTO: adopt it so the list and the footer
  // show the new value without waiting for the next poll.
  const onSettingsSaved = useCallback(status => adopt(status, profile), [adopt, profile])
  // Opening a Dialog closes the Popover (spike S8): the dialog lives outside it.
  const onRedeem = useCallback(
    (credit, account) => {
      onOpenChange(false)
      setConfirm({ credit, label: account.label })
    },
    [onOpenChange]
  )
  const doRedeem = useCallback(async () => {
    const { credit, label } = confirm
    let result
    try {
      result = await ctx.rest('/redeem', {
        method: 'POST',
        body: { credit_key: credit.credit_key, confirm: true },
        timeoutMs: ACTION_TIMEOUT_MS
      })
    } catch (err) {
      throw new Error(isBusyError(err) ? t('scan.busy') : t('redeem.failed'))
    }
    if (DONE_OUTCOMES.has(result?.outcome)) host.notify({ kind: 'success', message: t('redeem.done', label) })
    else if (result?.outcome === 'dry_run') host.notify({ kind: 'info', message: t('redeem.dryRun') })
    else throw new Error(t('redeem.failed'))
    reload()
  }, [ctx, confirm, t, reload])

  return jsxs(Fragment, {
    children: [
      jsxs(Popover, {
        open,
        onOpenChange,
        children: [
          // D-034: the SDK Tip (a native status-bar item's tooltip,
          // statusbar-controls.tsx:330), stacked on the popover trigger.
          jsx(Tip, {
            label: tip,
            children: jsx(PopoverTrigger, {
              asChild: true,
              children: jsx('button', {
                type: 'button',
                className: CHIP_STATE_CLASS[chip.kind] ? `${STATUSBAR_ITEM_CLASS} ${CHIP_STATE_CLASS[chip.kind]}` : STATUSBAR_ITEM_CLASS,
                'data-rr-chip': chip.kind,
                'aria-label': tip,
                // The native label markup (statusbar-controls.tsx:251).
                children: jsx('span', { className: 'truncate', children: chipLabel(chip, lang, t) })
              })
            })
          }),
          jsx(PopoverContent, {
            side: 'top',
            align: 'end',
            style: { ...S.content, height },
            children: jsxs('div', {
              'data-rr-scroll': 'true',
              ref: attachScroll,
              style: S.scroll,
              children: [
              jsxs('div', {
                style: S.inner,
                children: [
                  jsx(Header, { status, scan, view, setView, onScan: runScan, onSettings, lang, t }, 'header'),
                  view === 'settings'
                    ? jsx(
                        SettingsView,
                        { ctx, queue: saveQueue, status, profile, focus, onSaved: onSettingsSaved, onReload: reload, t },
                        // D-033 (review r7 P1-02, r8 P1): an owner switch (connection or
                        // profile) remounts the tab, so it reads that owner's GET /settings
                        // and shows no other intents.
                        `${profile}:${focus ? `settings-${focus}` : 'settings'}`
                      )
                    : view === 'history'
                      ? jsx(HistoryView, { ctx, status, lang, t }, 'history')
                      : jsx(ListView, { status, phase, sort, setSort, now, lang, t, onRedeem, onTag }, 'list'),
                  // D-031: pinned to the bottom when the floor leaves room.
                  footer ? jsx('footer', { 'data-rr-footer': 'true', style: { ...S.footer, marginTop: 'auto' }, children: footer }, 'footer') : null
                ]
              }),
              measureSample,
              listSample,
              floorSample
              ]
            })
          })
        ]
      }),
      jsx(ConfirmDialog, {
        open: confirm !== null,
        onClose: () => setConfirm(null),
        onConfirm: doRedeem,
        title: t('redeem.title'),
        description: confirm ? t('redeem.description', confirm.label, formatWhen(confirm.credit.expires_at, lang) ?? t('redeem.soon')) : null,
        confirmLabel: t('redeem.confirm'),
        busyLabel: t('redeem.busy'),
        cancelLabel: t('redeem.cancel')
      })
    ]
  })
}

export default {
  id: ID,
  name: META.name,
  description: META.description,
  defaultEnabled: false,
  register(ctx) {
    ctx.i18n.register(REGISTERED)
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(Chip, { ctx }) })
  }
}
