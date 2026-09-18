import { useLayoutEffect, useRef, useState } from 'react'

/**
 * Measure a container so SVG charts can lay themselves out in real pixels
 * instead of guessing from a viewBox.
 */
export function useElementSize() {
  const ref = useRef(null)
  const [size, setSize] = useState({ width: 0, height: 0 })

  useLayoutEffect(() => {
    const node = ref.current
    if (!node) return undefined
    const observer = new ResizeObserver(([entry]) => {
      const box = entry.contentRect
      setSize((prev) =>
        Math.abs(prev.width - box.width) < 1 && Math.abs(prev.height - box.height) < 1
          ? prev
          : { width: box.width, height: box.height },
      )
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  return [ref, size]
}
