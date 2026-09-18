import { Meter, StatusDot } from './Panel.jsx'
import { INK_2, INK_3, NODE_STATUS, STATUS } from '../lib/theme.js'
import { fmtDuration, fmtMs, fmtRate } from '../lib/format.js'
import { useTicker } from '../hooks/useTicker.js'

function pingTone(ms, status) {
  if (status === 'offline') return STATUS.critical
  if (ms >= 30) return STATUS.critical
  if (ms >= 20) return STATUS.warning
  return STATUS.good
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
        <span className="num" style={{ color: tone }}>
          {fmtMs(bot.last_ping_ms)}
        </span>
        <span className="num" style={{ color: INK_3 }}>
          {fmtRate(bot.throughput)}
        </span>
        <span className="num" style={{ color: bot.queue_depth > 14 ? STATUS.warning : INK_3 }}>
          q{bot.queue_depth}
        </span>
      </div>

      <div className="flex items-baseline justify-between gap-1 text-[9.5px]" style={{ color: INK_3 }}>
        <span className="num">up {fmtDuration(bot.uptime_s)}</span>
        <span className="num" title="Time since the last heartbeat">
          {stale > 4000 ? `${Math.round(stale / 1000)}s ago` : 'now'}
        </span>
      </div>
    </li>
  )
}

/** The eight sub-bots, always in roster order so a tile never moves under the cursor. */
export function BotStatusBar({ swarm }) {
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
    <ul className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 xl:grid-cols-8">
      {bots.map((bot) => (
        <Tile key={bot.id} bot={bot} now={now} />
      ))}
    </ul>
  )
}
