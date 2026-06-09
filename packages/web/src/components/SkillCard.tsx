import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Skill } from '@harness/shared';
import { Pill } from './primitives.js';
import { MarkdownView } from './MarkdownView.js';
import { stripFrontmatter } from '../lib/frontmatter.js';
import { VariantSwitcher } from './VariantSwitcher.js';
import { variantOf, useGetSkillIdeasQuery, type SkillIdea } from '../api/baseApi.js';

/**
 * Distinct-session count at which an idea is "corroborated" (skill-idea loop,
 * default K=2). The server is the authority on surfacing (it enforces this on
 * the corroborated-only candidate-learnings path); the HQ dropdown only uses K
 * to LABEL each idea, so a local copy of the default is fine here.
 */
const CORROBORATION_K = 2;

/** The author/creator of a skill — its createdBy name, falling back to source. */
export function authorOf(s: Skill): string {
  return s.createdBy?.name ?? s.source ?? 'system';
}

/**
 * The one-liner a card shows. Normally this is just `skill.description`, but a
 * locally-ingested skill can arrive with its description set to the whole raw
 * SKILL.md (frontmatter and all). Strip a leading frontmatter block so the card
 * shows prose, not a `--- name: … ---` dump; the `line-clamp` then bounds the
 * height. Falls back to the raw description if stripping leaves nothing.
 */
export function descriptionOf(s: Skill): string {
  const raw = s.description ?? '';
  const stripped = stripFrontmatter(raw).trim();
  return stripped || raw;
}

/**
 * The canonical catalog card for one skill. A bundle renders as a clickable card
 * that drills into its sub-skills; a plain skill renders as a static card. This
 * is the SINGLE representation of a skill in the catalog — the top-level Skills
 * grid and a bundle's member grid both render it, so a bundle reads exactly like
 * a sub-directory of the same cards (no bespoke per-surface layout).
 */
export function SkillCard({
  skill,
  showVariants = false,
}: {
  skill: Skill;
  /**
   * Catalog versioning (KTD6): show the per-name variant switcher + "Promote to
   * true" under a plain skill card. The card itself renders the org-wide TRUE
   * variant; the switcher lets any member view another fork/revision and promote
   * it. Off by default so embedded uses (project tab, bundle drill-in) stay lean.
   */
  showVariants?: boolean;
}) {
  if (skill.kind === 'bundle') {
    const memberCount = (skill.resolvedMembers ?? skill.members).length;
    return (
      <Link
        to={`/skills/${skill.name}`}
        className="hq-box block border-l-[3px] border-l-[#6f8fb5] bg-paper no-underline text-ink"
        data-testid={`skill-card-${skill.name}`}
      >
        <div className="flex items-center justify-between">
          <span>
            <Pill variant="skill" className="font-semibold">
              ▤ {skill.name}
            </Pill>{' '}
            <Pill>bundle</Pill>
          </span>
          <span className="text-[11px] text-faint">{memberCount} skills ›</span>
        </div>
        <div className="my-1.5 line-clamp-3 text-xs text-mut">{descriptionOf(skill)}</div>
        <div className="mt-1.5 text-[11px] text-faint" data-testid={`skill-author-${skill.name}`}>
          by {authorOf(skill)}
        </div>
      </Link>
    );
  }
  return (
    <div className="hq-box bg-paper" data-testid={`skill-card-${skill.name}`}>
      <div className="flex justify-between">
        <Pill variant="skill">{skill.name}</Pill>
        <span className="text-[11px] text-faint">{skill.source}</span>
      </div>
      <div className="my-1.5 line-clamp-3 text-xs text-mut">{descriptionOf(skill)}</div>
      <SkillBodyPreview skill={skill} />
      <div className="mt-1.5 text-[11px] text-faint" data-testid={`skill-author-${skill.name}`}>
        by {authorOf(skill)}
      </div>
      {showVariants && (
        <div className="mt-2 border-t border-odd pt-2">
          <VariantSwitcher name={variantOf(skill).baseName} allowPromote />
        </div>
      )}
    </div>
  );
}

/**
 * The card keeps only the skill's description (rendered by the caller); the full
 * SKILL.md body lives behind an Expand button that opens it in a fullscreen
 * overlay. Frontmatter is stripped so the overlay shows real content, not a
 * `--- name: … ---` block. Renders nothing when there's no body to expand.
 */
function SkillBodyPreview({ skill }: { skill: Skill }) {
  const [open, setOpen] = useState(false);
  // The full skill text is normally `body` (the whole SKILL.md). A locally
  // ingested skill can instead carry the whole file in `description` with an
  // empty body, so fall back to it — otherwise the Expand button would vanish
  // exactly when the card has the most to show.
  const source = (skill.body ?? '').trim() ? (skill.body as string) : (skill.description ?? '');
  const body = stripFrontmatter(source).trim();
  if (!body) return null;

  return (
    <>
      <button
        type="button"
        className="hq-btn mt-1.5"
        data-testid={`skill-expand-${skill.name}`}
        onClick={() => setOpen(true)}
      >
        ⤢ Expand
      </button>
      {open && <SkillBodyModal skill={skill} body={body} onClose={() => setOpen(false)} />}
    </>
  );
}

/** Fullscreen overlay rendering a skill's full body as Markdown. */
function SkillBodyModal({
  skill,
  body,
  onClose,
}: {
  skill: Skill;
  body: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-black/50 p-4 sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={`${skill.name} skill`}
      data-testid={`skill-modal-${skill.name}`}
      onClick={onClose}
    >
      <div
        className="hq-box mx-auto flex h-full w-full max-w-[900px] flex-col overflow-hidden bg-paper"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-odd pb-2">
          <Pill variant="skill" className="font-semibold">
            {skill.name}
          </Pill>
          <button
            type="button"
            className="hq-btn"
            data-testid={`skill-modal-close-${skill.name}`}
            onClick={onClose}
          >
            ✕ Close
          </button>
        </div>
        <div className="mt-2 min-h-0 flex-1 overflow-auto">
          <SkillIdeasDropdown name={variantOf(skill).baseName} />
          <MarkdownView markdown={body} />
        </div>
      </div>
    </div>
  );
}

/** Whether an idea has reached the corroboration bar (distinct-session count ≥ K). */
function isCorroborated(idea: SkillIdea): boolean {
  return idea.corroborationCount >= CORROBORATION_K;
}

/**
 * The per-skill ideas dropdown for the full-screen Command HQ skill view
 * (skill-idea loop, U14/R18). Lists EVERY idea proposed for the skill: open
 * ideas (split into corroborated vs not-yet-corroborated by their distinct-
 * session count) up top, then folded ideas grouped as read-only history. Each
 * row shows its corroboration count + a status badge. Collapsed by default so it
 * never crowds the body; renders an empty state when the skill has no ideas.
 *
 * Reads from `getSkillIdeas` (tagged per-skill), so a fold/mark-folded of one of
 * this skill's ideas invalidates the `{ type: 'Idea', id: name }` tag and the
 * list refetches — the freshly-folded idea moves into the history group.
 */
function SkillIdeasDropdown({ name }: { name: string }) {
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useGetSkillIdeasQuery(name);

  const ideas = data ?? [];
  const openIdeas = ideas.filter((i) => i.status !== 'folded');
  const folded = ideas.filter((i) => i.status === 'folded');
  const corroborated = openIdeas.filter(isCorroborated);
  const uncorroborated = openIdeas.filter((i) => !isCorroborated(i));

  return (
    <div className="mb-3 border border-odd bg-paper" data-testid={`skill-ideas-${name}`}>
      <button
        type="button"
        className="hq-btn flex w-full items-center justify-between"
        aria-expanded={open}
        data-testid={`skill-ideas-toggle-${name}`}
        onClick={() => setOpen((o) => !o)}
      >
        <span>💡 Ideas {ideas.length > 0 && <Pill>{ideas.length}</Pill>}</span>
        <span className="text-faint">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="border-t border-odd p-2" data-testid={`skill-ideas-body-${name}`}>
          {isLoading ? (
            <div className="text-[11px] text-faint">loading…</div>
          ) : ideas.length === 0 ? (
            <div className="text-[11px] text-faint" data-testid={`skill-ideas-empty-${name}`}>
              No ideas proposed for this skill yet.
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {corroborated.length > 0 && (
                <IdeaGroup title="Corroborated" ideas={corroborated} badge="good" />
              )}
              {uncorroborated.length > 0 && (
                <IdeaGroup title="Not yet corroborated" ideas={uncorroborated} badge="idle" />
              )}
              {folded.length > 0 && (
                <IdeaGroup title="Folded (history)" ideas={folded} badge="skill" folded />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** One labelled group of ideas in the dropdown (corroborated / uncorroborated / folded). */
function IdeaGroup({
  title,
  ideas,
  badge,
  folded = false,
}: {
  title: string;
  ideas: SkillIdea[];
  badge: 'good' | 'idle' | 'skill';
  folded?: boolean;
}) {
  return (
    <div data-testid={`idea-group-${title.toLowerCase().replace(/[^a-z]+/g, '-').replace(/^-|-$/g, '')}`}>
      <div className="mb-1 text-[10px] uppercase tracking-wide text-faint">{title}</div>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
        {ideas.map((idea) => (
          <li
            key={idea.ideaId}
            className="border-l-2 border-l-odd pl-2 text-xs text-mut"
            data-testid={`idea-${idea.ideaId}`}
          >
            <div className="mb-0.5 flex items-center gap-1.5" data-testid={`idea-badge-${idea.ideaId}`}>
              <Pill variant={badge}>
                {folded
                  ? idea.foldedIntoRev
                    ? `folded · rev ${idea.foldedIntoRev}`
                    : 'folded'
                  : isCorroborated(idea)
                    ? 'corroborated'
                    : 'open'}
              </Pill>
              <span className="text-[11px] text-faint">
                {idea.corroborationCount} session{idea.corroborationCount === 1 ? '' : 's'}
              </span>
            </div>
            <div className="line-clamp-3">{idea.text}</div>
          </li>
        ))}
      </ul>
    </div>
  );
}
