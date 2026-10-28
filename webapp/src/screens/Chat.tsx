import { Paperclip, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Hash, LoadingPanel, PageHeader, Panel, PeerPill } from "../components/ui";
import type { Peer } from "../domain/types";

interface MessageRecord {
  msg_id: string;
  dir: "in" | "out";
  from: string;
  to: string;
  text?: string;
  ts?: string;
  kind?: string;
  delivered?: boolean;
  // "direct" when the peer took it on the wire, "mailbox" when the node handed
  // it to the network mailbox instead, absent when neither route accepted it.
  via?: "direct" | "mailbox";
  attachment?: { filename: string; mime: string; size?: number };
}

// The messages route base mirrors the live client's `${baseUrl}/messages`
// prefix. Derive it from the stream URL so packaged (Tauri) and dev builds
// resolve the same daemon base without re-implementing the precedence logic.
function attachmentBase(streamUrl: string): string {
  return streamUrl.replace(/\/stream$/, "");
}

function formatTime(ts?: string): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

export default function Chat() {
  const { client, peers } = useAppContext();
  const conversationPeers = useMemo(() => peers.filter((peer) => !peer.isSelf), [peers]);
  const [selected, setSelected] = useState<Peer | null>(null);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [text, setText] = useState("");
  const [attachment, setAttachment] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<Peer | null>(null);
  selectedRef.current = selected;

  const msgBase = attachmentBase(client.messagesStreamUrl());

  useEffect(() => {
    if (!selected && conversationPeers[0]) setSelected(conversationPeers[0]);
  }, [conversationPeers, selected]);

  // Load history when the open peer changes.
  useEffect(() => {
    if (!selected) {
      setMessages([]);
      return;
    }
    let alive = true;
    setLoading(true);
    void (async () => {
      try {
        const history = (await client.listMessages(selected.id)) as MessageRecord[];
        if (alive) setMessages(Array.isArray(history) ? history : []);
      } catch {
        if (alive) setMessages([]);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [client, selected]);

  // Subscribe once to the SSE stream; append records for the open peer.
  useEffect(() => {
    const source = new EventSource(client.messagesStreamUrl());
    source.onmessage = (event) => {
      let record: MessageRecord;
      try {
        record = JSON.parse(event.data) as MessageRecord;
      } catch {
        return;
      }
      const open = selectedRef.current;
      if (!open) return;
      // Incoming from the open peer, or our own echo addressed to them.
      if (record.from === open.id || record.to === open.id) {
        setMessages((current) => {
          if (record.msg_id && current.some((m) => m.msg_id === record.msg_id)) {
            return current.map((m) => (m.msg_id === record.msg_id ? record : m));
          }
          return [...current, record];
        });
      }
    };
    return () => source.close();
  }, [client]);

  // Keep the conversation scrolled to the newest message.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const send = async () => {
    if (!selected) return;
    const trimmed = text.trim();
    if (!trimmed && !attachment) return;
    setSending(true);
    try {
      const payload: {
        peer_id: string;
        text?: string;
        attachment?: { filename: string; mime: string; bytes_b64: string };
      } = { peer_id: selected.id };
      if (trimmed) payload.text = trimmed;
      if (attachment) {
        payload.attachment = {
          filename: attachment.name,
          mime: attachment.type || "application/octet-stream",
          bytes_b64: await fileToBase64(attachment),
        };
      }
      const record = (await client.sendMessage(payload)) as MessageRecord;
      setMessages((current) =>
        record.msg_id && current.some((m) => m.msg_id === record.msg_id)
          ? current.map((m) => (m.msg_id === record.msg_id ? record : m))
          : [...current, record],
      );
      setText("");
      setAttachment(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Chat"
        title="Direct peer messaging"
        context="End-to-end-encrypted 1:1 chat between Ryn nodes over the overlay. Each node stores only its own history."
      />
      <div className="chat-grid">
        <Panel className="chat-peer-list">
          <span className="eyebrow">Peers</span>
          {conversationPeers.length === 0 ? (
            <p className="muted">No peers discovered yet.</p>
          ) : (
            <ul className="chat-peer-items">
              {conversationPeers.map((peer) => (
                <li key={peer.id}>
                  <button
                    type="button"
                    className={`chat-peer-item${selected?.id === peer.id ? " active" : ""}`}
                    onClick={() => setSelected(peer)}
                  >
                    <PeerPill peer={peer} />
                    <Hash value={peer.id} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel className="chat-conversation">
          {!selected ? (
            <div className="empty-state">
              <h3>Select a peer</h3>
              <p>Pick a peer on the left to open a conversation.</p>
            </div>
          ) : (
            <>
              <div className="chat-conversation-head">
                <PeerPill peer={selected} />
                <Hash value={selected.id} />
              </div>
              <div className="chat-messages" ref={scrollRef}>
                {loading ? (
                  <LoadingPanel label="Loading conversation" />
                ) : messages.length === 0 ? (
                  <div className="empty-state">
                    <h3>No messages yet</h3>
                    <p>Say hello — messages are sealed for this peer only.</p>
                  </div>
                ) : (
                  messages.map((record, index) => {
                    const out = record.dir === "out";
                    const att = record.attachment;
                    const attUrl = att
                      ? `${msgBase}/attachment/${encodeURIComponent(record.msg_id)}?peer_id=${encodeURIComponent(selected.id)}`
                      : "";
                    return (
                      <div
                        key={record.msg_id || `${record.ts}-${index}`}
                        className={`chat-bubble${out ? " out" : " in"}`}
                      >
                        {record.text ? <span className="chat-text">{record.text}</span> : null}
                        {att ? (
                          record.kind === "image" ? (
                            <img className="chat-attachment-image" src={attUrl} alt={att.filename} />
                          ) : (
                            <a className="chat-attachment-chip" href={attUrl} download={att.filename}>
                              <Paperclip size={13} />
                              {att.filename}
                            </a>
                          )
                        ) : null}
                        <span className="chat-meta">
                          {formatTime(record.ts)}
                          {out && record.delivered ? <span className="chat-check"> ✓</span> : null}
                          {out && !record.delivered && record.via === "mailbox" ? (
                            <span
                              className="chat-queued"
                              title="Queued via the network mailbox; delivered when the peer comes online"
                            >
                              {" "}
                              queued
                            </span>
                          ) : null}
                        </span>
                      </div>
                    );
                  })
                )}
              </div>
              <div className="chat-composer">
                <input
                  ref={fileInputRef}
                  type="file"
                  className="chat-file-input"
                  onChange={(event) => setAttachment(event.target.files?.[0] ?? null)}
                />
                <Button icon={Paperclip} onClick={() => fileInputRef.current?.click()}>
                  {attachment ? attachment.name : "Attach"}
                </Button>
                <input
                  className="chat-input"
                  value={text}
                  placeholder="Type a message"
                  onChange={(event) => setText(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void send();
                    }
                  }}
                />
                <Button variant="primary" icon={SendHorizontal} disabled={sending} onClick={() => void send()}>
                  Send
                </Button>
              </div>
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}
