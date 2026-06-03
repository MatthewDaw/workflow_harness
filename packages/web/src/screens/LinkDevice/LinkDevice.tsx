import { useState, type FormEvent } from 'react';
import { useApproveDeviceMutation } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';

/**
 * Link a device (device-auth approval). A signed-in HQ user pastes the user code
 * shown by `claude+ login` in their terminal and approves it here. On success the
 * terminal's poll loop receives a token; on a miss we tell them the code is gone.
 */
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
        title="Link a device"
        subtitle="Approve a claude+ login. Enter the code shown in your terminal."
      />

      <form className="hq-box bg-paper" onSubmit={onSubmit}>
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
          <div className="mt-3 text-[13px] text-good" data-testid="link-device-result" role="status">
            Device approved — return to your terminal.
          </div>
        )}
        {result === 'notfound' && (
          <div className="mt-3 text-[13px] text-mut" data-testid="link-device-result" role="status">
            That code wasn&rsquo;t found or has expired.
          </div>
        )}
        {result === 'error' && (
          <div className="mt-3 text-[13px] text-mut" data-testid="link-device-result" role="status">
            Something went wrong. Check the code and try again.
          </div>
        )}
      </form>
    </div>
  );
}
