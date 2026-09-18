const usd = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const usdCompact = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  maximumFractionDigits: 1,
})

const clock = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

const hhmm = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })

export const fmtUsd = (v) => usd.format(v ?? 0)
export const fmtUsdCompact = (v) => usdCompact.format(v ?? 0)
export const fmtTime = (ms) => clock.format(new Date(ms))
export const fmtHHMM = (ms) => hhmm.format(new Date(ms))

/** Signed currency with an explicit sign, for deltas. */
export const fmtSignedUsd = (v) => `${(v ?? 0) >= 0 ? '+' : '-'}${usd.format(Math.abs(v ?? 0))}`
export const fmtSignedPct = (v, digits = 2) => `${(v ?? 0) >= 0 ? '+' : '-'}${Math.abs(v ?? 0).toFixed(digits)}%`

export const fmtEth = (v, digits = 4) => `${(v ?? 0).toFixed(digits)}`
export const fmtMs = (v) => `${(v ?? 0).toFixed(1)}ms`
export const fmtRate = (v) => `${Math.round(v ?? 0)}/s`

/** Elapsed wall time as HH:MM:SS, for the session clock and node uptime. */
export function fmtDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds ?? 0))
  const h = String(Math.floor(total / 3600)).padStart(2, '0')
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
  const s = String(total % 60).padStart(2, '0')
  return `${h}:${m}:${s}`
}
