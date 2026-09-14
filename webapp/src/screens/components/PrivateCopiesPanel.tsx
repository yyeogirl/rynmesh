import { useEffect, useRef, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button } from "../../components/ui";
import { friendsApi } from "../../domain/friendsClient";
import { LibraryCleanupError, libraryCleanup, libraryCleanupScope, libraryReviewCounts, type LibraryCleanupJob, type LibraryReview } from "../../domain/libraryCleanup";

type Document = Awaited<ReturnType<typeof friendsApi.documents>>["documents"][number];
export default function PrivateCopiesPanel() {
  const { confirm } = useAppContext();
  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [job, setJob] = useState<LibraryCleanupJob | null>(null);
  const [attempt, setAttempt] = useState<LibraryReview | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const running = useRef(false), mounted = useRef(true);
  const status = useRef<HTMLParagraphElement>(null);
  const refresh = async () => {
    const current = await libraryCleanup.status();
    if (mounted.current) {
      setJob(current); setLoaded(true);
      setAttempt((previous) => previous?.review_token === current?.id ? null : previous);
    }
    const result = await friendsApi.documents();
    if (mounted.current) setDocuments(result.documents);
  };
  const run = async (operation: () => Promise<void>) => {
    if (running.current) return;
    running.current = true; setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : "Document cleanup is unfinished. Refresh and retry.");
        if (cause instanceof LibraryCleanupError && ['library_cleanup_review_changed', 'library_cleanup_pending'].includes(cause.code)) setAttempt(null);
      }
      try { await refresh(); } catch { /* Keep confirmed progress and original request. */ }
    } finally { running.current = false; if (mounted.current) setBusy(false); }
  };
  useEffect(() => { mounted.current = true; void run(refresh); return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (error || notice) status.current?.focus(); }, [error, notice]);
  const accept = async (result: LibraryCleanupJob) => {
    if (!mounted.current) return;
    setJob(result); setAttempt(null);
    setNotice(result.local_copies_complete ? result.removed + " reviewed document copies cleared on this node. Other copies are unconfirmed." : "Document cleanup is unfinished. Continue remaining files.");
    await refresh();
  };
  const remove = async (document?: Document) => {
    let review: LibraryReview | undefined;
    await run(async () => { review = await libraryCleanup.preview(document?.import_id ?? null); });
    if (!review || !mounted.current) return;
    const selected = review;
    confirm({ title: document ? "Remove " + document.filename + "?" : "Clear reviewed document copies?",
      body: libraryReviewCounts(selected) + " " + libraryCleanupScope, risk: "high", confirmLabel: "Clear reviewed document copies",
      onConfirm: () => run(async () => { setAttempt(selected); await accept(await libraryCleanup.begin(selected)); }),
    });
  };
  const reviewFiles = async (current: LibraryCleanupJob) => {
    let review: Awaited<ReturnType<typeof libraryCleanup.reviewFiles>> | undefined;
    await run(async () => { review = await libraryCleanup.reviewFiles(current.id); });
    if (!review || !mounted.current) return;
    const selected = review;
    confirm({ title: "Clear remaining document files?", risk: "high", confirmLabel: "Clear reviewed remaining files",
      body: selected.files + " files, " + (selected.bytes / 1024).toFixed(1) + " KiB. These managed files may differ from the original review. Only the selected document directories are included. Later saved documents elsewhere remain.",
      onConfirm: () => run(async () => { await accept(await libraryCleanup.approveFiles(current.id, selected.review_token)); }),
    });
  };
  const pending = Boolean(job && !job.local_copies_complete);
  const cannotReview = busy || !loaded || pending || Boolean(attempt);
  return <section aria-labelledby="private-copies-title">
    <h3 id="private-copies-title">Private document copies</h3><p>{libraryCleanupScope}</p>
    <p ref={status} tabIndex={-1} role={error ? "alert" : "status"}>{error || notice}</p>
    <Button disabled={busy} onClick={() => void run(refresh)}>Refresh document copies</Button>
    <Button disabled={cannotReview} onClick={() => void remove()}>Review all document copies</Button>
    {attempt ? <Button disabled={busy} onClick={() => void run(async () => { await accept(await libraryCleanup.begin(attempt)); })}>Retry original document cleanup</Button> : null}
    {job ? <div><h4>Latest document cleanup {job.sequence}</h4>
      <p>{job.local_copies_complete ? "Reviewed local document files cleared" : "Document cleanup unfinished — selected copies are unavailable"}</p>
      {pending ? <><Button disabled={busy} onClick={() => void run(async () => { await accept(await libraryCleanup.resume(job.id)); })}>Continue document cleanup</Button>
        <Button disabled={busy} onClick={() => void reviewFiles(job)}>Review remaining document files</Button></> : null}
    </div> : null}
    {documents === null ? <p>Inspecting document copies…</p> : <>
      <p>{documents.length} copies · {(documents.reduce((sum, row) => sum + (row.size_bytes ?? 0), 0) / (1024 * 1024)).toFixed(1)} MiB of source documents</p>
      <ul>{documents.map((document) => <li key={document.import_id}>
        {document.filename} · {document.state === "ready" ? (document.size_bytes ?? 0) + " bytes" : "Unavailable"}
        <Button disabled={cannotReview} onClick={() => void remove(document)}>Remove {document.filename}</Button>
      </li>)}</ul>
    </>}
  </section>;
}
