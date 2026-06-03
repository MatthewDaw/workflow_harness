import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  useGetSkillsQuery,
  useAddBundleMemberMutation,
  useRemoveBundleMemberMutation,
  useDissolveBundleMutation,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Skill bundle detail (U17/U24): a bundle is a skill made of skills. Add/remove
 * members, or dissolve the whole bundle. Destructive ops surface a blast-radius
 * count (how many members are affected) before firing.
 */
export function SkillBundle() {
  const { bundleName = '' } = useParams();
  const navigate = useNavigate();
  const { data } = useGetSkillsQuery();
  const skills = data ?? [];
  const bundle = skills.find((s) => s.name === bundleName && s.kind === 'bundle');

  const [addMember] = useAddBundleMemberMutation();
  const [removeMember] = useRemoveBundleMemberMutation();
  const [dissolve] = useDissolveBundleMutation();
  const [adding, setAdding] = useState('');

  // Candidate members: any skill not already in the bundle and not the bundle itself.
  const candidates = skills.filter(
    (s) => s.name !== bundleName && !(bundle?.members ?? []).includes(s.name),
  );

  const onAdd = () => {
    if (!bundle || !adding) return;
    addMember({ name: bundle.name, scope: bundle.scope, member: adding });
    setAdding('');
  };

  const onRemove = (member: string) => {
    if (!bundle) return;
    removeMember({ name: bundle.name, scope: bundle.scope, member });
  };

  const onDissolve = () => {
    if (!bundle) return;
    dissolve({ name: bundle.name, scope: bundle.scope });
    navigate('/skills');
  };

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
            <div className="flex items-center gap-1.5">
              <label className="sr-only" htmlFor="add-member-select">
                Skill to add
              </label>
              <select
                id="add-member-select"
                className="hq-btn"
                data-testid="add-member-select"
                value={adding}
                onChange={(e) => setAdding(e.target.value)}
              >
                <option value="">Select skill…</option>
                {candidates.map((c) => (
                  <option key={`${c.scope.tier}-${c.name}`} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="hq-btn hq-btn-pri"
                data-testid="add-member"
                disabled={!adding}
                onClick={onAdd}
              >
                + Add skill to bundle
              </button>
            </div>
          </div>
          <div className="hq-hr" />
          {bundle.members.map((m) => {
            const member = skills.find((s) => s.name === m);
            const isNested = member?.kind === 'bundle';
            return (
              <div
                key={m}
                className="flex items-center justify-between border-b border-line2 py-1.5 text-[13px]"
                data-testid={`member-${m}`}
              >
                <span>
                  <Pill variant="skill" className="mr-1.5">
                    {isNested ? `▤ ${m}` : m}
                  </Pill>
                  {isNested && <Pill>nested bundle</Pill>}
                </span>
                <button
                  type="button"
                  className="hq-btn"
                  data-testid={`remove-member-${m}`}
                  onClick={() => onRemove(m)}
                >
                  ✕ remove
                </button>
              </div>
            );
          })}

          <div className="hq-hr" />
          <DissolveControl count={bundle.members.length} onConfirm={onDissolve} />
        </div>
      )}
    </div>
  );
}

/**
 * Dissolve is destructive — it ejects every member to standalone and removes the
 * bundle. Show the blast-radius (member count) and require a confirm click.
 */
function DissolveControl({ count, onConfirm }: { count: number; onConfirm: () => void }) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <button
        type="button"
        className="hq-btn"
        data-testid="dissolve-bundle"
        onClick={() => setConfirming(true)}
      >
        ✕ Dissolve bundle
      </button>
    );
  }
  return (
    <div className="hq-note" data-testid="dissolve-confirm">
      Dissolving frees{' '}
      <b data-testid="blast-radius">
        {count} member{count === 1 ? '' : 's'}
      </b>{' '}
      back to standalone skills and removes the bundle.
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          data-testid="dissolve-confirm-btn"
          onClick={onConfirm}
        >
          Confirm dissolve
        </button>
        <button type="button" className="hq-btn" onClick={() => setConfirming(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}
