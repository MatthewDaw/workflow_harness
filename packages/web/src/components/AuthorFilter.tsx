import { useMemo, useState } from 'react';

export const ANY_AUTHOR = '__any__';

/**
 * Author-filter state for a catalog screen: the distinct (sorted) author list,
 * the current selection, and a `matches` predicate to apply to each item.
 */
export function useAuthorFilter<T>(items: T[], authorOf: (item: T) => string) {
  const [author, setAuthor] = useState<string>(ANY_AUTHOR);
  const authors = useMemo(() => {
    const set = new Set<string>();
    for (const item of items) set.add(authorOf(item));
    return [...set].sort();
  }, [items, authorOf]);
  const matches = (item: T) => author === ANY_AUTHOR || authorOf(item) === author;
  return { author, setAuthor, authors, matches };
}

/**
 * The author `<select>` shared by the catalog screens. Per-screen drift is kept
 * deliberate via props: Skills and MCP Servers hide the select when there is
 * nothing to filter (`hideWhenSingle`); Agents and Workflows always show it.
 */
export function AuthorSelect({
  author,
  onChange,
  authors,
  testid,
  anyLabel = 'any author',
  hideWhenSingle = false,
  labelClassName = '',
  containerClassName,
}: {
  author: string;
  onChange: (author: string) => void;
  authors: string[];
  testid: string;
  anyLabel?: string;
  hideWhenSingle?: boolean;
  labelClassName?: string;
  /** When set, wraps the label in a div (rendered only when the select shows). */
  containerClassName?: string;
}) {
  if (hideWhenSingle && authors.length <= 1) return null;
  const label = (
    <label className={`flex items-center gap-1.5 ${labelClassName}`.trim()}>
      Author
      <select
        className="hq-btn normal-case"
        data-testid={testid}
        value={author}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value={ANY_AUTHOR}>{anyLabel}</option>
        {authors.map((a) => (
          <option key={a} value={a}>
            {a}
          </option>
        ))}
      </select>
    </label>
  );
  return containerClassName ? <div className={containerClassName}>{label}</div> : label;
}
