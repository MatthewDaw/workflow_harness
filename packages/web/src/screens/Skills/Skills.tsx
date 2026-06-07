import { useMemo, useState } from 'react';
import type { Skill } from '@harness/shared';
import { useGetSkillsQuery } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';
import { SkillCard, authorOf } from '../../components/SkillCard.js';

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
 * Skills registry: the single org-scoped catalog (the scope-collapse model — no
 * per-skill tier picker). Bundles drill in; skills that belong to a bundle are
 * hidden from the top-level grid by default and revealed by the toggle. An
 * optional author filter narrows the catalog.
 */
export function Skills() {
  const { data, isLoading } = useGetSkillsQuery();
  const skills = data ?? [];

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
    <div className="hq-pad" data-testid="skills-screen">
      <ScreenHeader
        title="Skills (org catalog)"
        subtitle="The catalog agents and projects draw from. Bundles open into their sub-skills."
      />
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
      {isLoading && <div className="text-mut">Loading skills…</div>}
      {!isLoading && catalog.length === 0 && (
        <div className="hq-box text-mut">No skills in the catalog.</div>
      )}
      <div className="grid grid-cols-3 gap-3.5">
        {catalog.map((s) => (
          <SkillCard key={s.name} skill={s} />
        ))}
      </div>
    </div>
  );
}
