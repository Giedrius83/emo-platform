# LARPIX trading terminal

Single-page React dashboard for the Grok bot swarm: live PnL, tail risk, swarm
telemetry and per-node status, styled as a high-frequency trading terminal.

## Run

Start the telemetry service first (see `services/telemetry`), then:

```bash
cd apps/trading-terminal
npm install
npm run dev              # http://127.0.0.1:5173
```

The dev server proxies `/api` and `/ws` to `http://127.0.0.1:8000`, so the
browser talks to one origin and there is no CORS hop. Point it elsewhere with
`TELEMETRY_ORIGIN=http://host:port npm run dev`.

For a production build, set the service origin at build time:

```bash
VITE_TELEMETRY_URL=https://telemetry.example.com npm run build
```

## Layout

| Region | Panels |
| --- | --- |
| Top metrics bar | Wallet balance, total PnL, session clock, agent network status, feed health |
| Centre | Balance history chart, handoff mesh, relationship pipeline with consensus gauge |
| Right rail | Tail probability ridge, real-time activity log |
| Bottom | Eight sub-bot tiles: SCOUT, SIGNAL, QUANT, VECTOR, NEXUS, PULSE, GUARD, CORE |

## Charts

Every chart is hand-written SVG rather than a charting library: the panels
update twice a second, and the ridge, the mesh and the gauge are all shapes a
general-purpose library would fight. The whole bundle is around 80 kB gzipped.

- **Balance history** plots one measure at a time, PnL in USD or balance in ETH.
  Never two y-axes. It carries a crosshair tooltip and a direct label on the
  current value, so the single series needs no legend.
- **Tail probability ridge** draws three horizon densities on one shared
  amplitude scale, so a taller peak really does mean more concentrated
  probability. The shaded region left of the threshold is the mass the
  `P(≤ -2.5%)` column reports. The table below doubles as the legend.
- **Handoff graph** encodes link latency as colour on a single-hue ramp and
  throughput as stroke width. A link past the SLA switches to the reserved
  critical colour and a dashed stroke, and the panel header counts breaches.
- **Consensus gauge** marks the quorum on the arc and pairs the result with a
  glyph and a decision label, so the state never rests on colour alone.

Colours come from a validated data-viz palette. The series and status hexes in
`src/lib/theme.js` mirror the `@theme` block in `src/styles.css`; they clear the
lightness band, chroma floor, colour-vision-deficiency separation and 3:1
contrast against the panel surface. Change them in both places and re-validate.

## Connection handling

`src/hooks/useTelemetry.js` owns the socket. It reconnects with exponential
backoff, keeps the last known state on screen and dims it rather than blanking
the terminal, and surfaces the server's own dropped-frame count. `resync` asks
for a fresh snapshot, falling back to the REST endpoint if the socket is gone.

## Responsive behaviour

The terminal is a fixed-viewport grid at `lg` and above. Below that it becomes a
scrolling stack. Panels that hold measured SVG carry a minimum height so they
never collapse, and the pipeline panel uses container queries: it drops its flow
arrows, then its per-member pings, and finally stacks its stages rather than
squeezing the cards until they truncate.
