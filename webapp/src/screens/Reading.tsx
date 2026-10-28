import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import ContentViewer from "../components/ContentViewer";
import { Button, PageHeader, Panel } from "../components/ui";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import { contentFromHistory } from "../domain/readingHistory";

export default function Reading() {
  const { client } = useAppContext();
  const [rows, setRows] = useState<ConsumptionRecord[] | null>(null);
  const [filter, setFilter] = useState("continue");
  const [limit, setLimit] = useState(20);
  const [reading, setReading] = useState<ConsumptionRecord | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true), generation = useRef(0);
  const refresh = useCallback(async () => {
    const ticket = ++generation.current;
    setBusy(true); setError("");
    try {
      const next = await digestApi.listConsumption();
      if (mounted.current && ticket === generation.current) setRows(next);
    } catch {
      if (mounted.current && ticket === generation.current) setError("Your reading list could not be refreshed. Retry when the local node is available.");
    } finally { if (mounted.current && ticket === generation.current) setBusy(false); }
  }, []);
  useEffect(() => {
    mounted.current = true; void refresh();
    return () => { mounted.current = false; generation.current++; };
  }, [refresh]);
  const visible = (rows ?? []).filter((row) => {
    if (filter === "saved") return row.bookmarked;
    const opened = row.open_count > 0 || row.progress > 0 || row.sync_reading_available;
    if (filter === "history") return opened;
    return row.sync_conflicts?.reading || (opened && !row.completed);
  });
  return <div className="screen-stack">
    <PageHeader eyebrow="Your content" title="My reading" context="Continue reading and find saved articles from this node and your approved devices." />
    <Panel>
      <p>Synced titles and positions do not include article text. Opening an item tries a saved local copy first; fetching a source needs a connection.
        {" "}<Link to="/offline">Manage offline copies</Link> · <Link to="/devices">My devices</Link></p>
      <label>Show <select aria-label="Reading list" value={filter} onChange={(event) => { setFilter(event.target.value); setLimit(20); }}>
        <option value="continue">Continue reading</option><option value="saved">Saved content</option><option value="history">Reading history</option>
      </select></label>
      <Button disabled={busy} onClick={() => void refresh()}>Refresh reading list</Button>
      {error ? <p role="alert">{error}</p> : null}
      {rows === null ? <p role="status">{busy ? "Loading your reading list…" : "The reading list is unavailable."}</p>
        : visible.length === 0 ? <p>No items in this list yet. <Link to="/digest">Find something to read</Link>.</p>
        : <div>{visible.slice(0, limit).map((row) => <article key={row.item_id} style={{ padding: "16px 0", borderTop: "1px solid var(--line)", overflowWrap: "anywhere" }}>
          <h2>{row.item.title || "Untitled content"}</h2><p>{row.item.source_title || "Source unavailable"}</p>
          <p>{row.bookmarked ? "Saved · " : ""}{row.sync_conflicts?.reading ? "Reading position needs review" : row.completed ? "Completed" : `${Math.round(row.progress * 100)}% read`}
            {row.last_opened_unix > 0 ? ` · Last opened on this device ${new Date(row.last_opened_unix * 1000).toLocaleString()}`
              : row.sync_reading_available ? " · Saved position; not yet opened on this device" : " · Not opened on this device"}</p>
          {row.sync_conflicts?.reading ? <Link to="/devices#reading-sync-conflicts">Review reading choices</Link> : null}
          <Button onClick={() => setReading(row)}>{row.sync_conflicts?.reading ? "Read without choosing a position" : "Open reading view"}</Button>
        </article>)}</div>}
      {visible.length > limit ? <Button onClick={() => setLimit((value) => value + 20)}>Show more reading items</Button> : null}
    </Panel>
    {reading ? <ContentViewer key={reading.item_id} client={client} item={contentFromHistory(reading)}
      onRead={() => client.recordContentConsumption(contentFromHistory(reading), "opened")}
      onClose={() => { setReading(null); void refresh(); }} /> : null}
  </div>;
}
