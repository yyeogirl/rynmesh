import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/ui";
import { friendsApi } from "../../domain/friendsClient";

type Review = Awaited<ReturnType<typeof friendsApi.reviewCardCleanup>>;

export default function FriendCardCleanupPanel({ onChanged }: { onChanged: () => void }) {
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const feedback = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLSpanElement>(null);
  useEffect(() => { if (!busy && (review || error || notice)) feedback.current?.focus(); }, [review, busy, error, notice]);
  const inspect = async () => {
    setBusy(true); setError(""); setNotice("");
    try { setReview(await friendsApi.reviewCardCleanup()); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not review card history. Retry."); }
    finally { setBusy(false); }
  };
  const clear = async () => {
    if (!review) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await friendsApi.clearCards(review.review_token);
      if (!result.complete) throw new Error("Card cleanup is unfinished. Retry the reviewed operation.");
      setReview(null);
      setNotice(`${result.cards} reviewed card entries cleared locally. Other devices are not confirmed cleared.`);
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Card cleanup was not confirmed. Retry the same operation.");
      onChanged();
    } finally { setBusy(false); }
  };
  return <section aria-label="Card history maintenance">
    <h3>Clear card history</h3>
    <p>This clears reviewed sharing-card metadata and matching entries in known legacy files. Saved documents, reading records, messages, friendships and AI permissions remain. Use their own data controls to manage them. Deliveries already in flight or already received by someone else cannot be recalled.</p>
    <p>Only card identifiers and bounded cleanup receipts are retained to prevent old deliveries or retries from restoring cleared cards. Up to 10,000 deletion identifiers are retained; they are excluded from ordinary exports.</p>
    <span ref={trigger}><Button disabled={busy} onClick={() => void inspect()}>Review card history cleanup</Button></span>
    {review || error || notice ? <div ref={feedback} tabIndex={-1} role={error ? "alert" : notice ? "status" : "region"} aria-label="Card cleanup review and result">
      {error ? <p>{error}</p> : null}
      {notice ? <p>{notice}</p> : null}
      {review ? <>
        <p>{review.cards} card entries · {review.legacy_files} legacy files. {review.resuming ? "Continue the unfinished cleanup; cards created afterwards are kept." : "Review this local scope before confirming."}</p>
        <Button disabled={busy || review.cards === 0} variant="danger" onClick={() => void clear()}>{error ? "Retry reviewed card cleanup" : "Clear reviewed card history"}</Button>
        <Button disabled={busy} onClick={() => { setReview(null); setError(""); trigger.current?.querySelector("button")?.focus(); }}>Cancel review</Button>
      </> : null}
    </div> : null}
  </section>;
}
