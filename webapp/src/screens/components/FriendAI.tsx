import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../../appContext";
import { Button, Panel } from "../../components/ui";
import { aiAccess, type AIGrant, type FriendAISnapshot } from "../../domain/aiAccess";
import type { FriendRecord } from "../../domain/friendTypes";
import type { LLMProviderStatus } from "../../domain/nodeClient";

export default function FriendAI({ friends }: { friends: FriendRecord[] }) {
  const { client, confirm } = useAppContext();
  const [grants, setGrants] = useState<AIGrant[]>([]);
  const [remote, setRemote] = useState<FriendAISnapshot[]>([]);
  const [provider, setProvider] = useState<LLMProviderStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const epoch = useRef(0);
  const working = useRef(false);
  const active = friends.filter((friend) => friend.status === "active");
  const load = async () => {
    const generation = ++epoch.current;
    try {
      const [permissions, snapshots, status] = await Promise.all([aiAccess.list(), aiAccess.friends(), client.getLLMServiceStatus()]);
      if (generation !== epoch.current) return;
      setGrants(permissions.grants); setRemote(snapshots.friends); setProvider(status); setLoaded(true); setError("");
    } catch (reason) { if (generation === epoch.current) setError(reason instanceof Error ? reason.message : "AI settings could not be loaded."); }
  };
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (!working.current) void load(); }, 5000);
    return () => { window.clearInterval(timer); epoch.current += 1; };
  }, [client]);
  const change = async (friend: FriendRecord, service: string, allowed: boolean, previous?: AIGrant) => {
    epoch.current += 1; working.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const result = await aiAccess.set(service, friend.relationship_id, allowed, previous?.revision ?? 0);
      epoch.current += 1;
      setGrants((rows) => [...rows.filter((row) => row.service_id !== service || row.relationship_id !== friend.relationship_id), result.grant]);
      setNotice(allowed ? `Permission saved for ${friend.node_name}. They can refresh their AI services.`
        : `Access revoked for ${friend.node_name}. New requests are blocked. Running computation may continue while cancellation is checked${result.cancellation === "pending" ? "; the node will retry the cancellation check" : ""}.`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "The permission change could not be confirmed."); }
    finally { working.current = false; setBusy(false); }
  };
  const refresh = async (friend: FriendRecord) => {
    epoch.current += 1; working.current = true; setBusy(true); setError("");
    try {
      const snapshot = await aiAccess.refresh(friend.peer_id);
      epoch.current += 1;
      setRemote((rows) => [...rows.filter((row) => row.peer_id !== friend.peer_id), snapshot]);
    } catch (reason) {
      setRemote((rows) => rows.filter((row) => row.peer_id !== friend.peer_id));
      setError(reason instanceof Error ? reason.message : "This friend could not be reached.");
    } finally { working.current = false; setBusy(false); }
  };
  return <Panel><h2>AI with friends</h2>
    <p>Being friends does not grant AI access. Choose who can use each service; your friend's device sees the questions sent to their model.</p>
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy} onClick={() => void load()}>Refresh AI permissions</Button>
    {!loaded ? <p>Loading AI permissions…</p> : <>
      {provider?.service ? <div><h3>Your AI: {provider.service.model_alias}</h3>
        <p>{provider.ready ? "Model ready" : "Model not ready"} · {provider.publication_enabled ? "Friend sharing enabled" : "Friend sharing paused"}</p>
        <Link to="/services/manage">Manage local model</Link>
        {!provider.publication_enabled ? <Button disabled={busy} onClick={() => confirm({ title: "Enable friend AI sharing?",
          body: "Publishes this service's model and capacity metadata to the network. Only explicitly authorized friends can submit requests. Prompts and model files are not published.",
          risk: "medium", confirmLabel: "Enable friend sharing", onConfirm: async () => {
            working.current = true; setBusy(true); try { await client.publishLLMService({ network_id: provider.network_id, benchmark: false }); await load(); }
            finally { working.current = false; setBusy(false); }
          } })}>Enable friend AI sharing</Button> : null}
      </div> : <p>No local AI service configured. <Link to="/services/manage">Set up local AI</Link></p>}
      {active.length === 0 ? <p>Add a friend to share AI.</p> : active.map((friend) => {
        const service = provider?.service?.package_id;
        const grant = grants.find((row) => row.relationship_id === friend.relationship_id && row.service_id === service);
        const snapshot = remote.find((row) => row.relationship_id === friend.relationship_id);
        return <section key={friend.relationship_id} aria-label={`AI with ${friend.node_name}`}>
          <h3>{friend.node_name}</h3>
          {service ? <><p>Your {provider?.service?.model_alias}: {grant?.effective ? "Allowed" : "Not allowed"}</p>
            <Button disabled={busy} onClick={() => confirm({ title: `${grant?.effective ? "Revoke" : "Allow"} AI access for ${friend.node_name}?`,
              body: grant?.effective ? "Blocks new requests immediately. Running work will be asked to cancel, but computation may not stop immediately."
                : `Allows ${friend.node_name} to use ${provider?.service?.model_alias} while friend sharing is enabled. Other services remain unchanged.`,
              risk: "medium", confirmLabel: grant?.effective ? "Revoke access" : "Allow access",
              onConfirm: () => change(friend, service, !grant?.effective, grant) })}>{grant?.effective ? "Revoke AI access" : "Allow AI access"}</Button></> : null}
          {grants.filter((row) => row.relationship_id === friend.relationship_id && row.service_id !== service && row.allowed).map((old) =>
            <div key={old.service_id}><span>Previous service: {old.service_id}</span> <Button disabled={busy} onClick={() => void change(friend, old.service_id, false, old)}>Revoke {old.service_id}</Button></div>)}
          <Button disabled={busy} onClick={() => void refresh(friend)}>Check {friend.node_name}'s AI</Button>
          {!snapshot ? <p>Friend AI access has not been checked.</p> : <>
            <p>Last checked {new Date(snapshot.checked_at * 1000).toLocaleString()}{snapshot.status === "stale" ? " · Refresh before using" : ""}</p>
            {snapshot.services.length === 0 ? <p>{snapshot.status === "revoked" ? "Your friend revoked AI access. Ask them before requesting a new grant."
              : snapshot.status === "not_authorized" ? "Your friend has not allowed you to use their AI. Ask them to review your permission."
              : snapshot.status === "stale" ? "Permission status expired. Refresh to check again."
              : "Your friend's model is unavailable. Ask them to check its setup."}</p> : snapshot.services.map((record) => {
              const age = Date.now() / 1000 - snapshot.checked_at;
              const stale = snapshot.status === "stale" || age < 0 || age > 120;
              const usable = !stale && record.online && record.capacity?.available !== 0;
              return <p key={record.service.package_id}>{record.service.model_alias} · {stale ? "Status expired — refresh" : record.capacity?.available === 0 ? "Busy — wait and refresh" : record.online ? "Ready" : "Not ready or sharing paused"}{" "}
                {usable ? <Link to={`/ask?${new URLSearchParams({ peer: friend.peer_id, service: record.service.package_id, network: record.network_id ?? "rynmesh-main" })}`}>Ask {friend.node_name}'s AI</Link> : null}</p>;
            })}
          </>}
        </section>;
      })}
    </>}
  </Panel>;
}
