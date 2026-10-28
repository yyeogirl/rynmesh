import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Panel } from "./ui";
import { deviceSyncApi } from "../domain/deviceSync";
import type { DevicePair, ReadingConflict } from "../domain/deviceSync";

function Choice({ issue, localActor, devices, busy, resolve }: { issue: ReadingConflict; localActor: string; devices: DevicePair[];
  busy: boolean; resolve: (issue: ReadingConflict, choice: string) => void }) {
  const [selected, setSelected] = useState("");
  return <article style={{ borderTop: "1px solid var(--line)", padding: "16px 0", overflowWrap: "anywhere" }}>
    <h3>{issue.item?.title || "Reading item"}</h3><p>{issue.item?.source_title || "Source unavailable"}</p>
    <fieldset disabled={busy} style={{ display: "grid", gap: 12, border: 0, padding: 0 }}>
      <legend>{issue.scope === "reading" ? "Choose a reading position" : "Choose whether to keep this saved"}</legend>
      {issue.candidates.map((candidate) => {
        const actor = candidate.choice_id.split(":")[0];
        const name = actor === localActor ? "This device" : devices.find((pair) => pair.device.actor === actor)?.device.name || "Another device";
        const value = candidate.value;
        const description = issue.scope === "bookmarks" ? value?.bookmarked ? "Saved" : "Not saved"
          : value === null ? "Remove synced reading position" : `${Math.round((value.progress ?? 0) * 100)}% read${value.completed ? " · completed" : ""}`;
        return <label key={candidate.choice_id} style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
          <input type="radio" name={`choice-${issue.scope}-${issue.id}`} value={candidate.choice_id} checked={selected === candidate.choice_id}
            onChange={() => setSelected(candidate.choice_id)} style={{ width: 18, height: 18, flex: "0 0 18px", padding: 0 }} />
          <span>{description} · {name} · {actor.slice(0, 12)}
            {issue.scope === "reading" && value ? <small style={{ display: "block" }}>Content version: {value.content_version || "unknown"}</small> : null}</span>
        </label>;
      })}
    </fieldset>
    <p>Choosing updates this device now. The choice can reach other paired devices when this category is allowed. Article text is not copied.</p>
    <Button disabled={busy || !selected} onClick={() => resolve(issue, selected)}>Use this choice</Button>
  </article>;
}

export default function ReadingSyncConflicts({ devices, onResolved }: { devices: DevicePair[]; onResolved: () => Promise<void> }) {
  const [issues, setIssues] = useState<ReadingConflict[]>([]);
  const [actor, setActor] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef(true), generation = useRef(0);
  const load = useCallback(async () => {
    const version = ++generation.current;
    const result = await deviceSyncApi.readingConflicts();
    if (!active.current || version !== generation.current) return;
    setIssues(result.conflicts); setActor(result.local_actor); setLoaded(true);
  }, []);
  useEffect(() => {
    active.current = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await load(); }
      catch { if (active.current) setError("Reading changes could not be loaded. Refresh to retry."); }
      finally { running = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { active.current = false; window.clearInterval(timer); };
  }, [load]);
  const resolve = async (issue: ReadingConflict, choice: string) => {
    setBusy(true); setError(""); setNotice("");
    try {
      await deviceSyncApi.resolveReading(issue, choice);
      if (active.current) setNotice("Choice saved on this device. Check the device status for sync confirmation.");
      await load(); await onResolved();
    } catch (cause) {
      if (active.current) setError(cause instanceof Error ? cause.message : "This choice could not be confirmed. Refresh and review again.");
    } finally { if (active.current) setBusy(false); }
  };
  return <Panel><h2 id="reading-sync-conflicts">Review reading changes</h2>
    <p>Different devices may keep different positions or saved choices. Review the candidates; a larger percentage or a newer computer clock does not decide for you.</p>
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy} onClick={() => { setError(""); void load().catch(() => setError("Reading changes could not be loaded. Refresh to retry.")); }}>Refresh reading changes</Button>
    {!loaded ? <p>Loading reading changes…</p> : issues.length === 0 ? <p>No reading conflicts to resolve.</p> : issues.map((issue) =>
      <Choice key={`${issue.scope}:${issue.id}:${issue.revision}`} issue={issue} localActor={actor} devices={devices} busy={busy}
        resolve={(value, choice) => void resolve(value, choice)} />)}
  </Panel>;
}
