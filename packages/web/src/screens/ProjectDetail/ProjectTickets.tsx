import { useParams } from 'react-router-dom';
import type { TicketStatus } from '@harness/shared';
import { useGetTicketsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const COLUMN_LABELS: Record<TicketStatus, string> = {
  backlog: 'Backlog',
  in_progress: 'In progress',
  in_review: 'In review',
  done: 'Done',
  icebox: 'Icebox',
};

/**
 * Project Tickets sub-tab (U19 scaffold for U22): a lifecycle board grouped by
 * status. Tickets are project-scoped and sourced from the HQ ticket store.
 */
export function ProjectTickets() {
  const { projectId = '' } = useParams();
  const { data: tickets, isLoading } = useGetTicketsQuery(projectId, { skip: !projectId });
  const all = tickets ?? [];

  return (
    <div className="hq-pad" data-testid="project-tickets">
      <ScreenHeader
        title="Tickets"
        subtitle="Project-scoped backlog — HQ is the source of truth."
      />
      {isLoading && <div className="text-mut">Loading tickets…</div>}
      <div className="grid grid-cols-3 gap-3.5">
        {(['backlog', 'in_progress', 'in_review'] as const).map((status) => {
          const items = all.filter((t) => t.status === status);
          return (
            <div key={status}>
              <div className="mb-1.5 text-[11px] uppercase tracking-wide text-faint">
                {COLUMN_LABELS[status]} · {items.length}
              </div>
              {items.length === 0 && <div className="hq-box text-xs text-mut">Empty</div>}
              {items.map((t) => (
                <div key={t.id} className="hq-box mb-2 bg-paper">
                  <div className="flex justify-between">
                    <span className="font-mono text-[11px] text-faint">{t.id}</span>
                    <Pill>● {t.priority}</Pill>
                  </div>
                  <div className="mt-1 text-[13px]">{t.title}</div>
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
