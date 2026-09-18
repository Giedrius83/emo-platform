/** Panel shell: hairline surface, uppercase title, optional right-hand slot. */
export function Panel({ title, subtitle, right, children, className = '', bodyClassName = '' }) {
  return (
    <section className={`panel @container ${className}`}>
      <header className="flex items-center justify-between gap-3 border-b border-line px-3 py-2">
        <div className="flex min-w-0 items-baseline gap-2">
          <h2 className="panel-title truncate">{title}</h2>
          {subtitle ? <span className="hidden truncate text-[10px] text-ink-3/80 @md:inline">{subtitle}</span> : null}
        </div>
        {right ? <div className="flex shrink-0 items-center gap-2">{right}</div> : null}
      </header>
      <div className={`min-h-0 flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  )
}

/** A status dot that always ships beside its own label, never colour alone. */
export function StatusDot({ color, pulse = false, size = 7 }) {
  return (
    <span className="relative inline-flex shrink-0" style={{ width: size, height: size }}>
      {pulse ? (
        <span
          className="pulse-ring absolute inset-0 rounded-full"
          style={{ background: color, opacity: 0.55 }}
          aria-hidden="true"
        />
      ) : null}
      <span className="relative inline-block h-full w-full rounded-full" style={{ background: color }} />
    </span>
  )
}

/** Horizontal utilisation meter. 2px surface gap keeps fill and track distinct. */
export function Meter({ value, color, height = 3, label }) {
  const pct = Math.round(Math.max(0, Math.min(1, value ?? 0)) * 100)
  return (
    <div
      className="w-full rounded-sm bg-sunken"
      style={{ height }}
      role="meter"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div className="h-full rounded-sm transition-[width] duration-500" style={{ width: `${pct}%`, background: color }} />
    </div>
  )
}
