import { describe, it, expect } from 'vitest';
import { relativeTime } from './time.js';

/**
 * `relativeTime` powers the Sessions "last activity" column. It is pure (now is
 * passed in) so these cases are deterministic.
 */
describe('relativeTime', () => {
  const now = 1_000_000_000_000;

  it('returns an em-dash for a missing or invalid timestamp', () => {
    expect(relativeTime(0, now)).toBe('—');
    expect(relativeTime(-1, now)).toBe('—');
    expect(relativeTime(Number.NaN, now)).toBe('—');
  });

  it('treats sub-5s and future stamps as "just now"', () => {
    expect(relativeTime(now, now)).toBe('just now');
    expect(relativeTime(now - 2_000, now)).toBe('just now');
    expect(relativeTime(now + 5_000, now)).toBe('just now'); // clock skew
  });

  it('formats seconds, minutes, hours, and days', () => {
    expect(relativeTime(now - 15_000, now)).toBe('15s ago');
    expect(relativeTime(now - 3 * 60_000, now)).toBe('3m ago');
    expect(relativeTime(now - 3 * 3_600_000, now)).toBe('3h ago');
    expect(relativeTime(now - 2 * 86_400_000, now)).toBe('2d ago');
  });

  it('rolls over at each unit boundary', () => {
    expect(relativeTime(now - 59_000, now)).toBe('59s ago');
    expect(relativeTime(now - 60_000, now)).toBe('1m ago');
    expect(relativeTime(now - 59 * 60_000, now)).toBe('59m ago');
    expect(relativeTime(now - 60 * 60_000, now)).toBe('1h ago');
    expect(relativeTime(now - 23 * 3_600_000, now)).toBe('23h ago');
    expect(relativeTime(now - 24 * 3_600_000, now)).toBe('1d ago');
  });
});
