import { useEffect, useState } from 'react';

/**
 * Human-readable "time since" label for an epoch-ms timestamp, relative to
 * `now` (also epoch ms). Used by the Sessions list to show how long ago a
 * session last streamed an event.
 *
 * Kept pure (no `Date.now()` inside) so it is deterministic and unit-testable;
 * callers pass `now`, typically from {@link useNow} so the label freshens on a
 * tick without refetching data.
 */
export function relativeTime(ts: number, now: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return '—';
  const diff = now - ts;
  if (diff < 0) return 'just now'; // clock skew / future stamp
  const sec = Math.floor(diff / 1000);
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

/**
 * Returns the current epoch-ms, refreshed every `intervalMs`. Lets relative-time
 * labels age in place (e.g. "3m ago" → "4m ago") without re-fetching the
 * underlying data. Defaults to a 15s tick — fine-grained enough for "minutes
 * ago" labels while staying cheap.
 */
export function useNow(intervalMs = 15000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
