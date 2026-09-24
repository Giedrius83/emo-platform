import { ActivityLog } from './components/ActivityLog.jsx'
import { BalanceChart } from './components/BalanceChart.jsx'
import { BotStatusBar } from './components/BotStatusBar.jsx'
import { HandoffGraph } from './components/HandoffGraph.jsx'
import { MetricsBar } from './components/MetricsBar.jsx'
import { PipelineGraph } from './components/PipelineGraph.jsx'
import { PositionsPanel } from './components/PositionsPanel.jsx'
import { TailRidge } from './components/TailRidge.jsx'
import { useTelemetry } from './hooks/useTelemetry.js'
import { STATUS } from './lib/theme.js'

export default function App() {
  const telemetry = useTelemetry()
  const degraded = telemetry.status === 'reconnecting' || telemetry.status === 'offline'
  // Until real bots publish, the swarm panels show the built-in simulation.
  const swarmSimulated = !telemetry.session?.swarm_live
  const broker = Boolean(telemetry.session?.source) && telemetry.session.source !== 'simulated'

  return (
    <div className="flex min-h-dvh flex-col gap-1.5 bg-plane p-1.5 lg:h-dvh lg:overflow-hidden">
      {degraded ? (
        <div
          className="shrink-0 rounded border px-3 py-1 text-[11px]"
          style={{
            borderColor: telemetry.status === 'offline' ? STATUS.critical : STATUS.warning,
            color: telemetry.status === 'offline' ? STATUS.critical : STATUS.warning,
          }}
          role="status"
        >
          <span aria-hidden="true">! </span>
          Telemetry stream {telemetry.status}
          {telemetry.error ? ` · ${telemetry.error}` : ''} · showing last known state
        </div>
      ) : null}

      <MetricsBar
        session={telemetry.session}
        wallet={telemetry.wallet}
        waiting={broker && !telemetry.portfolio}
        status={telemetry.status}
        dropped={telemetry.dropped}
        lastSeq={telemetry.lastSeq}
        onResync={telemetry.resync}
      />

      <main
        className="grid min-h-0 flex-1 grid-cols-1 gap-1.5 max-lg:auto-rows-[minmax(270px,auto)] lg:grid-cols-12"
        style={{ opacity: telemetry.status === 'offline' ? 0.66 : 1, transition: 'opacity 300ms' }}
      >
        <div className="grid min-h-0 grid-rows-[minmax(190px,1.05fr)_minmax(190px,1fr)] gap-1.5 max-lg:contents lg:col-span-8">
          <BalanceChart history={telemetry.history} wallet={telemetry.wallet} />
          <div className="grid min-h-0 grid-cols-1 gap-1.5 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <HandoffGraph swarm={telemetry.swarm} simulated={swarmSimulated} />
            <PipelineGraph swarm={telemetry.swarm} simulated={swarmSimulated} />
          </div>
        </div>

        <div className="grid min-h-0 grid-rows-[minmax(230px,1fr)_minmax(180px,1fr)] gap-1.5 max-lg:contents lg:col-span-4">
          {/* Real positions replace the simulated risk model whenever eToro is connected. */}
          {broker ? (
            <PositionsPanel portfolio={telemetry.portfolio} error={telemetry.session?.source_error} />
          ) : (
            <TailRidge tails={telemetry.tails} />
          )}
          <ActivityLog activity={telemetry.activity} status={telemetry.status} />
        </div>
      </main>

      <BotStatusBar swarm={telemetry.swarm} simulated={swarmSimulated} />
    </div>
  )
}
