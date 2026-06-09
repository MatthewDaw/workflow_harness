/**
 * U23 — Observability for the silent-failure modes of the skill-idea loop.
 *
 * The whole loop fails to EMPTY-STATE, not to an error: a topic that never
 * embeds, a skill whose vector never lands, an association that always rejects
 * to the bin — none of these throw a user-visible error. They just quietly stop
 * producing ideas, and a skill a hundred sessions later is no better for it. The
 * research pass flagged this as the dominant risk (E1/E2), so this module makes
 * those outcomes COUNTABLE.
 *
 * The mechanism is CloudWatch Embedded Metric Format (EMF): a structured JSON
 * line on stdout that the Lambda's CloudWatch Logs integration auto-extracts
 * into a CloudWatch metric — no SDK, no `PutMetricData` call, no extra IAM, no
 * added latency on the stream path (it is a single `console.log`). The same line
 * is also a readable structured log, so the metric and the log agree by
 * construction.
 *
 * Two metric families:
 *  - `EmbedOutcome`  — every skill / topic embed attempt, dimensioned
 *    `Outcome=success|failure`. Repeated failures are the DLQ-alarm's leading
 *    indicator (the DLQ only fills after retries exhaust; the failure metric
 *    spikes immediately).
 *  - `AssociationOutcome` — every topic→skill association, dimensioned
 *    `Outcome=candidates|unassigned|unresolved`. The association-to-bin rate
 *    (`unassigned` / total) is the signal that the catalog is mis-routing or the
 *    similarity floor is mis-tuned — observable here instead of inferred from an
 *    empty ideas list.
 *
 * The sink is injectable so tests assert the emitted shape without scraping
 * stdout; the default sink is `console.log`.
 */

/** The EMF namespace every skill-idea-loop metric is published under. */
export const METRIC_NAMESPACE = 'CommandHQ/SkillIdeaLoop';

/** Outcomes of an embed attempt (skill desc+body, or a topic description). */
export type EmbedOutcome = 'success' | 'failure';

/**
 * Outcomes of a topic→skill association, mirroring `AssociationOutcome` in
 * `ideas/associate.ts`:
 *  - `candidates`  — ≥1 skill cleared the floor (will reach the U9 judge);
 *  - `unassigned`  — org resolved but nothing cleared the floor (bin-bound);
 *  - `unresolved`  — no org / no embeddable description (a no-op, never a guess).
 */
export type AssociationMetricOutcome = 'candidates' | 'unassigned' | 'unresolved';

/** A line sink (defaults to stdout); injectable so tests capture the EMF JSON. */
export type MetricSink = (line: string) => void;

const defaultSink: MetricSink = (line) => {
  // eslint-disable-next-line no-console
  console.log(line);
};

/**
 * Build one CloudWatch EMF line for a single metric with a single string
 * dimension. Kept tiny and pure so both emitters share it and the shape is
 * trivially assertable in a test.
 */
function emfLine(
  metricName: string,
  dimensionName: string,
  dimensionValue: string,
  value: number,
  now: number,
): string {
  return JSON.stringify({
    _aws: {
      Timestamp: now,
      CloudWatchMetrics: [
        {
          Namespace: METRIC_NAMESPACE,
          Dimensions: [[dimensionName]],
          Metrics: [{ Name: metricName, Unit: 'Count' }],
        },
      ],
    },
    [dimensionName]: dimensionValue,
    [metricName]: value,
  });
}

/**
 * Emit a single `EmbedOutcome` count (`Outcome=success|failure`). Called on every
 * embed attempt in the stream consumer — a `failure` spike is the immediate
 * leading indicator that precedes the DLQ filling (see the alarm in api-stack).
 */
export function emitEmbedOutcome(
  outcome: EmbedOutcome,
  sink: MetricSink = defaultSink,
  now: number = Date.now(),
): void {
  sink(emfLine('EmbedOutcome', 'Outcome', outcome, 1, now));
}

/**
 * Emit a single `AssociationOutcome` count
 * (`Outcome=candidates|unassigned|unresolved`). The `unassigned` rate over total
 * is the association-to-bin rate the plan calls out as the must-be-observable
 * signal — visible here rather than guessed from an empty ideas list.
 */
export function emitAssociationOutcome(
  outcome: AssociationMetricOutcome,
  sink: MetricSink = defaultSink,
  now: number = Date.now(),
): void {
  sink(emfLine('AssociationOutcome', 'Outcome', outcome, 1, now));
}
