/**
 * Read a numeric env knob at call time, falling back to `fallback` when unset.
 * Call-time (not module-load) so tests and deploys can flip a knob without a
 * process restart.
 */
export function envNumber(name: string, fallback: number): number {
  return Number(process.env[name] ?? fallback);
}
