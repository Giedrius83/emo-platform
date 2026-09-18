import { useMemo, useState } from 'react'
import { Panel } from './Panel.jsx'
import { useElementSize } from '../hooks/useElementSize.js'
import { GRID, INK_2, INK_3, SERIES, STATUS, SUNKEN } from '../lib/theme.js'

const M = { top: 10, right: 12, bottom: 20, left: 12 }

/**
 * Ridgeline of the forecast return distribution, one curve per horizon.
 *
 * Every curve is a true density normalised on the server, and all three share
 * one amplitude scale - so a taller peak really does mean more concentrated
 * probability, and the shaded left region really is the tail mass quoted below.
 */
export function TailRidge({ tails }) {
  const [ref, { width, height }] = useElementSize()
  const [hoverX, setHoverX] = useState(null)

  const geom = useMemo(() => {
    const curves = tails?.curves ?? []
    if (!curves.length || width < 80 || height < 80) return null

    const [lo, hi] = tails.domain
    const innerW = width - M.left - M.right
    const innerH = height - M.top - M.bottom
    const x = (v) => M.left + ((v - lo) / (hi - lo)) * innerW

    const peak = Math.max(...curves.flatMap((c) => c.points.map((p) => p[1]))) || 1
    const amp = innerH * 0.42
    const step = curves.length > 1 ? (innerH - amp) / (curves.length - 1) : 0

    // Back to front: the longest horizon sits at the back, the 1-minute at the front.
    const ordered = [...curves].reverse()
    const layers = ordered.map((curve, i) => {
      const baseline = M.top + amp + i * step
      const y = (density) => baseline - (density / peak) * amp
      const line = curve.points
        .map((p, idx) => `${idx === 0 ? 'M' : 'L'}${x(p[0]).toFixed(2)},${y(p[1]).toFixed(2)}`)
        .join('')
      const area = `${line}L${x(hi).toFixed(2)},${baseline.toFixed(2)}L${x(lo).toFixed(2)},${baseline.toFixed(2)}Z`
      return {
        ...curve,
        color: SERIES[(curve.color_slot - 1) % SERIES.length],
        baseline,
        line,
        area,
        y,
      }
    })

    return { x, lo, hi, innerW, layers, thresholdX: x(tails.threshold), zeroX: x(0) }
  }, [tails, width, height])

  const hoverValue = useMemo(() => {
    if (!geom || hoverX == null) return null
    const [lo, hi] = [geom.lo, geom.hi]
    const ret = lo + ((hoverX - M.left) / geom.innerW) * (hi - lo)
    if (ret < lo || ret > hi) return null
    return {
      ret,
      readings: geom.layers.map((layer) => {
        const idx = Math.round(((ret - lo) / (hi - lo)) * (layer.points.length - 1))
        const point = layer.points[Math.max(0, Math.min(layer.points.length - 1, idx))]
        return { id: layer.id, label: layer.label, color: layer.color, density: point[1] }
      }),
    }
  }, [geom, hoverX])

  const ticks = [-8, -4, 0, 4, 8]

  return (
    <Panel
      title="Tail probability ridge"
      subtitle={`P(return ≤ ${tails?.threshold ?? -2.5}%)`}
      right={<span className="chip" style={{ borderColor: STATUS.critical, color: STATUS.critical }}>tail zone</span>}
      className="max-lg:min-h-[300px]"
      bodyClassName="flex flex-col"
    >
      <div ref={ref} className="relative min-h-0 flex-1">
        {geom ? (
          <svg
            width={width}
            height={height}
            role="img"
            aria-label="Forecast return density by horizon, with the loss tail shaded"
            onPointerMove={(e) => setHoverX(e.clientX - e.currentTarget.getBoundingClientRect().left)}
            onPointerLeave={() => setHoverX(null)}
          >
            {/* Tail region: the area the quoted probability integrates over. */}
            <rect
              x={M.left}
              y={M.top}
              width={Math.max(0, geom.thresholdX - M.left)}
              height={height - M.top - M.bottom}
              fill={STATUS.critical}
              opacity="0.09"
            />
            <line
              x1={geom.zeroX}
              x2={geom.zeroX}
              y1={M.top}
              y2={height - M.bottom}
              stroke={GRID}
              strokeWidth="1"
            />
            <line
              x1={geom.thresholdX}
              x2={geom.thresholdX}
              y1={M.top}
              y2={height - M.bottom}
              stroke={STATUS.critical}
              strokeWidth="1"
              strokeDasharray="3 3"
            />

            {geom.layers.map((layer) => (
              <g key={layer.id}>
                {/* Opaque backing so a front ridge occludes the one behind it. */}
                <path d={layer.area} fill={SUNKEN} opacity="0.94" />
                <path d={layer.area} fill={layer.color} opacity="0.22" />
                <path d={layer.line} fill="none" stroke={layer.color} strokeWidth="2" strokeLinejoin="round" />
                <line
                  x1={M.left}
                  x2={width - M.right}
                  y1={layer.baseline}
                  y2={layer.baseline}
                  stroke={layer.color}
                  strokeWidth="1"
                  opacity="0.45"
                />
                <text x={M.left + 2} y={layer.baseline - 4} fontSize="9.5" fill={INK_2} className="num">
                  {layer.label}
                </text>
              </g>
            ))}

            {hoverValue ? (
              <line
                x1={geom.x(hoverValue.ret)}
                x2={geom.x(hoverValue.ret)}
                y1={M.top}
                y2={height - M.bottom}
                stroke={INK_3}
                strokeWidth="1"
                strokeDasharray="2 3"
                pointerEvents="none"
              />
            ) : null}

            {ticks.map((t) => (
              <text
                key={t}
                x={geom.x(t)}
                y={height - 5}
                textAnchor="middle"
                fontSize="9.5"
                fill={t === tails.threshold ? STATUS.critical : INK_3}
                className="num"
              >
                {t > 0 ? `+${t}%` : `${t}%`}
              </text>
            ))}
          </svg>
        ) : (
          <div className="flex h-full items-center justify-center text-xs" style={{ color: INK_3 }}>
            awaiting distribution…
          </div>
        )}

        {hoverValue ? (
          <div
            className="pointer-events-none absolute top-1 z-10 rounded border border-line bg-raised/95 px-2 py-1.5"
            style={{ left: Math.min(Math.max(6, geom.x(hoverValue.ret) + 8), Math.max(6, width - 130)) }}
          >
            <div className="num text-[10px] text-ink-3">
              return {hoverValue.ret > 0 ? '+' : ''}
              {hoverValue.ret.toFixed(2)}%
            </div>
            {hoverValue.readings.map((r) => (
              <div key={r.id} className="num flex items-center gap-1.5 text-[10px] text-ink-2">
                <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: r.color }} />
                {r.id} ρ {r.density.toFixed(3)}
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {/* Table view of the same numbers: identity never rests on colour alone. */}
      <table className="w-full shrink-0 border-t border-line text-[10px]">
        <thead>
          <tr style={{ color: INK_3 }}>
            <th className="px-3 py-1 text-left font-medium tracking-[0.08em] uppercase">Horizon</th>
            <th className="px-2 py-1 text-right font-medium tracking-[0.08em] uppercase">VaR95</th>
            <th className="px-3 py-1 text-right font-medium tracking-[0.08em] uppercase">
              P(≤ {tails?.threshold ?? -2.5}%)
            </th>
          </tr>
        </thead>
        <tbody>
          {(tails?.curves ?? []).map((curve) => {
            const color = SERIES[(curve.color_slot - 1) % SERIES.length]
            const hot = curve.tail_prob >= 15
            return (
              <tr key={curve.id} className="border-t border-line/60">
                <td className="px-3 py-1">
                  <span className="flex items-center gap-1.5 text-ink-2">
                    <span className="inline-block h-2 w-2 rounded-[1px]" style={{ background: color }} />
                    {curve.label}
                  </span>
                </td>
                <td className="num px-2 py-1 text-right" style={{ color: INK_2 }}>
                  {curve.var95.toFixed(2)}%
                </td>
                <td
                  className="num px-3 py-1 text-right font-semibold"
                  style={{ color: hot ? STATUS.critical : INK_2 }}
                  title={hot ? 'Above the 15% tail-risk limit' : undefined}
                >
                  {hot ? '! ' : ''}
                  {curve.tail_prob.toFixed(2)}%
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Panel>
  )
}
