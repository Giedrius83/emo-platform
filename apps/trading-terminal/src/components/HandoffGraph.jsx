import { useMemo, useState } from 'react'
import { Panel, SimulatedBadge } from './Panel.jsx'
import { useElementSize } from '../hooks/useElementSize.js'
import { BLUE_RAMP, INK, INK_2, INK_3, NODE_STATUS, STATUS, SUNKEN, latencyColor } from '../lib/theme.js'
import { fmtMs, fmtRate } from '../lib/format.js'

const SLA_MS = 26
const PAD = { x: 34, y: 30 }

/** Fixed layout: work flows left to right, so the mesh reads as a pipeline. */
const LAYOUT = {
  PLANNER: [0.14, 0.04],
  MANAGER: [0.6, 0.04],
  SCOUT: [0.0, 0.52],
  QUANT: [0.33, 0.52],
  GUARD: [0.66, 0.52],
  TRADER: [1.0, 0.52],
  CODER: [0.12, 1.0],
  ORKA: [0.5, 1.0],
}

/** Perpendicular bow, so parallel links stay individually traceable. */
function curve(ax, ay, bx, by, bow) {
  const mx = (ax + bx) / 2
  const my = (ay + by) / 2
  const dx = bx - ax
  const dy = by - ay
  const len = Math.hypot(dx, dy) || 1
  const cx = mx + (-dy / len) * bow
  const cy = my + (dx / len) * bow
  return `M${ax.toFixed(2)},${ay.toFixed(2)}Q${cx.toFixed(2)},${cy.toFixed(2)} ${bx.toFixed(2)},${by.toFixed(2)}`
}

export function HandoffGraph({ swarm, simulated }) {
  const [ref, { width, height }] = useElementSize()
  const [hover, setHover] = useState(null)

  const geom = useMemo(() => {
    const bots = swarm?.bots ?? []
    const edges = swarm?.edges ?? []
    if (!bots.length || width < 120 || height < 120) return null

    const innerW = width - PAD.x * 2
    const innerH = height - PAD.y * 2
    const nodes = new Map(
      bots.map((bot) => {
        const [nx, ny] = LAYOUT[bot.id] ?? [0.5, 0.5]
        return [bot.id, { ...bot, x: PAD.x + nx * innerW, y: PAD.y + ny * innerH }]
      }),
    )

    const maxThroughput = Math.max(1, ...edges.map((e) => e.throughput))
    const drawn = edges
      .map((edge) => {
        const a = nodes.get(edge.source)
        const b = nodes.get(edge.target)
        if (!a || !b) return null
        // Feedback links bow the other way so they never overlay the forward path.
        const backwards = b.x < a.x
        const bow = backwards ? 54 : 16
        const breached = edge.latency_ms >= SLA_MS
        return {
          ...edge,
          id: `${edge.source}-${edge.target}`,
          d: curve(a.x, a.y, b.x, b.y, bow),
          color: latencyColor(edge.latency_ms, SLA_MS),
          strokeWidth: 0.9 + (edge.throughput / maxThroughput) * 3.1,
          breached,
          // Quantised so a jittering latency does not restart the animation every frame.
          flowDur: Math.max(0.5, Math.round((edge.latency_ms / 6) * 2) / 2 + 0.5),
        }
      })
      .filter(Boolean)

    return { nodes: [...nodes.values()], edges: drawn, maxThroughput }
  }, [swarm, width, height])

  const worst = useMemo(() => {
    const edges = swarm?.edges ?? []
    if (!edges.length) return null
    return edges.reduce((a, b) => (b.latency_ms > a.latency_ms ? b : a))
  }, [swarm])

  const breaches = (swarm?.edges ?? []).filter((e) => e.latency_ms >= SLA_MS).length

  return (
    <Panel
      title="Handoff graph"
      badge={<SimulatedBadge show={simulated} />}
      subtitle={`${swarm?.edges?.length ?? 0} links · ${SLA_MS}ms SLA`}
      right={
        breaches ? (
          <span className="chip" style={{ borderColor: STATUS.critical, color: STATUS.critical }}>
            ! {breaches} over SLA
          </span>
        ) : (
          <span className="chip" style={{ borderColor: STATUS.good, color: STATUS.good }}>
            ✓ within SLA
          </span>
        )
      }
      className="max-lg:min-h-[300px]"
      bodyClassName="flex flex-col"
    >
      <div ref={ref} className="relative min-h-0 flex-1">
        {geom ? (
          <svg width={width} height={height} role="img" aria-label="Latency and throughput between sub-bots">
            <g fill="none">
              {geom.edges.map((edge) => (
                <g key={edge.id} onPointerEnter={() => setHover({ kind: 'edge', data: edge })} onPointerLeave={() => setHover(null)}>
                  {/* Fat invisible hit target: easier to grab than a 2px line. */}
                  <path d={edge.d} stroke="transparent" strokeWidth="14" pointerEvents="stroke" />
                  <path
                    d={edge.d}
                    stroke={edge.color}
                    strokeWidth={edge.strokeWidth}
                    opacity={hover?.kind === 'edge' && hover.data.id !== edge.id ? 0.25 : 0.75}
                    strokeDasharray={edge.breached ? '5 4' : undefined}
                  />
                  <path
                    className="packet"
                    d={edge.d}
                    stroke={edge.breached ? STATUS.critical : INK}
                    strokeWidth={Math.min(3, edge.strokeWidth + 0.6)}
                    opacity="0.85"
                    style={{ animationDuration: `${edge.flowDur}s` }}
                  />
                </g>
              ))}
            </g>

            {geom.nodes.map((node) => {
              const meta = NODE_STATUS[node.status] ?? NODE_STATUS.offline
              const active = hover?.kind === 'node' && hover.data.id === node.id
              const r = 13
              const circumference = 2 * Math.PI * (r + 4)
              return (
                <g
                  key={node.id}
                  transform={`translate(${node.x},${node.y})`}
                  onPointerEnter={() => setHover({ kind: 'node', data: node })}
                  onPointerLeave={() => setHover(null)}
                  style={{ cursor: 'default' }}
                >
                  <circle r={r + 10} fill="transparent" pointerEvents="all" />
                  {/* Load as an arc ring around the node. */}
                  <circle r={r + 4} fill="none" stroke={SUNKEN} strokeWidth="3" />
                  <circle
                    r={r + 4}
                    fill="none"
                    stroke={meta.color}
                    strokeWidth="3"
                    strokeLinecap="round"
                    strokeDasharray={`${(node.load * circumference).toFixed(1)} ${circumference.toFixed(1)}`}
                    transform="rotate(-90)"
                    opacity="0.9"
                  />
                  <circle r={r} fill={SUNKEN} stroke={meta.color} strokeWidth={active ? 2 : 1.25} />
                  {node.status === 'online' ? (
                    <circle r={r} fill="none" stroke={meta.color} strokeWidth="1" className="pulse-ring" opacity="0.4" />
                  ) : null}
                  <text
                    textAnchor="middle"
                    dominantBaseline="middle"
                    fontSize="9"
                    fontWeight="700"
                    fill={active ? INK : INK_2}
                    className="num"
                  >
                    {node.load ? Math.round(node.load * 100) : ''}
                  </text>
                  <text
                    textAnchor="middle"
                    y={r + 15}
                    fontSize="8.5"
                    fontWeight={active ? '700' : '400'}
                    fill={active ? INK : INK_3}
                    className="num"
                  >
                    {node.id}
                  </text>
                </g>
              )
            })}
          </svg>
        ) : (
          <div className="flex h-full items-center justify-center text-xs" style={{ color: INK_3 }}>
            awaiting mesh…
          </div>
        )}

        {hover ? (
          <div className="pointer-events-none absolute top-2 left-2 z-10 rounded border border-line bg-raised/95 px-2 py-1.5">
            {hover.kind === 'edge' ? (
              <>
                <div className="num text-[10px] font-semibold text-ink">
                  {hover.data.source} → {hover.data.target}
                </div>
                <div className="num text-[10px]" style={{ color: hover.data.breached ? STATUS.critical : INK_2 }}>
                  {hover.data.breached ? '! ' : ''}
                  latency {fmtMs(hover.data.latency_ms)}
                </div>
                <div className="num text-[10px] text-ink-3">
                  {fmtRate(hover.data.throughput)} · {hover.data.volume.toLocaleString()} total
                </div>
              </>
            ) : (
              <>
                <div className="num text-[10px] font-semibold text-ink">{hover.data.id}</div>
                <div className="text-[10px] text-ink-2">{hover.data.role}</div>
                <div className="num text-[10px] text-ink-3">
                  ping {fmtMs(hover.data.last_ping_ms)} · load {Math.round(hover.data.load * 100)}% · q
                  {hover.data.queue_depth}
                </div>
              </>
            )}
          </div>
        ) : null}
      </div>

      <footer className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-3 py-1.5 text-[10px]" style={{ color: INK_3 }}>
        <span className="flex items-center gap-1.5">
          latency
          <span className="flex overflow-hidden rounded-[2px]">
            {BLUE_RAMP.slice().reverse().map((c) => (
              <span key={c} className="h-2 w-3" style={{ background: c }} />
            ))}
            <span className="h-2 w-3" style={{ background: STATUS.critical }} />
          </span>
          <span className="num">0 → {SLA_MS}ms+</span>
        </span>
        <span className="flex items-center gap-1.5">
          <svg width="26" height="8" aria-hidden="true">
            <line x1="1" y1="6" x2="24" y2="6" stroke={INK_3} strokeWidth="1" />
            <line x1="1" y1="2" x2="24" y2="2" stroke={INK_3} strokeWidth="3" />
          </svg>
          width = handoffs/s
        </span>
        <span className="num">node ring &amp; centre = load %</span>
        {worst && worst.latency_ms > 0 ? (
          <span className="num ml-auto">
            slowest {worst.source}→{worst.target}{' '}
            <span style={{ color: worst.latency_ms >= SLA_MS ? STATUS.critical : INK_2 }}>{fmtMs(worst.latency_ms)}</span>
          </span>
        ) : null}
      </footer>
    </Panel>
  )
}
