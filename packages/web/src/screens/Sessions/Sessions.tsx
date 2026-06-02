import { useGetSessionsQuery } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';
import { SessionsTable } from './SessionsTable.js';

/** Cross-project sessions firehose (U23): every session, live ones first. */
export function Sessions() {
  const { data, isLoading, isError } = useGetSessionsQuery();
  const sessions = data ?? [];

  return (
    <div className="hq-pad" data-testid="sessions-screen">
      <ScreenHeader
        title="Sessions (all projects)"
        subtitle="Every Claude session anywhere, live ones first."
      />
      {isLoading && <div className="text-mut">Loading sessions…</div>}
      {isError && (
        <div className="hq-note" role="alert">
          Could not load sessions.
        </div>
      )}
      {!isLoading && <SessionsTable sessions={sessions} />}
    </div>
  );
}
