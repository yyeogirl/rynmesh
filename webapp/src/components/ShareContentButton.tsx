import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { FriendRecord } from "../domain/friendTypes";
import { friendsApi } from "../domain/friendsClient";
import { Button } from "./ui";

export default function ShareContentButton({ itemId, title, offlineJobId }: { itemId: string; title: string; offlineJobId?: string }) {
  const [open, setOpen] = useState(false);
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [peer, setPeer] = useState("");
  const [attempt, setAttempt] = useState<{ peer_id: string; item_id: string; card_id: string; offline_job_id?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const busyRef = useRef(busy); busyRef.current = busy;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!busyRef.current) setOpen(false); }
      if (event.key !== "Tab") return;
      const choices = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), select:not(:disabled), a[href]') ?? []);
      const index = choices.indexOf(document.activeElement as HTMLElement);
      if (choices.length && (index < 0 || (!event.shiftKey && index === choices.length - 1) || (event.shiftKey && index === 0))) {
        event.preventDefault(); choices[event.shiftKey ? choices.length - 1 : 0].focus();
      }
    };
    document.addEventListener("keydown", key, true);
    return () => { document.removeEventListener("keydown", key, true); previous?.focus(); };
  }, [open]);
  const load = async () => {
    setBusy(true); setError("");
    try { setFriends((await friendsApi.list()).friends.filter((friend) => friend.status === "active")); }
    catch { setError("Could not load friends. Retry to choose a recipient."); }
    finally { setBusy(false); }
  };
  const send = async () => {
    const request = attempt ?? { peer_id: peer, item_id: itemId, card_id: crypto.randomUUID().replaceAll("-", ""), ...(offlineJobId ? { offline_job_id: offlineJobId } : {}) };
    setAttempt(request); setBusy(true); setError("");
    try {
      const card = await friendsApi.share(request);
      setResult(card.delivery_state === "delivered" ? "Card received by your friend. They choose whether to download the document."
        : card.delivery_state === "mailbox" ? "Card is in the encrypted mailbox, waiting for confirmation."
        : card.delivery_state === "expired" ? "This card expired before confirmed delivery."
        : card.delivery_state === "failed" ? "Card could not be delivered. Retry from Friends."
        : "Card is waiting for a connection. Check its status in Friends.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Share could not be confirmed. Retry this attempt."); }
    finally { setBusy(false); }
  };
  return <>
    <Button onClick={() => { setOpen(true); setPeer(""); setAttempt(null); setResult(""); void load(); }}>Share with a friend</Button>
    {open ? <div className="modal-backdrop" role="presentation"><div ref={dialogRef} tabIndex={-1} className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="share-content-title">
      <h2 id="share-content-title">Share {title}</h2>
      <p>Your friend receives the title, source and summary first. They can choose to download a private copy of the document. Copies they save cannot be recalled.</p>
      {error ? <p role="alert">{error}</p> : null}
      {result ? <p role="status">{result}</p> : <>
        <label>Recipient<select aria-label="Share recipient" value={peer} disabled={busy || !!attempt} onChange={(event) => setPeer(event.target.value)}>
          <option value="">Choose a friend</option>{friends.map((friend) => <option key={friend.relationship_id} value={friend.peer_id}>{friend.node_name}</option>)}
        </select></label>
        {!friends.length && !busy ? <p>Add a friend in <Link to="/friends">Friends</Link> to share this document.</p> : null}
        {!attempt ? <Button disabled={busy} onClick={() => void load()}>Refresh recipients</Button> : null}
        <Button disabled={busy || (!peer && !attempt)} onClick={() => void send()}>{attempt ? "Retry this share" : "Send content card"}</Button>
      </>}
      <Button disabled={busy} onClick={() => setOpen(false)}>Close sharing</Button>
    </div></div> : null}
  </>;
}
