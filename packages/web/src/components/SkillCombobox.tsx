import { useId, useMemo, useRef, useState } from 'react';

/**
 * Accessible, keyboard-navigable, searchable combobox for picking a skill name
 * from a (possibly large) catalog. Type to filter; ↑/↓ to move; Enter to pick;
 * Esc to close. Used by the bundle member picker and the Agent editor.
 *
 * It is a controlled "type-to-filter" combobox following the ARIA 1.2
 * combobox-with-listbox pattern: a text input owns the value, an attached
 * listbox renders the filtered options, and selection commits a chosen option.
 */
export interface SkillOption {
  /** The skill name (the value committed on select). */
  name: string;
  /** Optional secondary label, e.g. scope tier or "bundle". */
  hint?: string;
}

export function SkillCombobox({
  options,
  onSelect,
  placeholder = 'Search skills…',
  testid = 'skill-combobox',
  buttonLabel,
  onCommit,
  emptyHint = 'No matching skills',
}: {
  options: SkillOption[];
  /** Fired when an option is highlighted/selected (sets the pending value). */
  onSelect?: (name: string) => void;
  placeholder?: string;
  testid?: string;
  /** When set, renders a commit button (e.g. "+ Add") that fires onCommit. */
  buttonLabel?: string;
  /** Fired with the chosen name when the commit button is pressed. */
  onCommit?: (name: string) => void;
  /**
   * Message shown in the listbox when nothing matches the typed query. Callers
   * pass a context-specific hint (e.g. "register it with /hq-add-skill") so a
   * not-in-catalog dead-end isn't silent.
   */
  emptyHint?: string;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [chosen, setChosen] = useState('');
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) => o.name.toLowerCase().includes(q) || (o.hint ?? '').toLowerCase().includes(q),
    );
  }, [options, query]);

  const choose = (name: string) => {
    setChosen(name);
    setQuery(name);
    setOpen(false);
    onSelect?.(name);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const opt = filtered[active];
      if (opt) choose(opt.name);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  return (
    <span className="relative inline-flex items-center gap-1.5" data-testid={testid}>
      <span className="relative inline-block">
        <input
          ref={inputRef}
          className="hq-input"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          data-testid={`${testid}-input`}
          placeholder={placeholder}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setChosen('');
            setOpen(true);
            setActive(0);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          onKeyDown={onKeyDown}
        />
        {open && (
          <ul
            id={listId}
            role="listbox"
            className="absolute left-0 top-full z-20 mt-0.5 max-h-56 w-56 overflow-auto rounded border border-line2 bg-paper py-1 shadow-md"
            data-testid={`${testid}-list`}
          >
            {filtered.length === 0 && (
              <li className="px-2 py-1 text-xs text-faint" data-testid={`${testid}-empty`}>
                {emptyHint}
              </li>
            )}
            {filtered.map((o, i) => (
              <li
                key={o.name}
                role="option"
                aria-selected={i === active}
                data-testid={`${testid}-option-${o.name}`}
                className={`cursor-pointer px-2 py-1 text-[13px] ${
                  i === active ? 'bg-line2' : ''
                }`}
                // onMouseDown (not onClick) so it fires before the input blur.
                onMouseDown={(e) => {
                  e.preventDefault();
                  choose(o.name);
                }}
                onMouseEnter={() => setActive(i)}
              >
                {o.name}
                {o.hint && <span className="ml-1.5 text-[11px] text-faint">{o.hint}</span>}
              </li>
            ))}
          </ul>
        )}
      </span>
      {buttonLabel && (
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          data-testid={`${testid}-commit`}
          disabled={!chosen}
          onClick={() => {
            if (chosen) {
              onCommit?.(chosen);
              setChosen('');
              setQuery('');
            }
          }}
        >
          {buttonLabel}
        </button>
      )}
    </span>
  );
}
