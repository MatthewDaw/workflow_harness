import type { UnassignedIdea } from '../../api/baseApi.js';
import { relativeTime, useNow } from '../../lib/time.js';

/**
 * Sort the bin most-frequent-first (a recurring off-catalog topic is the
 * strongest new-skill candidate), breaking ties by most-recent. The backend
 * already orders this way; we re-sort defensively so the bare-array test stub
 * and any unordered source still render strongest-first.
 */
function sortByFrequency(entries: UnassignedIdea[]): UnassignedIdea[] {
  return [...entries].sort((a, b) => {
    const byFrequency = b.frequency - a.frequency;
    if (byFrequency !== 0) return byFrequency;
    return b.updatedAt - a.updatedAt;
  });
}

/**
 * The org's unassigned bin (skill-idea loop, U15): topics the judge rejected
 * from every candidate skill — the new-skill backlog. A read-table mirroring
 * `SessionsTable`: each row shows the topic text, its provenance, its frequency
 * (distinct sessions), and how long ago it last recurred. Acting on an entry
 * (creating a skill from it) is admin-only; non-admins see the read-only table
 * with no action affordance.
 */
export function BinTable({
  entries,
  isAdmin = false,
}: {
  entries: UnassignedIdea[];
  isAdmin?: boolean;
}) {
  const rows = sortByFrequency(entries);
  // One ticking "now" for every row so the "last seen" labels age in place.
  const now = useNow();
  if (rows.length === 0) {
    return (
      <div className="hq-box text-mut" data-testid="bin-empty">
        No unassigned topics — every recent finding routed to a skill.
      </div>
    );
  }
  return (
    <table className="w-full border-collapse text-[13px]" data-testid="bin-table">
      <thead>
        <tr className="text-[11px] uppercase tracking-wide text-faint">
          <th className="border-b border-line2 p-2 text-left">topic</th>
          <th className="border-b border-line2 p-2 text-left">frequency</th>
          <th className="border-b border-line2 p-2 text-left">provenance</th>
          <th className="border-b border-line2 p-2 text-left">last seen</th>
          <th className="border-b border-line2 p-2" />
        </tr>
      </thead>
      <tbody>
        {rows.map((e) => (
          <BinRow key={e.entryId} entry={e} now={now} isAdmin={isAdmin} />
        ))}
      </tbody>
    </table>
  );
}

/** One bin entry row. */
function BinRow({
  entry: e,
  now,
  isAdmin,
}: {
  entry: UnassignedIdea;
  now: number;
  isAdmin: boolean;
}) {
  // Provenance: the distinct projects this topic surfaced in (a quick read on
  // where the gap lives), falling back to the raw session count.
  const projects = Array.from(
    new Set(e.sources.map((s) => s.projectId).filter((p): p is string => Boolean(p))),
  );
  return (
    <tr data-testid={`bin-row-${e.entryId}`}>
      <td className="max-w-[40ch] border-b border-line2 p-2" data-testid={`bin-topic-${e.entryId}`}>
        {e.text ? (
          <span className="line-clamp-2" title={e.text}>
            {e.text}
          </span>
        ) : (
          <span className="text-faint" data-testid={`bin-topic-untitled-${e.entryId}`}>
            Untitled topic
          </span>
        )}
      </td>
      <td className="border-b border-line2 p-2 text-mut" data-testid={`bin-frequency-${e.entryId}`}>
        {e.frequency}×
      </td>
      <td
        className="border-b border-line2 p-2 text-faint"
        data-testid={`bin-provenance-${e.entryId}`}
      >
        {projects.length > 0 ? projects.join(', ') : `${e.sources.length} session(s)`}
      </td>
      <td className="border-b border-line2 p-2 text-mut" data-testid={`bin-lastseen-${e.entryId}`}>
        {relativeTime(e.updatedAt, now)}
      </td>
      <td className="border-b border-line2 p-2">
        <div className="flex items-center justify-end">
          {isAdmin && (
            // v1 admin-only action stub: acting on a bin entry (creating a skill
            // from it) is the `/skill-idea-iterate` flow's territory. Disabled —
            // not silently inert — until that flow wires it up. Non-admins never
            // see it.
            <button
              type="button"
              className="hq-btn"
              data-testid={`bin-create-skill-${e.entryId}`}
              disabled
              title="Not wired up yet — run /skill-idea-iterate to create a skill from this topic"
            >
              create skill
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}
