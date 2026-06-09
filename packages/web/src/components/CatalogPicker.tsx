import { useEffect, useState } from 'react';
import { Pill } from './primitives.js';

/**
 * A reference to one toggleable catalog entry. The picker is catalog-agnostic —
 * skills, whole bundles, MCP servers and agents all flow through the same modal
 * as `{ type, name }` refs, so the four project opt-in surfaces (Skills /
 * Bundles / MCP / Agents tabs) reuse a SINGLE staging+diff UI instead of four
 * bespoke ones. The `type` discriminator is what lets a caller fan a flat diff
 * back out to the right per-type mutation.
 */
export type CatalogRef =
  | { type: 'skill'; name: string }
  | { type: 'bundle'; name: string }
  | { type: 'mcp'; name: string }
  | { type: 'agent'; name: string }
  | { type: 'agent-bundle'; name: string }
  | { type: 'workflow'; name: string };

/**
 * The stable identity of a ref as a string key. The picker stages selections in
 * a `Set<string>` (Set can't dedupe object refs by value), and the diff is
 * computed by comparing these keys — so `refKey` is the one place that defines
 * what "the same entry" means. `${type}:${name}` keeps skills and bundles that
 * happen to share a name distinct.
 */
export function refKey(ref: CatalogRef): string {
  return `${ref.type}:${ref.name}`;
}

/**
 * One row in the picker. `members` is populated ONLY on bundle rows and holds
 * the bundle's member skill rows (each `ref.type === 'skill'`); this lets a
 * bundle expand to reveal — and, when the bundle itself is not selected, let the
 * user pick — its individual skills, mirroring how the catalog drills a bundle
 * into its sub-skills. `hint` is a small secondary label (e.g. 'bundle', a
 * transport, a model, or an author) shown next to the label.
 */
export interface CatalogPickerRow {
  ref: CatalogRef;
  label: string;
  description?: string;
  hint?: string;
  members?: CatalogPickerRow[];
}

/**
 * Props for the picker. It is purely presentational + staging: it knows nothing
 * about the API. `initialSelected` are the refs that are ON when the modal
 * opens; on Apply the picker hands the caller the DIFF (refs newly enabled /
 * disabled) and the caller is responsible for translating that into the
 * sequential read-modify-write mutations the backend requires. `applying`
 * reflects an in-flight apply so the caller can also drive the busy state.
 */
export interface CatalogPickerProps {
  open: boolean;
  title: string;
  rows: CatalogPickerRow[];
  initialSelected: CatalogRef[];
  emptyHint?: string;
  onClose: () => void;
  onApply: (diff: { enable: CatalogRef[]; disable: CatalogRef[] }) => Promise<void> | void;
  applying?: boolean;
}

/**
 * The fullscreen catalog-picker overlay. Modeled exactly on the existing
 * SkillBodyModal / McpServerModal overlays (backdrop click + Escape close, inner
 * stopPropagation), but instead of read-only detail it stages a selection: it
 * holds an INTERNAL `Set<string>` of selected refKeys seeded from
 * `initialSelected`, toggling only mutates that set (never the API), and Apply
 * computes the diff against the initial set and calls `onApply`. Bundles are the
 * one subtlety — selecting a whole bundle covers (and disables) its members so
 * the staged set records the bundle as a single intent, while an unselected
 * bundle lets the user cherry-pick individual member skills.
 */
export function CatalogPicker({
  open,
  title,
  rows,
  initialSelected,
  emptyHint = 'Nothing to add.',
  onClose,
  onApply,
  applying,
}: CatalogPickerProps) {
  // Staged selection, seeded from initialSelected each time the modal opens.
  // Re-seed when `open` flips on (or the initial set changes) so reopening the
  // modal always reflects the project's current truth, not stale staging.
  const [staged, setStaged] = useState<Set<string>>(() => new Set(initialSelected.map(refKey)));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) setStaged(new Set(initialSelected.map(refKey)));
    // initialSelected is a fresh array each render; key off its serialized
    // identity so we re-seed only when the actual contents change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialSelected.map(refKey).sort().join('|')]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const isStaged = (ref: CatalogRef) => staged.has(refKey(ref));

  const toggle = (ref: CatalogRef) => {
    setStaged((prev) => {
      const next = new Set(prev);
      const key = refKey(ref);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const busyNow = busy || Boolean(applying);

  const handleApply = async () => {
    // enable = staged refs not initially on; disable = initial refs no longer
    // staged. We rebuild full CatalogRef objects (not just keys) so the caller
    // gets the typed discriminator back without re-parsing the string key.
    const initialKeys = new Set(initialSelected.map(refKey));
    const allRows = flattenRows(rows);
    const byKey = new Map(allRows.map((r) => [refKey(r.ref), r.ref] as const));

    const enable: CatalogRef[] = [];
    for (const key of staged) {
      if (!initialKeys.has(key)) {
        const ref = byKey.get(key);
        if (ref) enable.push(ref);
      }
    }
    const disable: CatalogRef[] = [];
    for (const ref of initialSelected) {
      if (!staged.has(refKey(ref))) disable.push(ref);
    }

    setBusy(true);
    try {
      await onApply({ enable, disable });
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-black/50 p-4 sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid="catalog-picker"
      onClick={onClose}
    >
      <div
        className="hq-box mx-auto flex h-full w-full max-w-[760px] flex-col overflow-hidden bg-paper"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-odd pb-2">
          <h2 className="m-0 text-base font-semibold">{title}</h2>
          <button
            type="button"
            className="hq-btn"
            data-testid="catalog-picker-cancel"
            onClick={onClose}
          >
            ✕ Cancel
          </button>
        </div>

        <div className="mt-3 min-h-0 flex-1 overflow-auto">
          {rows.length === 0 ? (
            <div className="hq-box text-mut" data-testid="catalog-picker-empty">
              {emptyHint}
            </div>
          ) : (
            <div className="flex flex-col gap-1.5">
              {rows.map((row) =>
                row.ref.type === 'bundle' || row.ref.type === 'agent-bundle' ? (
                  <BundleRow
                    key={refKey(row.ref)}
                    row={row}
                    bundleStaged={isStaged(row.ref)}
                    isStaged={isStaged}
                    onToggleBundle={() => toggle(row.ref)}
                    onToggleMember={toggle}
                  />
                ) : (
                  <PlainRow
                    key={refKey(row.ref)}
                    row={row}
                    checked={isStaged(row.ref)}
                    onToggle={() => toggle(row.ref)}
                  />
                ),
              )}
            </div>
          )}
        </div>

        <div className="mt-3 flex items-center justify-end gap-2 border-t border-odd pt-3">
          <button
            type="button"
            className="hq-btn"
            data-testid="catalog-picker-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="button"
            className="hq-btn hq-btn-pri"
            data-testid="catalog-picker-apply"
            disabled={busyNow}
            onClick={handleApply}
          >
            {busyNow ? 'Applying…' : 'Apply'}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Flatten the row tree (bundles + their members) into a flat list of rows. */
function flattenRows(rows: CatalogPickerRow[]): CatalogPickerRow[] {
  const out: CatalogPickerRow[] = [];
  for (const row of rows) {
    out.push(row);
    if (row.members) out.push(...row.members);
  }
  return out;
}

/**
 * A non-bundle row: a checkbox + label (+ optional hint + description). The whole
 * label is clickable. Toggling only stages — it never calls the API.
 */
function PlainRow({
  row,
  checked,
  onToggle,
  disabled,
  indent,
}: {
  row: CatalogPickerRow;
  checked: boolean;
  onToggle: () => void;
  disabled?: boolean;
  indent?: boolean;
}) {
  const { type, name } = row.ref;
  return (
    <label
      className={`hq-box flex cursor-pointer items-start gap-2 bg-paper ${
        indent ? 'ml-6' : ''
      } ${disabled ? 'opacity-60' : ''}`}
    >
      <input
        type="checkbox"
        className="mt-0.5"
        checked={checked}
        disabled={disabled}
        onChange={onToggle}
        data-testid={
          indent ? `catalog-picker-member-${name}` : `catalog-picker-row-${type}-${name}`
        }
      />
      <span className="min-w-0">
        <span className="flex items-center gap-1.5">
          <span className="font-semibold">{row.label}</span>
          {row.hint && <Pill>{row.hint}</Pill>}
        </span>
        {row.description && <span className="mt-1 block text-xs text-mut">{row.description}</span>}
      </span>
    </label>
  );
}

/**
 * A bundle row: a primary toggle that stages the WHOLE bundle ref, plus an
 * expand/collapse control that reveals the member skill rows. When the bundle is
 * staged its members render checked + disabled (they are covered by the bundle);
 * when it is not staged each member is an independently toggleable skill ref —
 * mirroring the catalog's bundle-as-subdirectory behavior.
 */
function BundleRow({
  row,
  bundleStaged,
  isStaged,
  onToggleBundle,
  onToggleMember,
}: {
  row: CatalogPickerRow;
  bundleStaged: boolean;
  isStaged: (ref: CatalogRef) => boolean;
  onToggleBundle: () => void;
  onToggleMember: (ref: CatalogRef) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const { name } = row.ref;
  const members = row.members ?? [];
  // Members are agents for an agent-bundle, skills for a skill bundle.
  const memberNoun = row.ref.type === 'agent-bundle' ? 'agents' : 'skills';

  return (
    <div className="flex flex-col gap-1.5">
      <div className="hq-box flex items-start gap-2 border-l-[3px] border-l-[#6f8fb5] bg-paper">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={bundleStaged}
          onChange={onToggleBundle}
          data-testid={`catalog-picker-row-bundle-${name}`}
          aria-label={`Add bundle ${name}`}
        />
        <span className="min-w-0 flex-1">
          <span className="flex items-center justify-between gap-1.5">
            <span className="flex items-center gap-1.5">
              <span className="font-semibold">▤ {row.label}</span>
              <Pill>{row.hint ?? 'bundle'}</Pill>
            </span>
            {members.length > 0 && (
              <button
                type="button"
                className="hq-btn"
                data-testid={`catalog-picker-expand-${name}`}
                aria-expanded={expanded}
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? '▾' : '›'} {members.length} {memberNoun}
              </button>
            )}
          </span>
          {row.description && (
            <span className="mt-1 block text-xs text-mut">{row.description}</span>
          )}
        </span>
      </div>
      {expanded &&
        members.map((m) => (
          <PlainRow
            key={refKey(m.ref)}
            row={m}
            indent
            // When the bundle is selected, its members are implicitly on and
            // locked; otherwise the member is an independently staged skill.
            checked={bundleStaged || isStaged(m.ref)}
            disabled={bundleStaged}
            onToggle={() => onToggleMember(m.ref)}
          />
        ))}
    </div>
  );
}
