import { mcpServerSchema, orgScope, type McpServer } from '@harness/shared';
import type { Repo } from '../db/repo.js';

/**
 * Org-scope seed for MCP servers (U-Ver-Seed). Mirrors `seed/skills.ts` +
 * `seed/agents.ts`: the builder is pure and stamps each record as the BASE
 * variant of its name (rev 1, empty repo/user), so a later edit forks/advances
 * the variant cleanly and a re-seed never resets the catalog. `seedMcpServers`
 * upserts through `Repo.putMcpServer`, which is idempotent by key.
 *
 * A seed MCP server is the structured discriminated-union record (transport +
 * per-transport fields), NOT a markdown body — so the input shape is the union
 * itself, minus the catalog/version stamps this builder adds.
 */

/** An MCP server definition for the seed: the transport-specific fields, sans stamps. */
export type SeedMcpServerFile =
  | { name: string; transport: 'stdio'; command: string; args?: string[]; env?: Record<string, string> }
  | { name: string; transport: 'http'; url: string; headers?: Record<string, string> }
  | { name: string; transport: 'sse'; url: string; headers?: Record<string, string> };

/**
 * Build the seed `McpServer[]`: one org-scoped record per file, each stamped as
 * the base variant (rev 1) with a `system` authorship stamp. Parsing through
 * `mcpServerSchema` applies the per-transport defaults (args/env/headers) and
 * guards the discriminated union, so a record here is byte-compatible with what
 * the MCP REST layer reads/writes.
 */
export function buildSeedMcpServers(org: string, files: SeedMcpServerFile[]): McpServer[] {
  const scope = orgScope(org);
  const createdBy = { userId: 'system', name: 'system' } as const;
  return files.map((f) =>
    mcpServerSchema.parse({
      ...f,
      scope,
      createdBy,
      baseName: f.name,
      variantId: f.name,
      version: 1,
    }),
  );
}

/**
 * Upsert the seeded MCP servers into HQ at org scope. Idempotent: `putMcpServer`
 * overwrites by key, so a second run converges. Returns the records written.
 */
export async function seedMcpServers(
  repo: Repo,
  org: string,
  files: SeedMcpServerFile[],
): Promise<McpServer[]> {
  const records = buildSeedMcpServers(org, files);
  for (const record of records) {
    await repo.putMcpServer(record);
  }
  return records;
}
