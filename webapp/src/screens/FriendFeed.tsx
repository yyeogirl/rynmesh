import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import ContentViewer from "../components/ContentViewer";
import { Button, PageHeader, Panel } from "../components/ui";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import { feedApi, type FeedAudience, type FeedEntry, type FeedPublication, type FeedSnapshot } from "../domain/friendFeed";
import { friendsApi } from "../domain/friendsClient";
import type { FriendRecord } from "../domain/friendTypes";
import { contentFromHistory } from "../domain/readingHistory";
import type { ContentItem } from "../domain/types";

const newId = () => crypto.randomUUID().replaceAll("-", "");
const selected: FeedAudience = { mode: "selected", relationship_ids: [] };
const stamp = (value: number | null) => value ? new Date(value * 1000).toLocaleString() : "Never checked";

export default function FriendFeed() {
  const { client, confirm } = useAppContext();
  const [snapshot, setSnapshot] = useState<FeedSnapshot>({ subscriptions: [], timeline: [] });
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [publications, setPublications] = useState<FeedPublication[]>([]);
  const [items, setItems] = useState<ConsumptionRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<FeedPublication | null>(null);
  const [itemId, setItemId] = useState("");
  const [audience, setAudience] = useState<FeedAudience>(selected);
  const [dirty, setDirty] = useState(true);
  const [reading, setReading] = useState<{ item: ContentItem; rid: string; entry: FeedEntry } | null>(null);
  const draftId = useRef(newId());
  const attempt = useRef<{ key: string; id: string } | null>(null);
  const sequence = useRef(0);
  const mounted = useRef(true);
  const op = (key: string) => {
    if (attempt.current?.key !== key) attempt.current = { key, id: newId() };
    return attempt.current.id;
  };
  const load = useCallback(async () => {
    const ticket = ++sequence.current;
    const [feed, contacts, own, saved] = await Promise.all([feedApi.snapshot(), friendsApi.list(), feedApi.publications(), digestApi.listConsumption()]);
    if (!mounted.current || ticket !== sequence.current) return;
    setSnapshot(feed); setFriends(contacts.friends.filter((row) => row.status === "active"));
    setPublications(own.publications); setItems(saved); setLoaded(true); setLoadError("");
  }, []);
  useEffect(() => {
    mounted.current = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await load(); } catch { if (mounted.current) setLoadError("Could not refresh your local feed. Retry when the node is available."); }
      finally { running = false; }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 5000);
    return () => { mounted.current = false; sequence.current += 1; window.clearInterval(timer); };
  }, [load]);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice(""); sequence.current += 1;
    try { await operation(); await load(); }
    catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not confirm this operation. Retry.");
      await load().catch(() => undefined); // A durable subscription may precede a failed remote refresh.
    }
    finally { if (mounted.current) setBusy(false); }
  };
  const edit = (row: FeedPublication) => {
    setEditing(row); draftId.current = row.id;
    setItemId(row.draft?.card.library_id ?? ""); setAudience(row.draft?.audience ?? selected); setDirty(false);
  };
  const save = async () => {
    const request = { reference: { item_id: itemId }, audience, expected_revision: editing?.revision ?? 0 };
    const row = await feedApi.draft(draftId.current, { ...request, operation_id: op(JSON.stringify([draftId.current, "draft", request])) });
    setEditing(row); setDirty(false); setNotice("Draft saved on this node. Review it before publishing.");
  };
  const publish = () => {
    if (!editing?.draft || dirty) return;
    const reviewed = editing;
    const all = reviewed.draft!.audience.mode === "all_friends";
    confirm({ title: "Publish this content?", body: `${reviewed.draft!.card.title}. ${all ? "All current and future friends will be allowed to access it." : "Only the selected friends will be allowed to access it."} Friends who follow you can see the update. Saved copies cannot be recalled.`,
      risk: "medium", confirmLabel: all ? "Publish to current and future friends" : "Publish to selected friends",
      onConfirm: () => act(async () => {
        const row = await feedApi.publish(reviewed.id, { expected_revision: reviewed.revision, operation_id: op(`${reviewed.id}:publish:${reviewed.revision}`), confirm_all_friends: all });
        setEditing(row); setNotice("Published on your node. This does not mean anyone has received or read it.");
      }) });
  };
  const open = async (rid: string, entry: FeedEntry) => {
    const copy = await feedApi.fetch(rid, entry.id, entry.revision);
    const saved = await digestApi.listConsumption();
    const record = saved.find((row) => row.item_id === copy.library_id);
    if (!record) throw new Error("The copy was saved. Refresh your saved content to open it.");
    setReading({ item: contentFromHistory(record), rid, entry });
  };
  return <div className="screen-stack">
    <PageHeader eyebrow="Private sharing" title="Friend updates" context="Follow friends to see the content they choose to publish. Reading and saving never publish automatically." />
    {error || loadError ? <p role="alert">{error || loadError}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy} onClick={() => void act(load)}>Refresh local feed</Button>
    {!loaded ? <p>Loading friend updates…</p> : null}
    <Panel><h2>Follow friends</h2>
      <p>Following is off by default. It gives no extra content, file or AI permission.</p>
      {!friends.length && loaded ? <p>No active friends. <Link to="/friends">Invite a friend</Link> to get started.</p> : null}
      {friends.map((friend) => {
        const subscription = snapshot.subscriptions.find((row) => row.relationship_id === friend.relationship_id);
        return <div key={friend.relationship_id}><span>{friend.node_name} </span>
          <Button disabled={busy} onClick={() => void act(async () => {
            await feedApi.subscribe(friend.relationship_id, !subscription?.enabled, subscription?.revision ?? 0);
            if (!subscription?.enabled) await feedApi.refresh(friend.relationship_id);
          })}>{subscription?.enabled ? `Unfollow ${friend.node_name}` : `Follow ${friend.node_name}`}</Button></div>;
      })}
      <p>Unfollowing removes these update entries. Private conversations and copies you saved remain.</p>
    </Panel>
    <Panel><h2>Updates you follow</h2>
      {loaded && !snapshot.timeline.length ? <p>You are not following anyone yet.</p> : null}
      {snapshot.timeline.map((feed) => <section key={feed.relationship_id}><h3>{feed.node_name}</h3>
        <p>Last checked: {stamp(feed.checked_at)}. Access may have changed while offline.</p>
        {feed.error_code ? <p role="status">Could not confirm the latest updates. Retry when your friend is reachable.</p> : null}
        <Button disabled={busy} onClick={() => void act(async () => { await feedApi.refresh(feed.relationship_id); })}>Refresh {feed.node_name}</Button>
        {!feed.rows.length ? <p>No visible updates received yet.</p> : feed.rows.map((entry) => <article key={entry.id}>
          <h4>{entry.card.title}</h4><p>{entry.card.summary}</p><p>{entry.card.source || "Source not supplied"} · {stamp(entry.published_at)}</p>
          {entry.card.source_url ? <p style={{ overflowWrap: "anywhere" }}>Source: {entry.card.source_url}</p> : null}
          {entry.card.content_truncated ? <p>This shared text is shortened; it does not include the full source.</p> : null}
          <p>{entry.read ? "Read" : "Unread"} · Version {entry.revision}{entry.updated_at > entry.published_at ? " · Updated" : ""}</p>
          <Button disabled={busy} onClick={() => void act(() => open(feed.relationship_id, entry))}>Save copy and read {entry.card.title}</Button>
        </article>)}
        {feed.next_cursor ? <Button disabled={busy} onClick={() => void act(async () => { await feedApi.refresh(feed.relationship_id, feed.next_cursor); })}>Load more from {feed.node_name}</Button> : null}
      </section>)}
      <p>Saving downloads a private copy. A publisher cannot erase a copy you already saved.</p>
    </Panel>
    <Panel><h2>{editing ? "Edit publication draft" : "New publication draft"}</h2>
      <label>Content<select aria-label="Publication content" disabled={busy} value={itemId} onChange={(event) => { setItemId(event.target.value); setDirty(true); }}>
        <option value="">Choose saved or previously read content</option>
        {itemId && !items.some((row) => row.item_id === itemId) ? <option value={itemId}>{editing?.draft?.card.title ?? "Current snapshot"}</option> : null}
        {items.map((row) => <option key={row.item_id} value={row.item_id}>{row.item.title}</option>)}
      </select></label>
      <label>Visible to<select aria-label="Publication audience" disabled={busy} value={audience.mode} onChange={(event) => {
        setAudience({ mode: event.target.value as FeedAudience["mode"], relationship_ids: [] }); setDirty(true);
      }}><option value="selected">Selected friends</option><option value="all_friends">All current and future friends</option></select></label>
      {audience.mode === "selected" ? <fieldset disabled={busy}><legend>Choose who may access this content</legend>{friends.map((friend) => <label key={friend.relationship_id}>
        <input type="checkbox" checked={audience.relationship_ids.includes(friend.relationship_id)} onChange={(event) => {
          setAudience({ ...audience, relationship_ids: event.target.checked ? [...audience.relationship_ids, friend.relationship_id] : audience.relationship_ids.filter((rid) => rid !== friend.relationship_id) }); setDirty(true);
        }} />{friend.node_name}
      </label>)}</fieldset> : <p>Includes friends you add later. Publishing requires explicit confirmation.</p>}
      <Button disabled={busy || !itemId || audience.mode === "selected" && !audience.relationship_ids.length} onClick={() => void act(save)}>Save draft for review</Button>
      {editing?.draft ? <div><h3>Saved draft preview</h3><p>{editing.draft.card.title} · {editing.draft.card.source}</p>
        <p>Visible to: {editing.draft.audience.mode === "all_friends" ? "All current and future friends" : editing.draft.audience.relationship_ids.map((rid) => friends.find((friend) => friend.relationship_id === rid)?.node_name ?? "Inactive friend").join(", ")}</p>
        <p>{dirty ? "Unsaved changes: save and review before publishing." : "Draft saved. Publishing changes who can access this snapshot."}</p>
        <Button disabled={busy || dirty} onClick={publish}>Review and publish</Button>
      </div> : null}
      <Button disabled={busy} onClick={() => { setEditing(null); draftId.current = newId(); setItemId(""); setAudience(selected); setDirty(true); }}>Start another draft</Button>
    </Panel>
    <Panel><h2>Your publications</h2>
      {loaded && !publications.length ? <p>No publications or saved drafts.</p> : publications.map((row) => <article key={row.id}>
        <h3>{row.published?.card.title ?? row.draft?.card.title}</h3>
        <p>{row.stopped ? "Sharing stopped" : row.published ? `Published · Version ${row.published.revision}` : "Draft only"}</p>
        <p>Currently allowed: {row.current_audience?.map((friend) => friend.node_name).join(", ") || "Nobody"}. Permission does not indicate receipt or reading.</p>
        <Button disabled={busy} onClick={() => edit(row)}>Edit draft and audience</Button>
        {row.published && !row.stopped ? <Button disabled={busy} onClick={() => confirm({ title: "Stop sharing this publication?", risk: "medium",
          body: "New protected reads will be denied. Offline friends learn about this on reconnect. Copies already saved cannot be recalled.", confirmLabel: "Stop sharing",
          onConfirm: () => act(async () => {
            const stopped = await feedApi.stop(row.id, { expected_revision: row.revision, operation_id: op(`${row.id}:stop:${row.revision}`) });
            if (editing?.id === row.id) setEditing(stopped);
            setNotice("Sharing stopped. Previously saved copies remain with their owners.");
          }) })}>Stop sharing</Button> : null}
      </article>)}
    </Panel>
    {reading ? <ContentViewer item={reading.item} client={client} onClose={() => setReading(null)} onRead={async () => {
      await client.recordContentConsumption(reading.item, "opened");
      await feedApi.read(reading.rid, reading.entry.id, reading.entry.revision); await load();
    }} /> : null}
  </div>;
}
