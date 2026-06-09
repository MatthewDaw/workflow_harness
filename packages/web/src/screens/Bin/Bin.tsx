import { useGetUnassignedIdeasQuery, useGetMeQuery } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';
import { BinTable } from './BinTable.js';

/**
 * The org's unassigned bin (skill-idea loop, U15/R6/R7): the new-skill backlog
 * of topics the judge rejected from every candidate skill. The whole org may
 * read it; only an admin sees the per-entry create-skill action (resolved from
 * `/me`, mirrored by the server-side admin gate on the action endpoint).
 */
export function Bin() {
  const { data, isLoading, isError } = useGetUnassignedIdeasQuery();
  const { data: me } = useGetMeQuery();
  const isAdmin = Boolean(me?.admin);
  const entries = data ?? [];

  return (
    <div className="hq-pad" data-testid="bin-screen">
      <ScreenHeader
        title="Unassigned bin"
        subtitle="Recurring topics that found no skill — the new-skill backlog."
      />
      {isLoading && <div className="text-mut">Loading bin…</div>}
      {isError && (
        <div className="hq-note" role="alert">
          Could not load the unassigned bin.
        </div>
      )}
      {!isLoading && <BinTable entries={entries} isAdmin={isAdmin} />}
    </div>
  );
}
