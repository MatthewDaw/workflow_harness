import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Skill } from '@harness/shared';
import { useGetSkillsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const ANY_AUTHOR = '__any__';

const TIER_OPTION: Record<ScopeTier, string> = {
  org: 'Org',
  user: 'My global',
  project: 'Project',
};

function targetScope(tier: ScopeTier, current: ScopeRef, ctx: { org: string; userId: string }): ScopeRef {
  if (tier === 'org') return { tier: 'org', id: ctx.org };
  if (tier === 'user') return { tier: 'user', id: ctx.userId };
  return { tier: 'project', id: current.tier === 'project' ? current.id : ctx.userId };
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

/** Skills registry (U17/U24): scoped catalog with elevate/demote; bundles drill in. */
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
      <label
        className="mb-3 flex items-center gap-1.5 text-xs text-mut"
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
      {isLoading && <div className="text-mut">Loading skills…</div>}
      {[...SCOPE_TIERS].reverse().map((tier) => {
        const inTier = skills.filter((s) => s.scope.tier === tier && visible(s));
        if (inTier.length === 0) return null;
        return (
          <section key={tier} className="mb-4" data-testid={`scope-group-${tier}`}>
            <div className="my-1.5 text-xs font-medium text-mut">{SCOPE_LABEL[tier]}</div>
            <div className="grid grid-cols-3 gap-3.5">
              {inTier.map((s) => (
                <SkillCard key={`${tier}-${s.name}`} skill={s} ctx={ctx} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function ScopePicker({
  skill,
  ctx,
}: {
  skill: Skill;
  ctx: { org: string; userId: string };
}) {
  const [changeScope, { isLoading }] = useChangeSkillScopeMutation();
  const tier = skill.scope.tier;
  return (
    // Stop click/navigation: a bundle card wraps this in a <Link>; without this a
    // click on the control would navigate into the bundle instead of opening it.
    <span
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
    >
      <label className="sr-only" htmlFor={`skill-scope-${skill.name}`}>
        Scope for {skill.name}
      </label>
      <select
        id={`skill-scope-${skill.name}`}
        className="hq-btn"
        data-testid={`skill-scope-picker-${skill.name}`}
        value={tier}
        disabled={isLoading}
        onChange={(e) => {
          const to = e.target.value as ScopeTier;
          if (to === tier) return;
          changeScope({
            name: skill.name,
            from: skill.scope,
            to: targetScope(to, skill.scope, ctx),
          });
        }}
      >
        {SCOPE_TIERS.map((t) => (
          <option key={t} value={t}>
            {TIER_OPTION[t]}
          </option>
        ))}
      </select>
    </span>
  );
}

function SkillCard({ skill, ctx }: { skill: Skill; ctx: { org: string; userId: string } }) {
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
