import { describe, expect, it } from 'vitest';
import {
  METRIC_NAMESPACE,
  emitAssociationOutcome,
  emitEmbedOutcome,
} from '../src/observability/metrics.js';

/**
 * U23 — observability metrics. The emitters write a single CloudWatch EMF line
 * (auto-extracted into a metric by the Lambda's log integration). We assert the
 * EMF shape via an injected sink so the metric and dimension are correct without
 * scraping stdout: the namespace, the metric name, the single `Outcome`
 * dimension carrying the value, and the count of 1.
 */

function capture() {
  const lines: string[] = [];
  return { sink: (l: string) => lines.push(l), lines };
}

/** Parse the one captured EMF line and pull out the asserted fields. */
function parse(line: string) {
  const obj = JSON.parse(line);
  const cwm = obj._aws.CloudWatchMetrics[0];
  return {
    namespace: cwm.Namespace,
    metricName: cwm.Metrics[0].Name,
    unit: cwm.Metrics[0].Unit,
    dimensions: cwm.Dimensions,
    outcome: obj.Outcome,
    value: obj[cwm.Metrics[0].Name],
    timestamp: obj._aws.Timestamp,
  };
}

describe('emitEmbedOutcome (U23)', () => {
  it('emits an EmbedOutcome=success EMF line under the loop namespace', () => {
    const { sink, lines } = capture();
    emitEmbedOutcome('success', sink, 1_700_000_000_000);

    expect(lines).toHaveLength(1);
    const m = parse(lines[0]!);
    expect(m.namespace).toBe(METRIC_NAMESPACE);
    expect(m.metricName).toBe('EmbedOutcome');
    expect(m.unit).toBe('Count');
    expect(m.dimensions).toEqual([['Outcome']]);
    expect(m.outcome).toBe('success');
    expect(m.value).toBe(1);
    expect(m.timestamp).toBe(1_700_000_000_000);
  });

  it('emits EmbedOutcome=failure (the leading indicator of a filling DLQ)', () => {
    const { sink, lines } = capture();
    emitEmbedOutcome('failure', sink);
    expect(parse(lines[0]!).outcome).toBe('failure');
    expect(parse(lines[0]!).metricName).toBe('EmbedOutcome');
  });
});

describe('emitAssociationOutcome (U23)', () => {
  it.each(['candidates', 'unassigned', 'unresolved'] as const)(
    'emits AssociationOutcome=%s so the to-bin rate is observable',
    (outcome) => {
      const { sink, lines } = capture();
      emitAssociationOutcome(outcome, sink);

      const m = parse(lines[0]!);
      expect(m.namespace).toBe(METRIC_NAMESPACE);
      expect(m.metricName).toBe('AssociationOutcome');
      expect(m.dimensions).toEqual([['Outcome']]);
      expect(m.outcome).toBe(outcome);
      expect(m.value).toBe(1);
    },
  );
});
