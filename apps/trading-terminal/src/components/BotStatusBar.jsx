import { Meter, StatusDot } from './Panel.jsx'
import { INK_2, INK_3, NODE_STATUS, STATUS } from '../lib/theme.js'
import { fmtDuration, fmtMs, fmtRate } from '../lib/format.js'
import { useTicker } from '../hooks/useTicker.js'

function pingTone(ms, status) {
  if (status === 'offline') return STATUS.critical
  if (status === 'idle' || !ms) return INK_3
  if (ms >= 30) return STATUS.critical
  if (ms >= 20) return STATUS.warning
  return STATUS.good
}

function fmtAgo(ms) {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function Tile({ bot, now }) {
  const meta = NODE_STATUS[bot.status] ?? NODE_STATUS.offline
  const stale = now - bot.last_seen
  const tone = pingTone(bot.last_ping_ms, bot.status)
  const offline = bot.status === 'offline'

  return (
    <li
      className="panel min-w-0 flex-1 gap-1.5 px-2.5 py-2 transition-opacity"
      style={{ borderColor: offline ? STATUS.critical : undefined, opacity: offline ? 0.75 : 1 }}
    >
      <div className="flex items-center justify-between gap-1.5">
        <span className="num truncate text-[12px] font-bold tracking-[0.1em] text-ink">{bot.id}</span>
        <span className="flex shrink-0 items-center gap-1" title={meta.label}>
          <StatusDot color={meta.color} pulse={bot.status === 'online'} size={6} />
          <span className="text-[9px] tracking-[0.06em]" style={{ color: meta.color }}>
            {meta.label}
          </span>
        </span>
      </div>

      <p className="truncate text-[10px]" style={{ color: INK_3 }} title={bot.role}>
        {bot.role}
      </p>

      <p className="truncate text-[10.5px]" style={{ color: INK_2 }} title={bot.task}>
        <span aria-hidden="true" style={{ color: meta.color }}>
          ▸{' '}
        </span>
        {bot.task}
      </p>

      <Meter value={bot.load} color={meta.color} label={`${bot.id} load`} />

      <div className="flex items-baseline justify-between gap-1 text-[10px]">
        {/* Hosted bots do not measure latency or rate; show nothing rather than a fake zero. */}
        <span className="num" style={{ color: tone }}>
          {bot.last_ping_ms ? fmtMs(bot.last_ping_ms) : '—'}
        </span>
        <span className="num" style={{ color: INK_3 }}>
          {bot.throughput ? fmtRate(bot.throughput) : ''}
        </span>
        <span className="num" style={{ color: bot.queue_depth > 14 ? STATUS.warning : INK_3 }}>
          {bot.queue_depth ? `q${bot.queue_depth}` : ''}
        </span>
      </div>

      <div className="flex items-baseline justify-between gap-1 text-[9.5px]" style={{ color: INK_3 }}>
        <span className="num">{bot.uptime_s ? `up ${fmtDuration(bot.uptime_s)}` : bot.last_seen ? 'last seen' : 'never reported'}</span>
        <span className="num" title="Time since this bot last reported">
          {!bot.last_seen ? '—' : stale < 4000 ? 'now' : fmtAgo(stale)}
        </span>
      </div>
    </li>
  )
}

/** The eight sub-bots, always in roster order so a tile never moves under the cursor. */
export function BotStatusBar({ swarm, simulated }) {
  const now = useTicker(1000)
  const bots = swarm?.bots ?? []

  if (!bots.length) {
    return (
      <div className="panel shrink-0 items-center justify-center px-3 py-6 text-xs" style={{ color: INK_3 }}>
        awaiting sub-bot roster…
      </div>
    )
  }

  return (
    <ul
      className={`grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 xl:grid-cols-8 ${simulated ? 'opacity-70' : ''}`}
      aria-label={simulated ? 'Sub-bots (simulated: bots are not reporting yet)' : 'Sub-bots'}
      title={simulated ? 'Simulated: your bots are not reporting yet.' : undefined}
    >
      {bots.map((bot) => (
        <Tile key={bot.id} bot={bot} now={now} />
      ))}
    </ul>
  )
}
