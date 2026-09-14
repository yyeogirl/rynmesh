import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Panel } from "../../components/ui";
import type { FriendMessage, FriendRecord } from "../../domain/friendTypes";
import { friendDeliveryExplanation, friendsApi } from "../../domain/friendsClient";

const MAX_FILE = 5 * 1024 * 1024;
const stateLabels = {
  queued: "Waiting to send", sending: "Sending", mailbox: "In encrypted mailbox · waiting for confirmation",
  delivered: "Delivered · confirmed by your friend", failed: "Could not confirm delivery · retry available", expired: "Expired · delivery unconfirmed",
};
type SendBody = Parameters<typeof friendsApi.send>[1];

async function encodeAttachment(file: File): Promise<NonNullable<SendBody["attachment"]>> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return { filename: file.name, mime: file.type || "application/octet-stream", data_base64: btoa(binary) };
}

export default function FriendConversation({ friend, focusMessage }: { friend: FriendRecord; focusMessage?: string | null }) {
  const [messages, setMessages] = useState<FriendMessage[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [pending, setPending] = useState<SendBody | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  useEffect(() => {
    if (!loaded || !focusMessage) return;
    const element = document.getElementById(`friend-message-${focusMessage}`);
    element?.focus({ preventScroll: true }); element?.scrollIntoView?.({ block: "center" });
  }, [loaded, focusMessage]);
  const refresh = useCallback(async () => {
    const result = await friendsApi.history(friend.peer_id);
    if (alive.current) { setMessages(result.messages); setLoaded(true); setLoadError(""); }
  }, [friend.peer_id]);
  useEffect(() => {
    alive.current = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await refresh(); }
      catch { if (alive.current) setLoadError("Could not refresh messages. Retry when the local node is available."); }
      finally { running = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { alive.current = false; window.clearInterval(timer); };
  }, [refresh]);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await operation(); }
    catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : "Could not confirm the operation. Retry."); }
    finally { if (alive.current) setBusy(false); }
  };
  const send = async () => {
    const request = pending ?? { message_id: crypto.randomUUID().replaceAll("-", ""), text,
      ...(file ? { attachment: await encodeAttachment(file) } : {}) };
    setPending(request);
    await friendsApi.send(friend.peer_id, request);
    if (!alive.current) return;
    setPending(null); setText(""); setFile(null);
    if (fileInput.current) fileInput.current.value = "";
    await refresh();
  };
  const download = async (message: FriendMessage) => {
    const response = await fetch(friendsApi.attachmentUrl(friend.peer_id, message.msg_id), { credentials: "include" });
    if (!response.ok) throw new Error("Could not download this attachment. Access may have been removed.");
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = message.attachment?.filename ?? "attachment";
    anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 10000);
  };
  return <Panel>
    <h2>Messages with {friend.node_name}</h2>
    <p>Attachments: up to 5 MiB for direct delivery. The mailbox accepts envelopes up to 64 KiB including encryption; larger messages wait for a direct connection.</p>
    {error || loadError ? <p role="alert">{error || loadError}</p> : null}
    <Button disabled={busy} onClick={() => void act(refresh)}>Refresh messages</Button>
    {!loaded ? <p>Loading messages…</p> : messages.length === 0 ? <p>Send your first message.</p> : <ol aria-label="Conversation">
      {messages.map((message) => <li key={message.msg_id} id={`friend-message-${message.msg_id}`} tabIndex={-1}
        style={message.msg_id === focusMessage ? { outline: "2px solid currentColor" } : undefined}>
        <strong>{message.dir === "in" ? friend.node_name : "You"}</strong>
        <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{message.text}</p>
        {message.attachment ? <Button disabled={busy} onClick={() => void act(() => download(message))}>Save attachment: {message.attachment.filename} ({message.attachment.size ?? 0} bytes)</Button> : null}
        <p>{message.dir === "in" ? "Received" : stateLabels[message.delivery_state ?? "queued"]}</p>
        {message.dir === "out" && friendDeliveryExplanation(message) ? <p role="status">{friendDeliveryExplanation(message)}</p> : null}
      </li>)}
    </ol>}
    {messages.some((message) => message.dir === "out" && ["queued", "mailbox", "failed"].includes(message.delivery_state ?? "")) ? <Button disabled={busy} onClick={() => void act(async () => { await friendsApi.retry(friend.peer_id); await refresh(); })}>Retry next pending message</Button> : null}
    <label>Message<textarea aria-label="Message" maxLength={32000} value={text} disabled={busy || !!pending} onChange={(event) => setText(event.target.value)} /></label>
    <label>Attachment<input ref={fileInput} aria-label="Attachment" type="file" disabled={busy || !!pending} onChange={(event) => {
      const next = event.target.files?.[0] ?? null;
      if (next && next.size > MAX_FILE) { setError("Attachment exceeds 5 MiB. Choose a smaller file."); setFile(null); event.target.value = ""; }
      else { setFile(next); setError(""); }
    }} /></label>
    {file ? <p>{file.name} · {file.size} bytes</p> : null}
    {pending ? <p>The send result is unconfirmed. Retry keeps the same message identity and content.</p> : null}
    <Button disabled={busy || !loaded || (!pending && !text.trim() && !file)} onClick={() => void act(send)}>{busy ? "Working…" : pending ? "Retry this send" : "Send message"}</Button>
  </Panel>;
}
