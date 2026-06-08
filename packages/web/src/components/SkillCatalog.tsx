import { useMemo, useState, type ReactNode } from 'react';
import type { Skill } from '@harness/shared';
import { SkillCard, authorOf } from './SkillCard.js';

const ANY_AUTHOR = '__any__';

/** Names that are members of any resolved bundle (transitive leaves preferred). */
function bundleMemberNames(skills: Skill[]): Set<string> {
  const names = new Set<string>();
  for (const s of skills) {
    if (s.kind !== 'bundle') continue;
    const members = s.resolvedMembers ?? s.members;
    for (const m of members) names.add(m);
  }
  return names;
}

/**
 * The shared skills-catalog view: the filter bar (bundle toggle + author filter)
 * over the 3-column `SkillCard` grid, with bundle members collapsed under their
 * bundle card by default. This is the SINGLE rendering of a skill catalog —
 * the global Skills screen and a project's Skills tab both render through it, so
 * the two read identically and bundles drill into their sub-skills the same way
 * in both. An optional `renderFooter` hangs a per-card control (e.g. a project's
 * remove button) under each card, mirroring the bundle member-grid layout.
 */
export function SkillCatalog({
  skills,
  renderFooter,
  emptyHint = 'No skills in the catalog.',
  showVariants = false,
}: {
  skills: Skill[];
  renderFooter?: (skill: Skill) => ReactNode;
  emptyHint?: string;
  /**
   * Catalog versioning (KTD6): show the per-skill variant switcher + "Promote to
   * true" on each plain card. The global Skills catalog turns this on; the
   * project Skills tab leaves it off (it pins variants in the attach flow).
   */
  showVariants?: boolean;
}) {
  const [showInBundles, setShowInBundles] = useState(false);
  const [author, setAuthor] = useState<string>(ANY_AUTHOR);
  const memberNames = useMemo(() => bundleMemberNames(skills), [skills]);

  const authors = useMemo(() => {
    const set = new Set<string>();
    for (const s of skills) set.add(authorOf(s));
    return [...set].sort();
  }, [skills]);

  const visible = (s: Skill): boolean => {
    if (author !== ANY_AUTHOR && authorOf(s) !== author) return false;
    if (s.kind === 'bundle') return true;
    if (showInBundles) return true;
    return !memberNames.has(s.name);
  };

  const catalog = skills.filter(visible);

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-4 text-xs text-mut">
        <label className="flex items-center gap-1.5" data-testid="show-in-bundles-toggle">
          <input
            type="checkbox"
            checked={showInBundles}
            onChange={(e) => setShowInBundles(e.target.checked)}
            data-testid="show-in-bundles-checkbox"
          />
          Show skills that are in bundles
        </label>
        {authors.length > 1 && (
          <label className="flex items-center gap-1.5">
            Author
            <select
              className="hq-btn"
              data-testid="author-filter"
              value={author}
              onChange={(e) => setAuthor(e.target.value)}
            >
              <option value={ANY_AUTHOR}>any</option>
              {authors.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {catalog.length === 0 && <div className="hq-box text-mut">{emptyHint}</div>}
      <div className="grid grid-cols-3 gap-3.5">
        {catalog.map((s) =>
          renderFooter ? (
            <div
              key={s.name}
              className="flex flex-col gap-1.5"
              data-testid={`skill-cell-${s.name}`}
            >
              <SkillCard skill={s} showVariants={showVariants} />
              {renderFooter(s)}
            </div>
          ) : (
            <SkillCard key={s.name} skill={s} showVariants={showVariants} />
          ),
        )}
      </div>
    </>
  );
}
