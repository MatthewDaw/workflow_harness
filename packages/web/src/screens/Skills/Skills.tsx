import { useGetSkillsQuery } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';
import { SkillCatalog } from '../../components/SkillCatalog.js';

/**
 * Skills registry: the single org-scoped catalog (the scope-collapse model — no
 * per-skill tier picker). The catalog view (filter bar + card grid with bundles
 * collapsed) lives in the shared `SkillCatalog` so the project Skills tab renders
 * identically.
 */
export function Skills() {
  const { data, isLoading } = useGetSkillsQuery();
  const skills = data ?? [];

  return (
    <div className="hq-pad" data-testid="skills-screen">
      <ScreenHeader
        title="Skills (org catalog)"
        subtitle="The catalog agents and projects draw from. Bundles open into their sub-skills."
      />
      {isLoading && <div className="text-mut">Loading skills…</div>}
      {!isLoading && <SkillCatalog skills={skills} />}
    </div>
  );
}
