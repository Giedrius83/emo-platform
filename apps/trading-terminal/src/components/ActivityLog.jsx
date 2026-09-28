import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Panel, StatusDot } from './Panel.jsx'
import { INK_2, INK_3, LOG_LEVEL, STATUS, pnlColor } from '../lib/theme.js'
import { fmtSignedUsd, fmtTime } from '../lib/format.js'

// PIPELINE (the default) shows what matters for trading: signals, decisions,
// orders, fills, exits, errors and halts. Heartbeat-style lines stay under ALL.
const MEANINGFUL = new Set([
  'SIGNAL', 'APPROVED', 'REJECTED', 'RISK_BLOCK', 'INVALID', 'EXECUTION_SUBMITTED', 'EXECUTION_FILLED',
  'POSITION_OPENED', 'POSITION_CLOSED', 'TP', 'SL', 'TIMEOUT', 'EMERGENCY', 'API_ERROR', 'EMERGENCY_HALT',
  'ENTRY_HALTED', 'ENTRY_RESUMED', 'RECOVERED', 'STARTED', 'OPEN', 'CLOSE', 'ORDER',
])
const KINDS = ['PIPELINE', 'ALL', 'SIGNAL', 'REJECTED', 'POSITION_CLOSED', 'OPEN', 'CLOSE', 'HANDOFF', 'STATUS', 'NODE']
const RENDER_LIMIT = 140

const KIND_TONE = {
  OPEN: '#3987e5',
  CLOSE: INK_2,
  APPROVED: STATUS.good,
  REJECTED: STATUS.warning,
  RISK_BLOCK: STATUS.warning,
  INVALID: STATUS.critical,
  EXECUTION_SUBMITTED: '#3987e5',
  EXECUTION_FILLED: STATUS.good,
  POSITION_OPENED: STATUS.good,
  POSITION_CLOSED: INK_2,
  TP: STATUS.good,
  SL: STATUS.critical,
  TIMEOUT: STATUS.warning,
  API_ERROR: STATUS.critical,
  EMERGENCY_HALT: STATUS.critical,
  STATUS: INK_2,
  HANDOFF: INK_2,
  ORDER: STATUS.good,
  SIGNAL: '#3987e5',
  NODE: STATUS.warning,
  CONSENSUS: '#199e70',
}

/** Clock time for today; date and minute for anything older, so replayed trades read as history. */
function fmtStamp(ms) {
  const d = new Date(ms)
  if (d.toDateString() === new Date().toDateString()) return fmtTime(ms)
  const day = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
  const hm = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${day} ${hm}`
}

function Row({ event }) {
  const level = LOG_LEVEL[event.level] ?? LOG_LEVEL.info
  const kindTone = KIND_TONE[event.kind] ?? INK_3
  const isOrder =
    (event.kind === 'ORDER' || event.kind === 'CLOSE' || event.kind === 'POSITION_CLOSED') && typeof event.value === 'number'

  return (
    <li className="row-in flex items-baseline gap-2 border-b border-line/50 px-3 py-1 text-[11px] leading-snug hover:bg-raised">
      <span className="num shrink-0 text-ink-3" title={new Date(event.t).toLocaleString()}>
        {fmtStamp(event.t)}
      </span>
      <span className="w-2.5 shrink-0 text-center font-bold" style={{ color: level.color }} title={level.label} aria-label={level.label}>
        {level.glyph}
      </span>
      <span className="num w-[96px] shrink-0 truncate font-semibold tracking-wide" style={{ color: kindTone }} title={event.kind}>
        {event.kind}
      </span>
      <span className="num w-[84px] shrink-0 truncate text-ink-2">
        {event.source}
        {event.target ? <span className="text-ink-3">→{event.target}</span> : null}
      </span>
      <span className="min-w-0 flex-1 truncate text-ink-2" title={event.message}>
        {event.message}
      </span>
      {isOrder ? (
        <span className="num shrink-0 font-semibold" style={{ color: pnlColor(event.value) }}>
          {fmtSignedUsd(event.value)}
        </span>
      ) : null}
    </li>
  )
}

export function ActivityLog({ activity, status }) {
  const [filter, setFilter] = useState('PIPELINE')
  const [pinned, setPinned] = useState(true)
  const scrollRef = useRef(null)

  const rows = useMemo(() => {
    const filtered =
      filter === 'ALL'
        ? activity
        : filter === 'PIPELINE'
          ? activity.filter((e) => MEANINGFUL.has(e.kind))
          : activity.filter((e) => e.kind === filter)
    return filtered.slice(-RENDER_LIMIT)
  }, [activity, filter])

  // Follow the tape while the reader is at the bottom; stop the moment they scroll up.
  useLayoutEffect(() => {
    const node = scrollRef.current
    if (node && pinned) node.scrollTop = node.scrollHeight
  }, [rows, pinned])

  useEffect(() => {
    const node = scrollRef.current
    if (!node) return undefined
    const onScroll = () => {
      const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 24
      setPinned(atBottom)
    }
    node.addEventListener('scroll', onScroll, { passive: true })
    return () => node.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <Panel
      title="Activity log"
      subtitle={`${activity.length} events`}
      right={
        <div className="flex items-center gap-1">
          <StatusDot color={status === 'live' ? STATUS.good : STATUS.warning} pulse={status === 'live'} size={6} />
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            aria-label="Filter activity by event kind"
            className="chip cursor-pointer bg-surface text-ink-2 outline-none focus-visible:border-rule"
          >
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>
      }
      className="max-lg:min-h-[300px]"
      bodyClassName="relative"
    >
      <div ref={scrollRef} className="scroll-thin absolute inset-0 overflow-y-auto">
        {rows.length ? (
          <ul>
            {rows.map((event) => (
              <Row key={event.id} event={event} />
            ))}
          </ul>
        ) : (
          <div className="flex h-full items-center justify-center text-xs" style={{ color: INK_3 }}>
            no events on this filter
          </div>
        )}
      </div>

      {!pinned ? (
        <button
          type="button"
          onClick={() => setPinned(true)}
          className="absolute right-3 bottom-3 rounded border border-rule bg-raised px-2 py-1 text-[10px] tracking-[0.1em] text-ink-2 uppercase shadow-lg transition-colors hover:text-ink"
        >
          ↓ follow tape
        </button>
      ) : null}
    </Panel>
  )
}
