import { useMemo, useState } from 'react'
import { Panel } from './Panel.jsx'
import { useElementSize } from '../hooks/useElementSize.js'
import { AXIS, GRID, INK_2, INK_3, SERIES, STATUS, SURFACE, pnlColor } from '../lib/theme.js'
import { fmtEth, fmtHHMM, fmtSignedPct, fmtSignedUsd, fmtTime, fmtUsd, fmtUsdCompact } from '../lib/format.js'

const M = { top: 14, right: 66, bottom: 22, left: 8 }

/**
 * Two views of one session, never two y-axes on one plot: PnL in USD or the
 * hot-wallet balance in ETH. One measure is on screen at a time.
 */
const METRICS = {
  pnl: {
    key: 'pnl',
    label: 'PnL (USD)',
    axis: (v) => fmtUsdCompact(v),
    readout: (v) => fmtSignedUsd(v),
    zeroRule: true,
  },
  balance: {
    key: 'balance',
    label: 'Balance (ETH)',
    axis: (v) => `${v.toFixed(3)}`,
    readout: (v) => `${fmtEth(v)} ETH`,
    zeroRule: false,
  },
}

function niceTicks(lo, hi, count) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo === hi) return [lo]
  const raw = (hi - lo) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag
  const start = Math.ceil(lo / step) * step
  const ticks = []
  for (let v = start; v <= hi + step * 0.001; v += step) ticks.push(Number(v.toFixed(8)))
  return ticks
}

export function BalanceChart({ history, wallet }) {
  const [ref, { width, height }] = useElementSize()
  const [metric, setMetric] = useState('pnl')
  const [hover, setHover] = useState(null)
  const spec = METRICS[metric]

  const geom = useMemo(() => {
    if (!history?.length || width < 80 || height < 80) return null
    const innerW = width - M.left - M.right
    const innerH = height - M.top - M.bottom
    const values = history.map((p) => p[spec.key])
    let lo = Math.min(...values)
    let hi = Math.max(...values)
    if (spec.zeroRule) {
      lo = Math.min(lo, 0)
      hi = Math.max(hi, 0)
    }
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.12
    lo -= pad
    hi += pad

    const t0 = history[0].t
    const t1 = history[history.length - 1].t
    const spanT = Math.max(1, t1 - t0)
    const x = (t) => M.left + ((t - t0) / spanT) * innerW
    const y = (v) => M.top + innerH - ((v - lo) / (hi - lo || 1)) * innerH

    const line = history.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.t).toFixed(2)},${y(p[spec.key]).toFixed(2)}`).join('')
    const baseY = spec.zeroRule ? y(0) : M.top + innerH
    const area = `${line}L${x(t1).toFixed(2)},${baseY.toFixed(2)}L${x(t0).toFixed(2)},${baseY.toFixed(2)}Z`

    return {
      innerW,
      innerH,
      x,
      y,
      t0,
      t1,
      line,
      area,
      baseY,
      yTicks: niceTicks(lo, hi, 4),
      xTicks: [0, 0.25, 0.5, 0.75, 1].map((f) => t0 + f * spanT),
      last: history[history.length - 1],
    }
  }, [history, width, height, spec])

  const last = geom?.last
  const value = last ? last[spec.key] : 0
  const tone = metric === 'pnl' ? pnlColor(value) : SERIES[0]
  const first = history?.[0]
  // The chart holds a rolling window, so this is the delta across what is on
  // screen - not the whole session, which the metrics bar already reports.
  const sessionDelta = first && last ? value - first[spec.key] : 0
  const windowMinutes = first && last ? Math.round((last.t - first.t) / 60000) : 0
  const windowLabel = windowMinutes > 0 ? `${windowMinutes}m` : 'window'

  function onMove(event) {
    if (!geom || !history?.length) return
    const box = event.currentTarget.getBoundingClientRect()
    const px = event.clientX - box.left
    const frac = (px - M.left) / geom.innerW
    const idx = Math.round(frac * (history.length - 1))
    const point = history[Math.max(0, Math.min(history.length - 1, idx))]
    if (point) setHover(point)
  }

  return (
    <Panel
      title="Balance history"
      subtitle={`${history?.length ?? 0} pts · 500ms`}
      right={
        <div className="flex items-center gap-1" role="group" aria-label="Chart metric">
          {Object.values(METRICS).map((m) => (
            <button
              key={m.key}
              type="button"
              onClick={() => setMetric(m.key)}
              aria-pressed={metric === m.key}
              className={`chip transition-colors ${
                metric === m.key ? 'border-rule text-ink' : 'hover:text-ink-2'
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
      }
      className="max-lg:min-h-[280px]"
      bodyClassName="relative flex flex-col"
    >
      <div ref={ref} className="min-h-0 w-full flex-1">
        {geom ? (
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={`${spec.label} over the session, currently ${spec.readout(value)}`}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
          >
            <defs>
              <linearGradient id="balance-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={tone} stopOpacity="0.28" />
                <stop offset="100%" stopColor={tone} stopOpacity="0.02" />
              </linearGradient>
            </defs>

            {geom.yTicks.map((v) => (
              <g key={`y${v}`}>
                <line x1={M.left} x2={width - M.right} y1={geom.y(v)} y2={geom.y(v)} stroke={GRID} strokeWidth="1" />
                <text
                  x={width - M.right + 6}
                  y={geom.y(v)}
                  dominantBaseline="middle"
                  fontSize="10"
                  fill={INK_3}
                  className="num"
                >
                  {spec.axis(v)}
                </text>
              </g>
            ))}

            {geom.xTicks.map((t, i) => (
              <text
                key={`x${i}`}
                x={geom.x(t)}
                y={height - 6}
                textAnchor={i === 0 ? 'start' : i === geom.xTicks.length - 1 ? 'end' : 'middle'}
                fontSize="10"
                fill={INK_3}
                className="num"
              >
                {fmtHHMM(t)}
              </text>
            ))}

            {spec.zeroRule ? (
              <line
                x1={M.left}
                x2={width - M.right}
                y1={geom.baseY}
                y2={geom.baseY}
                stroke={AXIS}
                strokeWidth="1"
                strokeDasharray="3 3"
              />
            ) : null}

            <path d={geom.area} fill="url(#balance-fill)" />
            <path d={geom.line} fill="none" stroke={tone} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />

            {/* Current value: marker plus a direct label, so the series needs no legend. */}
            <circle cx={geom.x(last.t)} cy={geom.y(value)} r="4.5" fill={tone} stroke={SURFACE} strokeWidth="2" />
            <circle cx={geom.x(last.t)} cy={geom.y(value)} r="4.5" fill={tone} className="pulse-ring" opacity="0.5" />

            {hover ? (
              <g pointerEvents="none">
                <line
                  x1={geom.x(hover.t)}
                  x2={geom.x(hover.t)}
                  y1={M.top}
                  y2={height - M.bottom}
                  stroke={INK_3}
                  strokeWidth="1"
                  strokeDasharray="2 3"
                />
                <circle
                  cx={geom.x(hover.t)}
                  cy={geom.y(hover[spec.key])}
                  r="5"
                  fill={tone}
                  stroke={SURFACE}
                  strokeWidth="2"
                />
              </g>
            ) : null}
          </svg>
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-ink-3">awaiting stream…</div>
        )}
      </div>

      {/* Tooltip in HTML rather than SVG text: wraps, and picks up terminal styling. */}
      {hover && geom ? (
        <div
          className="pointer-events-none absolute z-10 rounded border border-line bg-raised/95 px-2 py-1.5 shadow-lg backdrop-blur-sm"
          style={{
            left: Math.min(Math.max(8, geom.x(hover.t) + 10), Math.max(8, width - 168)),
            top: M.top + 4,
          }}
        >
          <div className="num text-[10px] text-ink-3">{fmtTime(hover.t)}</div>
          <div className="num text-sm font-semibold" style={{ color: metric === 'pnl' ? pnlColor(hover.pnl) : SERIES[0] }}>
            {spec.readout(hover[spec.key])}
          </div>
          <div className="num text-[10px] text-ink-3">
            {metric === 'pnl' ? `${fmtEth(hover.balance)} ETH` : fmtSignedUsd(hover.pnl)}
          </div>
        </div>
      ) : null}

      <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-line px-3 py-1.5 text-[11px]">
        <span className="num" style={{ color: INK_2 }}>
          {windowLabel} Δ{' '}
          <span style={{ color: metric === 'pnl' ? pnlColor(sessionDelta) : INK_2 }}>
            {metric === 'pnl' ? fmtSignedUsd(sessionDelta) : `${sessionDelta >= 0 ? '+' : ''}${sessionDelta.toFixed(4)} ETH`}
          </span>
        </span>
        <span className="num text-ink-3">
          exposure {fmtUsd(wallet?.exposure_usd)} · start equity {fmtUsd(wallet?.start_equity_usd)} ·{' '}
          <span style={{ color: (wallet?.pnl_pct ?? 0) >= 0 ? STATUS.good : STATUS.critical }}>
            {fmtSignedPct(wallet?.pnl_pct)}
          </span>
        </span>
      </footer>
    </Panel>
  )
}
