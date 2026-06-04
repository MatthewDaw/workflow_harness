import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import { stripFrontmatter } from '../lib/frontmatter.js';

/**
 * MarkdownView (U9): renders GitHub-Flavored Markdown for the requirements
 * surfaces. GFM gives us tables, strikethrough, and — crucially for the
 * requirements model — task lists (`- [x] done` / `- [ ] todo`), which we
 * render as ☑ / ☐ glyphs so a requirements doc reads like a checklist.
 *
 * Raw HTML: our converted `.html` docs have a body that is raw HTML (not
 * markdown syntax), so we enable `rehype-raw` to parse embedded HTML and
 * `rehype-sanitize` to strip scripts and event handlers. This gives us a single
 * render path for both `.md` (markdown) and `.html` (raw HTML body) docs while
 * preserving the existing safety posture — dangerous markup never executes.
 *
 * Repo-sourced docs (docs/PRD.md, docs/plans/**) lead with a `---` YAML
 * frontmatter block (`completion:`, `status:`, …) that is metadata, not prose —
 * we strip it so it never renders as a stray `--- completion: 88 ---` line.
 */
export function MarkdownView({ markdown }: { markdown: string }) {
  return (
    <div className="hq-markdown" data-testid="markdown-view">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, rehypeSanitize]}
        components={{
          // Render GFM task-list checkboxes as static glyphs (the underlying
          // <input type="checkbox"> is read-only/disabled by remark-gfm).
          input: ({ checked, type }) =>
            type === 'checkbox' ? (
              <span aria-hidden="true" className="hq-task-box">
                {checked ? '☑' : '☐'}
              </span>
            ) : null,
        }}
      >
        {stripFrontmatter(markdown)}
      </ReactMarkdown>
    </div>
  );
}
