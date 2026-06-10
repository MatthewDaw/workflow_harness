import type { ReactNode } from 'react';

/**
 * Reusable visual primitives promoted from wireframe.html so screens stay
 * faithful to the prototype. Thin wrappers over the component classes defined
 * in index.css.
 */

export function Pill({
  children,
  variant,
  className = '',
}: {
  children: ReactNode;
  variant?: 'live' | 'good' | 'idle' | 'skill';
  className?: string;
}) {
  const v =
    variant === 'live'
      ? 'hq-pill-live'
      : variant === 'good'
        ? 'hq-pill-good'
        : variant === 'idle'
          ? 'hq-pill-idle'
          : variant === 'skill'
            ? 'hq-pill-skill'
            : '';
  return <span className={`hq-pill ${v} ${className}`}>{children}</span>;
}

export function StatusDot({ variant }: { variant?: 'live' | 'good' }) {
  const v = variant === 'live' ? 'hq-dot-live' : variant === 'good' ? 'hq-dot-good' : '';
  return <span className={`hq-dot ${v}`} aria-hidden="true" />;
}

export function Bar({ pct, color }: { pct: number; color?: string }) {
  return (
    <div
      className="hq-bar"
      role="progressbar"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <i style={{ width: `${pct}%`, ...(color ? { background: color } : {}) }} />
    </div>
  );
}

export function ScreenHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-3">
      <h2 className="m-0 text-base font-semibold">{title}</h2>
      {subtitle && <p className="m-0 mt-0.5 max-w-[760px] text-[13px] text-mut">{subtitle}</p>}
    </div>
  );
}
