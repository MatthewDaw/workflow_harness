import type { LearningRecord, SessionProjection } from '@harness/shared';
import { relativeTime } from '../../lib/time.js';

/**
 * One row of the within-session topic timeline: a topic segment with the time it
 * opened and a one-line description. A segment opens once; an in-place relabel
 * (judge re-titling the same focus) just updates `label` on the latest row.
 */
export interface TopicSegment {
  segmentId: string;
  label: string;
  /** Epoch-ms the segment first appeared (its open time). */
  openedAt: number;
  description?: string;
}

/**
 * Derive the within-session topic timeline (strictly this session — no
 * cross-session grouping). Segments are mined from the session's learnings
 * (each carries the `segmentId` + `topicLabel` it belongs to); the earliest
 * stamp of a segment is its open time and the latest stamp wins the label, so an
 * in-place relabel updates the row rather than appending a new one. The session
 * projection's CURRENT topic is the live latest segment, so it is merged onto the
 * most-recent row (or appended when no learning has landed for it yet).
 */
export function topicSegments(
  session: SessionProjection | undefined,
  learnings: LearningRecord[],
): TopicSegment[] {
  const bySegment = new Map<string, TopicSegment>();
  for (const l of learnings) {
    const existing = bySegment.get(l.segmentId);
    if (!existing) {
      bySegment.set(l.segmentId, { segmentId: l.segmentId, label: l.topicLabel, openedAt: l.ts });
      continue;
    }
    if (l.ts < existing.openedAt) existing.openedAt = l.ts; // earliest stamp = open time
    if (l.ts >= existing.openedAt) existing.label = l.topicLabel; // latest stamp wins the label
  }
  const segments = [...bySegment.values()].sort((a, b) => a.openedAt - b.openedAt);

  // Fold the live current topic onto the timeline. The projection has no
  // segmentId, so the current topic is the latest segment: relabel the last row
  // when present, else append a synthetic latest row carrying the description.
  if (session?.topic) {
    const last = segments[segments.length - 1];
    const openedAt = session.summaryUpdatedAt ?? session.lastEventAt;
    if (last) {
      last.label = session.topic;
      last.description = session.description;
    } else {
      segments.push({
        segmentId: 'current',
        label: session.topic,
        openedAt,
        description: session.description,
      });
    }
  }
  return segments;
}

/** The within-session topic timeline (chronological, oldest first). */
export function TopicTimeline({
  segments,
  now,
  isLoading,
}: {
  segments: TopicSegment[];
  now: number;
  isLoading: boolean;
}) {
  return (
    <div className="hq-box bg-paper" data-testid="topic-timeline">
      <div className="text-[11px] uppercase tracking-wide text-faint">Topic timeline</div>
      {isLoading ? (
        <div className="mt-1.5 text-xs text-mut" data-testid="topic-timeline-loading">
          Loading topics…
        </div>
      ) : segments.length === 0 ? (
        <div className="mt-1.5 text-xs text-faint" data-testid="topic-timeline-empty">
          No topic segments yet
        </div>
      ) : (
        <ol className="mt-1.5 flex flex-col gap-2">
          {segments.map((seg) => (
            <li
              key={seg.segmentId}
              className="border-l-2 border-line2 pl-2.5"
              data-testid="topic-segment"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs font-semibold" data-testid="topic-segment-label">
                  {seg.label}
                </span>
                <span className="shrink-0 text-[11px] text-faint">
                  {relativeTime(seg.openedAt, now)}
                </span>
              </div>
              {seg.description && (
                <div
                  className="truncate text-[11px] text-mut"
                  data-testid="topic-segment-desc"
                  title={seg.description}
                >
                  {seg.description}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/**
 * One labeled learnings section (impl or doc). Both are always rendered — the doc
 * section deliberately shows a zero-state rather than hiding when empty.
 */
export function LearningSection({
  title,
  learnings,
  emptyLabel,
  isLoading,
  testid,
}: {
  title: string;
  learnings: LearningRecord[];
  emptyLabel: string;
  isLoading: boolean;
  testid: string;
}) {
  const rows = [...learnings].sort((a, b) => a.ts - b.ts);
  return (
    <div className="hq-box bg-paper" data-testid={testid}>
      <div className="text-[11px] uppercase tracking-wide text-faint">{title}</div>
      {isLoading ? (
        <div className="mt-1.5 text-xs text-mut" data-testid={`${testid}-loading`}>
          Loading…
        </div>
      ) : rows.length === 0 ? (
        <div className="mt-1.5 text-xs text-faint" data-testid={`${testid}-empty`}>
          {emptyLabel}
        </div>
      ) : (
        <ul className="mt-1.5 flex flex-col gap-2">
          {rows.map((l) => (
            <li
              key={`${l.sessionId}#${l.turnId}`}
              className="text-xs"
              data-testid={`${testid}-row`}
            >
              <div className="text-faint">{l.topicLabel}</div>
              <div className="text-mut">{l.text}</div>
              {l.docRef && <div className="font-mono text-[11px] text-faint">{l.docRef}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
