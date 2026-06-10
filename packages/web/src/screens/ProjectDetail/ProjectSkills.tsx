import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import type { Skill } from '@harness/shared';
import {
  useGetProjectQuery,
  useGetSkillsQuery,
  useEnableProjectSkillMutation,
  useDisableProjectSkillMutation,
  useEnableProjectBundleMutation,
  useDisableProjectBundleMutation,
} from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';
import { SkillCatalog } from '../../components/SkillCatalog.js';
import { bundleMemberNames } from '../../lib/bundles.js';
import { VariantSwitcher } from '../../components/VariantSwitcher.js';
import {
  CatalogPicker,
  type CatalogRef,
  type CatalogPickerRow,
} from '../../components/CatalogPicker.js';

/**
 * Project Skills sub-tab: the project's enabled skills, rendered through the SAME
 * `SkillCatalog` as the global Skills screen so the two read identically —
 * bundles show as a single card that drills into its sub-skills. A project's
 * `enabledSkills` is the flat set of member names, so we reconstruct the bundle
 * cards here: a bundle is shown whenever any of its members are enabled.
 *
 * Adding/removing now flows through the shared `CatalogPicker` modal instead of a
 * combobox: a single "+ Add to project" button opens a staging modal over the
 * org catalog where the user can toggle whole bundles or individual skills, and
 * on Apply we fan the staged diff back out to the per-type project opt-in
 * mutations. Crucially those mutations each do a full read-modify-write of the
 * single project META record, so the diff MUST be applied SEQUENTIALLY (await
 * each) — firing them concurrently would clobber each other's writes. The
 * per-card remove footer below is kept as a harmless secondary affordance.
 */
export function ProjectSkills() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: catalogData } = useGetSkillsQuery();
  const catalog = catalogData ?? [];

  const [enableSkill] = useEnableProjectSkillMutation();
  const [disableSkill] = useDisableProjectSkillMutation();
  const [enableBundle] = useEnableProjectBundleMutation();
  const [disableBundle] = useDisableProjectBundleMutation();

  const [pickerOpen, setPickerOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  // A project's enabled-set entry carries the chosen variant `{ name, variantId }`
  // (KTD6). Until the schema area lands that shape, `enabledSkills` may still be a
  // bare `string[]`; normalize both so the rest of this screen reads plain names
  // and can look up a per-skill pinned variantId defensively.
  type EnabledEntry = string | { name: string; variantId?: string };
  const rawEnabled = (project?.enabledSkills ?? []) as EnabledEntry[];
  const enabled = rawEnabled.map((e) => (typeof e === 'string' ? e : e.name));
  const pinnedVariant = (name: string): string | undefined => {
    const entry = rawEnabled.find((e) => typeof e !== 'string' && e.name === name);
    return entry && typeof entry !== 'string' ? entry.variantId : undefined;
  };
  const enabledSet = new Set(enabled);
  const enabledBundles = project?.enabledBundles ?? [];

  // Reconstruct the catalog view for the project's enabled set: show a bundle
  // card when any of its members are enabled (it drills into the sub-skills the
  // same way the global catalog does), plus each enabled standalone skill. The
  // shared SkillCatalog then collapses the enabled members under their bundle.
  const display = catalog.filter((s) =>
    s.kind === 'bundle'
      ? (s.resolvedMembers ?? s.members).some((m) => enabledSet.has(m))
      : enabledSet.has(s.name),
  );

  // The leaf members of every catalog bundle. Used both to build the modal's
  // bundle member rows and to decide which standalone skills get a top-level row
  // (a skill that belongs to some bundle is only reachable via that bundle).
  const memberNames = useMemo(() => bundleMemberNames(catalog), [catalog]);

  // The modal rows: one bundle row per catalog bundle (members mapped to skill
  // refs), plus one skill row per standalone skill that is NOT a member of any
  // bundle (bundle members are reachable by expanding their bundle).
  const rows: CatalogPickerRow[] = useMemo(() => {
    const out: CatalogPickerRow[] = [];
    for (const s of catalog) {
      if (s.kind === 'bundle') {
        const members = s.resolvedMembers ?? s.members;
        out.push({
          ref: { type: 'bundle', name: s.name },
          label: s.name,
          description: s.description || undefined,
          hint: 'bundle',
          members: members.map((m) => ({
            ref: { type: 'skill', name: m },
            label: m,
          })),
        });
      }
    }
    for (const s of catalog) {
      if (s.kind === 'bundle') continue;
      if (memberNames.has(s.name)) continue;
      out.push({
        ref: { type: 'skill', name: s.name },
        label: s.name,
        description: s.description || undefined,
      });
    }
    return out;
  }, [catalog, memberNames]);

  // The refs that are ON when the modal opens: each enabled bundle as a bundle
  // ref, plus each enabled skill NOT already covered by an enabled bundle as a
  // skill ref. The covered set is each enabled bundle flattened via the catalog.
  const initialSelected: CatalogRef[] = useMemo(() => {
    const covered = new Set<string>();
    const refs: CatalogRef[] = [];
    for (const name of enabledBundles) {
      refs.push({ type: 'bundle', name });
      const bundle = catalog.find((s) => s.kind === 'bundle' && s.name === name);
      if (bundle) for (const m of bundle.resolvedMembers ?? bundle.members) covered.add(m);
    }
    for (const name of enabled) {
      if (covered.has(name)) continue;
      refs.push({ type: 'skill', name });
    }
    return refs;
    // enabled/enabledBundles are fresh arrays each render; key off contents.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled.join('|'), enabledBundles.join('|'), catalog]);

  // Fan the staged diff out to the per-type mutations, SEQUENTIALLY (await each)
  // because every mutation read-modify-writes the whole project META record.
  // On ENABLE process bundles before skills (so a bundle's members are unioned
  // before any individually-picked skill); on DISABLE process skills before
  // bundles (strip individual members before removing the whole bundle).
  const onApply = async (diff: { enable: CatalogRef[]; disable: CatalogRef[] }) => {
    if (!projectId) return;
    setApplying(true);
    setAddError(null);
    try {
      const enableBundles = diff.enable.filter((r) => r.type === 'bundle');
      const enableSkills = diff.enable.filter((r) => r.type === 'skill');
      const disableSkills = diff.disable.filter((r) => r.type === 'skill');
      const disableBundles = diff.disable.filter((r) => r.type === 'bundle');

      for (const ref of enableBundles) {
        await enableBundle({ projectId, bundleName: ref.name }).unwrap();
      }
      for (const ref of enableSkills) {
        await enableSkill({ projectId, skillName: ref.name }).unwrap();
      }
      for (const ref of disableSkills) {
        await disableSkill({ projectId, skillName: ref.name }).unwrap();
      }
      for (const ref of disableBundles) {
        await disableBundle({ projectId, bundleName: ref.name }).unwrap();
      }
    } catch {
      // A 404 here means a name isn't in the org catalog; surface it rather than
      // failing silently — registering happens via /hq-add-skill.
      setAddError(
        "Couldn't apply some changes — an item isn't in the org catalog. Register it first with /hq-add-skill.",
      );
      throw new Error('apply failed');
    } finally {
      setApplying(false);
    }
  };

  const onRemove = (skill: Skill) => {
    if (!projectId) return;
    if (skill.kind === 'bundle') {
      // Removing a bundle card disables every member it currently contributes.
      for (const m of skill.resolvedMembers ?? skill.members) {
        if (enabledSet.has(m)) disableSkill({ projectId, skillName: m });
      }
    } else {
      disableSkill({ projectId, skillName: skill.name });
    }
  };

  return (
    <div className="hq-pad" data-testid="project-skills">
      <ScreenHeader
        title="Skills"
        subtitle="Skills enabled for this project, shown the same way as the org catalog — bundles drill into their sub-skills. Add from the catalog; agents also auto-add their skills here."
      />

      <div className="hq-box bg-paper">
        <div className="flex items-center justify-between">
          <b>Skills</b>
          <button
            type="button"
            className="hq-btn hq-btn-pri"
            data-testid="enable-skill"
            onClick={() => {
              setAddError(null);
              setPickerOpen(true);
            }}
          >
            + Add to project
          </button>
        </div>
        {addError && (
          <div className="mt-2 text-xs text-rose-600" role="alert" data-testid="add-skill-error">
            {addError}
          </div>
        )}
      </div>

      {enabled.length === 0 ? (
        <div className="hq-box mt-3 text-mut" data-testid="project-skills-empty">
          No skills enabled yet.
        </div>
      ) : (
        <div className="mt-3">
          <SkillCatalog
            skills={display}
            emptyHint="No skills enabled yet."
            renderFooter={(s) => (
              <div className="flex flex-col gap-1.5">
                {s.kind !== 'bundle' && (
                  // Per-repo variant pin (KTD6): default to the org-wide TRUE
                  // variant, but let this project pin another fork/revision. Picking
                  // one re-enables the skill with that variantId, which the opt-in
                  // handler records on the enabled-set entry; sync materializes it.
                  <VariantSwitcher
                    name={s.name}
                    selectedVariantId={pinnedVariant(s.name)}
                    onSelect={(v) =>
                      projectId &&
                      enableSkill({ projectId, skillName: s.name, variantId: v.variantId })
                    }
                  />
                )}
                <button
                  type="button"
                  className="hq-btn self-start"
                  data-testid={`remove-skill-${s.name}`}
                  onClick={() => onRemove(s)}
                >
                  ✕ remove
                </button>
              </div>
            )}
          />
        </div>
      )}

      <CatalogPicker
        open={pickerOpen}
        title="Add skills to project"
        rows={rows}
        initialSelected={initialSelected}
        emptyHint="Nothing in the org catalog yet — register skills with /hq-add-skill first."
        onClose={() => setPickerOpen(false)}
        onApply={onApply}
        applying={applying}
      />
    </div>
  );
}
