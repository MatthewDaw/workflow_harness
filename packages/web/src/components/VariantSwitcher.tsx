import { useMemo, useState } from 'react';
import {
  useGetSkillVariantsQuery,
  usePromoteSkillMutation,
  type SkillVariant,
} from '../api/baseApi.js';

/**
 * The per-name variant switcher (catalog versioning, KTD6). A skill name has one
 * org-wide TRUE variant plus any number of `(repo, person)` forks and earlier
 * revisions. This dropdown lists them all (TRUE first, flagged), defaults to the
 * currently-selected variant (the TRUE one in the catalog, the project's pin in
 * the attach flow), and calls `onSelect` when the user picks another.
 *
 * When `allowPromote` is set (the catalog screen), a "Promote to true" button
 * repoints the org-wide TRUE pointer to the selected variant via the (NOT
 * admin-gated) promote mutation; promotion never edits or deletes a variant.
 * The project attach flow renders it WITHOUT promote — it only pins which
 * variant that repo materializes.
 */
export function VariantSwitcher({
  name,
  selectedVariantId,
  onSelect,
  allowPromote = false,
  lazy = false,
  className,
}: {
  /** The shared skill name whose variants this switches over. */
  name: string;
  /** The currently-chosen variantId; undefined defaults to the TRUE variant. */
  selectedVariantId?: string;
  /** Fired with the chosen variant when the dropdown changes. */
  onSelect?: (variant: SkillVariant) => void;
  /** Catalog only: expose the "Promote to true" action. */
  allowPromote?: boolean;
  /**
   * Defer the per-name variants fetch until the user actually engages the
   * switcher (hover/focus). The org Skills catalog turns this on so a grid of N
   * cards does NOT fire N `skills/:name/variants` requests on page load — the
   * dropdown is rarely opened, so we pay for it on demand. The controlled attach
   * flow leaves it off so its dropdown is pre-populated with the pinned variant.
   */
  lazy?: boolean;
  className?: string;
}) {
  // Until activated, a lazy switcher skips its fetch and shows a collapsed
  // placeholder; the first hover/focus flips this and the query fires.
  const [activated, setActivated] = useState(false);
  const collapsed = lazy && !activated;
  const { data, isLoading } = useGetSkillVariantsQuery(name, { skip: collapsed });
  const [promote, { isLoading: promoting }] = usePromoteSkillMutation();

  // The switcher keeps its OWN selection so it works uncontrolled (the catalog,
  // where promote acts on whatever the user picked) and controlled (the project
  // attach flow, where `selectedVariantId` seeds the pin). `null` = fall back to
  // the prop / TRUE variant below.
  const [picked, setPicked] = useState<string | null>(null);

  // Order variants TRUE-first, then newest revision first, so the default option
  // and the most relevant forks surface at the top of the dropdown.
  const variants = useMemo(() => {
    const list = [...(data ?? [])];
    list.sort((a, b) => {
      if (!!a.isTrue !== !!b.isTrue) return a.isTrue ? -1 : 1;
      return (b.version ?? 0) - (a.version ?? 0);
    });
    return list;
  }, [data]);

  const trueVariant = variants.find((v) => v.isTrue);
  // The effective selection: the user's in-component pick, else the caller's pin,
  // else the TRUE variant, else first.
  const selected =
    variants.find((v) => v.variantId === picked) ??
    variants.find((v) => v.variantId === selectedVariantId) ??
    trueVariant ??
    variants[0];

  // Nothing to switch between (a single base variant) — render nothing so a
  // never-edited skill shows no clutter. While collapsed we don't yet know the
  // count (the fetch is deferred), so keep the placeholder so it can activate.
  if (!collapsed && !isLoading && variants.length <= 1 && !allowPromote) return null;

  const activate = () => setActivated(true);

  const label = (v: SkillVariant): string => {
    const who = v.authorName ?? v.authorUserId;
    const where = v.repoId;
    const prov = where ? `${who ?? 'someone'}@${where}` : 'base';
    const star = v.isTrue ? ' ★ true' : '';
    return `${prov} · v${v.version}${star}`;
  };

  const onChange = (variantId: string) => {
    const v = variants.find((x) => x.variantId === variantId);
    if (!v) return;
    setPicked(variantId);
    if (onSelect) onSelect(v);
  };

  const onPromote = () => {
    if (!selected || selected.isTrue) return;
    promote({ name, variantId: selected.variantId, rev: selected.version });
  };

  return (
    <div
      className={`flex flex-wrap items-center gap-1.5 text-[11px] text-mut ${className ?? ''}`}
      data-testid={`variant-switcher-${name}`}
      onMouseEnter={lazy ? activate : undefined}
      onFocus={lazy ? activate : undefined}
    >
      <label className="flex items-center gap-1">
        <span className="text-faint">version</span>
        <select
          className="hq-btn"
          data-testid={`variant-select-${name}`}
          value={selected?.variantId ?? ''}
          // Collapsed lazy switchers stay enabled so a hover/focus can wake them;
          // once fetching they disable until options arrive.
          disabled={!collapsed && (isLoading || variants.length === 0)}
          onChange={(e) => onChange(e.target.value)}
        >
          {collapsed && <option value="">version ▾</option>}
          {!collapsed && isLoading && <option value="">loading…</option>}
          {variants.map((v) => (
            <option key={v.variantId} value={v.variantId}>
              {label(v)}
            </option>
          ))}
        </select>
      </label>
      {allowPromote && (
        <button
          type="button"
          className="hq-btn"
          data-testid={`promote-variant-${name}`}
          disabled={promoting || !selected || !!selected.isTrue}
          onClick={onPromote}
          title={selected?.isTrue ? 'Already the true version' : 'Make this the org-wide true version'}
        >
          {promoting ? 'Promoting…' : '★ Promote to true'}
        </button>
      )}
    </div>
  );
}
