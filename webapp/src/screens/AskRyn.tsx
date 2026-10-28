import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { AppOutletContext } from "../appContext";
import { Button, PageHeader, Panel } from "../components/ui";
import { askHistory, legacyMigrationNotice } from "../domain/askHistory";
import { friendsApi } from "../domain/friendsClient";
import { createConversation, readLegacyConversations, saveConversation, type LLMConversation } from "../domain/llmConversationStore";
import { llmServiceAvailability, llmServiceRecordKey } from "../domain/llmOrders";
import type { LLMServiceRecord } from "../domain/nodeClient";
import PrivateAIChat from "./PrivateAIChat";
import styles from "./AskRyn.module.css";
import AskMaterials, { AskAnswerSources } from "../components/AskMaterials";
import AskSyncConflicts from "../components/AskSyncConflicts";

export function conversationUrl(row: LLMConversation, continueChat = false) {
  const query = new URLSearchParams({ conversation: row.id, network: row.networkId });
  if (continueChat) { query.set("peer", row.providerPeerId); query.set("service", row.serviceKey.slice(row.providerPeerId.length + 2)); }
  return `/ask?${query}`;
}

export default function AskRyn() {
  const [params] = useSearchParams();
  return params.get("peer") && params.get("service") ? <PrivateAIChat /> : <AskRynHome />;
}

function AskRynHome() {
  const { client, node, confirm } = useAppContext();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [rows, setRows] = useState<LLMConversation[]>([]);
  const [services, setServices] = useState<LLMServiceRecord[]>([]);
  const [friends, setFriends] = useState<string[]>([]);
  const [network, setNetwork] = useState("rynmesh-main");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [title, setTitle] = useState("");
  const [draft, setDraft] = useState("");
  const [draftReady, setDraftReady] = useState(false);
  const [draftStatus, setDraftStatus] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportResult, setExportResult] = useState<{ failed: boolean; text: string } | null>(null);
  const exportBusy = useRef(false);
  const exportFeedback = useRef<HTMLParagraphElement>(null);
  const [loading, setLoading] = useState(true);
  const draftRecord = useRef({ text: "", revision: 0 });
  const drafts = useRef(Promise.resolve());
  const selection = rows.find((row) => row.id === params.get("conversation"));
  const material = params.get("material");
  useEffect(() => { if (exportResult) exportFeedback.current?.focus(); }, [exportResult]);
  const load = useCallback(async () => {
    setError(""); setLoading(true);
    try {
      const settings = await client.getSettings();
      const currentNetwork = params.get("network") || settings.network_id || "rynmesh-main";
      const [history, discovered] = await Promise.all([
        client.mode === "live" ? askHistory.list() : readLegacyConversations().then((result) => result.conversations),
        client.listLLMServices(currentNetwork).catch(() => { setNotice("Service discovery is unavailable. Saved history remains on this node."); return []; }),
      ]);
      setRows(history); setServices(discovered); setNetwork(currentNetwork);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not load history."); }
    finally { setLoading(false); }
  }, [client, params]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    let active = true;
    if (client.mode !== "live") { setDraftReady(true); return; }
    void askHistory.draft().then((saved) => { if (active) { draftRecord.current = saved; setDraft(saved.text); setDraftReady(true); } })
      .catch(() => { if (active) setDraftStatus("Draft could not be loaded. Reopen Ask Ryn after reconnecting."); });
    void friendsApi.list().then((result) => { if (active) setFriends(result.friends.filter((row) => row.status === "active").map((row) => row.peer_id)); }).catch(() => undefined);
    return () => { active = false; };
  }, [client.mode]);
  useEffect(() => { setTitle(selection?.title ?? ""); }, [selection?.id, selection?.title]);
  useEffect(() => {
    if (!selection || !params.get("message")) return;
    const element = document.getElementById(`ask-message-${params.get("message")}`);
    element?.focus({ preventScroll: true }); element?.scrollIntoView?.({ block: "center" });
  }, [selection?.id, params, loading]);
  useEffect(() => {
    if (!selection || loading || params.get("message")) return;
    const element = document.getElementById("ask-selected-conversation");
    element?.focus({ preventScroll: true }); element?.scrollIntoView?.({ block: "start" });
  }, [selection?.id, params, loading]);
  const persistDraft = useCallback((text: string) => {
    if (!draftReady || client.mode !== "live") return Promise.resolve();
    const next = drafts.current.catch(() => undefined).then(async () => {
      if (draftRecord.current.text === text) return;
      setDraftStatus("Saving draft…");
      draftRecord.current = await askHistory.saveDraft(text, draftRecord.current.revision);
      setDraftStatus("Draft saved on your node. It has not been sent to a model.");
    });
    drafts.current = next;
    return next.catch((cause: Error) => { setDraftStatus(cause.message); throw cause; });
  }, [client.mode, draftReady]);
  useEffect(() => {
    const timer = window.setTimeout(() => { void persistDraft(draft).catch(() => undefined); }, 400);
    return () => window.clearTimeout(timer);
  }, [draft, persistDraft]);
  const openService = (service: LLMServiceRecord) => confirm({
    title: `Start with ${service.service.model_alias}?`, risk: "medium", confirmLabel: "Open separate conversation",
    body: `Messages you send will be received by ${service.node_name || service.peer_id} (${service.peer_id}). Previous conversations stay with their original service.${material ? " The selected article will be included for review before sending." : ""}${draft.trim() ? " Your draft will be copied into the new composer for review; it is not sent now." : ""}`,
    onConfirm: async () => {
      await persistDraft(draft);
      const fresh = { ...createConversation({ serviceKey: llmServiceRecordKey(service), serviceName: service.service.model_alias, providerPeerId: service.peer_id, networkId: network }), draft, contextIds: material ? [material] : [] };
      if (client.mode === "live") await askHistory.save(fresh);
      else await saveConversation(fresh);
      navigate(conversationUrl(fresh, true));
    },
  });
  const exportHistory = async () => {
    if (exportBusy.current || !draftReady) return;
    exportBusy.current = true; setExporting(true); setExportResult(null);
    try {
      await persistDraft(draft);
      const value = await askHistory.export();
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
      try {
        const anchor = document.createElement("a"); anchor.href = url; anchor.download = "ryn-conversations.json"; anchor.click();
      } finally { URL.revokeObjectURL(url); }
      setExportResult({ failed: false, text: "Export prepared and download requested. Check your downloads to confirm the file was saved." });
    } catch {
      setExportResult({ failed: true, text: "Export was not completed. Your draft remains here. Check the node connection and draft save status, then retry export." });
    } finally { exportBusy.current = false; setExporting(false); }
  };
  return <div className={styles.home}>
    <PageHeader eyebrow="Your assistant" title="Ask Ryn" context="Your conversations stay on your node. Choose who receives each new conversation." />
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <div className={styles.columns}>
      <Panel title="Conversations">
        <label>Find a conversation<input aria-label="Find a conversation" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        <Button onClick={() => void load()} disabled={loading}>Refresh history and services</Button>
        {loading ? <p>Loading node history…</p> : !rows.length ? <p>No conversations yet.</p> : <ul className={styles.history}>
          {rows.filter((row) => `${row.title} ${row.serviceName}`.toLowerCase().includes(query.toLowerCase())).map((row) => <li key={row.id}>
            <Link to={conversationUrl(row)}>{row.title}</Link><small>{row.serviceName} · {row.providerPeerId}</small>
          </li>)}
        </ul>}
        {client.mode === "live" ? <>
          <Button disabled={exporting || !draftReady} onClick={() => void exportHistory()}>{exporting ? "Preparing export…" : "Export conversations and draft"}</Button>
          {exportResult ? <p ref={exportFeedback} tabIndex={-1} role={exportResult.failed ? "alert" : "status"}>{exportResult.text}</p> : null}
          <Button onClick={() => confirm({ title: "Import older browser conversations?", risk: "medium", confirmLabel: "Import conversations", body: "Read this browser's encrypted history into the node, keeping original providers and service keys. Browser originals remain recovery copies.", onConfirm: async () => { const result = await askHistory.importLegacy(); setNotice(legacyMigrationNotice(result)); await load(); } })}>Import older browser conversations</Button>
        </> : null}
      </Panel>
      <div className={styles.workspace}>
        {client.mode === "live" ? <AskSyncConflicts refreshKey={rows} onRestored={async (row) => { await load(); navigate(conversationUrl(row)); }} /> : null}
        {selection ? <div id="ask-selected-conversation" className={styles.selection} role="region" aria-label="Selected conversation" tabIndex={-1}><Panel title={selection.title}>
          <p>Original recipient: {selection.providerPeerId} · {selection.serviceName} · {selection.networkId}</p>
          <small>Conversation ID: {selection.id}</small>
          <div className={styles.transcript}>{selection.messages.length ? selection.messages.map((message) => <article key={message.id} id={`ask-message-${message.id}`} tabIndex={-1} style={message.id === params.get("message") ? { outline: "2px solid currentColor" } : undefined}><strong>{message.role === "user" ? "You" : "Ryn"}</strong><p>{message.content}</p><small>{message.status}</small>{message.contextIds?.length ? <AskAnswerSources ids={message.contextIds} byteLimits={message.contextBytes} /> : null}</article>) : <p>No messages yet.</p>}</div>
          {selection.draft ? <p>Saved draft: {selection.draft}</p> : null}
          {services.some((service) => llmServiceRecordKey(service) === selection.serviceKey) ? <Link to={conversationUrl(selection, true)}>Continue with original provider</Link> : <p>The original service is unavailable. Your history is still readable; a different service starts a separate conversation.</p>}
          {client.mode === "live" ? <>
            <label>Conversation name<input aria-label="Conversation name" value={title} onChange={(event) => setTitle(event.target.value)} /></label>
            <Button disabled={!title.trim()} onClick={() => void askHistory.save({ ...selection, title: title.trim() }).then(load).catch((cause: Error) => setError(cause.message))}>Save conversation name</Button>
            <Button onClick={() => confirm({ title: "Delete this conversation?", risk: "high", confirmLabel: "Delete conversation", body: `${selection.sync ? "Delete this conversation from synchronized history. Approved devices receive the deletion when connected; concurrent replies may remain in recovery. " : "Remove this node's conversation and saved draft. "}Older browser recovery copies and order results are separate. Running computation may continue.`, onConfirm: async () => { await askHistory.remove(selection); navigate("/ask"); await load(); } })}>Delete conversation</Button>
          </> : null}
        </Panel></div> : params.get("conversation") && !loading ? <p role="alert">This conversation is unavailable or was deleted. It has not been replaced by another conversation.</p> : null}
        <Panel title="Start a conversation">
          {material ? <AskMaterials ids={[material]} onRemove={() => { const next = new URLSearchParams(params); next.delete("material"); navigate(`/ask?${next}`); }} /> : null}
          <label>Your draft<textarea aria-label="Your draft" rows={4} disabled={!draftReady} value={draft} onChange={(event) => setDraft(event.target.value)} onBlur={() => void persistDraft(draft).catch(() => undefined)} placeholder="Write a question even before a model is ready…" /></label>
          {draftStatus ? <p role="status">{draftStatus}</p> : null}
          <Button disabled={!draftReady} onClick={() => void persistDraft(draft).catch(() => undefined)}>Save draft</Button>
          <Link to="/services/manage">{services.some((service) => service.access === "self") ? "Manage local model" : "Set up a local model"}</Link>
          {!loading && !services.length ? <p>No model service is available. You can keep your draft, read saved history, or set up local AI.</p> : null}
          <ul className={styles.services}>{services.map((service) => <li key={llmServiceRecordKey(service)}>
            <strong>{service.service.model_alias}</strong>
            <p>{service.peer_id === node.peer_id ? "This node" : friends.includes(service.peer_id) ? "Friend" : "Remote provider"}: {service.node_name || service.peer_id}</p>
            <small>{service.peer_id} · {service.service.package_id}</small>
            <p>{llmServiceAvailability(service)} · Context: {service.service.context_window} tokens</p>
            <p>{service.service.pricing?.minimum === undefined ? "Price unavailable" : `Minimum ${service.service.pricing.minimum} ${service.service.pricing.currency}; input ${service.service.pricing.input_per_1k}/1k, output ${service.service.pricing.output_per_1k}/1k`}</p>
            <Button onClick={() => openService(service)}>Choose {service.service.model_alias}</Button>
          </li>)}</ul>
        </Panel>
      </div>
    </div>
  </div>;
}

export function AskRynQuickPanel({ context }: { context: AppOutletContext }) {
  const [rows, setRows] = useState<LLMConversation[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const read = () => { void (context.client.mode === "live" ? askHistory.list() : readLegacyConversations().then((result) => result.conversations))
      .then((result) => { if (active) { setRows(result.slice(0, 5)); setError(""); } })
      .catch(() => { if (active) setError("Reconnect to load node history."); }); };
    read(); const timer = window.setInterval(read, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [context.client]);
  return <div className="ai-side-inner"><h2>Ask Ryn</h2><p>Open the same conversation from any entry point.</p>
    <Link to="/ask">Open Ask Ryn</Link>{error ? <p role="alert">{error}</p> : null}
    <ul className={styles.history}>{rows.map((row) => <li key={row.id}><Link to={conversationUrl(row)}>{row.title}</Link><small>{row.serviceName}</small></li>)}</ul>
  </div>;
}
