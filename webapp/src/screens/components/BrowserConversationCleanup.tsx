import { useEffect, useRef, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button } from "../../components/ui";
import { BrowserErasureError, eraseReviewedBrowserConversations, reviewBrowserConversations, type BrowserErasureReview } from "../../domain/llmConversationStore";
import styles from "./ConversationCleanup.module.css";

export default function BrowserConversationCleanup() {
  const { confirm } = useAppContext();
  const [review, setReview] = useState<BrowserErasureReview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const status = useRef<HTMLParagraphElement>(null);
  const reviewRef = useRef<HTMLDivElement>(null);
  const reviewButton = useRef<HTMLSpanElement>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (error || notice) status.current?.focus(); }, [error, notice]);
  useEffect(() => { if (review && !busy && !error && !notice) reviewRef.current?.focus(); }, [review, busy, error, notice]);
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await action(); }
    catch (cause) {
      if (mounted.current) setError(cause instanceof BrowserErasureError ? cause.message : "Browser storage did not confirm cleanup. Close other Ryn tabs if needed, then review and retry. Your node and other browser profiles were not cleared by this operation.");
    } finally { if (mounted.current) setBusy(false); }
  };
  const clear = (value: BrowserErasureReview) => confirm({
    title: "Clear reviewed browser conversation copies?",
    body: `Older browser copies: ${value.copies}. Kept only in this tab: ${value.memoryCopies}. This clears reviewed encrypted records even if they cannot be read, and prevents them being saved back under the same identities. New conversations created after review require a new review. Node history, other tabs' open views, other browser profiles and remote devices are separate.`,
    risk: "high", confirmLabel: "Clear reviewed browser copies",
    onConfirm: () => run(async () => {
      const result = await eraseReviewedBrowserConversations(value.token);
      if (mounted.current) { setReview(null); setNotice(`${result.removed} reviewed browser ${result.removed === 1 ? "copy" : "copies"} cleared. Other tabs' open views, other browser profiles and remote devices have not been confirmed cleared.`); }
    }),
  });
  return <section className={styles.panel} aria-labelledby="browser-conversation-cleanup-title">
    <h3 id="browser-conversation-cleanup-title">Older browser conversation copies</h3>
    <p>Earlier versions kept encrypted conversations in this browser. Importing them into your node kept these originals as recovery copies.</p>
    <p ref={status} tabIndex={-1} role={error ? "alert" : "status"}>{error || notice}</p>
    <span ref={reviewButton}><Button disabled={busy} onClick={() => void run(async () => { const value = await reviewBrowserConversations(); if (mounted.current) setReview(value); })}>Review browser copies</Button></span>
    {review ? <div className={styles.review} ref={reviewRef} tabIndex={-1} aria-label="Reviewed browser scope">
      <p>Copies: {review.copies} · Kept only in this tab: {review.memoryCopies}</p>
      <Button variant="danger" disabled={busy || !review.copies} onClick={() => clear(review)}>Clear reviewed browser copies</Button>
      <Button disabled={busy} onClick={() => { setReview(null); reviewButton.current?.querySelector('button')?.focus(); }}>Discard browser review</Button>
    </div> : null}
  </section>;
}
