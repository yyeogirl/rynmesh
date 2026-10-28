import { ArrowLeft, Film, Play, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { JobCapacity, WorkResult } from "../domain/types";
import styles from "./ServiceExperience.module.css";

const CAPABILITY = "signal50.veo_motion.v1";
const OPERATION = "signal50.remote_action.complete_flow_video_veo_motion_clips";

export default function VideoRendering() {
  const { client, notify } = useAppContext();
  const navigate = useNavigate();
  const [providers, setProviders] = useState<JobCapacity[]>([]);
  const [projectId, setProjectId] = useState("");
  const [maxScenes, setMaxScenes] = useState("0");
  const [skipExisting, setSkipExisting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [orderId, setOrderId] = useState("");
  const [results, setResults] = useState<WorkResult[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void client.listJobCapacities({ capability: CAPABILITY })
      .then((items) => { if (active) setProviders(items.filter((item) => item.capabilities.includes(CAPABILITY))); })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "Could not find a renderer."); });
    return () => { active = false; };
  }, [client]);

  const provider = providers[0];
  const price = Number(provider?.price_credits[CAPABILITY] ?? 0);
  const refreshResult = async () => {
    if (!orderId) return;
    setResults(await client.listWorkResults({ work_order_id: orderId }));
  };

  const submit = async () => {
    if (!provider || !projectId.trim()) return;
    setBusy(true);
    setError("");
    try {
      const order = await client.submitWorkOrder({
        provider_peer_id: provider.peer_id,
        capability: CAPABILITY,
        operation: OPERATION,
        max_credit_cost: price,
        network_id: provider.network_id,
        params: { video_id: projectId.trim(), max_scenes: Number(maxScenes || 0), skip_existing: skipExisting },
      });
      setOrderId(order.work_order_id);
      setResults([]);
      notify("ok", "Video render request submitted");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not submit the render request.");
    } finally {
      setBusy(false);
    }
  };

  const latest = useMemo(() => results[0], [results]);

  return (
    <div className={styles.page}>
      <button type="button" className={styles.back} onClick={() => navigate("/services")}><ArrowLeft size={15} /> All services</button>
      <header className={styles.hero}>
        <div className={styles.heroTitle}><span className={styles.heroIcon}><Film size={25} /></span><div><h1>Video rendering</h1><p>Create motion clips through an available rendering service.</p></div></div>
        <span className={styles.status}>{provider ? "Renderer ready" : "Finding renderer"}</span>
      </header>
      <div className={styles.layout}>
        <section className={styles.panel}>
          <h2>Create a render</h2><p className={styles.panelLead}>Choose the project to render. Ryn selects the provider and route for you.</p>
          <div className={styles.form}>
            <label className={styles.field}>Video project ID<input aria-label="Video project ID" value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="Enter a project ID" /></label>
            <label className={styles.field}>Maximum scenes<input aria-label="Maximum scenes" type="number" min="0" value={maxScenes} onChange={(event) => setMaxScenes(event.target.value)} /></label>
            <label className={styles.check}><input type="checkbox" checked={skipExisting} onChange={(event) => setSkipExisting(event.target.checked)} /> Skip clips that are already rendered</label>
            {error ? <span role="alert" className={styles.error}>{error}</span> : null}
            <button type="button" className={styles.primary} disabled={!provider || !projectId.trim() || busy} onClick={() => void submit()}><Play size={16} /> {busy ? "Submitting…" : "Start rendering"}</button>
            {orderId ? <div className={styles.result}><strong>Render request submitted</strong><p>{latest ? `${latest.status}: ${latest.message}` : "Your provider has received the request. Check again for progress."}</p><button type="button" className={styles.secondary} onClick={() => void refreshResult()}><RotateCcw size={14} /> Check progress</button></div> : null}
          </div>
        </section>
        <aside className={styles.panel}>
          <h2>Before you start</h2><p className={styles.panelLead}>The final cost will not exceed the amount shown here.</p>
          <div className={styles.summary}><div className={styles.summaryRow}><span>Availability</span><strong>{provider ? "Ready" : "Unavailable"}</strong></div><div className={styles.summaryRow}><span>Maximum cost</span><strong>{provider ? `${price} credits` : "—"}</strong></div><div className={styles.summaryRow}><span>Capacity</span><strong>{provider ? `${provider.capacity_units} slot${provider.capacity_units === 1 ? "" : "s"}` : "—"}</strong></div></div>
          {provider ? <details className={styles.details}><summary>Provider details</summary><div className={styles.detailBody}><span>{provider.provider_name || provider.node_name}</span><span>{provider.network_id}</span><span>{provider.peer_id}</span></div></details> : null}
        </aside>
      </div>
    </div>
  );
}
