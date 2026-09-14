import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { offlineApi, offlineLabels } from "../domain/offlineReading";
import { Button } from "./ui";

export default function OfflineDownloadButton({ itemId }: { itemId: string }) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const pending = useRef(false);
  return <span><Button disabled={busy} onClick={() => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(""); setNotice("");
    void offlineApi.download(itemId).then((row) => setNotice(offlineLabels[row.state] ?? "Check download status"))
      .catch((cause) => setError(cause instanceof Error ? cause.message : "Download could not be confirmed."))
      .finally(() => { pending.current = false; setBusy(false); });
  }}>{busy ? "Requesting download…" : "Download for offline"}</Button>
    {notice ? <span role="status">{notice}. </span> : null}
    {error ? <span role="alert">{error} </span> : null}
    <Link to="/offline">Offline downloads</Link>
  </span>;
}
