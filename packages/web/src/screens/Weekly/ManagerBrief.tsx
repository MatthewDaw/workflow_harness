import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useGetManagerBriefQuery,
  type BriefException,
  type ReportBrief,
} from '../../api/baseApi.js';
import { Bar, Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Manager brief screen (U12). Renders the reports-scoped exception/divergence
 * brief (U8, KTD6): for each of the caller's reports — the users whose
 * `managerUserId` is the caller — the things that actually need a manager's
 * attention (highest-leverage commit not started, oldest carry, longest-starved
 * Supporting Outcome, any week that failed to lock) plus their strategic
 * concentration (U17). The default is "nothing needs you": a report with a clean
 * week shows no exception rows, and a caller with NO reports gets the empty-brief
 * state (scoping is by the manager EDGE, not a role gate — U8 returns no 403).
 *
 * The brief is keyset-paginated over reports (R10): "Load more" follows the
 * `nextCursor` the endpoint hands back, accumulating pages so a manager of up to
 * the 2000-record team target can page through without sweeping everyone at once.
 *
 * Each report deep-links into that report's project weekly tab so the manager can
 * act on the exception directly.
 */

/** A human label + variant for each exception kind so the UI can icon/group them. */
const EXCEPTION_LABEL: Record<BriefException['kind'], string> = {
  highest_leverage_not_started: 'not started',
  oldest_carry: 'carrying',
  longest_starved_outcome: 'starved',
  lock_failure: 'cannot lock',
};

/** Render one exception row under a report. */
function ExceptionRow({ exception }: { exception: BriefException }) {
  return (
    <li
      className="hq-box bg-paper2 mt-1.5 flex items-center gap-2"
      data-testid={`brief-exception-${exception.kind}`}
    >
      <Pill variant="idle">{EXCEPTION_LABEL[exception.kind]}</Pill>
      <span className="text-[12.5px]">{exception.detail}</span>
    </li>
  );
}

/** Render one report's brief node: latest week, exceptions, and concentration. */
function ReportCard({ report }: { report: ReportBrief }) {
  // Herfindahl is a 0–1 concentration index; surface it on the 0–100 Bar.
  const concentrationPct = Math.round(report.concentration.herfindahl * 100);

  return (
    <div className="hq-box mb-3 bg-paper" data-testid={`brief-report-${report.userId}`}>
      <div className="flex items-center justify-between">
        <span className="font-medium">{report.name ?? report.userId}</span>
        {report.latestWeek ? (
          <Link
            to={`/projects/${report.latestWeek.projectId}/weekly`}
            className="text-xs text-mut no-underline hover:text-cream"
            data-testid={`brief-report-link-${report.userId}`}
          >
            {report.latestWeek.isoWeek} ·{' '}
            <Pill variant="idle">{report.latestWeek.status.toLowerCase()}</Pill> ›
          </Link>
        ) : (
          <span className="text-xs text-faint" data-testid={`brief-no-week-${report.userId}`}>
            no weeks yet
          </span>
        )}
      </div>

      {/* Strategic concentration (U17), reported as divergence from declared posture. */}
      <div className="my-2">
        <div className="flex items-center justify-between text-[12.5px] text-mut">
          <span>concentration</span>
          <span data-testid={`brief-concentration-${report.userId}`}>
            {concentrationPct}% · {report.concentration.nodes} node
            {report.concentration.nodes === 1 ? '' : 's'}
          </span>
        </div>
        <div className="mt-1">
          <Bar pct={concentrationPct} />
        </div>
      </div>

      {/* Exceptions — empty ⇒ "nothing needs you" for this report. */}
      {report.exceptions.length === 0 ? (
        <div className="text-[12.5px] text-mut" data-testid={`brief-report-clean-${report.userId}`}>
          Nothing needs you.
        </div>
      ) : (
        <ul className="list-none p-0">
          {report.exceptions.map((e, i) => (
            <ExceptionRow
              key={`${e.kind}-${e.commitId ?? e.supportingOutcomeId ?? i}`}
              exception={e}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

export function ManagerBrief() {
  // The cursor for the NEXT page to fetch; undefined fetches the first page. We
  // accumulate report nodes across pages so "Load more" appends rather than
  // replaces (the brief endpoint is keyset-paginated by report userId — U8/R10).
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  const [pages, setPages] = useState<ReportBrief[]>([]);

  const { data, isLoading, isFetching } = useGetManagerBriefQuery(
    cursor === undefined ? undefined : { cursor },
  );

  // Fold each arriving page into the accumulated list. The first page (cursor
  // undefined) seeds the list; subsequent pages append. Dedupe by userId so a
  // re-render with the same `data` reference doesn't double-append.
  const reports = useMemo(() => {
    if (!data) return pages;
    const seen = new Set(pages.map((r) => r.userId));
    const merged = [...pages];
    for (const r of data.reports) {
      if (!seen.has(r.userId)) {
        seen.add(r.userId);
        merged.push(r);
      }
    }
    return merged;
  }, [data, pages]);

  // The keyset cursor the latest page hands back; its presence drives "Load more".
  const serverNextCursor = data?.nextCursor;

  function loadMore() {
    if (!data?.nextCursor) return;
    // Commit the current accumulation, then advance the cursor to fetch the next page.
    setPages(reports);
    setCursor(data.nextCursor);
  }

  return (
    <div className="hq-pad" data-testid="manager-brief">
      <ScreenHeader
        title="Manager Brief"
        subtitle="What needs you across your reports this week — highest-leverage work not started, oldest carries, starved outcomes, and anything that failed to lock."
      />

      {isLoading && <div className="text-mut">Loading brief…</div>}

      {/* A caller with no reports gets the empty "nothing needs you" brief. */}
      {!isLoading && reports.length === 0 && (
        <div className="hq-box bg-paper text-mut" data-testid="brief-empty">
          Nothing needs you. You have no reports, or every report is on track.
        </div>
      )}

      {reports.map((r) => (
        <ReportCard key={r.userId} report={r} />
      ))}

      {serverNextCursor && (
        <button
          type="button"
          className="hq-btn"
          data-testid="brief-load-more"
          disabled={isFetching}
          onClick={loadMore}
        >
          Load more
        </button>
      )}
    </div>
  );
}
