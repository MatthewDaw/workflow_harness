import type { SVGProps } from 'react';

/**
 * Command HQ emblem — the field-command brand mark (olive-drab field, brass
 * compass tick + dashed perimeter, cream chevrons, corner rivets). Inline SVG so
 * it inherits crispness at any size and needs no asset request. Source of truth
 * is the brand logo kit; `public/logo/command-hq-emblem.svg` mirrors it for the
 * favicon.
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
      <rect
        x="2.5"
        y="2.5"
        width="39"
        height="39"
        rx="6"
        fill="#5b6235"
        stroke="#2a271d"
        strokeWidth="2.5"
      />
      <rect
        x="6.8"
        y="6.8"
        width="30.4"
        height="30.4"
        rx="4"
        fill="none"
        stroke="#b1842f"
        strokeWidth="1.2"
        strokeDasharray="4 3"
      />
      <path
        d="M25.317 13 L23.313 13.465 L26.029 14.467 L25.985 14.49 L23.276 13.893 L23.639 14.28 L25.427 15.927 L25.472 16.126 L25.365 16.307 L25.25 16.307 L25.1 16.21 L23.276 14.757 L23.261 14.802 L24.336 16.463 L24.373 16.797 L24.299 16.982 L24.066 17.056 L23.828 17.011 L23.276 16.239 L22.299 14.697 L22.258 14.728 L22 17.513 L21.878 17.653 L21.584 17.749 L21.431 17.631 L21.812 14.427 L21.776 14.414 L20.337 16.335 L19.675 17.027 L19.552 17.074 L19.473 17.045 L21.274 14.097 L18.833 15.658 L18.386 15.724 L18.204 15.561 L18.219 15.272 L18.304 15.177 L20.853 13.649 L20.879 13.559 L20.847 13.513 L17.644 13.381 L17.396 13.282 L20.883 13.059 L20.902 13.01 L20.758 12.891 L18.186 11.14 L17.994 10.914 L17.952 10.52 L18.179 10.274 L18.531 10.29 L20.784 11.961 L20.842 11.92 L19.436 9.406 L19.373 8.955 L19.62 8.617 L19.778 8.543 L20.173 8.589 L20.303 8.691 L21.02 10.231 L21.733 11.625 L21.876 12.058 L21.926 12.056 L22.2 9.179 L22.364 8.839 L22.645 8.684 L22.856 8.795 L23.025 9.038 L22.54 11.788 L22.617 11.788 L22.706 11.7 L23.697 10.437 L24.312 9.759 L24.53 9.581 L24.93 9.57 L25.224 9.994 L25.102 10.443 L23.859 12.073 L23.568 12.594 L23.625 12.625 L26.084 12.169 L26.39 12.344 L26.417 12.458 L26.302 12.737 L25.465 12.97 Z"
        fill="#b1842f"
      />
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
