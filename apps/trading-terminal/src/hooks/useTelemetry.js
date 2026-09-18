import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'

const HISTORY_LIMIT = 1240
const ACTIVITY_LIMIT = 300
const MAX_BACKOFF_MS = 15_000

/** Resolve the telemetry origin: explicit env var in production, same-origin proxy in dev. */
function telemetryUrls() {
  const configured = import.meta.env?.VITE_TELEMETRY_URL
  const base = configured ? new URL(configured) : new URL(window.location.origin)
  const wsProtocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
  return {
    socket: `${wsProtocol}//${base.host}/ws/telemetry`,
    snapshot: `${base.origin}/api/snapshot`,
  }
}

const initialState = {
  /** 'connecting' | 'live' | 'reconnecting' | 'offline' */
  status: 'connecting',
  session: null,
  wallet: null,
  history: [],
  swarm: null,
  tails: null,
  activity: [],
  lastSeq: 0,
  dropped: 0,
  error: null,
}

function reducer(state, action) {
  switch (action.type) {
    case 'status':
      return { ...state, status: action.status, error: action.error ?? null }

    case 'frame': {
      const frame = action.frame
      // `seq` is the hub's global frame id, so a client that joins mid-stream
      // sees jumps that are not losses. The server counts the frames it
      // actually shed for this subscriber and stamps the next one with it.
      const base = {
        ...state,
        status: 'live',
        lastSeq: frame.seq,
        dropped: frame.dropped ?? state.dropped,
        error: null,
      }
      const p = frame.payload

      switch (frame.type) {
        case 'snapshot':
          return {
            ...base,
            session: p.session,
            wallet: p.wallet,
            history: p.history.slice(-HISTORY_LIMIT),
            swarm: p.swarm,
            tails: p.tails,
            activity: p.activity.slice(-ACTIVITY_LIMIT),
          }
        case 'tick': {
          const history = [...state.history, p.point]
          if (history.length > HISTORY_LIMIT) history.splice(0, history.length - HISTORY_LIMIT)
          return { ...base, session: p.session, wallet: p.wallet, history }
        }
        case 'swarm':
          return { ...base, swarm: p }
        case 'tails':
          return { ...base, tails: p }
        case 'activity': {
          const activity = [...state.activity, ...p.events]
          if (activity.length > ACTIVITY_LIMIT) activity.splice(0, activity.length - ACTIVITY_LIMIT)
          return { ...base, activity }
        }
        default:
          return base
      }
    }

    default:
      return state
  }
}

/**
 * Subscribe to the FastAPI telemetry stream.
 *
 * Keeps the last known state on screen across a drop and reconnects with
 * exponential backoff, so a blip dims the terminal instead of blanking it.
 */
export function useTelemetry() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const socketRef = useRef(null)
  const retryRef = useRef(0)
  const timerRef = useRef(null)
  // Bumped on every mount. A socket opened by an earlier generation keeps
  // firing events after StrictMode's remount, and its `onclose` would otherwise
  // schedule a second reconnect - two live sockets, every event delivered twice.
  const genRef = useRef(0)
  const urls = useMemo(telemetryUrls, [])

  const connect = useCallback(
    (generation) => {
      if (generation !== genRef.current) return
      let socket
      try {
        socket = new WebSocket(urls.socket)
      } catch (err) {
        dispatch({ type: 'status', status: 'offline', error: String(err) })
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
        if (generation !== genRef.current) return
        retryRef.current = 0
        dispatch({ type: 'status', status: 'live' })
      }

      socket.onmessage = (event) => {
        if (generation !== genRef.current) return
        try {
          dispatch({ type: 'frame', frame: JSON.parse(event.data) })
        } catch {
          // A malformed frame is not worth tearing the socket down for.
        }
      }

      socket.onerror = () => {
        if (generation !== genRef.current) return
        dispatch({ type: 'status', status: 'reconnecting', error: 'socket error' })
      }

      socket.onclose = () => {
        if (generation !== genRef.current) return
        socketRef.current = null
        retryRef.current += 1
        const wait = Math.min(MAX_BACKOFF_MS, 500 * 2 ** (retryRef.current - 1))
        dispatch({
          type: 'status',
          status: retryRef.current > 4 ? 'offline' : 'reconnecting',
          error: `retry ${retryRef.current} in ${Math.round(wait / 100) / 10}s`,
        })
        timerRef.current = window.setTimeout(() => connect(generation), wait)
      }
    },
    [urls.socket],
  )

  useEffect(() => {
    const generation = genRef.current + 1
    genRef.current = generation
    connect(generation)
    return () => {
      // Retiring the generation makes every in-flight handler a no-op.
      genRef.current += 1
      window.clearTimeout(timerRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [connect])

  /** Ask the server for a fresh snapshot after a detected frame gap. */
  const resync = useCallback(() => {
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send('resync')
      return
    }
    fetch(urls.snapshot)
      .then((r) => r.json())
      .then((frame) => dispatch({ type: 'frame', frame }))
      .catch(() => dispatch({ type: 'status', status: 'offline', error: 'snapshot unreachable' }))
  }, [urls.snapshot])

  return { ...state, resync }
}
