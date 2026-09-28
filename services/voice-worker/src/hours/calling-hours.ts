import { CALLING_HOURS } from "../policy.ts";

export type HoursWindow = {
  timezone: string;
  /** "HH:MM" inclusive */
  start: string;
  /** "HH:MM" exclusive */
  end: string;
};

export const DEFAULT_WINDOW: HoursWindow = {
  timezone: CALLING_HOURS.timezone,
  start: CALLING_HOURS.startLocal,
  end: CALLING_HOURS.endLocal,
};

const fmtCache = new Map<string, Intl.DateTimeFormat>();
function formatter(tz: string): Intl.DateTimeFormat {
  let f = fmtCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
    fmtCache.set(tz, f);
  }
  return f;
}

export type LocalClock = { weekday: string; hhmm: string };

/** Local weekday + HH:MM for an instant in the given IANA zone (DST-aware). */
export function localClock(at: Date, tz: string): LocalClock {
  const parts = formatter(tz).formatToParts(at);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return { weekday: get("weekday"), hhmm: `${get("hour")}:${get("minute")}` };
}

const WEEKDAYS = new Set<string>(CALLING_HOURS.days);

/** Mon–Fri, start ≤ local time < end, in the window's timezone. */
export function isWithinCallingHours(at: Date, window: HoursWindow = DEFAULT_WINDOW): boolean {
  const { weekday, hhmm } = localClock(at, window.timezone);
  if (!WEEKDAYS.has(weekday)) return false;
  return hhmm >= window.start && hhmm < window.end;
}

/** Human-readable description for health output / block messages. */
export function describeWindow(window: HoursWindow = DEFAULT_WINDOW): string {
  return `Mon–Fri ${window.start}–${window.end} ${window.timezone}`;
}
