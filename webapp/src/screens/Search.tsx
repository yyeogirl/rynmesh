import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, PageHeader, Panel } from "../components/ui";
import ContentViewer from "../components/ContentViewer";
import { localSearch, type SearchDocument, type SearchPage, type SearchSnippet, type SearchStatus } from "../domain/localSearch";
import { contentFromHistory } from "../domain/readingHistory";
import { friendsApi } from "../domain/friendsClient";
import type { FriendRecord } from "../domain/friendTypes";

type Form = { query: string; kind: string; source: string; friend: string; after: string; before: string; sort: string };
const empty: Form = { query: "", kind: "", source: "", friend: "", after: "", before: "", sort: "relevance" };
const labels: Record<string, string> = { saved: "Saved", history: "Reading history", share: "Friend shares", chat: "Conversations" };

export function Highlight({ value }: { value: SearchSnippet }) {
  const chars = Array.from(value.text);
  const marked = new Set(value.matches.flatMap(([start, end]) => Array.from({ length: end - start }, (_, i) => i + start)));
  const chunks: { text: string; marked: boolean }[] = [];
  chars.forEach((char, i) => {
    const previous = chunks.at(-1);
    if (previous?.marked === marked.has(i)) previous.text += char;
    else chunks.push({ text: char, marked: marked.has(i) });
  });
  return <>{value.prefix_omitted ? "…" : ""}{chunks.map((chunk, i) => chunk.marked ? <mark key={i}>{chunk.text}</mark> : chunk.text)}{value.suffix_omitted ? "…" : ""}</>;
}

export default function Search() {
  const { client } = useAppContext();
  const location = useLocation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const identifier = params.get("open");
  // Keep private keywords in browser navigation state, not URLs/access logs.
  const [form, setForm] = useState<Form>(() => ({ ...empty, ...location.state?.searchForm }));
  const [page, setPage] = useState<SearchPage | null>(null);
  const [status, setStatus] = useState<SearchStatus | null>(null);
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [document, setDocument] = useState<SearchDocument | null>(null);
  const [onlineReading, setOnlineReading] = useState(false);
  const sequence = useRef(0);
  const screenRoot = useRef<HTMLDivElement>(null);
  const resultsRoot = useRef<HTMLOListElement>(null);
  const newResultFocus = useRef<number | null>(null);
  const indexStamp = useRef<number | null>(null);
  const restoreScroll = useRef<number | null>(location.state?.searchScroll ?? null);
  useEffect(() => {
    if (newResultFocus.current === null || !page) return;
    const row = resultsRoot.current?.children.item(newResultFocus.current);
    newResultFocus.current = null;
    row?.querySelector<HTMLAnchorElement>("a[href]")?.focus();
  }, [page]);
  const request = useCallback((cursor = "") => ({ query: form.query, kind: form.kind, source: form.source, friend_id: form.friend,
    sort: form.sort, cursor, ...(form.after ? { after: new Date(`${form.after}T00:00:00`).getTime() / 1000 } : {}),
    ...(form.before ? { before: new Date(`${form.before}T23:59:59.999`).getTime() / 1000 } : {}) }), [form]);
  useEffect(() => {
    void friendsApi.list().then((value) => setFriends(value.friends.filter((row) => row.status === "active"))).catch(() => undefined);
    const read = () => { void localSearch.status().then(setStatus).catch(() => setStatus(null)); };
    read(); const timer = window.setInterval(read, 3000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (status?.updated_at && indexStamp.current && status.updated_at !== indexStamp.current && !identifier) setRevision((value) => value + 1);
    if (status?.updated_at) indexStamp.current = status.updated_at;
  }, [status?.updated_at, identifier]);
  useEffect(() => {
    const generation = ++sequence.current;
    const abort = new AbortController();
    setPage(null); setError("");
    if (identifier || !form.query.trim()) { setBusy(false); return; }
    setBusy(true);
    const timer = window.setTimeout(() => {
      void localSearch.query(request(), abort.signal).then(async (first) => {
        let value = first;
        const restoredCount = location.state?.searchCount ?? 0;
        while (value.next_cursor && value.results.length < restoredCount && generation === sequence.current) {
          const next = await localSearch.query(request(value.next_cursor), abort.signal);
          value = { ...next, results: [...value.results, ...next.results] };
        }
        if (generation !== sequence.current) return;
        setPage(value); setStatus(value.index);
        if (location.state?.searchScroll) restoreScroll.current = location.state.searchScroll;
        if (restoreScroll.current !== null) {
          const top = restoreScroll.current; restoreScroll.current = null;
          window.requestAnimationFrame(() => {
            const scroller = screenRoot.current?.closest<HTMLElement>(".app-main");
            if (scroller) scroller.scrollTop = top;
            else window.scrollTo(0, top);
          });
        }
      }).catch((cause) => { if (generation === sequence.current && !abort.signal.aborted) setError(cause.message); })
        .finally(() => { if (generation === sequence.current) setBusy(false); });
    }, 250);
    return () => { abort.abort(); window.clearTimeout(timer); };
  }, [form.query, request, identifier, revision]);
  useEffect(() => {
    if (!(page?.indexing_pending ?? page?.partial) || busy || identifier) return;
    const timer = window.setTimeout(() => setRevision((value) => value + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [page, busy, identifier]);
  useEffect(() => {
    let active = true;
    setDocument(null); setOnlineReading(false);
    if (identifier) void localSearch.open(identifier).then((value) => { if (active) setDocument(value); })
      .catch((cause) => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [identifier]);
  const remember = () => navigate(location.pathname + location.search, { replace: true,
    state: { ...location.state, searchForm: form,
      searchScroll: screenRoot.current?.closest<HTMLElement>(".app-main")?.scrollTop ?? window.scrollY,
      searchCount: page?.results.length ?? 0 } });
  const change = (key: keyof Form, value: string) => {
    sequence.current += 1; restoreScroll.current = null;
    const next = { ...form, [key]: value }; setForm(next);
    navigate("/search", { replace: true, state: { searchForm: next, searchScroll: 0 } });
  };
  const more = async () => {
    if (!page?.next_cursor) return;
    const trigger = window.document.activeElement;
    const generation = ++sequence.current; setBusy(true); setError("");
    try {
      const next = await localSearch.query(request(page.next_cursor));
      if (generation === sequence.current) {
        if (next.results.length && (window.document.activeElement === trigger || window.document.activeElement === window.document.body)) {
          newResultFocus.current = page.results.length;
        }
        setPage({ ...next, results: [...page.results, ...next.results] });
      }
    } catch (cause) { if (generation === sequence.current) { setPage(null); setError((cause as Error).message); } }
    finally { if (generation === sequence.current) setBusy(false); }
  };
  const loadLocal = useCallback(async () => {
    const current = await localSearch.open(identifier!);
    if (current.body_state !== "available" || !current.text) throw new Error("local_body_unavailable");
    return { text: current.text, truncated: !!current.text_truncated };
  }, [identifier]);
  if (identifier) return <div className="screen-stack"><Button onClick={() => navigate(-1)}>Back to search results</Button>
    {error ? <p role="alert">{error}</p> : !document ? <p>Checking this local result…</p> : <Panel>
      <h1>{document.title}</h1><p>{document.source}</p>
      {document.reading_record && (document.body_state === "available" || onlineReading) ? <ContentViewer
        item={contentFromHistory(document.reading_record)} client={client} loadBody={onlineReading ? undefined : loadLocal} offlineKey={document.offline_key}
        onClose={() => navigate(-1)} onRead={() => client.recordContentConsumption(contentFromHistory(document.reading_record!), "opened")} />
        : <><p>The local body is unavailable or has not been downloaded. It was not included in full-text search.</p>
          {document.reading_record ? <Button onClick={() => setOnlineReading(true)}>Open reading view and try fetching the source</Button> : null}
          {document.targets.filter((target) => !target.href.startsWith("/search?")).map((target) => <Link key={target.href} to={target.href}>{target.label}</Link>)}
        </>}
    </Panel>}</div>;
  return <div ref={screenRoot} className="screen-stack"><PageHeader eyebrow="Your local content" title="Search" context="Find saved articles, reading history, friend shares and conversations. Searches stay on your node." />
    <Panel><label>Keywords<input aria-label="Search keywords" value={form.query} maxLength={160} onChange={(e) => change("query", e.target.value)} placeholder="Chinese phrases or English keywords" /></label>
      <label>Type<select aria-label="Search type" value={form.kind} onChange={(e) => change("kind", e.target.value)}><option value="">All types</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>Source<input aria-label="Search source" value={form.source} onChange={(e) => change("source", e.target.value)} placeholder="Exact source name" /></label>
      <label>Friend<select aria-label="Search friend" value={form.friend} onChange={(e) => change("friend", e.target.value)}><option value="">All friends</option>{friends.map((friend) => <option key={friend.peer_id} value={friend.peer_id}>{friend.node_name}</option>)}</select></label>
      <label>From<input aria-label="Search from" type="date" value={form.after} onChange={(e) => change("after", e.target.value)} /></label>
      <label>Through<input aria-label="Search through" type="date" value={form.before} onChange={(e) => change("before", e.target.value)} /></label>
      <label>Sort<select aria-label="Search sort" value={form.sort} onChange={(e) => change("sort", e.target.value)}><option value="relevance">Relevance</option><option value="recent">Most recent</option></select></label>
      <Button onClick={() => { setForm(empty); navigate("/search", { replace: true, state: { searchForm: empty } }); }}>Clear filters and keywords</Button>
      <Button disabled={busy} onClick={() => setRevision((value) => value + 1)}>Refresh results</Button>
      <Button disabled={status?.state === "building"} onClick={() => {
        setError(""); setStatus((value) => value ? { ...value, state: "building" } : null);
        void localSearch.rebuild().then((value) => { setStatus(value); setRevision((prior) => prior + 1); })
          .catch((cause) => { setError(cause.message); void localSearch.status().then(setStatus).catch(() => setStatus(null)); });
      }}>Rebuild search index</Button>
      <p role="status">{status ? `Index: ${status.state} · ${status.indexed_count} records` : "Index status unavailable"}{busy ? " · Searching…" : ""}</p>
      {error ? <p role="alert">{error}</p> : null}
      {!form.query.trim() ? <p>Enter keywords to search local data. Undownloaded bodies and remote devices are not searched.</p> : page ? <>
        {page.indexing_pending ?? page.partial ? <p>Showing partial results while the index catches up. Some local records are not indexed yet.</p> : null}
        {page.unavailable_sources?.includes('saved_documents') ? <p role="status">Some saved document data is unavailable. Results may be incomplete. <Link to="/settings" onClick={remember}>Review document copies in Settings</Link>, then refresh results. Other local content remains searchable.</p> : null}
        {page.unavailable_sources?.includes('offline_downloads') ? <p role="status">Some downloaded content is unavailable. <Link to="/offline" onClick={remember}>Check offline reading</Link>, then refresh results. Other local content remains searchable.</p> : null}
        {!page.results.length && !page.partial ? <p>No matches. Try another keyword or clear the filters.</p> : <p>{page.total} matches</p>}
        <ol ref={resultsRoot}>{page.results.map((result) => <li key={result.id} style={{ marginBlock: "1.5rem" }}>
          <h2><Highlight value={result.title_match} /></h2><p>{result.kinds.map((kind) => labels[kind]).join(" · ")} · {result.source} · {result.timestamp > 0 ? new Date(result.timestamp * 1000).toLocaleString() : "Date unavailable"}</p>
          <p><Highlight value={result.snippet} /></p>{result.body_state !== "available" ? <p>Full text unavailable locally; metadata only.</p> : null}
          {result.text_truncated ? <p>The saved extraction is truncated. Only the extracted text was searched.</p> : null}
          {result.targets.map((target) => <Link key={target.href} to={target.href} onClick={remember} style={{ marginRight: "1rem" }}>{target.label}</Link>)}
        </li>)}</ol>
        {page.next_cursor ? <Button disabled={busy} onClick={() => void more()}>Load more results</Button> : null}
      </> : null}
    </Panel></div>;
}
