import { useCallback, useEffect, useRef, useState } from "react";
import { offlineApi, type OfflineBody } from "../domain/offlineReading";

function LocalImage({ itemKey, body, image, onSettled }: { itemKey: string; body: OfflineBody; image: OfflineBody["images"][number]; onSettled: (index: number) => void }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const stopTimer = useRef<() => void>(() => undefined);
  useEffect(() => {
    const abort = new AbortController(); let objectUrl = "";
    setUrl(""); setFailed(false); setLoaded(false);
    const timer = window.setTimeout(() => { abort.abort(); setFailed(true); }, 15000);
    stopTimer.current = () => window.clearTimeout(timer);
    if (image.state === "verified") void offlineApi.image(itemKey, body.job_id, image.index, abort.signal).then((blob) => {
      if (abort.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch(() => { if (!abort.signal.aborted) setFailed(true); });
    return () => { window.clearTimeout(timer); abort.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [itemKey, body.job_id, image.index, image.state]);
  useEffect(() => {
    if (image.state !== "verified" || failed || loaded) { stopTimer.current(); onSettled(image.index); }
  }, [image.state, image.index, failed, loaded, onSettled]);
  return <figure>{image.state !== "verified" || failed ? <p>Image unavailable in this offline copy{image.alt ? `: ${image.alt}` : "."}</p>
    : url ? <img src={url} alt={image.alt} onLoad={() => setLoaded(true)} onError={() => setFailed(true)} style={{ maxWidth: "100%" }} /> : <p role="status">Loading saved image…</p>}</figure>;
}
export default function OfflineImages({ body, itemKey, onReady }: { body: OfflineBody; itemKey: string; onReady?: (jobId: string) => void }) {
  const [settled, setSettled] = useState<{ job: string; indices: number[] }>({ job: body.job_id, indices: [] });
  const onSettled = useCallback((index: number) => setSettled((prior) => {
    const indices = prior.job === body.job_id ? prior.indices : [];
    return indices.includes(index) ? prior : { job: body.job_id, indices: [...indices, index] };
  }), [body.job_id]);
  useEffect(() => {
    if (body.images.every((image) => image.state !== "verified" || settled.job === body.job_id && settled.indices.includes(image.index))) onReady?.(body.job_id);
  }, [body.job_id, body.images, settled, onReady]);
  return <>{body.images_omitted ? <p>Some images were omitted to stay within the download limit.</p> : null}
    {body.images.map((image) => <LocalImage key={`${body.job_id}:${image.index}`} body={body} image={image} itemKey={itemKey} onSettled={onSettled} />)}</>;
}
