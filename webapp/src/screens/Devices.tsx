import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, PageHeader, Panel } from "../components/ui";
import ReadingSyncConflicts from "../components/ReadingSyncConflicts";
import { deviceSyncApi, pairLabels, scopeNames, syncScopes } from "../domain/deviceSync";
import type { DeviceIdentity, DeviceInvite, DevicePair, DeviceStatus, SyncScope } from "../domain/deviceSync";
import styles from "./Devices.module.css";

function ScopeChoice({ value, onChange, allowed = syncScopes, disabled = false, label }: {
  value: SyncScope[]; onChange: (value: SyncScope[]) => void; allowed?: SyncScope[]; disabled?: boolean; label: string;
}) {
  return <fieldset className={styles.scopes} disabled={disabled}><legend>{label}</legend>
    {syncScopes.map((scope) => <label key={scope}><input type="checkbox" checked={value.includes(scope)} disabled={!allowed.includes(scope)}
      onChange={(event) => onChange(event.target.checked ? [...value, scope].sort() : value.filter((item) => item !== scope))} />{scopeNames[scope]}</label>)}
  </fieldset>;
}

function Identity({ device }: { device: DeviceIdentity }) {
  return <dl className={styles.identity}><dt>Device</dt><dd>{device.name}</dd><dt>Identity</dt><dd>{device.actor}</dd>
    <dt>Address</dt><dd>{device.endpoint}</dd></dl>;
}

function PairCard({ pair, busy, act }: { pair: DevicePair; busy: boolean; act: (operation: () => Promise<unknown>) => Promise<void> }) {
  const { confirm } = useAppContext();
  const [scopes, setScopes] = useState<SyncScope[]>(pair.status === "awaiting_owner" ? [] : pair.scopes);
  const [checked, setChecked] = useState(false);
  const active = pair.status === "active";
  return <article className={styles.device} aria-label={`Device ${pair.device.name}`}>
    <h3>{pair.device.name}</h3><p>{pairLabels[pair.status] ?? "Status unavailable"}</p><Identity device={pair.device} />
    {pair.status !== "revoked" ? <p>Compare this code on both computers: <strong>{pair.verification_code}</strong></p> : null}
    {pair.status === "awaiting_owner" ? <>
      <p>Only approve a computer you own. Review its identity and choose what may sync in both directions.</p>
      <ScopeChoice label="Allow on this device" value={scopes} onChange={setScopes} allowed={pair.scopes} disabled={busy} />
      <label><input type="checkbox" checked={checked} disabled={busy} onChange={(event) => setChecked(event.target.checked)} />I checked this is my other computer</label>
      <Button disabled={busy || !checked} onClick={() => void act(() => deviceSyncApi.approve(pair, scopes))}>Approve this device</Button>
    </> : null}
    {active ? <>
      <p>{pair.paused ? "Paused on this device." : pair.remote_paused ? "Paused on the other device." : "Both devices have confirmed the pairing."}</p>
      {pair.sync ? <div role="status">
        <p>{({ confirmed: "Selected local changes confirmed by the other device.", pending: "Changes are waiting for confirmation.",
          waiting: "Last transfer was not confirmed. Reconnect and retry.", failed: "Local sync storage is unavailable. Free space or check storage, then retry.",
          paused: "Content transfer is paused.", no_scope: "No category is currently allowed by both devices.",
          conflict: "Changes from different devices need review. Both versions have been kept." } as Record<string, string>)[pair.sync.state] ?? "Checking sync status."}</p>
        {pair.sync.pending !== null ? <p>{pair.sync.pending} local changes waiting for confirmation.</p> : null}
        {pair.sync.last_success_at !== null ? <p>Last confirmation across selected categories: {new Date(pair.sync.last_success_at * 1000).toLocaleString()}.</p> : null}
        {pair.sync.conflicts > 0 ? <p>{pair.sync.conflicts} unresolved conflicts. <a href="#reading-sync-conflicts">Review reading choices below</a>; review conversation branches in <Link to="/ask">Ask Ryn</Link>.</p> : null}
      </div> : null}
      <p>Mutually allowed: {pair.effective_scopes.map((scope) => scopeNames[scope]).join(", ") || "None"}.</p>
      <ScopeChoice label="Your allowed scope" value={scopes} onChange={setScopes} disabled={busy} />
      <p>Turning a category off stops future transfers. Copies already on either computer stay there.</p>
      <div className={styles.actions}><Button disabled={busy} onClick={() => void act(() => deviceSyncApi.configure(pair, scopes, pair.paused))}>Save scope</Button>
        <Button disabled={busy} onClick={() => void act(() => deviceSyncApi.configure(pair, pair.scopes, !pair.paused))}>{pair.paused ? "Resume" : "Pause"}</Button></div>
    </> : null}
    {pair.status === "revoked" ? <p>{pair.removal_pending ? "Removed locally; waiting to notify the other device." : "Removal confirmed by the other device, or initiated there."} Existing copies cannot be recalled.</p> : null}
    <div className={styles.actions}>
      {pair.status !== "revoked" || pair.removal_pending ? <Button disabled={busy} onClick={() => void act(() => deviceSyncApi.retry(pair.id))}>Retry connection</Button> : null}
      {pair.status !== "revoked" ? <Button disabled={busy} onClick={() => confirm({ title: `Remove ${pair.device.name}?`, risk: "high", confirmLabel: "Remove device",
        body: "This computer will reject further sync immediately. A transfer already in flight may finish on the other device, and the removal notice may arrive later. Copies already there cannot be recalled. Reconnecting requires a new invitation and approval.",
        onConfirm: () => act(() => deviceSyncApi.remove(pair)) })}>Remove device</Button> : null}
    </div>
  </article>;
}

export default function Devices() {
  const [status, setStatus] = useState<DeviceStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [offered, setOffered] = useState<SyncScope[]>([]);
  const [invite, setInvite] = useState<{ uri: string; invite: DeviceInvite } | null>(null);
  const [paste, setPaste] = useState("");
  const [review, setReview] = useState<{ uri: string; invite: DeviceInvite } | null>(null);
  const [selected, setSelected] = useState<SyncScope[]>([]);
  const [checked, setChecked] = useState(false);
  const revision = useRef(0);
  const mounted = useRef(true);
  const loadVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++loadVersion.current;
    const result = await deviceSyncApi.status();
    if (!mounted.current || version !== loadVersion.current) return;
    setStatus(result);
    setInvite((prior) => prior && result.invites.some((row) => row.id === prior.invite.id && row.status === "open") ? prior : null);
  }, []);
  const act = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(""); setNotice("");
    try { await operation(); await load(); }
    catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : "Could not confirm this operation. Refresh and retry."); }
    finally { if (mounted.current) setBusy(false); }
  };
  useEffect(() => {
    mounted.current = true;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try { await load(); }
      catch { if (mounted.current) setError("Could not refresh device status. Check your connection and refresh."); }
      finally { polling = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { mounted.current = false; window.clearInterval(timer); };
  }, [load]);
  return <div className="screen-stack">
    <PageHeader eyebrow="Your computers" title="My devices" context="Pair computers you own. Review both identities and choose each category before allowing sync." />
    <p>Device pairing is separate from <Link to="/friends">Friends</Link>. It does not grant AI access or copy models, downloaded articles, credentials or friend permissions.</p>
    {status && !status.data_transfer_available ? <Panel><p>Device pairing is available in this development build. Personal data transfer is still being connected; a confirmed pairing does not mean your content has synced.</p></Panel> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy} onClick={() => void act(async () => undefined)}>Refresh devices</Button>
    {status && !status.pairing_available ? <p role="status">A reachable address is needed for new invitations. Check <Link to="/settings">Network settings</Link>. Existing devices can still be removed.</p> : null}
    <div className={styles.grid}>
      <Panel><h2>Invite your other computer</h2><p>One use, valid for 15 minutes. Choose the categories you want to allow in both directions; nothing is selected automatically.</p>
        <ScopeChoice label="Offer to sync" value={offered} onChange={setOffered} disabled={busy || Boolean(invite)} />
        {!invite ? <Button disabled={busy || !status?.pairing_available} onClick={() => void act(async () => setInvite(await deviceSyncApi.invite(offered)))}>Create device invite</Button> : <>
          <p>Expires {new Date(invite.invite.expires * 1000).toLocaleString()}. On the other computer, open My devices and paste this invitation.</p>
          <textarea className={styles.paste} readOnly aria-label="Device invitation to copy" value={invite.uri} />
          <div className={styles.actions}><Button disabled={busy} onClick={() => void act(async () => {
            try { await navigator.clipboard.writeText(invite.uri); setNotice("Device invitation copied."); }
            catch { throw new Error("Clipboard unavailable. Select and copy the invitation text above."); }
          })}>Copy device invite</Button><Button disabled={busy} onClick={() => void act(async () => { await deviceSyncApi.cancel(invite.invite.id); setInvite(null); })}>Cancel invitation</Button></div>
        </>}
      </Panel>
      <Panel><h2>Use a device invitation</h2>
        <textarea className={styles.paste} aria-label="Paste device invitation" value={paste} onChange={(event) => {
          revision.current += 1; setPaste(event.target.value); setReview(null); setSelected([]); setChecked(false);
        }} />
        {!review ? <Button disabled={busy || !paste.trim()} onClick={() => void act(async () => {
          const version = revision.current, uri = paste.trim();
          const preview = await deviceSyncApi.inspect(uri);
          if (version === revision.current) setReview({ uri, invite: preview });
        })}>Review device invite</Button> : <>
          <Identity device={review.invite.device} /><p>Signature checked locally. No personal data has been sent. Expires {new Date(review.invite.expires * 1000).toLocaleString()}.</p>
          <ScopeChoice label="Choose for this computer" value={selected} onChange={setSelected} allowed={review.invite.scopes} disabled={busy} />
          <p>These categories may sync in both directions after the other computer also approves.</p>
          <label><input type="checkbox" checked={checked} disabled={busy} onChange={(event) => setChecked(event.target.checked)} />I checked this invitation is from my own computer</label>
          <Button disabled={busy || !checked || !status?.pairing_available} onClick={() => void act(async () => {
            const version = revision.current;
            await deviceSyncApi.join(review.uri, selected);
            if (version === revision.current) { setPaste(""); setReview(null); setChecked(false); setSelected([]); }
            setNotice("Request saved. Compare the codes on both computers and approve on the computer that sent the invitation.");
          })}>Request pairing</Button>
        </>}
      </Panel>
    </div>
    <Panel><h2>Your devices</h2>
      {!status ? <p>Loading devices…</p> : !status.devices.length ? <p>No paired devices yet.</p> : status.devices.map((pair) =>
        <PairCard key={`${pair.id}:${pair.revision}:${pair.status}`} pair={pair} busy={busy} act={act} />)}
      {status?.invites.filter((row) => row.status === "open" && row.id !== invite?.invite.id).map((row) => <div key={row.id} className={styles.device}>
        <p>Open invitation · expires {new Date(row.expires * 1000).toLocaleString()}. Invitation text is only shown when created; cancel this invitation if you no longer have it.</p>
        <Button disabled={busy} onClick={() => void act(() => deviceSyncApi.cancel(row.id))}>Cancel unused invitation</Button>
      </div>)}
    </Panel>
    {status?.data_transfer_available ? <ReadingSyncConflicts devices={status.devices} onResolved={load} /> : null}
  </div>;
}
