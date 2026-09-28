import { StatusDot } from './Panel.jsx'
import { useTicker } from '../hooks/useTicker.js'
import { INK_2, INK_3, SERIES, STATUS, pnlColor } from '../lib/theme.js'
import { fmtSignedUsd, fmtUsd } from '../lib/format.js'

/** The three states that stop trading are different things and read differently. */
const MODE = {
  RUNNING: { color: STATUS.good, label: 'RUNNING', note: 'scanning and trading' },
  ENTRY_HALTED: { color: STATUS.warning, label: 'ENTRY HALTED', note: 'a daily limit was hit; open trades are still managed' },
  EMERGENCY_HALT: { color: STATUS.critical, label: 'EMERGENCY HALT', note: 'no new entries until an operator recovers' },
  SYSTEM_OFFLINE: { color: STATUS.critical, label: 'SYSTEM OFFLINE', note: 'the trader stopped reporting' },
  NOT_VERIFIED: { color: STATUS.warning, label: 'NOT VERIFIED', note: 'keys not yet proven for this environment' },
  NOT_CONFIGURED: { color: INK_3, label: 'NOT CONNECTED', note: 'no trader database on this server' },
}

const STAGES = ['SCOUT', 'QUANT', 'GUARD', 'TRADER', 'MANAGER']
const STAGE_METRIC = { SCOUT: 'SCOUT', QUANT: 'QUANT', GUARD: 'GUARD', TRADER: 'TRADER_SUBMIT', MANAGER: 'SIGNAL_TO_FILL' }

function Stat({ label, children, title }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 px-3 py-1.5" title={title}>
      <span className="panel-title">{label}</span>
      <span className="num truncate text-[12px] text-ink">{children}</span>
    </div>
  )
}

function Limit({ used, max, invert = false }) {
  const ratio = max ? used / max : 0
  const color = ratio >= 1 ? STATUS.critical : ratio >= 0.66 ? STATUS.warning : INK_2
  return (
    <span style={{ color: invert ? INK_2 : color }}>
      {used}
      <span className="text-ink-3">/{max}</span>
    </span>
  )
}

export function TradingStatus({ trading }) {
  const now = useTicker(1000)
  const mode = MODE[trading?.mode] ?? MODE.NOT_CONFIGURED
  const env = trading?.env ? trading.env.toUpperCase() : '—'
  const limits = trading?.limits ?? {}
  const day = trading?.day ?? {}
  const account = trading?.account
  const reason = trading?.emergency_reason || trading?.entry_reason || trading?.error || ''
  const bots = Object.fromEntries((trading?.bots ?? []).map((b) => [b.bot, b]))
  const latency = Object.fromEntries((trading?.latency ?? []).map((l) => [l.stage, l]))
  const exposure = account && account.equity ? (account.invested / account.equity) * 100 : null
  const open = trading?.positions?.length ?? 0

  return (
    <section className="panel shrink-0" aria-label="Trading pipeline status">
      <div className="flex flex-row flex-wrap items-stretch divide-x divide-line">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="panel-title">Trading environment</span>
        <span
          className="chip font-semibold"
          style={{ borderColor: env === 'REAL' ? STATUS.critical : SERIES[0], color: env === 'REAL' ? STATUS.critical : SERIES[0] }}
        >
          {env}
        </span>
      </div>
      <div className="flex min-w-0 items-center gap-2 px-3 py-1.5" title={mode.note}>
        <StatusDot color={mode.color} pulse={trading?.mode === 'RUNNING'} />
        <span className="text-[12px] font-semibold tracking-wide" style={{ color: mode.color }}>
          {mode.label}
        </span>
        {reason ? <span className="num max-w-[280px] truncate text-[11px] text-ink-3" title={reason}>{reason}</span> : null}
      </div>
      {trading?.configured ? (
        <>
          <Stat label="Trader equity" title="Cash and equity as eToro reports them to the trader">
            {account ? fmtUsd(account.equity) : 'DATA_UNAVAILABLE'}
            {account ? <span className="text-ink-3"> · cash {fmtUsd(account.cash)}</span> : null}
          </Stat>
          <Stat label="Day P&L / limit">
            <span style={{ color: pnlColor(day.realized_pnl ?? 0) }}>{fmtSignedUsd(day.realized_pnl ?? 0)}</span>
            <span className="text-ink-3"> / −{fmtUsd(limits.max_daily_loss_usd ?? 0)}</span>
          </Stat>
          <Stat label="Loss streak">
            <Limit used={day.consecutive_losses ?? 0} max={limits.max_consecutive_losses ?? 0} />
          </Stat>
          <Stat label="Trades today">
            <Limit used={day.trades ?? 0} max={limits.max_trade_count_per_day ?? 0} />
          </Stat>
          <Stat label="Open">
            <Limit used={open} max={limits.max_open_positions ?? 0} />
          </Stat>
          <Stat label="Exposure">
            {exposure == null ? 'n/a' : `${exposure.toFixed(1)}%`}
            <span className="text-ink-3"> / {limits.max_total_exposure_percent ?? 0}%</span>
          </Stat>
          <div className="flex flex-wrap items-center gap-1.5 px-3 py-1.5" aria-label="Pipeline stages">
            {STAGES.map((stage, i) => {
              const seen = bots[stage]?.ts
              const age = seen ? (now - seen) / 1000 : Infinity
              const color = age < 90 ? STATUS.good : age < 600 ? STATUS.warning : INK_3
              const lat = latency[STAGE_METRIC[stage]]
              return (
                <span key={stage} className="flex items-center gap-1 text-[10px]">
                  {i ? <span className="text-ink-3">→</span> : null}
                  <StatusDot color={color} size={6} />
                  <span className="font-semibold text-ink-2">{stage}</span>
                  {lat ? <span className="num text-ink-3">{Math.round(lat.avg_ms)}ms</span> : null}
                </span>
              )
            })}
          </div>
        </>
      ) : null}
      </div>
    </section>
  )
}
