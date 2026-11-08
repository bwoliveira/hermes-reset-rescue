// '@hermes/plugin-sdk' for scripts/screenshots.mjs only (never the tests).
//
// Everything comes from ../sdk-stub.mjs, except the pieces whose look the
// stub fakes or trims: Button, PopoverContent and SegmentedControl. Here they
// carry the host's own classes, verbatim, merged with tailwind-merge as the
// host's cn() does, so the page that loads Hermes' compiled CSS paints them as
// Hermes does:
//   /usr/local/lib/hermes-agent/apps/desktop/src/components/ui/button.tsx
//     (buttonVariants: every variant and size, the textStrong compound)
//   /usr/local/lib/hermes-agent/apps/desktop/src/components/ui/popover.tsx
//     (popoverContentVariants default + the CSS arrow, side=top)
//   /usr/local/lib/hermes-agent/apps/desktop/src/components/ui/segmented-control.tsx
// The popover is placed like Radix places it for side="top" align="end":
// sideOffset 6 + arrow height 7 above the trigger, right edges aligned, the
// arrow over the trigger's centre (clamped 12 px from the corners).
import { useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { jsx, jsxs } from 'react/jsx-runtime'
import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'
import { GlyphSpinner } from '../sdk-stub.mjs'

export * from '../sdk-stub.mjs'

export const cn = (...parts) => twMerge(clsx(parts))

const TEXT_ACTION_ICON = '[&_.codicon]:no-underline [&_svg]:no-underline'
const BUTTON_BASE =
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-[2.5px] text-xs leading-4 font-medium whitespace-nowrap shadow-none transition-all duration-100 focus-visible:outline-hidden focus-visible:border-ring focus-visible:ring-[0.1875rem] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-default disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5"
const BUTTON_VARIANT = {
  default: 'bg-primary text-primary-foreground hover:bg-primary/90',
  destructive: 'bg-destructive text-white hover:bg-destructive/90 focus-visible:ring-destructive/20 dark:bg-destructive/60 dark:focus-visible:ring-destructive/40',
  outline:
    'bg-transparent text-(--ui-text-primary) shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--ui-stroke-secondary)_50%,transparent)] hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
  secondary: 'bg-(--ui-bg-quaternary) text-(--ui-text-primary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
  chip: 'bg-(--ui-bg-quaternary) text-(--ui-text-primary) hover:bg-(--chrome-action-hover) hover:shadow-[inset_0_0_0_0.5px_var(--ui-stroke-secondary)]',
  ghost: 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
  grip: 'bg-transparent text-(--ui-text-tertiary) transition-colors hover:text-(--ui-text-secondary) focus-visible:text-(--ui-text-primary)',
  floating: 'bg-popover text-(--ui-text-secondary) shadow-md hover:text-(--ui-text-primary)',
  link: `text-primary underline-offset-4 decoration-current/20 hover:underline ${TEXT_ACTION_ICON}`,
  text: `text-muted-foreground underline-offset-4 hover:text-foreground hover:underline ${TEXT_ACTION_ICON}`,
  textStrong: `font-semibold text-muted-foreground underline underline-offset-4 hover:text-foreground ${TEXT_ACTION_ICON}`
}
const BUTTON_SIZE = {
  default: 'px-3 py-1.5 has-[>svg]:px-2.5',
  xs: "gap-1 px-2 py-0.5 text-[0.6875rem] leading-4 has-[>svg]:px-1.5 [&_svg:not([class*='size-'])]:size-3",
  sm: 'px-2.5 py-1 has-[>svg]:px-2',
  lg: 'px-5 py-2 text-sm leading-5 has-[>svg]:px-4',
  inline: 'h-auto gap-1 p-0 has-[>svg]:px-0',
  micro: "h-auto gap-0.5 px-1 py-0 text-xs leading-4 font-normal has-[>svg]:px-0.5 [&_svg:not([class*='size-'])]:size-3",
  icon: 'size-9 rounded-[4px]',
  'icon-xs': "size-6 rounded-[4px] [&_svg:not([class*='size-'])]:size-3",
  'icon-sm': 'size-8 rounded-[4px]'
}

// Same behavior as the stub's Button (loading = aria-busy + disabled + spinner).
export function Button({ children, variant = 'default', size = 'default', loading, asChild, disabled, className, ...props }) {
  void asChild
  return jsx('button', {
    type: 'button',
    'data-slot': 'button',
    'data-variant': variant,
    'data-size': size,
    'aria-busy': loading ? 'true' : undefined,
    disabled: Boolean(disabled || loading),
    className: cn(BUTTON_BASE, BUTTON_VARIANT[variant], BUTTON_SIZE[size], variant === 'textStrong' && 'px-0 has-[>svg]:px-0', className),
    ...props,
    children: loading ? [jsx(GlyphSpinner, {}, 'spin'), children] : children
  })
}

const POPOVER_CLASS =
  'z-50 w-72 origin-(--radix-popover-content-transform-origin) rounded-lg p-2 focus-visible:outline-hidden border border-(--ui-stroke-secondary) bg-(--popover-surface) text-popover-foreground backdrop-blur-md [--popover-surface:color-mix(in_srgb,var(--ui-bg-elevated)_92%,transparent)]'
const ARROW_CLASS =
  'absolute top-0 left-1/2 size-[11px] -translate-x-1/2 -translate-y-1/2 rotate-45 bg-(--popover-surface) border-r border-b border-(--ui-stroke-secondary) backdrop-blur-md'
const SIDE_OFFSET = 6
const ARROW_H = 7
const ARROW_W = 16
const ARROW_PAD = 12

export function PopoverContent({ children, side, align, className, style }) {
  const ref = useRef(null)
  const [pos, setPos] = useState(null)
  useLayoutEffect(() => {
    const place = () => {
      const trigger = document.querySelector('[data-popover-trigger="true"]')
      const el = ref.current
      if (!trigger || !el) return
      const t = trigger.getBoundingClientRect()
      const w = el.getBoundingClientRect().width
      const right = Math.max(8, window.innerWidth - t.right)
      const left = window.innerWidth - right - w
      const centre = t.left + t.width / 2 - left
      const arrowLeft = Math.min(Math.max(centre - ARROW_W / 2, ARROW_PAD), w - ARROW_PAD - ARROW_W)
      setPos({ bottom: window.innerHeight - t.top + SIDE_OFFSET + ARROW_H, right, arrowLeft })
    }
    place()
    const ro = new ResizeObserver(place)
    if (ref.current) ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return createPortal(
    jsxs('div', {
      ref,
      'data-slot': 'popover-content',
      'data-side': side,
      'data-align': align,
      'data-state': 'open',
      className: cn(POPOVER_CLASS, className),
      style: { position: 'fixed', bottom: pos?.bottom ?? 0, right: pos?.right ?? 8, visibility: pos ? 'visible' : 'hidden', ...style },
      children: [
        children,
        jsx(
          'span',
          {
            'data-rr-shot-arrow': 'true',
            style: { position: 'absolute', top: '100%', left: pos?.arrowLeft ?? 0, width: ARROW_W, height: ARROW_H, overflow: 'visible', display: 'block' },
            children: jsx('span', { className: ARROW_CLASS })
          },
          'arrow'
        )
      ]
    }),
    document.body
  )
}

export function SegmentedControl({ className, disabled = false, onChange, options, sizing = 'equal', value }) {
  return jsx('div', {
    'data-slot': 'segmented-control',
    className: cn('inline-grid w-fit grid-flow-col gap-0.5 rounded-[5px] bg-(--ui-bg-tertiary) p-0.5', sizing === 'equal' ? 'auto-cols-fr' : 'auto-cols-max', disabled && 'opacity-50', className),
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
          onClick: () => onChange(id),
          children: label
        },
        id
      )
    })
  })
}
