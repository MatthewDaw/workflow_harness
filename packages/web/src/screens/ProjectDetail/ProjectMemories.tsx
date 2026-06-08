import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useGetProjectMemoriesQuery, type Memory } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';
import { MarkdownView } from '../../components/MarkdownView.js';

/**
 * Project Memories sub-tab: the Claude Code "memories" synced up from claude+
 * (per user) as Claude learns. Memories are stored PER AUTHOR, so the flat list
 * the endpoint returns spans every user; we GROUP it by `userId` (labelled with
 * the friendly `userName` when present) and render each group as a section. When
 * more than one author has memories we offer a lightweight author filter so a
 * reader can narrow to a single person's memories.
 */

/** One author's memories, with the display label resolved once. */
interface MemoryGroup {
  userId: string;
  /** Friendly name when synced, else the raw userId so a group always has a label. */
  label: string;
  items: Memory[];
}

export function ProjectMemories() {
  const { projectId = '' } = useParams();
  const { data, isLoading } = useGetProjectMemoriesQuery(projectId, { skip: !projectId });

  // Bucket the flat list by author. Insertion order of the Map preserves the
  // order authors first appear, so the groups stay stable across renders.
  const groups = useMemo<MemoryGroup[]>(() => {
    const byUser = new Map<string, MemoryGroup>();
    for (const m of data ?? []) {
      let group = byUser.get(m.userId);
      if (!group) {
        group = { userId: m.userId, label: m.userName ?? m.userId, items: [] };
        byUser.set(m.userId, group);
      }
      group.items.push(m);
    }
    return [...byUser.values()];
  }, [data]);

  // The author filter (default '' = all). Only surfaced when >1 author exists.
  const [author, setAuthor] = useState('');
  const visible = author ? groups.filter((g) => g.userId === author) : groups;

  return (
    <div className="hq-pad" data-testid="project-memories">
      <ScreenHeader
        title="Memories"
        subtitle="Synced from claude+ as Claude learns (per user)"
      />

      {/* Author filter — only meaningful once two or more people have memories. */}
      {groups.length > 1 && (
        <div className="mb-2.5 flex items-center gap-2">
          <label htmlFor="memory-author" className="text-mut text-[12.5px]">
            Author
          </label>
          <select
            id="memory-author"
            className="hq-input"
            value={author}
            onChange={(e) => setAuthor(e.target.value)}
          >
            <option value="">All authors</option>
            {groups.map((g) => (
              <option key={g.userId} value={g.userId}>
                {g.label}
              </option>
            ))}
          </select>
        </div>
      )}

      {isLoading && <div className="text-mut">Loading…</div>}
      {!isLoading && groups.length === 0 && <div className="text-mut">No memories yet.</div>}

      {visible.map((group) => (
        <section key={group.userId} className="mb-4" data-testid={`memory-group-${group.userId}`}>
          <h3 className="m-0 mb-2 text-[13px] font-semibold text-mut">{group.label}</h3>
          <div className="flex flex-col gap-2.5">
            {group.items.map((m) => (
              <div key={m.name} className="hq-box bg-paper">
                <div className="flex items-center gap-2">
                  <b>{m.name}</b>
                  {m.type && <Pill variant="skill">{m.type}</Pill>}
                </div>
                {m.description && (
                  <p className="m-0 mt-0.5 text-[12.5px] text-mut">{m.description}</p>
                )}
                <MarkdownView markdown={m.content} />
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
