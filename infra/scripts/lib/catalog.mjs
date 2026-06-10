// Catalog readers shared by the seed scripts: SKILL.md front-matter parsing,
// whole-skill-directory reads, and the bundle manifest loader.
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';

/**
 * Parse the YAML front matter of a SKILL.md / agent .md. The repo skills use
 * `description: >-` folded scalars indented under the key, so indented
 * continuation lines are gathered until the next top-level key or the closing
 * `---`. Returns `{ name, description }`; with `{ extended: true }` it also
 * extracts `tools` (CSV -> trimmed string[]), `model`, and the post-front-matter
 * body as `prompt` (agent files).
 */
export function parseFrontmatter(md, { extended = false } = {}) {
  const lines = md.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') {
    return extended
      ? { name: undefined, description: '', tools: [], model: '', prompt: md }
      : { name: undefined, description: '' };
  }
  let name;
  let model = '';
  let tools = [];
  const descParts = [];
  let inDesc = false;
  let bodyStart = lines.length;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === '---') {
      bodyStart = i + 1;
      break;
    }
    const top = /^([A-Za-z0-9_-]+):\s?(.*)$/.exec(line);
    if (top && !line.startsWith(' ')) {
      inDesc = false;
      const [, key, value] = top;
      if (key === 'name') name = value.trim();
      else if (extended && key === 'model') model = value.trim();
      else if (extended && key === 'tools') {
        tools = value
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean);
      } else if (key === 'description') {
        inDesc = true;
        const v = value.trim();
        if (v && v !== '>-' && v !== '>' && v !== '|' && v !== '|-') descParts.push(v);
      }
      continue;
    }
    if (inDesc && line.trim()) descParts.push(line.trim());
  }
  const base = { name, description: descParts.join(' ').trim() };
  if (!extended) return base;
  return { ...base, tools, model, prompt: lines.slice(bodyStart).join('\n').trim() };
}

/**
 * Read the WHOLE skill directory tree into a `{ relPath: contents }` map
 * (U-Skill-Store), so a skill ships SKILL.md PLUS sibling scripts/resources.
 * Paths are POSIX-relative to the skill dir (the catalog stores plaintext only).
 */
export function readSkillDir(dir) {
  const out = {};
  const walk = (cur, rel) => {
    for (const entry of readdirSync(cur, { withFileTypes: true })) {
      const abs = path.join(cur, entry.name);
      const relPath = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        walk(abs, relPath);
      } else if (entry.isFile()) {
        out[relPath] = readFileSync(abs, 'utf8');
      }
    }
  };
  walk(dir, '');
  return out;
}

/**
 * Read every `<skillsDir>/<name>/SKILL.md` skill into
 * `{ name, description, body, files }`, sorted by name. Exits(1) when the
 * skills dir is missing.
 */
export function readSkillFiles(skillsDir, prefixTag) {
  if (!existsSync(skillsDir)) {
    console.error(`[${prefixTag}] no skills dir at ${skillsDir}`);
    process.exit(1);
  }
  const files = [];
  for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = path.join(skillsDir, entry.name);
    const skillMd = path.join(dir, 'SKILL.md');
    if (!existsSync(skillMd)) continue;
    const body = readFileSync(skillMd, 'utf8');
    const { name, description } = parseFrontmatter(body);
    files.push({ name: name ?? entry.name, description, body, files: readSkillDir(dir) });
  }
  return files.sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Load a bundle manifest (e.g. `catalog/skills/bundles.json`) — the single
 * source of truth for how seeded items group into bundles. Absent manifest =>
 * no bundles (`{}`), with a warning unless `warnIfAbsent` is false; unparseable
 * manifest exits(1). Each entry is `{ description, members[] }`.
 */
export function readBundleManifest(manifestPath, prefixTag, { warnIfAbsent = true } = {}) {
  if (!existsSync(manifestPath)) {
    if (warnIfAbsent) {
      console.warn(
        `[${prefixTag}] no bundle manifest at ${manifestPath} — seeding all skills standalone.`,
      );
    }
    return {};
  }
  try {
    return JSON.parse(readFileSync(manifestPath, 'utf8'));
  } catch (err) {
    console.error(`[${prefixTag}] could not parse ${manifestPath}:`, err);
    process.exit(1);
  }
}
