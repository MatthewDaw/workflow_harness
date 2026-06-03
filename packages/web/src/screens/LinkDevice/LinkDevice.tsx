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
        subtitle="Install claude+, then link this device by approving the code from your terminal."
      />

      {/* 1. Install claude+ */}
      <section aria-labelledby="install-claude-plus">
        <h3 id="install-claude-plus" className="text-sm font-semibold">
          1 · Install claude+
        </h3>
        <p className="mt-1 text-[13px] text-mut">
          Pick one. Installs the <code className="font-mono">claude-plus</code> binary (aliased{' '}
          <code className="font-mono">claude+</code>) on a host with the real{' '}
          <code className="font-mono">claude</code> CLI on PATH.
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
        <p className="mt-3 text-[13px] text-mut">
          Then run <code className="font-mono">claude+ login</code> in your terminal — it prints a
          short user code and waits. Enter that code below to link this machine.
        </p>
      </section>

      {/* 2. Link a device */}
      <section aria-labelledby="link-a-device" className="mt-6">
        <h3 id="link-a-device" className="text-sm font-semibold">
          2 · Link a device
        </h3>
        <form className="hq-box bg-paper mt-2" onSubmit={onSubmit}>
          <label
            htmlFor="device-user-code"
            className="block text-[11px] uppercase tracking-wide text-faint"
          >
            User code
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
            <div
              className="mt-3 text-[13px] text-good"
              data-testid="link-device-result"
              role="status"
            >
              Device approved — return to your terminal.
            </div>
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
