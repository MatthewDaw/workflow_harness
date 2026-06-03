import type { SVGProps } from 'react';

/**
 * Command HQ emblem — the field-command brand mark (olive-drab field, brass
 * compass tick + dashed perimeter, cream chevrons, corner rivets). Inline SVG so
 * it inherits crispness at any size and needs no asset request. Source of truth
 * is the brand logo kit; `public/logo/command-hq-emblem.svg` mirrors it for
 * favicon / <img> use.
 */
export function Emblem({ size = 44, ...props }: { size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 44 44"
      width={size}
      height={size}
      role="img"
      aria-label="Command HQ"
      {...props}
    >
      <rect x="2.5" y="2.5" width="39" height="39" rx="6" fill="#5b6235" stroke="#2a271d" strokeWidth="2.5" />
      <rect x="6.8" y="6.8" width="30.4" height="30.4" rx="4" fill="none" stroke="#b1842f" strokeWidth="1.2" strokeDasharray="4 3" />
      <path d="M22 9V17M18 13H26" stroke="#b1842f" strokeWidth="2.6" />
      <g fill="none" stroke="#f4eed7" strokeWidth="3" strokeLinejoin="miter">
        <path d="M12 27 L22 21 L32 27" />
        <path d="M12 34 L22 28 L32 34" />
      </g>
      <g fill="#2a271d" opacity="0.4">
        <circle cx="9" cy="9" r="1.3" />
        <circle cx="35" cy="9" r="1.3" />
        <circle cx="9" cy="35" r="1.3" />
        <circle cx="35" cy="35" r="1.3" />
      </g>
    </svg>
  );
}

/**
 * The horizontal lockup: emblem + stencil wordmark. `subtitle` defaults to the
 * brand tagline; pass a different one (or null) to suppress it.
 */
export function Wordmark({
  size = 30,
  subtitle = 'Field Command',
}: {
  size?: number;
  subtitle?: string | null;
}) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <Emblem size={size} />
      <span className="flex flex-col gap-0.5 leading-none">
        <span className="font-stencil text-[15px] font-bold uppercase tracking-wide text-ink">
          claude<span className="text-brassd">+</span> · Command HQ
        </span>
        {subtitle && (
          <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-mut">
            {subtitle}
          </span>
        )}
      </span>
    </span>
  );
}
