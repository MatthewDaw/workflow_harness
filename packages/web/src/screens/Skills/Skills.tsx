import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Skill } from '@harness/shared';
import { useGetSkillsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const ANY_AUTHOR = '__any__';

/** Author label for a skill; undefined createdBy groups under "Unknown". */
function authorOf(s: { createdBy?: { name: string } }): string {
  return s.createdBy?.name ?? 'Unknown';
}

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

/** Skills registry (collapsed model): a single flat org catalog. */
export function Skills() {
  const { data, isLoading } = useGetSkillsQuery();
  const skills = data ?? [];

  // By default, skills that belong to a bundle are surfaced via the bundle card
  // only — hide them from the top-level grid until the toggle reveals them.
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
      <div className="mb-3 flex items-center gap-3">
        <label
          className="flex items-center gap-1.5 text-xs text-mut"
          data-testid="show-in-bundles-toggle"
        >
          <input
            type="checkbox"
            checked={showInBundles}
            onChange={(e) => setShowInBundles(e.target.checked)}
            data-testid="show-in-bundles-checkbox"
          />
          Show skills that are in bundles
        </label>
        <label className="flex items-center gap-1.5 text-xs text-mut">
          Author
          <select
            className="hq-btn"
            data-testid="skill-author-filter"
            value={author}
            onChange={(e) => setAuthor(e.target.value)}
          >
            <option value={ANY_AUTHOR}>any author</option>
            {authors.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
      </div>
      {isLoading && <div className="text-mut">Loading skills…</div>}
      <div className="grid grid-cols-3 gap-3.5" data-testid="skill-catalog-grid">
        {catalog.map((s) => (
          <SkillCard key={s.name} skill={s} />
        ))}
      </div>
    </div>
  );
}

function SkillCard({ skill }: { skill: Skill }) {
  const author = authorOf(skill);
  if (skill.kind === 'bundle') {
    const memberCount = (skill.resolvedMembers ?? skill.members).length;
    return (
      <Link
        to={`/skills/${skill.name}`}
        className="hq-box block border-l-[3px] border-l-[#6f8fb5] bg-paper no-underline text-ink"
        data-testid={`skill-card-${skill.name}`}
      >
        <div className="flex items-center justify-between">
          <span>
            <Pill variant="skill" className="font-semibold">
              ▤ {skill.name}
            </Pill>{' '}
            <Pill>bundle</Pill>
          </span>
          <span className="text-[11px] text-faint">{memberCount} skills ›</span>
        </div>
        <div className="my-1.5 text-xs text-mut">{skill.description}</div>
        <div className="mt-1.5 text-[11px] text-faint" data-testid={`skill-author-${skill.name}`}>
          by {author}
        </div>
      </Link>
    );
  }
  return (
    <div className="hq-box bg-paper" data-testid={`skill-card-${skill.name}`}>
      <div className="flex justify-between">
        <Pill variant="skill">{skill.name}</Pill>
        <span className="text-[11px] text-faint">{skill.source}</span>
      </div>
      <div className="my-1.5 text-xs text-mut">{skill.description}</div>
      <div className="mt-1.5 text-[11px] text-faint" data-testid={`skill-author-${skill.name}`}>
        by {author}
      </div>
    </div>
  );
}
