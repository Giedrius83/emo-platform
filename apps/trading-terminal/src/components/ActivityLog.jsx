import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Panel, StatusDot } from './Panel.jsx'
import { INK_2, INK_3, LOG_LEVEL, STATUS, pnlColor } from '../lib/theme.js'
import { fmtSignedUsd, fmtTime } from '../lib/format.js'

const KINDS = ['ALL', 'HANDOFF', 'ORDER', 'SIGNAL', 'NODE', 'CONSENSUS']
const RENDER_LIMIT = 140

const KIND_TONE = {
  HANDOFF: INK_2,
  ORDER: STATUS.good,
  SIGNAL: '#3987e5',
  NODE: STATUS.warning,
  CONSENSUS: '#199e70',
}

function Row({ event }) {
  const level = LOG_LEVEL[event.level] ?? LOG_LEVEL.info
  const kindTone = KIND_TONE[event.kind] ?? INK_3
  const isOrder = event.kind === 'ORDER' && typeof event.value === 'number'

  return (
    <li className="row-in flex items-baseline gap-2 border-b border-line/50 px-3 py-1 text-[11px] leading-snug hover:bg-raised">
      <span className="num shrink-0 text-ink-3">{fmtTime(event.t)}</span>
      <span className="shrink-0 font-bold" style={{ color: level.color }} title={level.label} aria-label={level.label}>
        {level.glyph}
      </span>
      <span className="num w-[64px] shrink-0 truncate font-semibold tracking-wide" style={{ color: kindTone }}>
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
  const [filter, setFilter] = useState('ALL')
  const [pinned, setPinned] = useState(true)
  const scrollRef = useRef(null)

  const rows = useMemo(() => {
    const filtered = filter === 'ALL' ? activity : activity.filter((e) => e.kind === filter)
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
