import { useState, type FormEvent } from 'react';
import { useApproveDeviceMutation } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';

/**
 * Get started: install `claude+`, then link this device (device-auth approval).
 *
 * The install commands are the real ones from `scripts/install.sh` and the
 * `claude-plus` npm package, so a brand-new user can go from zero to a linked
 * terminal on one screen. Below them, a signed-in HQ user pastes the user code
 * shown by `claude+ login` in their terminal and approves it; on success the
 * terminal's poll loop receives a token, on a miss we tell them the code is gone.
 */

const RELEASES_URL = 'https://github.com/workflow-harness/claude-plus/releases';

/** The command-line ways to install `claude+`, sourced from the real installer
 * (the "download a release" link is rendered separately below). */
const INSTALL_OPTIONS: { label: string; command: string }[] = [
  {
    label: 'curl (macOS / Linux)',
    command:
      'curl -fsSL https://raw.githubusercontent.com/workflow-harness/claude-plus/main/scripts/install.sh | sh',
  },
  { label: 'npm (any platform)', command: 'npm i -g claude-plus' },
];

/** One install command with a Copy button. Copy degrades to a no-op when the
 * Clipboard API is unavailable; the command stays visible and selectable. */
function InstallCommand({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard?.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard blocked/unsupported: leave the text for manual selection.
    }
  }

  return (
    <div className="hq-box bg-paper mt-2">
      <div className="text-[11px] uppercase tracking-wide text-faint">{label}</div>
      <div className="mt-1.5 flex items-center gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-[13px]">
          {command}
        </code>
        <button type="button" className="hq-btn shrink-0 text-[11px]" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    </div>
  );
}

export function LinkDevice() {
  const [code, setCode] = useState('');
  const [approve, { isLoading }] = useApproveDeviceMutation();
  const [result, setResult] = useState<'approved' | 'notfound' | 'error' | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setResult(null);
    try {
      const res = await approve({ userCode: code.trim() }).unwrap();
      setResult(res.approved ? 'approved' : 'notfound');
    } catch {
      setResult('error');
    }
  }

  return (
    <div className="hq-pad" data-testid="link-device">
      <ScreenHeader
        title="Get started"
        subtitle="Linking connects a terminal running claude+ to Command HQ, so its sessions show up here — live, watchable, and steerable."
      />

      {/* What linking is + the 3 steps at a glance */}
      <div className="hq-box bg-paper">
        <div className="text-[13px] text-mut">
          “Linking a device” pairs one computer’s <code className="font-mono">claude+</code> with
          your HQ account. You do it once per machine. Three steps:
        </div>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-[13px]">
          <li>
            Install <code className="font-mono">claude+</code> on that machine.
          </li>
          <li>
            Run <code className="font-mono">claude+ login</code> there — it shows a short code and
            waits.
          </li>
          <li>
            Type that code into the box below and approve. Done — sessions start streaming here.
          </li>
        </ol>
      </div>

      {/* 1. Install claude+ */}
      <section aria-labelledby="install-claude-plus" className="mt-6">
        <h3 id="install-claude-plus" className="text-sm font-semibold">
          1 · Install claude+
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          On the machine you want to link. Pick one — installs the{' '}
          <code className="font-mono">claude-plus</code> binary (aliased{' '}
          <code className="font-mono">claude+</code>). The machine also needs the real{' '}
          <code className="font-mono">claude</code> CLI on its PATH.
        </p>
        {INSTALL_OPTIONS.map((opt) => (
          <InstallCommand key={opt.label} label={opt.label} command={opt.command} />
        ))}
        <div className="hq-box bg-paper mt-2">
          <div className="text-[11px] uppercase tracking-wide text-faint">
            Or download a release
          </div>
          <a
            className="mt-1.5 block font-mono text-[13px]"
            href={RELEASES_URL}
            target="_blank"
            rel="noreferrer"
          >
            github.com/workflow-harness/claude-plus/releases →
          </a>
        </div>
      </section>

      {/* 2. Get a code from the terminal */}
      <section aria-labelledby="run-login" className="mt-6">
        <h3 id="run-login" className="text-sm font-semibold">
          2 · Run claude+ login
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          In that machine’s terminal, run <code className="font-mono">claude+ login</code>. It
          prints a short code and waits for you to approve it here:
        </p>
        <pre className="hq-box bg-ink mt-2 overflow-x-auto whitespace-pre font-mono text-[12px] text-paper">
          {`$ claude+ login
To finish signing in, open Command HQ and approve this code:

    WDJB-MJXT

Waiting for approval… (Ctrl-C to cancel)`}
        </pre>
        <p className="mt-2 text-[13px] text-mut">
          Leave that terminal running — it keeps waiting until you approve the code in step 3.
        </p>
      </section>

      {/* 3. Approve the code here */}
      <section aria-labelledby="link-a-device" className="mt-6">
        <h3 id="link-a-device" className="text-sm font-semibold">
          3 · Approve the code here
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          Type the code your terminal showed (e.g. <code className="font-mono">WDJB-MJXT</code>) and
          approve. That links the machine to your account and its terminal finishes logging in.
        </p>
        <form className="hq-box bg-paper mt-2" onSubmit={onSubmit}>
          <label
            htmlFor="device-user-code"
            className="block text-[11px] uppercase tracking-wide text-faint"
          >
            User code (from your terminal)
          </label>
          <input
            id="device-user-code"
            className="hq-input mt-1.5 w-full max-w-[260px] font-mono tracking-wider uppercase"
            placeholder="WDJB-MJXT"
            autoComplete="off"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <div className="mt-2 flex gap-2">
            <button
              type="submit"
              className="hq-btn hq-btn-pri"
              disabled={isLoading || code.trim().length === 0}
            >
              {isLoading ? 'Approving…' : 'Approve device'}
            </button>
          </div>

          {result === 'approved' && (
            <>
              <div
                className="mt-3 text-[13px] text-good"
                data-testid="link-device-result"
                role="status"
              >
                Device approved — return to your terminal.
              </div>
              <div className="mt-1 text-[13px] text-mut">
                Its login completes automatically and sessions begin streaming. Open{' '}
                <strong>Sessions</strong> to watch them.
              </div>
            </>
          )}
          {result === 'notfound' && (
            <div
              className="mt-3 text-[13px] text-mut"
              data-testid="link-device-result"
              role="status"
            >
              That code wasn&rsquo;t found or has expired.
            </div>
          )}
          {result === 'error' && (
            <div
              className="mt-3 text-[13px] text-mut"
              data-testid="link-device-result"
              role="status"
            >
              Something went wrong. Check the code and try again.
            </div>
          )}
        </form>
      </section>
    </div>
  );
}
