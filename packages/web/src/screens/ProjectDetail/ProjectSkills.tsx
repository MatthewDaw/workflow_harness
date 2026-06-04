import { useState } from 'react';
import { useParams } from 'react-router-dom';
import type { Skill } from '@harness/shared';
import {
  useGetProjectQuery,
  useGetSkillsQuery,
  useEnableProjectSkillMutation,
  useDisableProjectSkillMutation,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';
import { SkillCombobox } from '../../components/SkillCombobox.js';

/**
 * Project Skills sub-tab (collapsed model): manage the project's enabledSkills
 * directly against the org catalog. Add via a searchable + author-filterable
 * combobox; remove with a per-row button.
 */
export function ProjectSkills() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: catalogData } = useGetSkillsQuery();
  const catalog = catalogData ?? [];

  const [enableSkill] = useEnableProjectSkillMutation();
  const [disableSkill] = useDisableProjectSkillMutation();

  const [addError, setAddError] = useState<string | null>(null);

  const enabled = project?.enabledSkills ?? [];
  const byName = new Map<string, Skill>(catalog.map((s) => [s.name, s]));

  // Candidates: catalog skills not already enabled.
  const candidates = catalog.filter((s) => !enabled.includes(s.name));

  const onAdd = async (skillName: string) => {
    if (!skillName || !projectId) return;
    try {
      await enableSkill({ projectId, skillName }).unwrap();
      setAddError(null);
    } catch {
      // The Skills tab can only enable skills that exist in the org catalog; a
      // 404 here means the name isn't registered. Surface it instead of failing
      // silently — registering happens via /hq-add-skill.
      setAddError(
        `Couldn't add "${skillName}" — it isn't in the org catalog. Register it first with /hq-add-skill.`,
      );
    }
  };
  const onRemove = (skillName: string) => {
    if (!projectId) return;
    disableSkill({ projectId, skillName });
  };

  return (
    <div className="hq-pad" data-testid="project-skills">
      <ScreenHeader
        title="Skills"
        subtitle="Skills enabled for this project. Add directly from the org catalog; agents also auto-add their skills here."
      />

      <div className="hq-box bg-paper">
        <div className="flex items-center justify-between">
          <b>Add a skill</b>
          <SkillCombobox
            testid="enable-skill"
            placeholder="Search the org catalog…"
            buttonLabel="+ Add to project"
            options={candidates.map((c) => ({
              name: c.name,
              hint: c.kind === 'bundle' ? 'bundle' : undefined,
              author: c.createdBy?.name,
            }))}
            onCommit={onAdd}
            emptyHint="Not in the org catalog — register it with /hq-add-skill first."
          />
        </div>
        {addError && (
          <div className="mt-2 text-xs text-rose-600" role="alert" data-testid="add-skill-error">
            {addError}
          </div>
        )}
      </div>

      {enabled.length === 0 && (
        <div className="hq-box mt-3 text-mut" data-testid="project-skills-empty">
          No skills enabled yet.
        </div>
      )}
      {enabled.length > 0 && (
        <div className="hq-box mt-3 bg-paper">
          {enabled.map((name) => {
            const skill = byName.get(name);
            return (
              <div
                key={name}
                className="flex items-center justify-between border-b border-line2 py-1.5 text-[13px]"
                data-testid={`enabled-skill-${name}`}
              >
                <span>
                  <Pill variant="skill" className="mr-1.5">
                    {skill?.kind === 'bundle' ? `▤ ${name}` : name}
                  </Pill>
                  <span className="text-xs text-mut">{skill?.description ?? ''}</span>
                </span>
                <button
                  type="button"
                  className="hq-btn"
                  data-testid={`remove-skill-${name}`}
                  onClick={() => onRemove(name)}
                >
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
