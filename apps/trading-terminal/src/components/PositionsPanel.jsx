import { useState } from 'react'
import { Panel } from './Panel.jsx'
import { useTicker } from '../hooks/useTicker.js'
import { INK_2, INK_3, STATUS, pnlColor } from '../lib/theme.js'
import { fmtSignedPct, fmtSignedUsd, fmtUsd } from '../lib/format.js'

/** Prices span $0.16 (ARB) to $80,000 (BTC); pick decimals by magnitude. */
function fmtPrice(v) {
  if (v == null) return '—'
  if (v >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  if (v >= 10) return v.toFixed(3)
  if (v >= 1) return v.toFixed(4)
  return v.toFixed(5)
}

function fmtWhen(ms, now) {
  const age = Math.max(0, now - ms)
  if (age < 60_000) return 'just now'
  if (age < 3_600_000) return `${Math.floor(age / 60_000)}m ago`
  if (age < 86_400_000) return `${Math.floor(age / 3_600_000)}h ago`
  return new Date(ms).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
}

function Asset({ symbol, logo, direction, leverage }) {
  const long = direction === 'long'
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      {logo ? (
        <img src={logo} alt="" width="16" height="16" loading="lazy" className="h-4 w-4 shrink-0 rounded-full bg-raised" />
      ) : (
        <span className="h-4 w-4 shrink-0 rounded-full bg-raised" aria-hidden="true" />
      )}
      <span className="num truncate font-semibold text-ink">{symbol}</span>
      <span
        className="num shrink-0 text-[9px] tracking-[0.06em]"
        style={{ color: long ? STATUS.good : STATUS.critical }}
        title={long ? 'Long (buy)' : 'Short (sell)'}
      >
        {long ? '▲ BUY' : '▼ SELL'}
        {leverage > 1 ? ` x${leverage}` : ''}
      </span>
    </span>
  )
}

function OpenTable({ positions, now }) {
  if (!positions.length) {
    return <Empty>No open positions. The bots are flat.</Empty>
  }
  return (
    <table className="w-full text-[10.5px]">
      <thead className="sticky top-0 bg-surface">
        <tr style={{ color: INK_3 }} className="text-[9.5px] tracking-[0.08em] uppercase">
          <th className="px-3 py-1 text-left font-medium">Asset</th>
          <th className="px-2 py-1 text-right font-medium">In</th>
          <th className="px-2 py-1 text-right font-medium">P&amp;L</th>
          <th className="px-3 py-1 text-right font-medium">Entry → now</th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => (
          <tr key={p.position_id} className="border-t border-line/60 align-top hover:bg-raised">
            <td className="px-3 py-1.5">
              <Asset {...p} />
              <div className="num mt-0.5 pl-[22px] text-[9.5px]" style={{ color: INK_3 }}>
                {fmtWhen(p.opened_at, now)}
                {p.stop_loss ? ` · SL ${fmtPrice(p.stop_loss)}` : ' · no SL'}
                {p.take_profit ? ` · TP ${fmtPrice(p.take_profit)}` : ''}
              </div>
            </td>
            <td className="num px-2 py-1.5 text-right" style={{ color: INK_2 }}>
              {fmtUsd(p.invested)}
            </td>
            <td className="num px-2 py-1.5 text-right font-semibold" style={{ color: pnlColor(p.pnl) }}>
              {fmtSignedUsd(p.pnl)}
              <div className="text-[9.5px] font-normal">{fmtSignedPct(p.pnl_pct)}</div>
            </td>
            <td className="num px-3 py-1.5 text-right" style={{ color: INK_2 }}>
              {fmtPrice(p.open_rate)}
              <div style={{ color: INK_3 }}>→ {fmtPrice(p.current_rate)}</div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ClosedTable({ trades, now }) {
  if (!trades.length) {
    return <Empty>No closed trades in this window yet.</Empty>
  }
  return (
    <table className="w-full text-[10.5px]">
      <thead className="sticky top-0 bg-surface">
        <tr style={{ color: INK_3 }} className="text-[9.5px] tracking-[0.08em] uppercase">
          <th className="px-3 py-1 text-left font-medium">Asset</th>
          <th className="px-2 py-1 text-right font-medium">In</th>
          <th className="px-2 py-1 text-right font-medium">Result</th>
          <th className="px-3 py-1 text-right font-medium">Closed</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.position_id} className="border-t border-line/60 hover:bg-raised">
            <td className="px-3 py-1">
              <Asset {...t} />
            </td>
            <td className="num px-2 py-1 text-right" style={{ color: INK_2 }}>
              {fmtUsd(t.invested)}
            </td>
            <td className="num px-2 py-1 text-right font-semibold" style={{ color: pnlColor(t.net_profit) }}>
              {fmtSignedUsd(t.net_profit)}
            </td>
            <td className="num px-3 py-1 text-right" style={{ color: INK_3 }} title={new Date(t.closed_at).toLocaleString()}>
              {fmtWhen(t.closed_at, now)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Empty({ children }) {
  return (
    <div className="flex h-full items-center justify-center px-4 text-center text-xs" style={{ color: INK_3 }}>
      {children}
    </div>
  )
}

/** What the bots hold and have traded, straight from the eToro account. */
export function PositionsPanel({ portfolio, error }) {
  const now = useTicker(5000)
  const [tab, setTab] = useState('open')
  const positions = portfolio?.positions ?? []
  const trades = portfolio?.trades ?? []
  const account = portfolio?.source === 'etoro-demo' ? 'DEMO' : 'REAL'
  const winRate = portfolio?.trades_count ? Math.round((portfolio.wins / portfolio.trades_count) * 100) : null
  const days = portfolio ? Math.round((portfolio.updated_at - portfolio.realized_since) / 86_400_000) : 0

  return (
    <Panel
      title="eToro positions"
      subtitle={`${account} account · updated ${portfolio ? fmtWhen(portfolio.updated_at, now) : '—'}`}
      right={
        <div className="flex items-center gap-1" role="tablist" aria-label="Positions view">
          {[
            ['open', `Open ${positions.length}`],
            ['closed', `Closed ${portfolio?.trades_count ?? 0}`],
          ].map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className={`chip transition-colors ${tab === key ? 'border-rule text-ink' : 'hover:text-ink-2'}`}
            >
              {label}
            </button>
          ))}
        </div>
      }
      bodyClassName="flex flex-col"
    >
      {error && portfolio ? (
        <div className="shrink-0 border-b border-line px-3 py-1 text-[10px]" style={{ color: STATUS.warning }} role="status">
          ! eToro: {error} · showing last good data
        </div>
      ) : null}

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {!portfolio ? (
          <Empty>
            {error ? (
              <span style={{ color: STATUS.critical }}>
                Cannot read your eToro account: {error}
                <br />
                <span style={{ color: INK_3 }}>Check the keys in the server&apos;s .env file, then restart it.</span>
              </span>
            ) : (
              'Connecting to eToro…'
            )}
          </Empty>
        ) : tab === 'open' ? (
          <OpenTable positions={positions} now={now} />
        ) : (
          <ClosedTable trades={trades} now={now} />
        )}
      </div>

      <footer className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-line px-3 py-1.5 text-[10px]" style={{ color: INK_3 }}>
        <span className="num">
          realised {days}d{' '}
          <span style={{ color: pnlColor(portfolio?.realized_pnl ?? 0) }}>{fmtSignedUsd(portfolio?.realized_pnl)}</span>
        </span>
        {winRate != null ? (
          <span className="num">
            {portfolio.wins}/{portfolio.trades_count} won ({winRate}%)
          </span>
        ) : null}
        <span className="num ml-auto">cash {fmtUsd(portfolio?.cash)}</span>
      </footer>
    </Panel>
  )
}
