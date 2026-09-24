import { StatusDot } from './Panel.jsx'
import { INK_3, STATUS, pnlColor } from '../lib/theme.js'
import { fmtDuration, fmtEth, fmtSignedPct, fmtSignedUsd, fmtTime, fmtUsd } from '../lib/format.js'
import { useTicker } from '../hooks/useTicker.js'

const FEED = {
  connecting: { color: STATUS.warning, label: 'CONNECTING', glyph: '◌' },
  live: { color: STATUS.good, label: 'LIVE', glyph: '●' },
  reconnecting: { color: STATUS.warning, label: 'RECONNECTING', glyph: '◐' },
  offline: { color: STATUS.critical, label: 'FEED DOWN', glyph: '○' },
}

const SOURCE = {
  'etoro-real': { color: STATUS.good, glyph: '●', label: 'eToro REAL', sub: 'read-only · live from your account' },
  'etoro-demo': { color: STATUS.good, glyph: '●', label: 'eToro DEMO', sub: 'read-only · virtual money' },
  simulated: { color: STATUS.warning, glyph: '◐', label: 'SIMULATED', sub: 'no eToro keys on the server' },
}

const NETWORK = {
  ACTIVE: STATUS.good,
  PARTIAL: STATUS.warning,
  DEGRADED: STATUS.critical,
}

function Cell({ label, children, className = '' }) {
  return (
    <div
      className={
        'flex min-w-0 flex-1 basis-full flex-col justify-center gap-1 border-b border-line px-4 py-2.5 ' +
        'last:border-b-0 sm:basis-1/2 xl:basis-auto xl:border-r xl:border-b-0 xl:last:border-r-0 ' +
        className
      }
    >
      <span className="panel-title">{label}</span>
      {children}
    </div>
  )
}

export function MetricsBar({ session, wallet, waiting = false, status, dropped, lastSeq, onResync }) {
  const now = useTicker(1000)
  const feed = FEED[status] ?? FEED.connecting
  const pnl = wallet?.pnl_usd ?? 0
  const pnlTone = pnlColor(pnl)
  const elapsed = session ? (now - session.started_at) / 1000 : 0
  const networkColor = NETWORK[session?.network] ?? STATUS.warning
  const connected = session?.bots_connected ?? 0
  const total = session?.bots_total ?? 8
  const sourceBase = SOURCE[session?.source] ?? SOURCE.simulated
  const source = session?.source_error
    ? { color: STATUS.critical, glyph: '×', label: waiting ? 'eToro ERROR' : sourceBase.label, sub: session.source_error }
    : waiting
      ? { color: STATUS.warning, glyph: '◌', label: sourceBase.label, sub: 'connecting…' }
      : sourceBase
  const broker = wallet?.currency && wallet.currency !== 'ETH'
  const swarmLive = Boolean(session?.swarm_live)

  return (
    <header className="panel shrink-0 flex-row flex-wrap items-stretch overflow-hidden">
      {broker ? (
        <Cell label="Account value" className="xl:min-w-[180px]">
          <div className="flex items-baseline gap-1.5">
            <span className="num text-2xl leading-none font-semibold tracking-tight">
              {waiting ? '—' : fmtUsd(wallet?.balance_usd)}
            </span>
            <span className="text-xs font-medium text-ink-3">{wallet?.currency}</span>
          </div>
          <span className="num text-[11px] text-ink-3">
            {waiting ? 'waiting for eToro' : `cash ${fmtUsd(wallet?.cash_usd)} · in trades ${fmtUsd(wallet?.exposure_usd)}`}
          </span>
        </Cell>
      ) : (
        <Cell label="Wallet balance" className="xl:min-w-[180px]">
          <div className="flex items-baseline gap-1.5">
            <span className="num text-2xl leading-none font-semibold tracking-tight">{fmtEth(wallet?.balance_eth)}</span>
            <span className="text-xs font-medium text-ink-3">ETH</span>
          </div>
          <span className="num text-[11px] text-ink-3">
            {fmtUsd(wallet?.balance_usd)} · ETH {fmtUsd(wallet?.eth_usd)}
          </span>
        </Cell>
      )}

      <Cell label="Total PnL" className="xl:min-w-[210px]">
        <div className="flex items-baseline gap-2">
          {waiting ? null : (
            <span aria-hidden="true" style={{ color: pnlTone }} className="text-sm leading-none">
              {pnl >= 0 ? '▲' : '▼'}
            </span>
          )}
          <span className="num text-2xl leading-none font-semibold tracking-tight" style={{ color: waiting ? undefined : pnlTone }}>
            {waiting ? '—' : fmtSignedUsd(pnl)}
          </span>
          {waiting ? null : (
            <span className="num text-xs" style={{ color: pnlTone }}>
              {fmtSignedPct(wallet?.pnl_pct)}
            </span>
          )}
        </div>
        <span className="num text-[11px] text-ink-3">
          {waiting
            ? 'waiting for eToro'
            : `realised ${fmtSignedUsd(wallet?.realized_usd)} · open ${fmtSignedUsd(wallet?.unrealized_usd)}`}
        </span>
      </Cell>

      <Cell label="Session clock" className="xl:min-w-[170px]">
        <span className="num text-2xl leading-none font-semibold tracking-tight">{fmtDuration(elapsed)}</span>
        <span className="num text-[11px] text-ink-3">
          {session ? `${session.id} · ${fmtTime(session.started_at)}` : '—'}
        </span>
      </Cell>

      <Cell label="Agent network" className="xl:min-w-[210px]">
        {swarmLive ? (
          <div className="flex items-center gap-2">
            <StatusDot color={networkColor} pulse={session?.network === 'ACTIVE'} size={8} />
            <span className="text-sm font-semibold tracking-[0.14em]" style={{ color: networkColor }}>
              {session?.network ?? 'OFFLINE'}
            </span>
            <span className="num text-sm text-ink-2">
              {connected}/{total} BOTS CONNECTED
            </span>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <span aria-hidden="true" style={{ color: STATUS.warning }}>
              ◐
            </span>
            <span className="text-sm font-semibold tracking-[0.14em]" style={{ color: STATUS.warning }}>
              NO BOTS REPORTING
            </span>
          </div>
        )}
        <span className="num text-[11px] text-ink-3">
          {swarmLive
            ? `${wallet?.fills ?? 0} trades · ${wallet?.open_positions ?? 0} open`
            : 'swarm panels show a simulation until bots report'}
        </span>
      </Cell>

      <Cell label="Account data" className="xl:min-w-[170px]">
        <div className="flex items-center gap-2">
          <span aria-hidden="true" style={{ color: source.color }}>
            {source.glyph}
          </span>
          <span className="text-sm font-semibold tracking-[0.12em]" style={{ color: source.color }}>
            {source.label}
          </span>
        </div>
        <span className="truncate text-[11px] text-ink-3" title={source.sub}>
          {source.sub}
        </span>
      </Cell>

      <Cell label="Feed" className="xl:ml-auto xl:min-w-[150px]">
        <div className="flex items-center gap-2">
          <span aria-hidden="true" style={{ color: feed.color }}>
            {feed.glyph}
          </span>
          <span className="text-sm font-semibold tracking-[0.14em]" style={{ color: feed.color }}>
            {feed.label}
          </span>
        </div>
        <button
          type="button"
          onClick={onResync}
          className="num w-fit text-[11px] underline decoration-dotted underline-offset-2 transition-colors hover:text-ink"
          style={{ color: dropped > 0 ? STATUS.warning : INK_3 }}
          title={`Request a fresh snapshot from the telemetry service (last frame #${lastSeq ?? 0})`}
        >
          {dropped > 0 ? `${dropped} frame${dropped === 1 ? '' : 's'} dropped · resync` : 'resync'}
        </button>
      </Cell>
    </header>
  )
}
