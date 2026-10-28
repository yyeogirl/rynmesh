import { useCallback, useEffect, useRef, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button } from "../../components/ui";
import styles from "./ConversationCleanup.module.css";

export interface CleanupReview { review_token: string }
export interface CleanupJob { id: string; sequence: number; done: string[]; pending: string[]; cancelled: boolean; local_copies_complete: boolean; remote_confirmed: boolean }
interface BackupReview { review_token: string; files: number; bytes: number }
export interface CleanupApi<R extends CleanupReview> {
  status: () => Promise<CleanupJob | null>; preview: () => Promise<R>;
  begin: (token: string) => Promise<CleanupJob>; resume: (id: string) => Promise<CleanupJob>;
  cancel?: (id: string) => Promise<CleanupJob>;
  reviewBackups: (id: string) => Promise<BackupReview>;
  approveBackups: (id: string, token: string) => Promise<CleanupJob>;
}
export interface CleanupConfig<R extends CleanupReview> {
  noun: string; title: string; scope: string; result: string; backupScope: string;
  labels: Record<string, string>; counts: (review: R) => string; invalidReview: (cause: unknown) => boolean;
}

export default function ReviewedCleanupPanel<R extends CleanupReview>({ api, config, onChange }: {
  api: CleanupApi<R>; config: CleanupConfig<R>; onChange?: () => void;
}) {
  const { noun, title, scope, labels } = config;
  const caption = noun[0].toUpperCase() + noun.slice(1);
  const titleId = noun.replaceAll(' ', '-') + '-cleanup-title';
  const { confirm } = useAppContext();
  const [job, setJob] = useState<CleanupJob | null>(null);
  const [review, setReview] = useState<R | null>(null);
  const [attempt, setAttempt] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  const running = useRef(false);
  const statusRef = useRef<HTMLParagraphElement>(null);
  const reviewRef = useRef<HTMLDivElement>(null);
  const refresh = useCallback(async () => {
    const current = await api.status();
    if (mounted.current) {
      setJob(current); setLoaded(true);
      setAttempt((previous) => current?.id === previous ? null : previous);
      if (current?.done.includes('source')) onChange?.();
    }
  }, [onChange, api]);
  const run = async (operation: () => Promise<void>) => {
    if (running.current) return;
    running.current = true;
    setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : `${caption} cleanup is unfinished. Refresh progress and retry.`);
        if (config.invalidReview(cause)) {
          setReview(null); setAttempt(null);
        }
      }
      try { await refresh(); } catch { /* Retain the last confirmed progress. */ }
    } finally { running.current = false; if (mounted.current) setBusy(false); }
  };
  useEffect(() => {
    mounted.current = true;
    void run(refresh);
    return () => { mounted.current = false; };
  }, [refresh]);
  useEffect(() => { if (error || notice) statusRef.current?.focus(); }, [error, notice]);
  useEffect(() => { if (review && !busy && !error) reviewRef.current?.focus(); }, [review, busy, error]);
  const accept = (value: CleanupJob) => {
    if (!mounted.current) return;
    setJob(value); setAttempt(null); setReview(null);
    if (value.done.includes('source')) onChange?.();
    setNotice(value.cancelled ? `Uncommitted ${noun} cleanup cancelled.`
      : value.local_copies_complete ? config.result
        : `${caption} cleanup is unfinished. Continue the remaining steps.`);
  };
  const begin = (value: R) => confirm({
    title: `Clear reviewed ${noun} data?`, risk: "high", confirmLabel: `Clear reviewed ${noun} copies`,
    body: `${config.counts(value)} ${scope}`,
    onConfirm: () => run(async () => { setAttempt(value.review_token); await api.begin(value.review_token).then(accept); }),
  });
  const reviewBackups = async (current: CleanupJob) => {
    let backup: BackupReview | undefined;
    await run(async () => { backup = await api.reviewBackups(current.id); });
    if (!backup || !mounted.current) return;
    const value = backup;
    confirm({ title: `Clear the remaining ${noun} backups?`, risk: "high", confirmLabel: `Clear reviewed ${noun} backups`,
      body: `Files: ${value.files}. Size: ${(value.bytes / 1024).toFixed(1)} KiB. These versions may differ from the original review. ${config.backupScope}`,
      onConfirm: () => run(async () => { await api.approveBackups(current.id, value.review_token).then(accept); }),
    });
  };
  const pending = job && !job.cancelled && !job.local_copies_complete;
  return <section className={styles.panel} aria-labelledby={titleId}>
    <h3 id={titleId}>{title}</h3><p>{scope}</p>
    <p ref={statusRef} tabIndex={-1} role={error ? "alert" : "status"}>{error || notice}</p>
    <div className="button-row">
      <Button disabled={busy} onClick={() => void run(refresh)}>Refresh {noun} cleanup</Button>
      <Button disabled={busy || !loaded || Boolean(pending) || Boolean(attempt)} onClick={() => void run(async () => { const value = await api.preview(); if (mounted.current) setReview(value); })}>Review {noun} data</Button>
    </div>
    {!loaded ? <p>Inspecting {noun} cleanup…</p> : null}
    {review && !pending && !attempt ? <div className={styles.review} ref={reviewRef} tabIndex={-1} aria-label={`Reviewed ${noun} scope`}>
      <p>{config.counts(review)}</p>
      <Button disabled={busy} variant="danger" onClick={() => begin(review)}>Clear reviewed {noun} copies</Button>
    </div> : null}
    {attempt ? <Button disabled={busy} onClick={() => void run(async () => { await api.begin(attempt).then(accept); })}>Retry original {noun} cleanup</Button> : null}
    {job ? <div>
      <h4>Latest {noun} cleanup {job.sequence}</h4>
      <p>{job.cancelled ? "Cancelled before source cleanup" : job.local_copies_complete ? `Reviewed local ${noun} copies cleared` : `${caption} cleanup unfinished`}</p>
      {job.done.length ? <p>Completed: {job.done.map((step) => labels[step]).join(", ")}</p> : null}
      {job.pending.length ? <p>Remaining: {job.pending.map((step) => labels[step]).join(", ")}</p> : null}
      {pending ? <div className="button-row">
        <Button disabled={busy} onClick={() => void run(async () => { await api.resume(job.id).then(accept); })}>Continue {noun} cleanup</Button>
        {!job.done.length && api.cancel ? <Button disabled={busy} onClick={() => void run(async () => { await api.cancel!(job.id).then(accept); })}>Cancel uncommitted {noun} cleanup</Button> : null}
        {job.pending[0] === "backups" ? <Button disabled={busy} onClick={() => void reviewBackups(job)}>Review remaining {noun} backups</Button> : null}
      </div> : null}
    </div> : loaded ? <p>No previous {noun} cleanup.</p> : null}
  </section>;
}
