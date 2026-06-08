import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * U20 assertion: the synth app (bin/infra.ts) must NOT include the OpenSearch
 * Serverless SearchStack. That stack carries a public-from-anywhere network
 * policy and would otherwise deploy under `cdk deploy --all`. The default Forge
 * backend is the brute-force cosine fallback over DynamoDB, so the collection
 * is unnecessary; `lib/search-stack.ts` stays on disk but unreferenced.
 *
 * This drives a real `cdk synth` (no AWS creds required at synth time) into a
 * throwaway output dir, then inspects the produced cloud assembly: the stack
 * list and every synthesized template must be free of SearchStack / OpenSearch.
 */
describe('synth app (bin/infra.ts)', () => {
  const infraDir = path.join(__dirname, '..');
  // Drive the CDK CLI through its JS entrypoint via `node` so this works
  // cross-platform (a bare `.cmd` cannot be exec'd without a shell on Windows).
  const cdkEntry = path.join(infraDir, 'node_modules', 'aws-cdk', 'bin', 'cdk');

  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'u20-synth-'));
  let stackIds: string[];
  let combinedTemplates: string;

  beforeAll(() => {
    // `cdk synth` with no stack id synthesizes the whole app to --output.
    execFileSync(process.execPath, [cdkEntry, 'synth', '--output', outDir], {
      cwd: infraDir,
      encoding: 'utf8',
      env: { ...process.env, DEVICE_TOKEN_SECRET: 'synth-test-secret' },
      maxBuffer: 64 * 1024 * 1024,
    });
    const files = fs.readdirSync(outDir).filter((f) => f.endsWith('.template.json'));
    stackIds = files.map((f) => f.replace('.template.json', ''));
    combinedTemplates = files.map((f) => fs.readFileSync(path.join(outDir, f), 'utf8')).join('\n');
  });

  afterAll(() => {
    fs.rmSync(outDir, { recursive: true, force: true });
  });

  test('does not synth the SearchStack', () => {
    expect(stackIds).not.toContain('SearchStack');
  });

  test('contains no OpenSearch Serverless collection', () => {
    expect(combinedTemplates).not.toMatch(/AWS::OpenSearchServerless::Collection/);
    expect(combinedTemplates).not.toMatch(/AWS::OpenSearchServerless::SecurityPolicy/);
    expect(combinedTemplates).not.toMatch(/forge-sessions/);
  });

  test('still synthesizes the Auth, Api, and Site stacks', () => {
    expect(stackIds.sort()).toEqual(['ApiStack', 'AuthStack', 'SiteStack']);
  });
});
