import { useCallback, useEffect, useRef, useState } from "react";
import { askHistory, AskRequestError, recoveryConversationId, type AskSyncChoice, type AskSyncConflict } from "../domain/askHistory";
import type { LLMConversation } from "../domain/llmConversationStore";
import { Button, Panel } from "./ui";
import styles from "./AskSyncConflicts.module.css";

export default function AskSyncConflicts({ refreshKey, onRestored }: {
  refreshKey?: unknown; onRestored: (conversation: LLMConversation) => void | Promise<void>;
}) {
  const [issues, setIssues] = useState<AskSyncConflict[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState("");
  const [replacement, setReplacement] = useState<{ issue: AskSyncConflict; choice: AskSyncChoice; deletedId: string } | null>(null);
  const [discardReview, setDiscardReview] = useState<AskSyncConflict | null>(null);
  const [notice, setNotice] = useState("");
  const replacementReview = useRef<HTMLElement>(null);
  const discardSection = useRef<HTMLElement>(null);
  const noticeSection = useRef<HTMLParagraphElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const alive = useRef(true);
  const busy = useRef(false);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setError("");
    try {
      const rows = await askHistory.syncConflicts();
      if (alive.current && current === generation.current) { setIssues(rows); setExpanded(""); setReplacement(null); setDiscardReview(null); }
    } catch (cause) {
      if (alive.current && current === generation.current) setError(cause instanceof Error ? cause.message : "Could not load conversation branches.");
    } finally { if (alive.current && current === generation.current) setLoading(false); }
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current += 1; }; }, []);
  useEffect(() => { void load(); }, [load, refreshKey]);
  useEffect(() => { if (replacement) replacementReview.current?.focus(); }, [replacement]);
  useEffect(() => { if (discardReview) discardSection.current?.focus(); }, [discardReview]);
  useEffect(() => { if (notice) noticeSection.current?.focus(); }, [notice]);
  useEffect(() => { const refresh = () => { if (!busy.current) void load(); }; window.addEventListener("focus", refresh); return () => window.removeEventListener("focus", refresh); }, [load]);
  const restore = async (issue: AskSyncConflict, choice: AskSyncChoice, replaces?: string) => {
    if (busy.current) return;
    if (!replaces) returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    busy.current = true; setSaving(true); setError("");
    setDiscardReview(null); setNotice("");
    if (!replaces) setReplacement(null);
    let id = "";
    try {
      // Stable across retries and page restarts: a lost response cannot create
      // another copy of the same reviewed branch.
      id = await recoveryConversationId(issue, choice, replaces);
      const saved = replaces ? await askHistory.restoreBranch(issue.id, choice.choice_id, id, issue.revision, replaces)
        : await askHistory.restoreBranch(issue.id, choice.choice_id, id, issue.revision);
      if (alive.current) { setReplacement(null); await onRestored(saved); }
    } catch (cause) {
      if (alive.current && id && cause instanceof AskRequestError && cause.code === "ask_conversation_deleted") {
        setReplacement({ issue, choice, deletedId: id });
      } else if (alive.current) setError(cause instanceof Error ? cause.message : "The node did not confirm keeping this branch. Retry to check the same copy.");
    }
    finally { busy.current = false; if (alive.current) setSaving(false); }
  };
  const discard = async () => {
    if (busy.current || !discardReview?.discard_token) return;
    busy.current = true; setSaving(true); setError(""); setNotice("");
    try {
      const result = await askHistory.discardRecovery(discardReview.id, discardReview.discard_token);
      if (!result.erased || !result.deleted) throw new Error("The node did not confirm discarding recovery. Retry to check the same decision.");
      if (alive.current) {
        setNotice("Recovery was discarded on this node. Approved devices receive the deletion when connected; check My devices for confirmation.");
        await load();
      }
    } catch (cause) {
      if (alive.current) setError(cause instanceof Error ? cause.message : "The node did not confirm discarding recovery. Retry to check the same decision.");
    } finally { busy.current = false; if (alive.current) setSaving(false); }
  };
  if (!loading && !error && !notice && !issues.length) return null;
  return <Panel title="Conversation branches and recovery">
    {error ? <p role="alert">{error}</p> : null}
    {notice ? <p ref={noticeSection} tabIndex={-1} role="status">{notice}</p> : null}
    {loading ? <p role="status">Loading conversation branches…</p> : null}
    {replacement ? <section className={styles.review} ref={replacementReview} tabIndex={-1} aria-label="Keep another recovery copy">
      <h3>The previously kept copy was deleted</h3>
      <p>Keep another independent copy of “{replacement.choice.value.title}”? The deleted conversation stays deleted.
        The new copy keeps the original recipient: {replacement.choice.value.providerPeerId} · {replacement.choice.value.serviceName}. No question will be sent.</p>
      <Button disabled={saving || loading} onClick={() => void restore(replacement.issue, replacement.choice, replacement.deletedId)}>Keep another copy</Button>
      <Button disabled={saving || loading} onClick={() => { setReplacement(null); setError(""); returnFocus.current?.focus(); }}>Cancel keeping another copy</Button>
    </section> : null}
    {discardReview ? <section className={styles.review} ref={discardSection} tabIndex={-1} aria-label="Discard pending recovery">
      <h3>Discard this deleted conversation's recovery?</h3>
      <p>{discardReview.recovery[0]?.value.title ?? discardReview.local_draft?.title ?? discardReview.id}</p>
      <p>This removes all {discardReview.recovery.length} recovery branches{discardReview.local_draft ? " and the unsent draft on this device" : ""} for this conversation.
        Devices that receive the deletion cannot restore this conversation from later messages. Copies already kept as separate conversations remain.</p>
      <p>This does not erase order results, older browser copies or backups, and does not confirm removal on offline or removed devices.</p>
      <Button disabled={saving || loading} onClick={() => void discard()}>Confirm discard recovery</Button>
      <Button disabled={saving || loading} onClick={() => { setDiscardReview(null); setError(""); returnFocus.current?.focus(); }}>Keep recovery for now</Button>
    </section> : null}
    <Button disabled={loading || saving} onClick={() => void load()}>Refresh branches</Button>
    <div className={styles.issues}>{issues.map((issue) => <section key={issue.id} aria-label={`Branches for ${issue.id}`}>
      <h3>{issue.deleted ? "Deleted conversation with pending recovery" : "Conversation changed on different devices"}</h3>
      <p>{issue.deleted ? "The original conversation stays out of your history. You can keep a concurrent branch as a separate conversation." : `Shared history: ${issue.common_messages.length} messages. Each branch retains its own continuation.`}</p>
      {issue.deferred ? <p role="status">This node is still checking the original task. Its outcome has not been confirmed; refresh after it finishes.</p> : null}
      {issue.deleted && issue.discard_token ? <Button disabled={saving || loading || issue.deferred} onClick={() => {
        returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setReplacement(null); setError(""); setNotice(""); setDiscardReview(issue);
      }}>Discard this conversation's recovery</Button> : null}
      {issue.local_draft ? <article>
        <h4>Unsent draft from this device</h4>
        <p>Original recipient: {issue.local_draft.providerPeerId} · {issue.local_draft.serviceName}</p>
        <p>This draft stayed on this device and has not been sent.</p>
        <textarea aria-label="Recovered unsent draft" readOnly value={issue.local_draft.draft ?? ""} rows={4} />
        <Button disabled={saving || loading} onClick={() => void restore(issue, { choice_id: "local-draft", value: issue.local_draft! })}>Keep draft as a separate conversation</Button>
      </article> : null}
      {(issue.deleted ? issue.recovery : issue.branches).map((choice, index) => {
        const key = `${issue.id}-${choice.choice_id}`;
        return <article key={key}>
          <h4>Branch {index + 1}: {choice.value.title}</h4>
          <p>Original recipient: {choice.value.providerPeerId} · {choice.value.serviceName} · {choice.value.networkId}</p>
          <small>{choice.value.messages.length} messages · {choice.value.serviceKey}</small>
          <button type="button" className="btn btn-standard" aria-expanded={expanded === key} aria-controls={`branch-${key}`} onClick={() => setExpanded(expanded === key ? "" : key)}>Read branch {index + 1}</button>
          {expanded === key ? <div id={`branch-${key}`} className={styles.transcript} tabIndex={0} aria-label={`Branch ${index + 1} messages`}>
            {choice.value.messages.map((message) => <article key={message.id}><strong>{message.role === "user" ? "You" : "Ryn"}</strong><p>{message.content}</p><small>{message.status}</small></article>)}
            {!choice.value.messages.length ? <p>No completed messages in this branch.</p> : null}
          </div> : null}
          <p>Keeping a branch saves a separate history with the original service. It does not send a question. Referenced articles may need to be saved on this device.</p>
          <Button disabled={saving || loading} onClick={() => void restore(issue, choice)}>Keep branch {index + 1} as a separate conversation</Button>
        </article>;
      })}
    </section>)}</div>
  </Panel>;
}
