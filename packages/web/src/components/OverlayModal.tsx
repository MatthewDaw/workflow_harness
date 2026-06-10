import { useEffect, type ReactNode } from 'react';

/**
 * The shared fullscreen overlay: a dimmed backdrop (click or Escape closes), a
 * centered panel, and a header row ending in a close button. The skill / agent /
 * MCP-server / workflow detail modals and the catalog picker all render through
 * it; per-surface testids and aria labels flow in as props.
 */
export function OverlayModal({
  ariaLabel,
  testid,
  header,
  onClose,
  closeTestid,
  closeLabel = '✕ Close',
  maxWidthClass = 'max-w-[900px]',
  contentClassName = 'mt-2 min-h-0 flex-1 overflow-auto',
  footer,
  children,
}: {
  ariaLabel: string;
  testid: string;
  /** The left side of the header row (the close button is rendered after it). */
  header: ReactNode;
  onClose: () => void;
  closeTestid: string;
  closeLabel?: string;
  maxWidthClass?: string;
  contentClassName?: string;
  /** Rendered below the scrollable content, inside the panel (picker actions). */
  footer?: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-black/50 p-4 sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      data-testid={testid}
      onClick={onClose}
    >
      <div
        className={`hq-box mx-auto flex h-full w-full ${maxWidthClass} flex-col overflow-hidden bg-paper`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-odd pb-2">
          {header}
          <button type="button" className="hq-btn" data-testid={closeTestid} onClick={onClose}>
            {closeLabel}
          </button>
        </div>
        <div className={contentClassName}>{children}</div>
        {footer}
      </div>
    </div>
  );
}
