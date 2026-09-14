import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/ui";
import { ProductExportError, productExport, type ExportOptions } from "../../domain/productExport";
import styles from "./ConversationCleanup.module.css";

export default function ProductExportPanel() {
  const [options, setOptions] = useState<ExportOptions | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const status = useRef<HTMLParagraphElement>(null);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const failure = (cause: unknown) => {
    const scope = cause instanceof ProductExportError ? options?.scopes.find((row) => row.id === cause.scope)?.label : undefined;
    setError(`${scope ? `${scope}: ` : ''}${cause instanceof ProductExportError ? cause.message : 'The node did not return a complete export. Check the connection and retry.'}`);
  };
  const load = async () => {
    setBusy(true); setError("");
    const active = new AbortController();
    controller.current = active;
    try {
      const value = await productExport.options(active.signal);
      if (mounted.current && !active.signal.aborted) { setOptions(value); setSelected(value.scopes.map((row) => row.id)); }
    } catch (cause) { if (mounted.current && !active.signal.aborted) failure(cause); }
    finally { if (mounted.current && !active.signal.aborted) setBusy(false); }
  };
  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; controller.current?.abort(); };
  }, []);
  useEffect(() => { if (error || notice) status.current?.focus(); }, [error, notice]);
  const download = async () => {
    setBusy(true); setError(""); setNotice("");
    controller.current = new AbortController();
    try {
      const blob = await productExport.download(selected, controller.current.signal);
      if (mounted.current) {
        productExport.save(blob);
        setNotice("Export prepared and download requested. Check your browser downloads. The ZIP manifest lists the included files and excluded data.");
      }
    } catch (cause) { if (mounted.current) failure(cause); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <section className={styles.panel} aria-labelledby="product-export-title">
    <h3 id="product-export-title">Export product data</h3>
    <p>Choose data from this node. Saved documents include original files and extracted text; offline copies include verified images. The downloaded ZIP contains readable personal data and an integrity manifest. It does not restore accounts or restart AI tasks.</p>
    <p ref={status} tabIndex={-1} role={error ? 'alert' : 'status'}>{error || notice}</p>
    {options ? <>
      <fieldset disabled={busy} className={styles.exportScopes}>
        <legend>Data to include</legend>
        {options.scopes.map((scope) => <label key={scope.id}>
          <input type="checkbox" checked={selected.includes(scope.id)} onChange={(event) => setSelected((current) =>
            event.target.checked ? [...current, scope.id] : current.filter((id) => id !== scope.id))} />
          {scope.label}
        </label>)}
      </fieldset>
      <details><summary>Data excluded from this ZIP</summary><ul>{options.excluded.map((text) => <li key={text}>{text}</li>)}</ul></details>
      <p>Scopes are read separately during export. Changes to a document or download can require a retry. Other browsers and devices are not contacted.</p>
      <Button disabled={busy || !selected.length} onClick={() => void download()}>{busy ? 'Preparing export…' : 'Download selected data (ZIP)'}</Button>
    </> : <Button disabled={busy} onClick={() => void load()}>{busy ? 'Loading export options…' : 'Retry export options'}</Button>}
  </section>;
}
