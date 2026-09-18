import { Fragment } from 'react'
import { Meter, Panel, StatusDot } from './Panel.jsx'
import { INK_2, INK_3, NODE_STATUS, SERIES, STATUS, SUNKEN } from '../lib/theme.js'
import { fmtMs, fmtRate } from '../lib/format.js'

const STAGE_STATE = {
  STREAMING: { color: STATUS.good, glyph: '▸' },
  BACKPRESSURE: { color: STATUS.warning, glyph: '≡' },
  PARTIAL: { color: STATUS.serious, glyph: '◐' },
  STALLED: { color: STATUS.critical, glyph: '■' },
}

function polar(cx, cy, r, deg) {
  const rad = ((deg - 180) * Math.PI) / 180
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)]
}

/** Half-circle arc, measured as 0..180 degrees of sweep from the left. */
function arc(cx, cy, r, fromDeg, toDeg) {
  const [x1, y1] = polar(cx, cy, r, fromDeg)
  const [x2, y2] = polar(cx, cy, r, toDeg)
  const large = toDeg - fromDeg > 180 ? 1 : 0
  return `M${x1.toFixed(2)},${y1.toFixed(2)}A${r},${r} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)}`
}

function ConsensusGauge({ pipeline }) {
  const pct = pipeline?.consensus_pct ?? 0
  const quorum = pipeline?.quorum_pct ?? 66
  const passing = pct >= quorum
  const tone = passing ? STATUS.good : STATUS.warning
  const W = 120
  const H = 72
  const cx = W / 2
  const cy = H - 8
  const r = 46
  const [qx, qy] = polar(cx, cy, r, (quorum / 100) * 180)
  const [qxi, qyi] = polar(cx, cy, r - 11, (quorum / 100) * 180)

  return (
    <div className="flex shrink-0 flex-col items-center justify-center gap-1 border-l border-line px-2 py-2">
      <span className="panel-title self-start">Consensus</span>
      <svg
        width={W}
        height={H}
        role="img"
        aria-label={`Swarm consensus ${pct.toFixed(1)} percent against a ${quorum} percent quorum`}
      >
        <path d={arc(cx, cy, r, 0, 180)} fill="none" stroke={SUNKEN} strokeWidth="9" strokeLinecap="round" />
        <path
          d={arc(cx, cy, r, 0, Math.max(0.6, (pct / 100) * 180))}
          fill="none"
          stroke={tone}
          strokeWidth="9"
          strokeLinecap="round"
          style={{ transition: 'd 400ms linear' }}
        />
        <line x1={qx} y1={qy} x2={qxi} y2={qyi} stroke={INK_2} strokeWidth="1.5" />
        <text x={cx} y={cy - 17} textAnchor="middle" fontSize="20" fontWeight="700" fill={tone} className="num">
          {pct.toFixed(0)}%
        </text>
        <text x={cx} y={cy - 4} textAnchor="middle" fontSize="8.5" fill={INK_3} className="num">
          quorum {quorum}%
        </text>
      </svg>
      <div className="flex items-center gap-1.5">
        <span aria-hidden="true" style={{ color: tone }}>
          {passing ? '✓' : '!'}
        </span>
        <span className="text-[11px] font-semibold tracking-[0.12em]" style={{ color: tone }}>
          {pipeline?.decision ?? 'HOLD'}
        </span>
      </div>
      <span className="num text-[10px]" style={{ color: INK_3 }}>
        {pipeline?.votes_for ?? 0} for · {pipeline?.votes_against ?? 0} against
      </span>
    </div>
  )
}

function Stage({ stage, bots, index }) {
  const meta = STAGE_STATE[stage.state] ?? STAGE_STATE.STALLED
  const accent = SERIES[index % SERIES.length]
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1.5 rounded border border-line bg-sunken px-2.5 py-2">
      {/* Two lines: at three-across the stage name and its state cannot share one. */}
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="h-3 w-[3px] shrink-0 rounded-full" style={{ background: accent }} />
          <span className="truncate text-[11px] font-semibold tracking-[0.1em] text-ink uppercase">{stage.label}</span>
        </span>
        <span className="flex items-center gap-1 pl-[9px]" style={{ color: meta.color }}>
          <span aria-hidden="true" className="text-[9px]">
            {meta.glyph}
          </span>
          <span className="truncate text-[9px] tracking-[0.08em]">{stage.state}</span>
        </span>
      </div>

      {/* The members carrying this stage, with the load and ping behind its state. */}
      <ul className="flex flex-col gap-1">
        {stage.members.map((id) => {
          const bot = bots.get(id)
          const status = NODE_STATUS[bot?.status] ?? NODE_STATUS.offline
          return (
            <li key={id} className="flex flex-col gap-0.5" title={`${id} · ${status.label}`}>
              <div className="flex min-w-0 items-baseline gap-1.5 text-[10px]">
                <StatusDot color={status.color} size={5} />
                <span className="num min-w-0 flex-1 truncate" style={{ color: INK_2 }}>
                  {id}
                </span>
                <span className="num hidden shrink-0 @xl:inline" style={{ color: INK_3 }}>
                  {bot ? fmtMs(bot.last_ping_ms) : '—'}
                </span>
              </div>
              <Meter value={bot?.load ?? 0} color={status.color} height={2} label={`${id} load`} />
            </li>
          )
        })}
      </ul>

      <dl className="mt-auto flex flex-col gap-0.5 text-[10px]">
        {[
          ['in-flight', String(stage.inflight)],
          ['latency', fmtMs(stage.latency_ms)],
          ['rate', fmtRate(stage.throughput)],
        ].map(([label, value]) => (
          <div key={label} className="flex min-w-0 items-baseline justify-between gap-2">
            <dt className="truncate" style={{ color: INK_3 }}>
              {label}
            </dt>
            <dd className="num shrink-0 font-semibold" style={{ color: INK_2 }}>
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function Connector() {
  return (
    <div className="hidden shrink-0 items-center @lg:flex" aria-hidden="true">
      <svg width="16" height="12">
        <line x1="0" y1="6" x2="15" y2="6" stroke={INK_3} strokeWidth="1" opacity="0.5" />
        <line
          className="packet"
          x1="0"
          y1="6"
          x2="15"
          y2="6"
          stroke={SERIES[0]}
          strokeWidth="2.5"
          strokeDasharray="4 12"
          style={{ animationDuration: '1.4s' }}
        />
        <path d="M11,2.5 L15,6 L11,9.5" fill="none" stroke={INK_3} strokeWidth="1" />
      </svg>
    </div>
  )
}

/** Signal → Strategy → Execution, with the swarm's vote on the resulting order. */
export function PipelineGraph({ swarm }) {
  const pipeline = swarm?.pipeline
  const bots = new Map((swarm?.bots ?? []).map((b) => [b.id, b]))
  const stages = pipeline?.stages ?? []

  return (
    <Panel
      title="Relationship graph"
      subtitle="signal → strategy → execution"
      right={
        <span className="chip num" style={{ color: INK_3 }}>
          {stages.reduce((sum, s) => sum + s.inflight, 0)} in flight
        </span>
      }
      className="max-lg:min-h-[340px]"
      bodyClassName="@container flex min-h-0"
    >
      <div className="flex min-h-0 w-full">
        {/* Below ~28rem of panel width three stages cannot sit side by side,
            so they stack and the flow arrows drop out. */}
        <div className="scroll-thin flex min-h-0 min-w-0 flex-1 flex-col items-stretch gap-1 overflow-y-auto p-2.5 @md:flex-row">
          {stages.length ? (
            stages.map((stage, i) => (
              <Fragment key={stage.id}>
                <Stage stage={stage} bots={bots} index={i} />
                {i < stages.length - 1 ? <Connector /> : null}
              </Fragment>
            ))
          ) : (
            <div className="flex h-full w-full items-center justify-center text-xs" style={{ color: INK_3 }}>
              awaiting pipeline…
            </div>
          )}
        </div>
        <ConsensusGauge pipeline={pipeline} />
      </div>
    </Panel>
  )
}
