import { useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { useGetWeeklyQuery } from '../../api/baseApi.js';
import { Bar, Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Project Weekly sub-tab (U19 scaffold for U25): the latest agent-authored
 * weekly update with Done / Plan subsections.
 */
export function ProjectWeekly() {
  const { projectId = '' } = useParams();
  const { data, isLoading } = useGetWeeklyQuery(projectId, { skip: !projectId });
  // The list is not guaranteed ordered (and arrived oldest-first); show the
  // newest week by sorting on the lexicographically-comparable ISO week (desc).
  const latest = useMemo(() => {
    const weeks = [...(data ?? [])];
    weeks.sort((a, b) => b.isoWeek.localeCompare(a.isoWeek));
    return weeks[0];
  }, [data]);

  return (
    <div className="hq-pad" data-testid="project-weekly">
      <ScreenHeader
        title="Weekly Update"
        subtitle="Authored with a Claude agent — Done (from git) + Plan (validated)."
      />
      <div className="mb-2.5 flex items-center justify-between">
        <div className="text-mut">
          {latest ? `Week ${latest.isoWeek}` : 'No weekly update yet'}{' '}
          {latest?.validated && <Pill variant="good">plan validated</Pill>}
          {latest?.conformityScore !== undefined && (
            <Pill variant={latest.conformityScore >= 70 ? 'good' : 'idle'}>
              conformity {latest.conformityScore}
            </Pill>
          )}
        </div>
        <span className="text-mut text-[12.5px]">
          Run <code>/weekly-update</code> in claude+ to draft this report.
        </span>
      </div>
      {isLoading && <div className="text-mut">Loading…</div>}
      {latest && (
        <div className="flex gap-3.5">
          <div className="hq-box flex-1 bg-paper">
            <b>Done last week</b>
            {latest.conformityScore !== undefined && (
              <div className="my-1.5">
                <Bar pct={latest.conformityScore} color="#3f7d4e" />
              </div>
            )}
            <p className="whitespace-pre-wrap text-[12.5px] text-mut">
              {latest.done || 'No summary recorded.'}
            </p>
          </div>
          <div className="hq-box flex-1 border-l-[3px] border-l-accent bg-paper">
            <b>Plan next week</b>
            <p className="mt-1.5 whitespace-pre-wrap text-[12.5px] text-mut">
              {latest.plan || 'No plan recorded.'}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
