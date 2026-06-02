import { useParams } from 'react-router-dom';
import { useGetSkillsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Skill bundle detail (U24 scaffold): a bundle is a skill made of skills. Lists
 * its members; nested bundles drill in further.
 */
export function SkillBundle() {
  const { bundleName = '' } = useParams();
  const { data } = useGetSkillsQuery();
  const skills = data ?? [];
  const bundle = skills.find((s) => s.name === bundleName && s.kind === 'bundle');

  return (
    <div className="hq-pad" data-testid="skill-bundle">
      <ScreenHeader
        title={`▤ ${bundleName}`}
        subtitle="A bundle made of skills. Add, remove, or eject members; nested bundles drill in."
      />
      {!bundle && <div className="hq-box text-mut">Bundle not found.</div>}
      {bundle && (
        <div className="hq-box bg-paper">
          <div className="flex items-center justify-between">
            <b>
              {bundle.name}{' '}
              <span className="text-xs text-faint">· {bundle.members.length} sub-skills</span>
            </b>
            <button type="button" className="hq-btn hq-btn-pri">
              + Add skill to bundle
            </button>
          </div>
          <div className="hq-hr" />
          {bundle.members.map((m) => {
            const member = skills.find((s) => s.name === m);
            const isNested = member?.kind === 'bundle';
            return (
              <div
                key={m}
                className="flex items-center justify-between border-b border-line2 py-1.5 text-[13px]"
              >
                <span>
                  <Pill variant="skill" className="mr-1.5">
                    {isNested ? `▤ ${m}` : m}
                  </Pill>
                  {isNested && <Pill>nested bundle</Pill>}
                </span>
                <button type="button" className="hq-btn">
                  ✕ remove
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
