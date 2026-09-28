import { useMemo } from 'react'
import { Panel } from './Panel.jsx'
import { useElementSize } from '../hooks/useElementSize.js'
import { useTicker } from '../hooks/useTicker.js'
import { AXIS, GRID, INK_2, INK_3, SERIES, STATUS, pnlColor } from '../lib/theme.js'
import { fmtSignedPct, fmtTime } from '../lib/format.js'

const STATUS_TONE = {
  SIGNAL: STATUS.good,
  CANDIDATE: SERIES[0],
  HELD: SERIES[0],
  RANKED: INK_3,
  SPREAD: STATUS.warning,
  STALE: STATUS.critical,
  DELAYED: STATUS.critical,
  NO_DATA: STATUS.warning,
}

/** Prices span BTC (~1e5) to micro-caps (~1e-5); keep six significant digits. */
function fmtPrice(v) {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  return Number(v.toPrecision(6)).toString()
}

function Pct({ value, digits = 2 }) {
  if (value == null) return <span className="text-ink-3">n/a</span>
  return <span style={{ color: pnlColor(value) }}>{fmtSignedPct(value, digits)}</span>
}

/** Push level labels apart so ENTRY, TP and SL never print over each other. */
function spread(levels, gap = 11) {
  const sorted = [...levels].sort((a, b) => a.py - b.py)
  let last = -Infinity
  return sorted.map((l) => {
    const ty = Math.max(l.py, last + gap)
    last = ty
    return { ...l, ty }
  })
}

/** 1m closes of one asset, with the trade's entry, take-profit and stop drawn across. */
function FocusChart({ asset, position, lastExit }) {
  const [ref, { width, height }] = useElementSize()
  const closes = asset?.closes ?? []
  const times = asset?.candle_ts ?? []
  const levels = useMemo(() => {
    const out = []
    if (position?.entry_price) out.push({ y: position.entry_price, label: 'ENTRY', color: SERIES[0] })
    if (position?.tp_price) out.push({ y: position.tp_price, label: 'TP', color: STATUS.good })
    if (position?.sl_price) out.push({ y: position.sl_price, label: 'SL', color: STATUS.critical })
    return out
  }, [position])

  const M = { top: 10, right: 92, bottom: 18, left: 6 }
  const w = Math.max(0, width - M.left - M.right)
  const h = Math.max(0, height - M.top - M.bottom)
  const values = [...closes, ...levels.map((l) => l.y)]
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const pad = (hi - lo || hi * 0.001 || 1) * 0.08
  const y = (v) => M.top + h - ((v - (lo - pad)) / (hi - lo + 2 * pad)) * h
  const x = (i) => M.left + (closes.length > 1 ? (i / (closes.length - 1)) * w : w / 2)
  const path = closes.map((c, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(c).toFixed(1)}`).join('')

  const entryIdx =
    position?.opened_at && times.length
      ? times.reduce((best, t, i) => (Math.abs(t - position.opened_at) < Math.abs(times[best] - position.opened_at) ? i : best), 0)
      : null
  const exitIdx =
    lastExit?.closed_at && times.length && lastExit.closed_at >= times[0]
      ? times.reduce((best, t, i) => (Math.abs(t - lastExit.closed_at) < Math.abs(times[best] - lastExit.closed_at) ? i : best), 0)
      : null

  return (
    <div ref={ref} className="relative min-h-[120px] flex-1">
      {closes.length < 2 || width === 0 ? (
        <div className="flex h-full items-center justify-center text-[11px] text-ink-3">
          {asset ? `${asset.symbol}: no 1m candles yet` : 'waiting for the first scan'}
        </div>
      ) : (
        <svg width={width} height={height} role="img" aria-label={`${asset.symbol} one-minute closes`}>
          {[0.25, 0.5, 0.75].map((f) => (
            <line key={f} x1={M.left} x2={M.left + w} y1={M.top + h * f} y2={M.top + h * f} stroke={GRID} />
          ))}
          <line x1={M.left} x2={M.left + w} y1={M.top + h} y2={M.top + h} stroke={AXIS} />
          {spread(levels.map((l) => ({ ...l, py: y(l.y) }))).map((l) => (
            <g key={l.label}>
              <line x1={M.left} x2={M.left + w} y1={l.py} y2={l.py} stroke={l.color} strokeDasharray="4 3" strokeWidth={1} />
              <text x={M.left + w + 4} y={l.ty + 3} fontSize={9} fill={l.color} className="num">
                {l.label} {fmtPrice(l.y)}
              </text>
            </g>
          ))}
          <path d={path} fill="none" stroke={SERIES[0]} strokeWidth={1.5} />
          {entryIdx != null ? (
            <g>
              <path d={`M${x(entryIdx)},${y(closes[entryIdx]) + 9} l-4,6 l8,0 z`} fill={STATUS.good} />
              <title>entry</title>
            </g>
          ) : null}
          {exitIdx != null ? (
            <g>
              <path d={`M${x(exitIdx)},${y(closes[exitIdx]) - 9} l-4,-6 l8,0 z`} fill={pnlColor(lastExit.pnl)} />
              <title>exit ({lastExit.close_reason})</title>
            </g>
          ) : null}
          {!levels.length ? (
            <text x={M.left + w + 4} y={y(closes[closes.length - 1]) + 3} fontSize={9} fill={INK_2} className="num">
              {fmtPrice(closes[closes.length - 1])}
            </text>
          ) : null}
          <text x={M.left} y={height - 4} fontSize={9} fill={INK_3} className="num">
            {times.length ? fmtTime(times[0]) : ''}
          </text>
          <text x={M.left + w} y={height - 4} fontSize={9} fill={INK_3} textAnchor="end" className="num">
            {times.length ? fmtTime(times[times.length - 1]) : ''}
          </text>
        </svg>
      )}
    </div>
  )
}

function OpenTrade({ position, now }) {
  const left = position.deadline_at ? Math.max(0, Math.round((position.deadline_at - now) / 1000)) : null
  return (
    <div className="num flex flex-wrap items-center gap-x-3 gap-y-0.5 border-b border-line/60 px-3 py-1 text-[11px]">
      <span className="font-semibold text-ink">{position.symbol}</span>
      <span className="text-ink-3">{position.state}</span>
      <span className="text-ink-2">${position.amount?.toFixed(2)}</span>
      <span className="text-ink-2">entry {fmtPrice(position.entry_price)}</span>
      <span style={{ color: STATUS.good }}>TP {fmtPrice(position.tp_price)}</span>
      <span style={{ color: STATUS.critical }}>SL {fmtPrice(position.sl_price)}</span>
      <span className="text-ink-3" title="Crash backstop stop placed at eToro (eToro's minimum distance)">
        backstop {fmtPrice(position.broker_sl_price)}
      </span>
      {left != null ? (
        <span style={{ color: left < 60 ? STATUS.warning : INK_2 }}>
          {Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')} left
        </span>
      ) : null}
    </div>
  )
}

export function TradingAssets({ trading }) {
  const now = useTicker(1000)
  const scan = trading?.scan ?? {}
  const ranked = scan.ranked ?? []
  const positions = trading?.positions ?? []
  const closed = trading?.closed ?? []
  const held = positions[0]
  const focus =
    (held && ranked.find((r) => r.instrument_id === held.instrument_id)) ||
    ranked.find((r) => r.status === 'SIGNAL') ||
    ranked.find((r) => r.closes?.length) ||
    ranked[0]
  const lastExit = focus ? closed.find((c) => c.instrument_id === focus.instrument_id) : null
  const env = trading?.env ? trading.env.toUpperCase() : null

  const badge = env ? (
    <span
      className="chip shrink-0 font-semibold"
      style={{ borderColor: env === 'REAL' ? STATUS.critical : SERIES[0], color: env === 'REAL' ? STATUS.critical : SERIES[0] }}
      title="The environment the trader places orders in"
    >
      {env}
    </span>
  ) : null

  if (!trading?.configured) {
    return (
      <Panel title="Trading assets" className="max-lg:min-h-[300px]" bodyClassName="flex items-center justify-center">
        <p className="max-w-sm px-4 text-center text-[11px] leading-relaxed text-ink-3">
          The trading pipeline is not connected to this dashboard. When the trader runs, its ranked crypto universe,
          signals, entries, exits, TP and SL appear here. Nothing is shown until it reports: no placeholder data.
        </p>
      </Panel>
    )
  }

  return (
    <Panel
      title="Trading assets"
      badge={badge}
      subtitle={`${trading.universe ?? 0} eligible crypto · ranked by 5m/1m momentum − spread − volatility${
        scan.ts ? ` · scan ${fmtTime(scan.ts)}` : ''
      }`}
      right={
        focus ? (
          <span className="num text-[11px] text-ink-2">
            {focus.symbol} {fmtPrice(focus.mid)}
          </span>
        ) : null
      }
      className="max-lg:min-h-[420px]"
      bodyClassName="flex flex-col"
    >
      <FocusChart asset={focus} position={held && focus && held.instrument_id === focus.instrument_id ? held : null} lastExit={lastExit} />
      {positions.map((p) => (
        <OpenTrade key={p.position_ref} position={p} now={now} />
      ))}
      <div className="scroll-thin max-h-[42%] min-h-[96px] overflow-auto border-t border-line">
        <table className="num w-full text-[11px]">
          <thead className="sticky top-0 bg-surface text-[9px] tracking-[0.08em] text-ink-3 uppercase">
            <tr>
              {['#', 'Asset', 'Price', 'Bid', 'Ask', 'Spread', '1m', '5m', 'Vol', 'Volume', 'Score', 'State'].map((h) => (
                <th key={h} className="px-1.5 py-1 text-right font-medium first:text-left [&:nth-child(2)]:text-left">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ranked.map((r, i) => (
              <tr key={r.instrument_id} className="border-t border-line/40 hover:bg-raised">
                <td className="px-1.5 py-0.5 text-ink-3">{i + 1}</td>
                <td className="px-1.5 py-0.5 font-semibold text-ink" title={r.name}>
                  {r.symbol}
                </td>
                <td className="px-1.5 py-0.5 text-right text-ink-2">{fmtPrice(r.mid)}</td>
                <td className="px-1.5 py-0.5 text-right text-ink-3">{fmtPrice(r.bid)}</td>
                <td className="px-1.5 py-0.5 text-right text-ink-3">{fmtPrice(r.ask)}</td>
                <td className="px-1.5 py-0.5 text-right" style={{ color: r.status === 'SPREAD' ? STATUS.warning : INK_2 }}>
                  {r.spread_pct?.toFixed(3)}%
                </td>
                <td className="px-1.5 py-0.5 text-right"><Pct value={r.move_1m} /></td>
                <td className="px-1.5 py-0.5 text-right"><Pct value={r.move_5m ?? r.drift_pct} /></td>
                <td className="px-1.5 py-0.5 text-right text-ink-3">
                  {r.volatility_1m != null ? r.volatility_1m.toFixed(3) : 'n/a'}
                </td>
                <td className="px-1.5 py-0.5 text-right text-ink-3" title="eToro does not report crypto volume">
                  {r.volume != null ? r.volume.toFixed(0) : 'DATA_UNAVAILABLE'}
                </td>
                <td className="px-1.5 py-0.5 text-right text-ink-2">{r.score != null ? r.score.toFixed(3) : '—'}</td>
                <td className="px-1.5 py-0.5 text-right font-semibold" style={{ color: STATUS_TONE[r.status] ?? INK_3 }}>
                  {r.status}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!ranked.length ? <div className="px-3 py-3 text-[11px] text-ink-3">no scan yet</div> : null}
      </div>
    </Panel>
  )
}
