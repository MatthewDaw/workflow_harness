import { Link } from 'react-router-dom';
import type { Skill } from '@harness/shared';
import { SCOPE_TIERS, type ScopeTier } from '@harness/shared';
import { useGetSkillsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const SCOPE_LABEL: Record<ScopeTier, string> = {
  org: '◆ Org · all users',
  user: '● My global · all my repos',
  project: '▪ Project',
};

/** Skills registry (U24): scoped catalog; bundles open a sub-skill manager. */
export function Skills() {
  const { data, isLoading } = useGetSkillsQuery();
  const skills = data ?? [];

  return (
    <div className="hq-pad" data-testid="skills-screen">
      <ScreenHeader
        title="Skills (scoped registry)"
        subtitle="The catalog agents draw from. Org → my global → project. Bundles open into their sub-skills."
      />
      {isLoading && <div className="text-mut">Loading skills…</div>}
      {[...SCOPE_TIERS].reverse().map((tier) => {
        const inTier = skills.filter((s) => s.scope.tier === tier);
        if (inTier.length === 0) return null;
        return (
          <section key={tier} className="mb-4">
            <div className="my-1.5 text-xs font-medium text-mut">{SCOPE_LABEL[tier]}</div>
            <div className="grid grid-cols-3 gap-3.5">
              {inTier.map((s) => (
                <SkillCard key={`${tier}-${s.name}`} skill={s} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function SkillCard({ skill }: { skill: Skill }) {
  if (skill.kind === 'bundle') {
    return (
      <Link
        to={`/skills/${skill.name}`}
        className="hq-box block border-l-[3px] border-l-[#6f8fb5] bg-paper no-underline text-ink"
      >
        <div className="flex items-center justify-between">
          <span>
            <Pill variant="skill" className="font-semibold">
              ▤ {skill.name}
            </Pill>{' '}
            <Pill>bundle</Pill>
          </span>
          <span className="text-[11px] text-faint">{skill.members.length} skills ›</span>
        </div>
        <div className="my-1.5 text-xs text-mut">{skill.description}</div>
        <div className="text-[11px] text-faint">scope: {skill.scope.tier}</div>
      </Link>
    );
  }
  return (
    <div className="hq-box bg-paper">
      <div className="flex justify-between">
        <Pill variant="skill">{skill.name}</Pill>
        <span className="text-[11px] text-faint">{skill.source}</span>
      </div>
      <div className="my-1.5 text-xs text-mut">{skill.description}</div>
      <div className="text-[11px] text-faint">scope: {skill.scope.tier}</div>
    </div>
  );
}
