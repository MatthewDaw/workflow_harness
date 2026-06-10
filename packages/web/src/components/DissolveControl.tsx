import { useState } from 'react';

/**
 * Dissolve is destructive — it ejects every member to standalone and removes the
 * bundle. Show the blast-radius (member count) and require a confirm click.
 */
export function DissolveControl({
  count,
  noun,
  onConfirm,
}: {
  count: number;
  /** Singular member noun ('skill' | 'agent') — names what members revert to. */
  noun: string;
  onConfirm: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <button
        type="button"
        className="hq-btn"
        data-testid="dissolve-bundle"
        onClick={() => setConfirming(true)}
      >
        ✕ Dissolve bundle
      </button>
    );
  }
  return (
    <div className="hq-note" data-testid="dissolve-confirm">
      Dissolving frees{' '}
      <b data-testid="blast-radius">
        {count} member{count === 1 ? '' : 's'}
      </b>{' '}
      back to standalone {noun}s and removes the bundle.
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          data-testid="dissolve-confirm-btn"
          onClick={onConfirm}
        >
          Confirm dissolve
        </button>
        <button type="button" className="hq-btn" onClick={() => setConfirming(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}
