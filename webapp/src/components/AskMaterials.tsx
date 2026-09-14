import { useEffect, useState } from "react";
import { askHistory, type AskSource } from "../domain/askHistory";
import { Button } from "./ui";

export default function AskMaterials({ ids, byteLimits, onRemove }: { ids: string[]; byteLimits?: number[]; onRemove?: (id: string) => Promise<void> | void }) {
  return <div>{ids.map((id, index) => <Material key={id} id={id} byteLimit={byteLimits?.[index]} onRemove={onRemove} />)}</div>;
}
export function AskAnswerSources({ ids, byteLimits }: { ids: string[]; byteLimits?: number[] }) {
  const [open, setOpen] = useState(false);
  return <details onToggle={(event) => setOpen(event.currentTarget.open)}><summary>Sources supplied for this answer</summary>{open ? <AskMaterials ids={ids} byteLimits={byteLimits} /> : null}</details>;
}
function Material({ id, byteLimit, onRemove }: { id: string; byteLimit?: number; onRemove?: (id: string) => Promise<void> | void }) {
  const [source, setSource] = useState<AskSource | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true; setError("");
    void askHistory.context(id).then((result) => { if (active) setSource(result); }).catch((cause: Error) => { if (active) { setSource(null); setError(cause.message); } });
    return () => { active = false; };
  }, [id, retry]);
  return <section>
    {source ? <>
      <strong>Article: {source.title}</strong>
      {source.source_url && /^https?:\/\//.test(source.source_url) ? <p>Source: <a href={source.source_url} target="_blank" rel="noreferrer">{source.source_url}</a></p> : <p>Source: private document on this node</p>}
      <p>{source.extraction_truncated ? "Document extraction was truncated. " : ""}{byteLimit === undefined ? "The model receives only the material shown in the send review." : `Supplied excerpt: ${byteLimit} bytes of ${source.text_bytes}.${byteLimit < source.text_bytes ? " Truncated for the context budget." : ""}`}</p>
      <details><summary>{byteLimit === undefined ? "Read local source copy" : "Read supplied excerpt"}</summary><p style={{ whiteSpace: "pre-wrap", maxHeight: "35vh", overflow: "auto" }}>{byteLimit === undefined ? source.text : new TextDecoder().decode(new TextEncoder().encode(source.text ?? "").slice(0, byteLimit))}</p></details>
    </> : !error ? <p>Reading local article copy…</p> : null}
    {error ? <><p role="alert">{error}</p><Button onClick={() => setRetry((value) => value + 1)}>Retry source</Button></> : null}
    {onRemove ? <Button onClick={() => Promise.resolve(onRemove(id)).catch((cause: Error) => setError(cause.message))}>Remove article from context</Button> : null}
  </section>;
}
