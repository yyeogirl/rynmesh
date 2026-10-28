import {
  Bot,
  Check,
  ChevronDown,
  Copy,
  LockKeyhole,
  MessageSquarePlus,
  RotateCcw,
  Search,
  SendHorizontal,
  ShieldCheck,
  Square,
  ThumbsUp,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { LLM_TERMINAL_STATES, llmServiceAvailability, llmServiceRecordKey } from "../domain/llmOrders";
import { Link, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { LoadingPanel } from "../components/ui";
import {
  buildConversationPrompt,
  createConversation,
  titleFromPrompt,
  type LLMChatMessage,
  type LLMConversation,
} from "../domain/llmConversationStore";
import { askHistory, AskRequestError, conversationRepository, legacyMigrationNotice, type AskPreview, type AskRunRequest } from "../domain/askHistory";
import AskMaterials, { AskAnswerSources } from "../components/AskMaterials";
import type { LLMOrderResult, LLMServiceRecord } from "../domain/nodeClient";
import styles from "./PrivateAIChat.module.css";

const TERMINAL_STATES = LLM_TERMINAL_STATES;
const SUGGESTIONS = ["Summarize a document", "Draft a professional email", "Explain a difficult topic"];

function serviceKey(service: LLMServiceRecord) {
  // Aliases are display names and are not unique. Scope history by both the
  // provider identity and package ID to prevent cross-provider conversation
  // mixups. Shared with Services so the two screens can never diverge on the
  // identity format (conversation-store keys persist in IndexedDB).
  return llmServiceRecordKey(service);
}

function messageId() {
  return typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `message_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function historyBucket(value: string) {
  const date = new Date(value);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const itemDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round((today.getTime() - itemDay.getTime()) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 7) return "Previous 7 days";
  return "Older";
}

function resultMessage(result: LLMOrderResult) {
  if (result.state === "cancelled") return "Cancellation was recorded. The provider may still be finishing computation.";
  if (result.state === "timed_out") return "The request timed out. Check its original task before submitting another request.";
  if (result.error_code === "runtime_busy" || result.error_code === "capacity_exhausted") return "The provider is busy. Wait for its current request to finish or choose another service.";
  if (result.error_code === "p2p_capacity_exhausted") return "The connection has no free session capacity. Wait for the active session to close, then retry.";
  if (result.error_code === "insufficient_balance") return "There are not enough credits to run this request.";
  if (result.error_code === "p2p_distinct_public_egress_required") return "The provider needs a different public network. Change networks and try again.";
  return result.output || (result.error_code ? `The request failed: ${result.error_code.replaceAll("_", " ")}.` : "The model did not return a response.");
}

export default function PrivateAIChat() {
  const { client, confirm, notify } = useAppContext();
  const history = useMemo(() => conversationRepository(client.mode), [client.mode]);
  const [searchParams, setSearchParams] = useSearchParams();
  const [services, setServices] = useState<LLMServiceRecord[]>([]);
  const [selectedService, setSelectedService] = useState<LLMServiceRecord | null>(null);
  const selectedServiceKeyRef = useRef("");
  selectedServiceKeyRef.current = selectedService ? serviceKey(selectedService) : "";
  const [networkId, setNetworkId] = useState(searchParams.get("network") || "rynmesh-main");
  const activeNetworkRef = useRef(networkId);
  activeNetworkRef.current = networkId;
  const [conversations, setConversations] = useState<LLMConversation[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [historyReady, setHistoryReady] = useState(false);
  const [sending, setSending] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState("");
  const [unconfirmed, setUnconfirmed] = useState<AskRunRequest | null>(null);
  const [error, setError] = useState("");
  const [storageMode, setStorageMode] = useState<"node-encrypted" | "encrypted" | "session-only">("node-encrypted");
  const [migrationNotice, setMigrationNotice] = useState("");
  const [helpfulMessages, setHelpfulMessages] = useState<Set<string>>(new Set());
  const messageScrollRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  // Conversations removed while a generation is in flight: the completion
  // callback must not resurrect them into state or encrypted storage.
  const deletedIdsRef = useRef<Set<string>>(new Set());
  // Stop pressed before submitLLMOrder returned a task id.
  const cancelRequestedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const selectedConversation = (selectedId ? conversations.find((conversation) => conversation.id === selectedId) : conversations[0]) ?? null;
  const selectedConversationRef = useRef(selectedConversation?.id);
  selectedConversationRef.current = selectedConversation?.id;
  const nodeTask = client.mode === "live" ? selectedConversation?.messages.find((message) => message.role === "assistant" && ["queued", "running", "cancel_requested"].includes(message.status))?.taskId : undefined;
  const isSending = sending || Boolean(nodeTask);

  const refreshNodeHistory = async () => {
    const rows = await history.list(selectedServiceKeyRef.current);
    if (mountedRef.current) setConversations(rows.filter((row) => row.serviceKey === selectedServiceKeyRef.current && row.networkId === activeNetworkRef.current));
  };

  useEffect(() => {
    if (client.mode !== "live" || !historyReady) return;
    let active = true;
    let checking = false;
    const timer = window.setInterval(() => {
      if (checking) return;
      checking = true;
      void history.list(selectedServiceKeyRef.current).then((rows) => {
        if (active) setConversations(rows.filter((row) => row.serviceKey === selectedServiceKeyRef.current && row.networkId === activeNetworkRef.current));
      }).catch(() => { if (active) setError("The node could not be reached. Saved tasks continue on the node; no new request was submitted."); })
        .finally(() => { checking = false; });
    }, 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, [client.mode, history, historyReady]);

  useEffect(() => {
    let active = true;
    let redirecting = false;
    setLoading(true);
    setHistoryReady(false);
    setConversations([]);
    setSelectedId("");
    void (async () => {
      try {
      const settings = await client.getSettings().catch(() => null);
      const network = searchParams.get("network") || settings?.network_id?.trim() || "rynmesh-main";
      const discovered = await client.listLLMServices(network).catch(() => []);
      if (!active) return;
      setNetworkId(network);
      setServices(discovered);
      const requestedPeer = searchParams.get("peer");
      const requestedService = searchParams.get("service");
      const selected = requestedPeer && requestedService
        ? discovered.find((item) => item.peer_id === requestedPeer && item.service.package_id === requestedService) ?? null
        : discovered.find((item) => item.online) ?? discovered[0] ?? null;
      setSelectedService(selected);
      setStorageMode(await history.storageMode());
      if (selected) {
        const key = serviceKey(selected);
        let stored = (await history.list(key)).filter((row) => row.networkId === network);
        const requestedId = searchParams.get("conversation");
        if (requestedId && !stored.some((row) => row.id === requestedId)) throw new Error("This conversation is unavailable for this provider and network. It has not been replaced by another history.");
        if (!stored.length && !requestedId) {
          const fresh = createConversation({
            serviceKey: key,
            serviceName: selected.service.model_alias,
            providerPeerId: selected.peer_id,
            networkId: network,
          });
          stored = [await history.save(fresh)];
        }
        if (active) {
          setConversations(stored);
          const opened = stored.find((row) => row.id === requestedId) ?? stored[0];
          setSelectedId(opened.id);
          setInput(opened.draft ?? "");
          setHistoryReady(true);
          if (!requestedId) { redirecting = true; setSearchParams((prior) => { const next = new URLSearchParams(prior); next.set("conversation", opened.id); return next; }, { replace: true }); }
        }
      }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : "Could not load conversation history.");
      } finally { if (active && !redirecting) setLoading(false); }
    })();
    return () => { active = false; };
  }, [client, history, searchParams, setSearchParams]);

  useEffect(() => {
    const element = messageScrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [selectedConversation?.messages.length, sending]);

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const visible = conversations.filter((conversation) => !needle || conversation.title.toLowerCase().includes(needle));
    return visible.reduce<Record<string, LLMConversation[]>>((groups, conversation) => {
      const bucket = historyBucket(conversation.updatedAt);
      (groups[bucket] ??= []).push(conversation);
      return groups;
    }, {});
  }, [conversations, query]);

  const replaceConversation = async (conversation: LLMConversation, select = true) => {
    const saved = await history.save(conversation);
    if (saved.serviceKey !== selectedServiceKeyRef.current || saved.networkId !== activeNetworkRef.current) return saved;
    setConversations((current) => [
      saved,
      ...current.filter((item) => item.id !== saved.id),
    ].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)));
    if (select) {
      setSelectedId(conversation.id);
      if (searchParams.get("conversation") !== conversation.id) setSearchParams((prior) => { const next = new URLSearchParams(prior); next.set("conversation", conversation.id); return next; });
    }
    return saved;
  };

  const newConversation = async () => {
    if (!selectedService) return;
    setLoading(true);
    const fresh = createConversation({
      serviceKey: serviceKey(selectedService),
      serviceName: selectedService.service.model_alias,
      providerPeerId: selectedService.peer_id,
      networkId,
    });
    try { await replaceConversation(fresh); }
    catch (cause) { setLoading(false); throw cause; }
    setInput("");
    setError("");
  };

  const removeConversation = async (conversationId: string) => {
    const row = conversations.find((item) => item.id === conversationId);
    if (!row) return;
    await history.remove(row);
    deletedIdsRef.current.add(conversationId);
    const remaining = conversations.filter((conversation) => conversation.id !== conversationId);
    if (remaining.length) {
      setConversations(remaining);
      if (selectedId === conversationId) setSearchParams((prior) => { const next = new URLSearchParams(prior); next.set("conversation", remaining[0].id); return next; });
    } else {
      setConversations([]);
      setSelectedId("");
      await newConversation();
    }
  };

  const clearHistory = () => {
    if (!selectedService) return;
    confirm({
      title: "Clear Private AI conversation history?",
      body: `This removes conversation history for this provider, service and network from the node. ${conversations.some((row) => row.sync) ? "Approved devices receive synchronized deletions when connected; concurrent replies may remain in recovery. " : ""}Retained order results and older browser recovery copies are separate. Running requests may continue.`,
      risk: "high",
      confirmLabel: "Clear history",
      onConfirm: async () => {
        await history.clear(serviceKey(selectedService), networkId);
        conversations.forEach((row) => deletedIdsRef.current.add(row.id));
        setConversations([]);
        setSelectedId("");
        await newConversation();
        notify("ok", "Private AI history cleared");
      },
    });
  };

  const submitReviewed = async (request: AskRunRequest) => {
    setSending(true); setActiveTaskId(request.task_id); setError("");
    setUnconfirmed(request);
    try {
      await askHistory.beginRun(request);
      if (mountedRef.current) { setUnconfirmed(null); setInput((current) => current.trim() === request.question ? "" : current); }
      if (cancelRequestedRef.current) { await askHistory.cancelRun(request.task_id); cancelRequestedRef.current = false; }
      await refreshNodeHistory();
    } catch (cause) {
      if (mountedRef.current && cause instanceof AskRequestError && [400, 409].includes(cause.status)) setUnconfirmed(null);
      if (mountedRef.current) setError(cause instanceof Error ? cause.message : "The node did not confirm this request. Check the original task or retry the same reviewed request.");
    } finally {
      if (mountedRef.current) { setSending(false); setActiveTaskId(""); }
    }
  };

  const checkOriginal = async (taskId: string) => {
    if (client.mode === "live") {
      try {
        const run = await askHistory.run(taskId);
        await refreshNodeHistory();
        if (unconfirmed?.task_id === taskId) setInput((current) => current.trim() === unconfirmed.question ? "" : current);
        setUnconfirmed((prior) => prior?.task_id === taskId ? null : prior);
        setError(`Original task: ${run.state}. No new request was submitted.`);
      } catch (cause) { setError(cause instanceof Error ? cause.message : "The original task could not be verified. No new request was submitted."); }
    } else {
      void client.getLLMOrder(taskId).then((result) => setError(`Original task ${result.state}. ${resultMessage(result)} No new request was submitted.`))
        .catch(() => setError("The original task could not be verified. No new request was submitted."));
    }
  };

  const runPrompt = async (promptText: string, preview?: AskPreview) => {
    const text = promptText.trim();
    if (!text || !selectedService || isSending || unconfirmed || !historyReady) return;
    let conversation = selectedConversation;
    if (client.mode === "live" && (!preview || preview.conversation_id !== selectedConversationRef.current || preview.provider_peer_id + "::" + preview.service_id !== selectedServiceKeyRef.current || preview.revision !== conversation?.revision)) {
      setError("This send review is out of date. Review the current conversation before sending.");
      return;
    }
    if (conversation && (conversation.serviceKey !== serviceKey(selectedService) || conversation.networkId !== networkId)) {
      setError("This conversation belongs to another provider. Open a separate conversation for the selected service.");
      return;
    }
    if (client.mode === "live" && preview) {
      cancelRequestedRef.current = false;
      return submitReviewed({ task_id: "task_" + crypto.randomUUID().replaceAll("-", ""), conversation_id: preview.conversation_id,
        expected_revision: preview.revision, question: text, prompt_sha256: preview.prompt_sha256,
        ...(preview.ai_permission ? { ai_permission: preview.ai_permission } : {}) });
    }
    if (!conversation) {
      conversation = createConversation({
        serviceKey: serviceKey(selectedService),
        serviceName: selectedService.service.model_alias,
        providerPeerId: selectedService.peer_id,
        networkId,
      });
    }
    const now = new Date().toISOString();
    const taskId = "task_" + messageId().replaceAll("-", "");
    const userMessage: LLMChatMessage = { id: messageId(), role: "user", content: text, createdAt: now, status: "complete", taskId };
    let withUser: LLMConversation = {
      ...conversation, draft: "",
      title: conversation.messages.length ? conversation.title : titleFromPrompt(text),
      updatedAt: now,
      messages: [...conversation.messages, userMessage],
    };
    setError("");
    setSending(true);
    cancelRequestedRef.current = false;
    let userSaved = false;
    try {
      withUser = await replaceConversation(withUser);
      userSaved = true;
      setInput("");
      let result = await client.submitLLMOrder({
        task_id: taskId, idempotency_key: taskId,
        network_id: networkId,
        provider_peer_id: selectedService.peer_id,
        service_id: selectedService.service.package_id,
        prompt: preview?.prompt ?? buildConversationPrompt(withUser.messages),
        max_tokens: preview?.max_output_tokens ?? Math.min(selectedService.service.max_output_tokens || 256, 256),
        transport: "auto",
      });
      setActiveTaskId(result.task_id);
      if (cancelRequestedRef.current) {
        // Stop was pressed while the submit call was still in flight; the
        // task id only just became known, so deliver the cancellation now.
        cancelRequestedRef.current = false;
        await client.cancelLLMOrder(result.task_id).catch(() => null);
      }
      // Orders are asynchronous at the node boundary. Keep polling centralized
      // here so the UI never bypasses node transport, settlement, or cancellation.
      while (mountedRef.current && !TERMINAL_STATES.has(result.state)) {
        await new Promise((resolve) => window.setTimeout(resolve, 650));
        result = await client.getLLMOrder(result.task_id);
      }
      if (!mountedRef.current) return;
      const success = result.state === "succeeded";
      const assistantMessage: LLMChatMessage = {
        id: messageId(),
        role: "assistant",
        content: resultMessage(result),
        createdAt: new Date().toISOString(),
        status: success ? "complete" : result.state === "cancelled" ? "cancelled" : "failed",
        taskId: result.task_id,
        inputTokens: result.input_tokens,
        outputTokens: result.output_tokens,
        cost: result.amount,
        ...(preview ? { contextIds: preview.sources.map((source) => source.library_id), contextBytes: preview.sources.map((source) => source.included_bytes ?? 0), promptSha256: preview.prompt_sha256 } : {}),
      };
      const completed = {
        ...withUser,
        updatedAt: assistantMessage.createdAt,
        messages: [...withUser.messages, assistantMessage],
      };
      if (deletedIdsRef.current.has(withUser.id)) return;
      await replaceConversation(completed, false);
      notify(success ? "ok" : "warn", success ? "Private AI response complete" : `Private AI request ${result.state}`);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "Private AI request failed";
      const failedMessage: LLMChatMessage = {
        id: messageId(), role: "assistant", content: message, createdAt: new Date().toISOString(), status: "failed", taskId,
      };
      if (mountedRef.current && !deletedIdsRef.current.has(withUser.id)) {
        if (userSaved) {
          try { await replaceConversation({ ...withUser, updatedAt: failedMessage.createdAt, messages: [...withUser.messages, failedMessage] }, false); }
          catch { setInput(text); }
        } else setInput(text);
        setError(message);
        notify("danger", message);
      }
    } finally {
      if (mountedRef.current) {
        setSending(false);
        setActiveTaskId("");
      }
    }
  };

  const stopGeneration = async () => {
    if (client.mode === "live") {
      cancelRequestedRef.current = true;
      const taskId = nodeTask || activeTaskId;
      if (taskId) {
        try { await askHistory.cancelRun(taskId); cancelRequestedRef.current = false; await refreshNodeHistory(); }
        catch { setError("Cancellation has not been confirmed. The task may still be running; check it again."); }
      }
      return;
    }
    if (!activeTaskId) {
      // The submit call has not returned a task id yet. Record the intent so
      // the cancellation is delivered the moment the id exists — otherwise
      // Stop during a slow submit was a silent no-op and the user paid for a
      // generation they explicitly stopped.
      if (sending) cancelRequestedRef.current = true;
      return;
    }
    await client.cancelLLMOrder(activeTaskId).catch(() => null);
  };

  const retryLast = () => {
    const messages = selectedConversation?.messages ?? [];
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    if (lastUser?.taskId) {
      void checkOriginal(lastUser.taskId);
    } else if (lastUser) setInput(lastUser.content);
  };

  const reviewAndSend = async () => {
    if (!selectedConversation || !selectedService || !input.trim() || isSending || unconfirmed || reviewing || !historyReady) return;
    if (client.mode !== "live") return runPrompt(input);
    const question = input.trim();
    setReviewing(true); setError("");
    try {
      const preview = await askHistory.preview(selectedConversation, question);
      confirm({ title: `Send to ${selectedService.service.model_alias}?`, risk: "medium", confirmLabel: "Send reviewed question",
        body: `Recipient: ${preview.provider_peer_id}. Conservative input estimate ${preview.input_token_upper_estimate}, framing reserve ${preview.framing_reserve}, output reserve ${preview.max_output_tokens}, context window ${preview.context_window}. ${preview.history_messages_omitted} older history messages omitted.${preview.sources.length ? " Article text is treated as untrusted material." : " No article material is attached."}`,
        details: [{ label: "Question", value: question }, ...preview.sources.map((source) => ({ label: `Source ${source.source_number}: ${source.title}`, value: `${source.source_url || "Private local document"} · ${source.included_bytes}/${source.text_bytes} bytes${source.budget_truncated || source.extraction_truncated ? " · TRUNCATED" : ""}` }))],
        onConfirm: () => runPrompt(question, preview),
      });
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not prepare the send review."); }
    finally { setReviewing(false); }
  };

  if (loading) return <LoadingPanel label="Opening Private AI" />;

  if (!selectedService) {
    return (
      <div className="empty-state">
        <Bot size={28} />
        <h3>The selected provider is unavailable</h3>
        <p>Your conversation remains bound to its original provider. Choose another service to start a separate conversation.</p>
        <Link to={searchParams.get("conversation") ? `/ask?conversation=${encodeURIComponent(searchParams.get("conversation")!)}&network=${encodeURIComponent(networkId)}` : "/ask"}>View history and choose a service</Link>
        <Link to="/services/manage">Set up a local model</Link>
        {error ? <p role="alert">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <aside className={styles.history} aria-label="Private AI conversations">
        <button className={styles.newButton} type="button" onClick={() => void newConversation().catch((cause: Error) => setError(cause.message))}>
          <MessageSquarePlus size={17} /> New chat
        </button>
        <label className={styles.historySearch}>
          <Search size={16} />
          <input aria-label="Search conversations" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search conversations" />
        </label>
        <div className={styles.historyScroll}>
          {["Today", "Yesterday", "Previous 7 days", "Older"].map((bucket) => grouped[bucket]?.length ? (
            <section className={styles.historyGroup} key={bucket}>
              <h2>{bucket}</h2>
              {grouped[bucket].map((conversation) => (
                <div className={`${styles.conversationRow}${selectedConversation?.id === conversation.id ? ` ${styles.conversationRowSelected}` : ""}`} key={conversation.id}>
                  <button className={styles.conversationButton} type="button" onClick={() => setSearchParams((prior) => { const next = new URLSearchParams(prior); next.set("conversation", conversation.id); return next; })}>
                    <strong>{conversation.title}</strong>
                    <small>{formatTime(conversation.updatedAt)}</small>
                  </button>
                  <button className={styles.deleteButton} type="button" aria-label={`Delete ${conversation.title}`} onClick={() => confirm({ title: "Delete this conversation?", body: `${conversation.sync ? "Delete this conversation from synchronized history. Approved devices receive the deletion when connected; concurrent replies may remain in recovery. " : "This removes this node's conversation history. "}Running requests and older browser recovery copies are separate.`, risk: "high", confirmLabel: "Delete conversation", onConfirm: () => removeConversation(conversation.id) })}>
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </section>
          ) : null)}
          {!Object.keys(grouped).length ? <div className={styles.historyEmpty}>No matching conversations</div> : null}
        </div>
        <button className={styles.clearButton} type="button" onClick={clearHistory}>
          <Trash2 size={14} /> Clear history
        </button>
        {client.mode === "live" ? <button type="button" onClick={() => confirm({ title: "Import older browser conversations?", body: "This reads encrypted history from this browser and saves it on your node. Original provider bindings are retained. Browser originals stay available as recovery copies.", confirmLabel: "Import conversations", risk: "medium", onConfirm: async () => {
          const result = await askHistory.importLegacy();
          setMigrationNotice(legacyMigrationNotice(result));
          setConversations((await history.list(serviceKey(selectedService))).filter((row) => row.networkId === networkId));
        } })}>Import older browser conversations</button> : null}
        {migrationNotice ? <p role="status">{migrationNotice}</p> : null}
      </aside>

      <main className={styles.workspace}>
        <header className={styles.chatHeader}>
          <div className={styles.modelLockup}>
            <span className={styles.modelIcon}><Bot size={24} /></span>
            <div className={styles.modelCopy}>
              <h1>Ask Ryn</h1>
              <Link to="/ask">History and model selection</Link>
              <span>{selectedService.service.model_alias}</span>
              <div className={styles.modelStatus}>
                <span className={styles.statusBadge}>{llmServiceAvailability(selectedService)}</span>
                {selectedService.access === "self" && !selectedService.online && <Link to="/services/manage">Manage local model</Link>}
                <span className={styles.statusBadge}><LockKeyhole size={11} /> Encrypted</span>
              </div>
            </div>
          </div>
          <details className={styles.details}>
            <summary className={styles.detailsButton}>Details <ChevronDown size={14} /></summary>
            <div className={styles.detailsPanel}>
              <dl>
                <div><dt>Model</dt><dd>{selectedService.service.model_alias}</dd></div>
                <div><dt>Provider</dt><dd>{selectedService.node_name || selectedService.peer_id}</dd></div>
                <div><dt>Service</dt><dd>{selectedService.service.package_id}</dd></div>
                <div><dt>Network</dt><dd>{networkId}</dd></div>
                <div><dt>Context</dt><dd>{selectedService.service.context_window} tokens</dd></div>
              </dl>
              <p className={styles.privacyCopy}>
                Requests are encrypted in transit. History is saved on your node; its files are encrypted using the node's identity. The selected provider sees plaintext while generating a response.
              </p>
            </div>
          </details>
        </header>

        {selectedConversation?.sync && (selectedConversation.sync.conflict || selectedConversation.sync.deferred) ? <p role="status">
          Other-device changes are saved for review. This conversation keeps its current context. <Link to={`/ask?${new URLSearchParams({ conversation: selectedConversation.id, network: selectedConversation.networkId })}`}>Review conversation branches</Link>
        </p> : null}
        <div className={styles.messages} ref={messageScrollRef}>
          {!selectedConversation?.messages.length ? (
            <div className={styles.welcome}>
              <span className={styles.welcomeIcon}><Bot size={27} /></span>
              <h2>Start a private conversation</h2>
              <p>Your history stays with this provider and service. The provider shown above receives the messages you send.</p>
              <div className={styles.suggestions}>
                {SUGGESTIONS.map((suggestion) => <button type="button" key={suggestion} onClick={() => setInput(suggestion)}>{suggestion}</button>)}
              </div>
            </div>
          ) : selectedConversation.messages.map((message) => (
            <div className={`${styles.messageRow}${message.role === "user" ? ` ${styles.messageRowUser}` : ""}`} key={message.id}>
              {message.role === "assistant" ? <span className={styles.assistantAvatar}><Bot size={18} /></span> : null}
              <div className={styles.messageBlock}>
                <div className={`${styles.messageBubble}${message.status === "failed" ? ` ${styles.messageFailed}` : ""}`}>{message.content}</div>
                <span className={styles.messageMeta}>{formatTime(message.createdAt)}{message.cost !== undefined ? ` · ${message.cost} credits` : ""}</span>
                {message.role === "assistant" ? (
                  <div className={styles.messageActions}>
                    <button type="button" onClick={() => void navigator.clipboard?.writeText(message.content)}><Copy size={12} /> Copy</button>
                    <button type="button" onClick={() => setHelpfulMessages((current) => new Set(current).add(message.id))}>
                      {helpfulMessages.has(message.id) ? <Check size={12} /> : <ThumbsUp size={12} />} {helpfulMessages.has(message.id) ? "Helpful" : "Good response"}
                    </button>
                    {message.status !== "complete" ? <button type="button" onClick={() => message.taskId ? void checkOriginal(message.taskId) : retryLast()}><RotateCcw size={12} /> Check original task</button> : null}
                  </div>
                ) : null}
                {message.contextIds?.length ? <AskAnswerSources ids={message.contextIds} byteLimits={message.contextBytes} /> : null}
              </div>
            </div>
          ))}
          {sending ? (
            <div className={styles.messageRow}>
              <span className={styles.assistantAvatar}><Bot size={18} /></span>
              <div className={styles.thinking} aria-label="Private AI is thinking"><span /><span /><span /></div>
            </div>
          ) : null}
        </div>

        <div className={styles.composerWrap}>
          {selectedConversation?.contextIds?.length ? <AskMaterials ids={selectedConversation.contextIds} onRemove={async (id) => { await replaceConversation({ ...selectedConversation, contextIds: selectedConversation.contextIds?.filter((value) => value !== id) }, false); }} /> : null}
          {error ? <div className={styles.error} role="alert">{error}</div> : null}
          {historyReady && !selectedConversation ? <div role="alert">This conversation is no longer available. Your input has been kept; open its history or create a new conversation.</div> : null}
          {unconfirmed ? <div role="status">The node has not confirmed this reviewed request for conversation {unconfirmed.conversation_id}.
            <button type="button" disabled={sending} onClick={() => void checkOriginal(unconfirmed.task_id)}>Check original task</button>
            <button type="button" disabled={sending} onClick={() => void submitReviewed(unconfirmed)}>Retry same reviewed request</button>
          </div> : null}
          <div className={styles.composer}>
            <textarea
              aria-label="Message Private AI"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Message Private AI"
              rows={1}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void reviewAndSend();
                }
              }}
            />
            {isSending ? (
              <button className={styles.stopButton} type="button" aria-label="Stop generating" onClick={() => void stopGeneration()}><Square size={15} /></button>
            ) : (
              <button className={styles.sendButton} type="button" aria-label="Send message" disabled={!input.trim() || !historyReady || reviewing || Boolean(unconfirmed)} onClick={() => void reviewAndSend()}><SendHorizontal size={17} /></button>
            )}
          </div>
          <div className={styles.composerMeta}>
            <span><ShieldCheck size={12} /> {storageMode === "node-encrypted" ? "Encrypted history on your node" : storageMode === "encrypted" ? "Encrypted in this browser" : "History kept for this session"}</span>
            <span>{selectedService.service.pricing?.minimum === undefined ? "Price unavailable" : `Estimated minimum ${selectedService.service.pricing.minimum} credits`}</span>
          </div>
        </div>
      </main>
    </div>
  );
}
