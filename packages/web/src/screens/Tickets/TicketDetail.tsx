import { Link, useParams } from 'react-router-dom';
import type { Ticket, TicketStatus, Priority } from '@harness/shared';
import { PRIORITIES, TICKET_TRANSITIONS } from '@harness/shared';
import {
  useGetTicketsQuery,
  useGetSessionsQuery,
  useTransitionTicketMutation,
  useUpdateTicketMutation,
} from '../../api/baseApi.js';
import { Pill, StatusDot, ScreenHeader } from '../../components/primitives.js';

const STATUS_LABEL: Record<TicketStatus, string> = {
  backlog: 'backlog',
  in_progress: 'in progress',
  in_review: 'in review',
  done: 'done',
  icebox: 'icebox',
};

/**
 * Ticket detail (U22): the spec, the activity log, and the
 * ticket → session → branch → PR chain, each node linking to its live view.
 * Actions write straight back to the HQ ticket store (move status, re-prioritize).
 */
export function TicketDetail() {
  const { projectId = '', ticketId = '' } = useParams();
  const { data: tickets, isLoading } = useGetTicketsQuery(projectId, { skip: !projectId });
  const { data: sessions } = useGetSessionsQuery();
  const [transition, { isLoading: moving }] = useTransitionTicketMutation();
  const [update] = useUpdateTicketMutation();

  const ticket = (tickets ?? []).find((t) => t.id === ticketId);
  const session = ticket?.sessionId
    ? (sessions ?? []).find((s) => s.sessionId === ticket.sessionId)
    : undefined;

  if (isLoading) return <div className="hq-pad text-mut">Loading ticket…</div>;
  if (!ticket) {
    return (
      <div className="hq-pad" data-testid="ticket-detail">
        <div className="hq-box text-mut">Ticket not found.</div>
      </div>
    );
  }

  const nextStatuses = TICKET_TRANSITIONS[ticket.status];

  return (
    <div className="p-3.5" data-testid="ticket-detail">
      <div className="flex gap-3.5">
        <div className="flex-[2]">
          <div className="hq-box bg-paper">
            <div className="flex items-baseline justify-between">
              <div>
                <span className="font-mono text-[11px] text-faint">
                  {ticket.id} · {projectId}
                </span>
                <h3 className="my-0.5 text-base font-semibold">{ticket.title}</h3>
              </div>
              <div className="flex gap-1.5">
                <Pill>● {ticket.priority}</Pill>
                <Pill variant={ticket.status === 'done' ? 'good' : 'live'}>
                  {STATUS_LABEL[ticket.status]}
                </Pill>
              </div>
            </div>

            <div className="mt-2.5 text-[11px] uppercase tracking-wide text-faint">Description</div>
            <div className="mt-1.5 text-[13px] text-mut" data-testid="ticket-description">
              {ticket.description ?? 'No description yet.'}
            </div>

            <div className="mt-3.5 text-[11px] uppercase tracking-wide text-faint">Activity</div>
            <table className="mt-1 w-full border-collapse text-[13px]">
              <tbody>
                {ticket.sessionId && (
                  <tr>
                    <td className="w-[90px] py-1 align-top text-faint">started</td>
                    <td className="py-1 text-mut">
                      <StatusDot variant="live" />
                      session <span className="font-mono">#{ticket.sessionId}</span>
                      {session?.agent && (
                        <>
                          {' '}
                          as <b>{session.agent}</b>
                        </>
                      )}
                    </td>
                  </tr>
                )}
                {ticket.branch && (
                  <tr>
                    <td className="py-1 align-top text-faint">branch</td>
                    <td className="py-1 text-mut">
                      branch <span className="font-mono">{ticket.branch}</span> pushed
                    </td>
                  </tr>
                )}
                {ticket.pr && (
                  <tr>
                    <td className="py-1 align-top text-faint">PR</td>
                    <td className="py-1 text-mut">
                      <Pill variant="good">{ticket.pr}</Pill> opened
                    </td>
                  </tr>
                )}
                {!ticket.sessionId && !ticket.branch && !ticket.pr && (
                  <tr>
                    <td className="py-1 text-mut" colSpan={2}>
                      No activity yet — start a session to begin.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="flex-1">
          <div className="hq-box mb-3 bg-paper" data-testid="ticket-chain">
            <div className="text-[11px] uppercase tracking-wide text-faint">The chain</div>
            <div className="mt-2 text-[13px] text-mut">
              <div>
                ticket <b>{ticket.id}</b>
              </div>
              <div className="text-faint">↓</div>
              <div>
                {ticket.sessionId ? (
                  <>
                    session <span className="font-mono">#{ticket.sessionId}</span>{' '}
                    {session && session.status !== 'done' && (
                      <Pill variant="live" className="!text-[10px]">
                        live
                      </Pill>
                    )}{' '}
                    <Link to={`/sessions/${ticket.sessionId}`} className="hq-btn">
                      watch
                    </Link>
                  </>
                ) : (
                  <span className="text-faint">session — not started</span>
                )}
              </div>
              <div className="text-faint">↓</div>
              <div>
                {ticket.branch ? (
                  <>
                    branch <span className="font-mono">{ticket.branch}</span>
                  </>
                ) : (
                  <span className="text-faint">branch — none yet</span>
                )}
              </div>
              <div className="text-faint">↓</div>
              <div>
                {ticket.pr ? (
                  <Pill variant="good">{ticket.pr}</Pill>
                ) : (
                  <span className="text-faint">PR — not opened yet</span>
                )}
              </div>
            </div>
          </div>

          <div className="hq-box bg-paper">
            <div className="text-[11px] uppercase tracking-wide text-faint">Actions</div>
            <div className="mt-2 flex flex-col gap-1.5">
              <button
                type="button"
                className="hq-btn hq-btn-pri text-left"
                disabled={moving}
                onClick={() =>
                  void transition({ projectId, ticketId, status: 'in_progress' })
                }
              >
                ▶ Start / attach a session
              </button>

              <label className="text-[11px] text-faint">Change priority</label>
              <select
                aria-label="Change priority"
                className="rounded-md border border-line p-1.5 text-xs"
                value={ticket.priority}
                onChange={(e) =>
                  void update({ projectId, ticketId, priority: e.target.value as Priority })
                }
              >
                {PRIORITIES.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>

              <label className="text-[11px] text-faint">Move status</label>
              <select
                aria-label="Move status"
                className="rounded-md border border-line p-1.5 text-xs"
                value={ticket.status}
                onChange={(e) =>
                  void transition({
                    projectId,
                    ticketId,
                    status: e.target.value as TicketStatus,
                  })
                }
              >
                <option value={ticket.status}>{STATUS_LABEL[ticket.status]} (current)</option>
                {nextStatuses.map((s: TicketStatus) => (
                  <option key={s} value={s}>
                    → {STATUS_LABEL[s]}
                  </option>
                ))}
              </select>
            </div>
            <div className="hq-hr" />
            <div className="text-[11px] text-faint">
              edits write to the HQ ticket store · reflected in the terminal instantly
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export type { Ticket };
