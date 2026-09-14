import { useEffect, useState } from "react";
import { digestApi, type FeedbackHistory } from "../domain/digestClient";
import { Button, Chip } from "./ui";

export default function FeedbackSignalsPanel({ revision, onRefresh }: {
  revision: number;
  onRefresh: () => Promise<void>;
}) {
  const [history, setHistory] = useState<FeedbackHistory | null>(null);
  const [offset, setOffset] = useState(0);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError("");
    void digestApi.feedbackHistory(offset).then((value) => {
      if (!cancelled) setHistory(value);
    }).catch(() => {
      if (!cancelled) setError("Feedback history could not be loaded. Please retry.");
    });
    return () => { cancelled = true; };
  }, [revision, offset, reload]);

  const undo = async (id: string) => {
    setPending(id);
    setError("");
    try {
      await digestApi.undoFeedback(id);
      setHistory(await digestApi.feedbackHistory(offset));
      await onRefresh();
    } catch {
      setError("The latest feedback state could not be confirmed. Retry is safe.");
    } finally { setPending(""); }
  };
  return <section aria-label="Feedback history">
    <h3>Why your recommendations change</h3>
    <p>More and Less adjust matching topics, platforms and sources. Hide also removes that item.
      The latest remaining action for each item applies. Undo restores its previous action, if any.</p>
    <p>These preferences stay on this node. They influence recommendations without guaranteeing a particular order.</p>
    {!history && !error ? <p role="status">Loading feedback…</p> : null}
    {history?.total === 0 ? <p>No feedback yet. Try More, Less or Hide on an article.</p> : null}
    <ul>{history?.items.map((event) => <li key={event.event_id}>
      <strong>{event.title || event.content_id}</strong>{" "}
      <Chip tone={event.active ? "info" : "muted"}>{event.undone_at ? "Undone" : event.active ? "Active" : "Replaced by a later action"}</Chip>
      <p>{({ more: "More", less: "Less", hide: "Hide", neutral: "Clear item feedback" })[event.action]} · {event.updated_at ? new Date(event.updated_at).toLocaleString() : "Original time unavailable"}</p>
      <p>Topics: {event.tags.length ? event.tags.join(", ") : "None"} · Platform: {event.platform || "None"} · Source: {event.publisher || "None"}</p>
      {event.action === "hide" ? <p>Hides this item while this action is active.</p> : null}
      {event.migrated ? <p>Recovered from your earlier preferences. Only the last saved action was available.</p> : null}
      {!event.undone_at ? <Button disabled={!!pending} onClick={() => void undo(event.event_id)}>
        {pending === event.event_id ? "Undoing…" : "Undo this feedback"}
      </Button> : null}
    </li>)}</ul>
    {history && history.total > history.limit ? <div>
      <Button disabled={offset === 0 || !!pending} onClick={() => setOffset(Math.max(0, offset - 20))}>Newer feedback</Button>
      <Button disabled={offset + history.limit >= history.total || !!pending} onClick={() => setOffset(offset + 20)}>Older feedback</Button>
    </div> : null}
    {error ? <p role="alert">{error} <Button disabled={!!pending} onClick={() => setReload((value) => value + 1)}>Reload feedback</Button></p> : null}
  </section>;
}
