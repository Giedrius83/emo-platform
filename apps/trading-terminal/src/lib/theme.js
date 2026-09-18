/**
 * Palette tokens for SVG, where CSS custom properties are awkward to read back.
 *
 * These mirror the `@theme` block in styles.css. They are the validated
 * dark-mode steps of the project data-viz palette; change them in both places
 * and re-run the palette validator.
 */
export const SURFACE = '#141416'
export const SUNKEN = '#0e0f11'
export const GRID = '#212125'
export const AXIS = '#3a3a40'
export const INK = '#ffffff'
export const INK_2 = '#b9bcc4'
export const INK_3 = '#8a8d94'

/** Categorical slots, assigned in fixed order and never cycled. */
export const SERIES = ['#3987e5', '#d95926', '#199e70']

export const STATUS = {
  good: '#0ca30c',
  warning: '#fab219',
  serious: '#ec835a',
  critical: '#d03b3b',
}

/** Single-hue blue ramp for magnitude encoding (link latency). */
export const BLUE_RAMP = ['#184f95', '#1c5cab', '#256abf', '#2a78d6', '#3987e5', '#5598e7', '#86b6ef']

/** Node health as a status colour + a glyph, so colour never carries it alone. */
export const NODE_STATUS = {
  online: { color: STATUS.good, glyph: '●', label: 'ONLINE' },
  degraded: { color: STATUS.warning, glyph: '◐', label: 'DEGRADED' },
  offline: { color: STATUS.critical, glyph: '○', label: 'OFFLINE' },
}

export const LOG_LEVEL = {
  info: { color: INK_2, glyph: '·', label: 'INFO' },
  success: { color: STATUS.good, glyph: '+', label: 'OK' },
  warn: { color: STATUS.warning, glyph: '!', label: 'WARN' },
  critical: { color: STATUS.critical, glyph: '×', label: 'CRIT' },
}

/**
 * Map a latency in milliseconds onto the blue ramp.
 * Past the SLA it stops being a magnitude and becomes a fault, so it takes the
 * reserved critical colour and the caller pairs it with a label.
 */
export function latencyColor(ms, sla = 26) {
  if (ms >= sla) return STATUS.critical
  const t = Math.min(1, Math.max(0, ms / sla))
  const idx = Math.round(t * (BLUE_RAMP.length - 1))
  return BLUE_RAMP[idx]
}

/** Green for profit, red for loss, muted at flat - always paired with an arrow glyph. */
export function pnlColor(value) {
  if (value > 0) return STATUS.good
  if (value < 0) return STATUS.critical
  return INK_2
}
