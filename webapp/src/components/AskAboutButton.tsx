import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { askHistory } from "../domain/askHistory";
import { Button } from "./ui";

export default function AskAboutButton({ itemId, offlineJobId }: { itemId: string; offlineJobId?: string }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const prepare = async () => {
    setBusy(true); setError("");
    try {
      const source = await askHistory.prepareContext(itemId, offlineJobId);
      navigate(`/ask?material=${encodeURIComponent(source.library_id)}`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not prepare this article. Reopen it and retry."); }
    finally { setBusy(false); }
  };
  return <><Button disabled={busy} onClick={() => void prepare()}>{busy ? "Preparing article…" : "Ask about this content"}</Button>{error ? <span role="alert">{error}</span> : null}</>;
}
