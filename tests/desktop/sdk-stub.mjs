// Test-only stand-in for '@hermes/plugin-sdk'. Mirrors the installed SDK's
// public shape for the exports Reset Rescue uses (apps/desktop/src/sdk/index.ts):
// host.state atoms, host.navigate/notify, useValue, STATUSBAR_AREAS, Popover*,
// ConfirmDialog, Button, GlyphSpinner, SegmentedControl, Tip/Tooltip*, useI18n,
// usePluginI18n and the i18n door.
// Dev-only; never shipped. Not a copy of host code: behavior-level fakes.
import { atom } from 'nanostores'
import { useStore } from '@nanostores/react'
import { cloneElement, createContext, isValidElement, useCallback, useContext, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }

export const useValue = useStore

export const navigations = []
export const notifications = []

export const host = {
  state: {
    profile: atom('default'),
    // Registry source of the active gateway; null when not source-scoped
    // (apps/desktop/src/sdk/index.ts: readonlyAtom<null | string>).
    connectionId: atom(null),
    gateway: atom('open'),
    activeSessionId: atom(null)
  },
  navigate: path => {
    navigations.push(path)
  },
  notify: n => {
    notifications.push(n)
  }
}

// ---- i18n (same resolution as the host: active locale -> en -> raw key) ----
export const $locale = atom('en')
const bundlesById = new Map()
const $version = atom(0)

export function resolveMessage(id, locale, key, args) {
  const pick = loc => {
    const tree = bundlesById.get(id)?.[loc]
    if (!tree) return undefined
    let node = tree
    for (const part of key.split('.')) {
      if (node == null || typeof node !== 'object') return undefined
      node = node[part]
    }
    return node
  }
  const value = pick(locale) ?? pick('en')
  if (typeof value === 'function') return value(...args)
  if (typeof value === 'string') return value
  return key
}

export function createI18n(id) {
  return {
    register(bundles) {
      bundlesById.set(id, { ...(bundlesById.get(id) ?? {}), ...bundles })
      $version.set($version.get() + 1)
      return () => {
        bundlesById.delete(id)
        $version.set($version.get() + 1)
      }
    },
    t: (key, ...args) => resolveMessage(id, $locale.get(), key, args),
    onLocaleChange: fn => $locale.listen(() => fn()),
    registerAppLocale: () => () => {}
  }
}

// Marker mode: every translated string renders as «key» so tests can prove no
// user-visible text bypasses the bundles.
export const $markerMode = atom(false)

export function usePluginI18n(id) {
  const locale = useStore($locale)
  const version = useStore($version)
  const marker = useStore($markerMode)
  return useCallback(
    (key, ...args) => (marker ? `«${key}»` : resolveMessage(id, locale, key, args)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [id, locale, version, marker]
  )
}

export function useI18n() {
  return { locale: useStore($locale) }
}

// ---- components ----
export function cn(...parts) {
  return parts.filter(Boolean).join(' ')
}

// The host Button's classes, verbatim from
// /usr/local/lib/hermes-agent/apps/desktop/src/components/ui/button.tsx:15
// (base), :31 (ghost) and :49 (sm), so a page that loads Hermes' compiled CSS
// (tests/desktop/render) paints a Button the way Hermes does. Other variants
// and sizes keep the base classes only.
const BUTTON_BASE =
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-[2.5px] text-xs leading-4 font-medium whitespace-nowrap shadow-none transition-all duration-100 focus-visible:outline-hidden focus-visible:border-ring focus-visible:ring-[0.1875rem] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-default disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5"
const BUTTON_VARIANT = { ghost: 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)' }
const BUTTON_SIZE = { sm: 'px-2.5 py-1 has-[>svg]:px-2' }

// Like the host Button: `loading` sets aria-busy, disables it and shows a spinner.
export function Button({ children, variant, size, loading, asChild, disabled, className, ...props }) {
  return jsx('button', {
    type: 'button',
    'data-variant': variant,
    'data-size': size,
    'aria-busy': loading ? 'true' : undefined,
    disabled: Boolean(disabled || loading),
    className: cn(BUTTON_BASE, BUTTON_VARIANT[variant], BUTTON_SIZE[size], className),
    ...props,
    children: loading ? [jsx(GlyphSpinner, {}, 'spin'), children] : children
  })
}

export function GlyphSpinner(props) {
  return jsx('span', { 'data-slot': 'spinner', 'aria-hidden': 'true', ...props })
}

const PopoverCtx = createContext(null)

export function Popover({ open, onOpenChange, defaultOpen, children }) {
  const [inner, setInner] = useState(Boolean(defaultOpen))
  const isOpen = open ?? inner
  const set = v => {
    if (open === undefined) setInner(v)
    onOpenChange?.(v)
  }
  return jsx(PopoverCtx.Provider, { value: { open: isOpen, set }, children })
}

// Like Radix's Slot: props from an outer asChild trigger (a Tip) reach the child.
export function PopoverTrigger({ asChild, children, ...rest }) {
  const ctx = useContext(PopoverCtx)
  const toggle = () => ctx.set(!ctx.open)
  if (asChild && isValidElement(children)) {
    const own = children.props.onClick
    return cloneElement(children, {
      ...rest,
      'data-popover-trigger': 'true',
      'aria-expanded': ctx.open ? 'true' : 'false',
      onClick: e => {
        own?.(e)
        toggle()
      }
    })
  }
  return jsx('button', { type: 'button', 'data-popover-trigger': 'true', onClick: toggle, children })
}

export function PopoverContent({ children, side, align, className, style }) {
  const ctx = useContext(PopoverCtx)
  if (!ctx.open) return null
  return jsx('div', { 'data-slot': 'popover-content', 'data-side': side, 'data-align': align, className, style, children })
}

// Like the host ConfirmDialog: Confirm awaits onConfirm, closes on success and
// shows the thrown message inline on failure; both buttons disable while busy.
export const confirmCalls = []

export function ConfirmDialog({ open, onClose, onConfirm, title, description, children, confirmLabel, busyLabel, cancelLabel, destructive }) {
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  if (!open) return null
  confirmCalls.push({ confirmLabel, cancelLabel, busyLabel, destructive })
  return jsxs('div', {
    role: 'dialog',
    'data-slot': 'confirm-dialog',
    children: [
      jsx('h2', { 'data-slot': 'confirm-title', children: title }),
      description ? jsx('div', { 'data-slot': 'confirm-description', children: description }) : null,
      children ?? null,
      error ? jsx('div', { 'data-slot': 'confirm-error', children: error }) : null,
      jsx('button', { type: 'button', 'data-slot': 'confirm-cancel', disabled: busy, onClick: () => onClose(), children: cancelLabel ?? 'Cancel' }),
      jsx('button', {
        type: 'button',
        'data-slot': 'confirm-ok',
        disabled: busy,
        onClick: async () => {
          setBusy(true)
          setError(null)
          try {
            await onConfirm()
            setBusy(false)
            onClose()
          } catch (err) {
            setBusy(false)
            setError(err instanceof Error ? err.message : 'failed')
          }
        },
        children: busy ? busyLabel ?? 'Loading' : confirmLabel ?? 'Confirm'
      })
    ]
  })
}

// D-034: the host SegmentedControl
// (/usr/local/lib/hermes-agent/apps/desktop/src/components/ui/segmented-control.tsx):
// same props, same markup (a track div, one <button aria-pressed> per option,
// classes verbatim). The render page loads no Hermes CSS, so the classes' sizes
// are repeated inline (rem values of the Tailwind classes) to lay it out alike.
export function SegmentedControl({ className, disabled = false, onChange, options, sizing = 'equal', value }) {
  return jsx('div', {
    'data-slot': 'segmented-control',
    className: cn('inline-grid w-fit grid-flow-col gap-0.5 rounded-[5px] bg-(--ui-bg-tertiary) p-0.5', sizing === 'equal' ? 'auto-cols-fr' : 'auto-cols-max', disabled && 'opacity-50', className),
    style: { display: 'inline-grid', gridAutoFlow: 'column', gridAutoColumns: sizing === 'equal' ? '1fr' : 'max-content', gap: '0.125rem', padding: '0.125rem', borderRadius: 5, background: 'var(--ui-bg-tertiary)', width: 'fit-content', opacity: disabled ? 0.5 : undefined },
    children: options.map(({ id, label }) => {
      const active = value === id
      return jsx(
        'button',
        {
          type: 'button',
          'aria-pressed': active,
          disabled,
          className: cn(
            'flex items-center justify-center gap-1 rounded-[3px] px-2.5 py-0.5 text-[0.6875rem] font-medium transition-colors hover:transition-none disabled:cursor-default',
            active ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
          ),
          style: { display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0.125rem 0.625rem', fontSize: '0.6875rem', fontWeight: 500, lineHeight: 'inherit', fontFamily: 'inherit', color: 'inherit', background: active ? 'var(--ui-bg-quaternary)' : 'transparent', border: 0, borderRadius: 3, whiteSpace: 'nowrap' },
          onClick: () => onChange(id),
          children: label
        },
        id
      )
    })
  })
}

// D-034: the host Tip (components/ui/tooltip.tsx) wraps one child as a Radix
// TooltipTrigger asChild. The stub puts the trigger marker and, for a string
// label, the tooltip text on the child, so tests can read what would show on
// hover. A falsy label renders the child untouched, like the host.
export function Tip({ label, children }) {
  if (!label || !isValidElement(children)) return children ?? null
  return cloneElement(children, { 'data-slot': 'tooltip-trigger', 'data-tip': typeof label === 'string' ? label : undefined })
}
export function TooltipProvider({ children }) {
  return children ?? null
}
export function Tooltip({ children }) {
  return children ?? null
}
export function TooltipTrigger({ children }) {
  return children ?? null
}
export function TooltipContent() {
  return null
}
