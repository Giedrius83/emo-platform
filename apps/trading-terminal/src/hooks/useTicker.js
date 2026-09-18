import { useEffect, useState } from 'react'

/** Re-render on an interval, for clocks that must advance between data frames. */
export function useTicker(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs])
  return now
}
