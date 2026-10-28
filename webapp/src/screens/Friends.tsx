import QRCode from "qrcode";
import { useSearchParams } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, PageHeader, Panel } from "../components/ui";
import type { FriendInvitePreview, FriendInviteResult, FriendRecord } from "../domain/friendTypes";
import { extractInvite, friendsApi, invitationText } from "../domain/friendsClient";
import FriendConversation from "./components/FriendConversation";
import FriendCards from "./components/FriendCards";
import FriendAI from "./components/FriendAI";
import styles from "./Friends.module.css";

export default function Friends() {
  const [params] = useSearchParams();
  const { confirm } = useAppContext();
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [invite, setInvite] = useState<FriendInviteResult | null>(null);
  const [qr, setQr] = useState("");
  const [paste, setPaste] = useState("");
  const [review, setReview] = useState<{ uri: string; preview: FriendInvitePreview } | null>(null);
  const revision = useRef(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const load = useCallback(async () => {
    const [result, invitations] = await Promise.all([friendsApi.list(), friendsApi.invites()]);
    setFriends(result.friends); setLoaded(true); setLoadError("");
    setInvite((current) => {
      const stored = invitations.invites.find((row) => row.invite_id === current?.invite.invite_id);
      return stored && (stored.status !== "active" || Date.parse(stored.expires_at) <= Date.now()) ? null : current;
    });
  }, []);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not confirm this operation. Retry."); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    let active = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await load(); }
      catch { if (active) setLoadError("Could not load friends. Retry to restore your list."); }
      finally { running = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [load]);
  useEffect(() => {
    let active = true;
    setQr("");
    if (invite) void QRCode.toDataURL(invite.invite_uri, { width: 512, margin: 4 })
      .then((value) => { if (active) setQr(value); })
      .catch(() => { if (active) setNotice("QR code unavailable. Copy the invitation instead."); });
    return () => { active = false; };
  }, [invite]);
  const activeFriends = friends.filter((friend) => friend.status === "active");
  const conversation = activeFriends.find((friend) => friend.peer_id === selected);
  useEffect(() => { setSelected(params.get("peer")); }, [params]);

  return <div className="screen-stack">
    <PageHeader eyebrow="Private sharing" title="Friends" context="Invite someone you know, review their identity, then share your first message." />
    {error || loadError ? <div role="alert">{error || loadError} <Button disabled={busy} onClick={() => void act(load)}>Refresh friends</Button></div> : null}
    {notice ? <p role="status">{notice}</p> : null}
    <div className={styles.heroGrid}>
      <Panel className={styles.actionCard}>
        <h2>Invite a friend</h2><p>One use, valid for 15 minutes. Allows messages, small attachments and content cards. AI access stays off.</p>
        {!invite ? <Button disabled={busy} onClick={() => void act(async () => setInvite(await friendsApi.createInvite()))}>Create invite</Button> : <div className={styles.inviteBox}>
          {qr ? <img className={styles.qr} src={qr} alt="Friend invitation QR code" /> : null}
          <div><p>Expires {new Date(invite.invite.expires_at).toLocaleString()}</p><p>Reachable address: {invite.invite.endpoint} · {invite.invite.address_category}</p>
            <textarea aria-label="Invitation and installation instructions" className={styles.paste} readOnly value={invitationText(invite)} />
            <div className={styles.choiceRow}>
              <Button disabled={busy} onClick={() => void act(async () => { await navigator.clipboard.writeText(invitationText(invite)); setNotice("Invitation and installation instructions copied."); })}>Copy invitation</Button>
              <Button disabled={busy} onClick={() => void act(async () => { await friendsApi.cancel(invite.invite.invite_id); setInvite(null); setNotice("Invitation cancelled."); })}>Cancel invite</Button>
            </div></div>
        </div>}
      </Panel>
      <Panel className={styles.actionCard}>
        <h2>Use an invite</h2>
        <textarea className={styles.paste} aria-label="Friend invite" value={paste} onChange={(event) => { revision.current += 1; setPaste(event.target.value); setReview(null); }} placeholder="Paste the invitation here" />
        {!review ? <Button disabled={busy || !paste.trim()} onClick={() => void act(async () => {
          const version = revision.current; const uri = extractInvite(paste);
          const preview = await friendsApi.inspect(uri);
          if (version === revision.current) setReview({ uri, preview });
        })}>Review invite</Button> : <div className={styles.review}>
          <strong>Signature checked locally. Your friend has not been contacted.</strong>
          <dl><dt>Device</dt><dd>{review.preview.node_name}</dd><dt>Identity</dt><dd>{review.preview.peer_id}</dd><dt>Address</dt><dd>{review.preview.endpoint} · {review.preview.address_category}</dd><dt>Expires</dt><dd>{new Date(review.preview.expires_at).toLocaleString()}</dd><dt>Allows</dt><dd>Messages, attachments up to 5 MiB, content cards</dd><dt>Stays off</dt><dd>AI, VPN, access to your files and credits</dd></dl>
          <Button disabled={busy} onClick={() => void act(async () => {
            const version = revision.current;
            const friend = await friendsApi.join(review.uri);
            if (version === revision.current) { setPaste(""); setReview(null); }
            setSelected(friend.peer_id); setNotice("Friend added. Send your first message below."); await load();
          })}>Add this friend</Button>
        </div>}
      </Panel>
    </div>
    <Panel><h2>Your friends</h2>
      <Button disabled={busy} onClick={() => void act(load)}>Refresh friends</Button>
      {!loaded ? <p>Loading friends…</p> : activeFriends.length === 0 ? <p>No friends yet. Create or paste an invite above.</p> : activeFriends.map((friend) => <div className={styles.friendCard} key={friend.relationship_id}>
        <div><h3>{friend.node_name}</h3><span className={styles.meta}>{friend.endpoint}</span></div>
        <div className={styles.choiceRow}><Button onClick={() => setSelected(friend.peer_id)}>Message {friend.node_name}</Button>
          <Button disabled={busy} onClick={() => confirm({ title: `Remove ${friend.node_name}?`, body: "New messages and private content requests are blocked immediately on this device. The other device may receive the notice later. Copies they already saved cannot be recalled.", risk: "high", confirmLabel: "Remove friend", onConfirm: () => act(async () => { await friendsApi.revoke(friend.relationship_id); await load(); }) })}>Remove</Button></div>
      </div>)}
      {friends.filter((friend) => friend.status === "revoked" && friend.revocation_delivery === "pending").map((friend) => <div className={styles.friendCard} key={friend.relationship_id}><p>{friend.node_name}: removed locally; waiting to notify their device.</p><Button disabled={busy} onClick={() => void act(async () => { await friendsApi.retryRevocation(friend.relationship_id); await load(); })}>Retry removal notice</Button></div>)}
    </Panel>
    {conversation ? <FriendConversation key={conversation.relationship_id} friend={conversation} focusMessage={params.get("message")} /> : params.get("peer") && loaded ? <p role="alert">This friend is unavailable or access was removed.</p> : null}
      <FriendCards friends={friends} focusCard={params.get("card")} />
    <FriendAI friends={friends} />
  </div>;
}
