import { useCallback, useEffect, useRef, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button } from "../../components/ui";
import { CleanupError, conversationCleanup, type CleanupJob, type CleanupReview, type CleanupStep } from "../../domain/conversationCleanup";
import styles from "./ConversationCleanup.module.css";

const labels: Record<CleanupStep, string> = { source: "Conversation history", replica: "Local sync copies", backups: "Reviewed backups", search: "Search index", orders: "Saved AI results" };
const scope = "Includes conversation drafts, conflict recovery, separately restored conversations, reviewed migration backups and interrupted-write copies, search entries and saved AI results. Backup copies can also contain other sync or search metadata; current reading data remains. Task identity and settlement records remain to prevent duplicate requests. Browser copies and remote devices need separate confirmation.";

export default function ConversationCleanupPanel() {
  const { confirm } = useAppContext();
  const [jobs, setJobs] = useState<CleanupJob[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [review, setReview] = useState<CleanupReview | null>(null);
  const [attempt, setAttempt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const statusRef = useRef<HTMLParagraphElement>(null);
  const reviewRef = useRef<HTMLDivElement>(null);
  const reviewButton = useRef<HTMLSpanElement>(null);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    const rows = await conversationCleanup.jobs();
    if (mounted.current) { setJobs(rows); setLoaded(true); }
  }, []);
  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : "Cleanup did not complete. Refresh its progress before retrying.");
        if (cause instanceof CleanupError && cause.code === "ask_privacy_review_changed") setReview(null);
      }
      try { await refresh(); } catch { /* Keep the confirmed snapshot and original error. */ }
    } finally { if (mounted.current) setBusy(false); }
  };
  useEffect(() => {
    mounted.current = true;
    void run(refresh);
    return () => { mounted.current = false; };
  }, [refresh]);
  useEffect(() => { if (notice || error) statusRef.current?.focus(); }, [notice, error]);
  useEffect(() => { if (review && !busy && !error && !notice) reviewRef.current?.focus(); }, [review, busy, error, notice]);

  const accept = async (job: CleanupJob) => {
    if (!mounted.current) return;
    setJobs((rows) => [job, ...rows.filter((row) => row.id !== job.id)]
      .sort((a, b) => (b.sequence ?? 0) - (a.sequence ?? 0)).slice(0, 32));
    setAttempt(null); setReview(null);
    setNotice(job.cancelled ? "Uncommitted cleanup cancelled. Your conversations were not cleared by this operation."
      : job.local_copies_complete ? "Reviewed node copies cleared. Browser cleanup is tracked separately below; remote devices remain unconfirmed."
        : "Cleanup is unfinished. Continue its remaining steps.");
  };
  const begin = (value: CleanupReview) => confirm({
    title: "Clear reviewed conversation data?",
    body: `Conversations: ${value.conversations}. Recovery branches: ${value.recovery_items}. Backup files: ${value.backup_files}. Task results: ${value.order_results}. ${scope}`,
    risk: "high", confirmLabel: "Clear reviewed node copies",
    onConfirm: () => run(async () => { setAttempt(value.review_token); await accept(await conversationCleanup.begin(value.review_token)); }),
  });
  const reviewBackups = (job: CleanupJob) => void run(async () => {
    const value = await conversationCleanup.reviewBackups(job.id);
    confirm({ title: "Clear the current remaining backups?",
      body: `Files: ${value.files}. Size: ${(value.bytes / 1024).toFixed(1)} KiB. These versions may differ from your original review. Only these remaining backup copies will be cleared; newly created conversations remain.`,
      risk: "high", confirmLabel: "Clear reviewed backups",
      onConfirm: () => run(async () => { await accept(await conversationCleanup.approveBackups(job.id, value.review_token)); }),
    });
  });
  const pending = jobs.some((row) => !row.cancelled && !row.local_copies_complete);
  return <section className={styles.panel} aria-labelledby="conversation-cleanup-title">
    <h3 id="conversation-cleanup-title">Conversation data</h3>
    <p>{scope}</p>
    <p ref={statusRef} tabIndex={-1} role={error ? "alert" : "status"}>{error || notice}</p>
    <div className="button-row">
      <Button disabled={busy} onClick={() => void run(refresh)}>Refresh cleanup progress</Button>
      <span ref={reviewButton}><Button disabled={busy || !loaded || pending} onClick={() => void run(async () => { const next = await conversationCleanup.preview(); if (mounted.current) setReview(next); })}>Review conversation data</Button></span>
    </div>
    {!loaded ? <p>Inspecting cleanup progress…</p> : null}
    {review ? <div className={styles.review} ref={reviewRef} tabIndex={-1} aria-label="Reviewed conversation scope">
      <p>Conversations: {review.conversations} · Recovery branches: {review.recovery_items} · Backup files: {review.backup_files} · Task results: {review.order_results}</p>
      {review.has_unassigned_draft ? <p>Your unsent Ask Ryn draft is included.</p> : null}
      {review.active_tasks ? <p role="alert">{review.active_tasks} original AI tasks are still being checked. Wait for their outcome, then review again.</p> : null}
      <Button variant="danger" disabled={busy || Boolean(review.active_tasks)} onClick={() => begin(review)}>Clear reviewed node copies</Button>
      <Button disabled={busy} onClick={() => { setReview(null); reviewButton.current?.querySelector('button')?.focus(); }}>Discard this review</Button>
    </div> : null}
    {attempt && !jobs.some((row) => row.id === attempt) ? <div>
      <p>The last request has not been confirmed. Retry its original identity to check whether it started.</p>
      <Button disabled={busy} onClick={() => void run(async () => { await accept(await conversationCleanup.begin(attempt)); })}>Retry same cleanup request</Button>
    </div> : null}
    {jobs.length ? <p>The most recent 32 cleanup records are kept here. Older completed or cancelled entries leave this list; deletion markers remain.</p> : null}
    {jobs.length ? <ol className={styles.jobs}>{jobs.map((job, index) => <li key={job.id}>
      <h4>Cleanup {job.sequence ?? jobs.length - index}</h4>
      <p>{job.cancelled ? "Cancelled before erasure" : job.local_copies_complete ? "Reviewed node copies cleared" : "Unfinished"}</p>
      {job.done.length ? <p>Completed: {job.done.map((step) => labels[step]).join(", ")}</p> : null}
      {job.pending.length ? <p>Remaining: {job.pending.map((step) => labels[step]).join(", ")}</p> : null}
      {!job.cancelled ? <p>Browser cleanup is tracked separately below. Remote devices remain unconfirmed.</p> : null}
      {job.pending.length ? <div className="button-row">
        <Button disabled={busy} onClick={() => void run(async () => { await accept(await conversationCleanup.resume(job.id)); })}>Continue cleanup {job.sequence ?? jobs.length - index}</Button>
        {!job.done.length ? <Button disabled={busy} onClick={() => void run(async () => { await accept(await conversationCleanup.cancel(job.id)); })}>Cancel uncommitted cleanup {job.sequence ?? jobs.length - index}</Button> : null}
        {job.pending[0] === "backups" ? <Button disabled={busy} onClick={() => reviewBackups(job)}>Review remaining backups</Button> : null}
      </div> : null}
    </li>)}</ol> : loaded ? <p>No previous conversation cleanup.</p> : null}
  </section>;
}
