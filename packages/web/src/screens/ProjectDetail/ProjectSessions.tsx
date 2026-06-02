import { useParams } from 'react-router-dom';
import { SessionsTable } from '../Sessions/SessionsTable.js';
import { useGetSessionsQuery } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';

/** Project Sessions sub-tab (U19 scaffold for U21/U23): sessions for this repo. */
export function ProjectSessions() {
  const { projectId = '' } = useParams();
  const { data } = useGetSessionsQuery();
  const rows = (data ?? []).filter((s) => s.projectId === projectId);

  return (
    <div className="hq-pad" data-testid="project-sessions">
      <ScreenHeader title="Sessions" subtitle="Sessions for this project — live first." />
      <SessionsTable sessions={rows} />
    </div>
  );
}
