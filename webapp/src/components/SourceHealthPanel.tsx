import { useState } from "react";
import { digestApi, type DiscoveryStatus } from "../domain/digestClient";
import { Button, Chip } from "./ui";

export default function SourceHealthPanel({ status, onRefresh }: {
  status: DiscoveryStatus | null;
  onRefresh: () => Promise<void>;
}) {
  const [retrying, setRetrying] = useState<string | null>(null);
  const [error, setError] = useState("");
  const retry = async (id: string) => {
    setRetrying(id);
    setError("");
    try {
      await digestApi.retrySource(id);
      await onRefresh();
    } catch {
      setError("This source could not be checked. If a refresh is already running, wait and retry.");
    } finally { setRetrying(null); }
  };
  const when = (stamp: number) => stamp ? new Date(stamp * 1000).toLocaleString() : "Never";
  return <section aria-label="Source health">
    <h3>Source health and recovery</h3>
    {!status ? <p role="status">Checking source health…</p> : !status.source_health.length ? <p>No sources have been configured.</p> : (
      <ul className="source-health-list">{status.source_health.map((source) => (
        <li key={source.id}>
          <strong>{source.title}</strong>{" "}
          <Chip tone={source.ok ? "ok" : source.status === "not_checked" ? "muted" : "warn"}>
            {source.ok ? "Available" : source.status === "not_checked" ? "Not checked" : source.using_cached_items ? "Using cached content" : "Unavailable"}
          </Chip>
          <p>{source.item_count} items · Last checked: {when(source.last_checked_unix)} · Last success: {when(source.last_success_unix)} · Consecutive failures: {source.consecutive_failures}</p>
          {source.error ? <p>{source.error === "source_feed_invalid" ? "This source returned an unreadable feed." : "This source could not be reached. Existing content is kept."}</p> : null}
          <Button disabled={retrying !== null} onClick={() => void retry(source.id)}>{retrying === source.id ? "Retrying…" : `Retry ${source.title}`}</Button>
        </li>
      ))}</ul>
    )}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
