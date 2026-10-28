import SourceHealthPanel from "../components/SourceHealthPanel";
import FeedbackSignalsPanel from "../components/FeedbackSignalsPanel";
import ContentViewer from "../components/ContentViewer";
import { contentFromHistory } from "../domain/readingHistory";
import { AlertTriangle, Bookmark, CheckCircle2, Clock3, Eye, ExternalLink, Plus, RefreshCcw, Save, SlidersHorizontal, Sparkles, ThumbsDown, ThumbsUp, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, EmptyState, EvidenceDetails, IconButton, LoadingPanel, NavIcons, PageHeader, Panel } from "../components/ui";
import DigestViewer, { type ViewerAction } from "../components/DigestViewer";
import {
  digestApi,
  type AiStatus,
  type ConsumptionRecord,
  type DiscoveryStatus,
  type Digest as DigestPayload,
  type DigestItem,
  type DigestSource,
  type Watcher,
} from "../domain/digestClient";
import type { ContentItem, RecommendationProfile } from "../domain/types";

function timeAgo(unix: number): string {
  if (!unix) return "";
  const hours = (Date.now() / 1000 - unix) / 3600;
  if (hours < 1) return "just now";
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

function timeUntil(unix: number): string {
  if (!unix) return "not scheduled";
  const seconds = Math.max(0, unix - Date.now() / 1000);
  if (seconds < 90) return "within a minute";
  if (seconds < 3600) return `in ${Math.ceil(seconds / 60)} minutes`;
  return `in ${Math.ceil(seconds / 3600)} hours`;
}

function DigestCard({
  item,
  onFeedback,
  onOpen,
}: {
  item: DigestItem;
  onFeedback: (item: DigestItem, action: ViewerAction) => Promise<void>;
  onOpen: (item: DigestItem) => void;
}) {
  const [pending, setPending] = useState(false);
  const [feedbackError, setFeedbackError] = useState("");
  const feedback = async (action: ViewerAction) => {
    setPending(true);
    setFeedbackError("");
    try { await onFeedback(item, action); }
    catch { setFeedbackError("Feedback could not be confirmed. Please retry."); }
    finally { setPending(false); }
  };
  return (
    <Panel className="digest-card">
      <div className="digest-card-main">
        {item.thumbnail ? (
          <button className="digest-thumb-button" type="button" onClick={() => onOpen(item)}>
            <img className="digest-thumb" src={item.thumbnail} alt="" loading="lazy" />
          </button>
        ) : null}
        <div className="digest-card-body">
          <div className="digest-source-line">
            <Chip tone="info">{item.source_kind}</Chip>
            <span className="digest-source-title">{item.source_title}</span>
            <span className="digest-when">{timeAgo(item.published_unix)}</span>
          </div>
          <button
            type="button"
            className="digest-title"
            onClick={() => onOpen(item)}
          >
            {item.title || item.link}
          </button>
          {item.ai_summary ? (
            <p className="digest-summary digest-ai-summary">
              <Sparkles size={12} /> {item.ai_summary}
            </p>
          ) : item.summary ? (
            <p className="digest-summary">{item.summary}</p>
          ) : null}
          <div className="digest-foot">
            <div className="chip-row">
              {item.reasons.map((reason) => (
                <Chip key={reason} tone="muted">
                  {reason}
                </Chip>
              ))}
            </div>
            <div className="digest-actions">
              <IconButton
                icon={ThumbsUp}
                label="More like this"
                onClick={() => void feedback("up")}
                disabled={pending}
              />
              <IconButton
                icon={ThumbsDown}
                label="Less like this"
                onClick={() => void feedback("down")}
                disabled={pending}
              />
              <Button disabled={pending} onClick={() => void feedback("hide")}>Hide</Button>
              <IconButton
                icon={ExternalLink}
                label="Open"
                onClick={() => {
                  onOpen(item);
                }}
              />
            </div>
          </div>
          {feedbackError ? <p role="alert">{feedbackError}</p> : null}
          <EvidenceDetails packet={item.evidence_packet} />
        </div>
      </div>
    </Panel>
  );
}

export default function Digest() {
  const { client, notify } = useAppContext();
  const [digest, setDigest] = useState<DigestPayload | null>(null);
  const [sources, setSources] = useState<DigestSource[]>([]);
  const [watchers, setWatchers] = useState<Watcher[]>([]);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [newSource, setNewSource] = useState("");
  const [saveUrl, setSaveUrl] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [feedbackRevision, setFeedbackRevision] = useState(0);
  const [discovery, setDiscovery] = useState<DiscoveryStatus | null>(null);
  const [viewer, setViewer] = useState<{ items: DigestItem[]; index: number } | null>(null);
  const [meshViewer, setMeshViewer] = useState<ContentItem | null>(null);
  const [consumption, setConsumption] = useState<ConsumptionRecord[]>([]);
  const [profile, setProfile] = useState<RecommendationProfile | null>(null);
  const [direction, setDirection] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const [digestPayload, sourceList, watcherList, ai, status, recommendationProfile, consumptionRecords] = await Promise.all([
        digestApi.getDigest(),
        digestApi.listSources(),
        digestApi.listWatchers(),
        digestApi.aiStatus().catch(() => null),
        digestApi.markDiscoverySeen().catch(() => null),
        client.getRecommendationProfile(),
        digestApi.listConsumption(),
      ]);
      setDigest(digestPayload);
      setSources(sourceList);
      setWatchers(watcherList);
      setAiStatus(ai);
      setDiscovery(status);
      setProfile(recommendationProfile);
      setDirection(recommendationProfile.direction);
      setConsumption(consumptionRecords);
      window.dispatchEvent(new Event("ryn-discovery-seen"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reach the local node.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const refresh = async () => {
    setRefreshing(true);
    setError("");
    try {
      const result = await digestApi.refreshDigest();
      setDigest(result.digest);
      setDiscovery(result.status);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Refresh failed.");
    } finally {
      setRefreshing(false);
    }
  };

  const addSource = async () => {
    const value = newSource.trim();
    if (!value) return;
    setAdding(true);
    setError("");
    try {
      await digestApi.addSource(value);
      setNewSource("");
      setSources(await digestApi.listSources());
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add source.");
    } finally {
      setAdding(false);
    }
  };

  const removeSource = async (sourceId: string) => {
    try {
      await digestApi.removeSource(sourceId);
      setSources((current) => current.filter((source) => source.id !== sourceId));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove source.");
    }
  };

  const refreshFeedback = async () => {
    const [nextProfile, nextDigest] = await Promise.all([
      client.getRecommendationProfile(), digestApi.getDigest(),
    ]);
    setProfile(nextProfile);
    setDigest(nextDigest);
  };

  const onFeedback = async (item: DigestItem, action: ViewerAction) => {
    if (action === "opened") {
      await digestApi.recordConsumption(item, "opened");
      setConsumption(await digestApi.listConsumption());
      try { await digestApi.sendFeedback(item.item_id, "opened"); }
      catch (cause) {
        // A saved article can outlive the recommendation that introduced it.
        // Its durable reading record does not depend on updating feed signals.
        if (!(cause instanceof Error && cause.message === "feedback_item_unknown")) {
          setNotice("Reading history was saved, but the recommendation signal could not be updated.");
        }
      }
      return;
    }
    await digestApi.sendFeedback(item.item_id, action);
    setFeedbackRevision((value) => value + 1);
    await refreshFeedback();
  };

  const updateConsumption = async (
    item: DigestItem,
    action: "bookmark" | "unbookmark" | "progress" | "completed",
    progress?: number,
  ) => {
    await digestApi.recordConsumption(item, action, progress);
    setConsumption(await digestApi.listConsumption());
  };

  const saveProfile = async (
    patch: Partial<Pick<RecommendationProfile, "direction" | "topics" | "platforms">>,
  ) => {
    const next = await client.updateRecommendationProfile(patch);
    setProfile(next);
    setDirection(next.direction);
    setDigest(await digestApi.getDigest());
    notify("ok", "Your local For You profile has been updated");
  };

  const toggleChoice = (field: "topics" | "platforms", id: string) => {
    if (!profile) return;
    const current = profile[field];
    const next = current.includes(id) ? current.filter((value) => value !== id) : [...current, id];
    void saveProfile({ [field]: next });
  };

  const saveForLater = async () => {
    const value = saveUrl.trim();
    if (!value) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await digestApi.saveReadLater(value);
      setSaveUrl("");
      setNotice(`Saved "${result.title}" — it'll lead your next digest.`);
      setDigest(await digestApi.getDigest().catch(() => digest));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the link.");
    } finally {
      setSaving(false);
    }
  };

  const watchPage = async () => {
    const value = saveUrl.trim();
    if (!value) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const watcher = await digestApi.addWatcher(value, "");
      setSaveUrl("");
      setWatchers((current) => [...current, watcher]);
      setNotice(`Watching "${watcher.title}" — changes will appear in your digest.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the watcher.");
    } finally {
      setSaving(false);
    }
  };

  const removeWatcher = async (watcherId: string) => {
    try {
      await digestApi.removeWatcher(watcherId);
      setWatchers((current) => current.filter((watcher) => watcher.id !== watcherId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the watcher.");
    }
  };

  if (loading) return <LoadingPanel />;

  const items = digest?.items ?? [];
  const allSourcesUnavailable = !!discovery?.source_count && discovery.failed_sources === discovery.source_count;
  const syncedReading = viewer ? consumption.find((record) => record.item_id === viewer.items[viewer.index]?.item_id
    && record.sync_revisions?.reading && !["video", "audio", "image"].includes(record.item.content_kind ?? "document")) : undefined;
  const generated = digest?.generated_at_unix ? timeAgo(digest.generated_at_unix) : null;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Personal assistant"
        title="For You"
        context="One local feed for public discovery, mesh recommendations, your interests, and every feedback signal."
        actions={
          <>
            {aiStatus?.provider ? (
              <Chip tone="ok">{aiStatus.provider}: {aiStatus.model}</Chip>
            ) : (
              <Chip tone="muted">no AI — run Ollama or add a key</Chip>
            )}
            {discovery?.phase === "refreshing" ? <Chip tone="info">agent discovering</Chip> : null}
            {generated ? <Chip tone="muted">built {generated}</Chip> : null}
            <Button icon={RefreshCcw} onClick={() => void refresh()} disabled={refreshing}>
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </>
        }
      />

      {digest?.brief ? (
        <Panel title="Briefing" className="digest-brief">
          <p className="digest-brief-basis">
            <Sparkles size={12} /> AI synthesis from titles and public-feed summaries only. Numbered references point to items below.
          </p>
          <p className="digest-brief-text">{digest.brief}</p>
        </Panel>
      ) : null}

      <Panel className="recommendation-status-panel">
        <div className="recommendation-status-heading">
          <div>
            <span className="eyebrow">Discovery health</span>
            <h2>{discovery?.item_count ? `${discovery.item_count} items are ready` : allSourcesUnavailable ? "Content sources are unavailable" : "Ryn is collecting your first items"}</h2>
            <p>{discovery?.message || "The background agent is preparing its first zero-setup review."}</p>
          </div>
          <Chip tone={discovery?.phase === "error" ? "danger" : discovery?.degraded ? "warn" : discovery?.item_count ? "ok" : "info"}>
            {discovery?.phase === "refreshing" ? "reviewing now" : allSourcesUnavailable ? discovery?.cached_sources ? "using cached content" : "waiting for connection" : discovery?.degraded ? "using healthy sources" : discovery?.item_count ? "ready" : "starting"}
          </Chip>
        </div>
        <div className="recommendation-readiness-grid">
          <div>
            <CheckCircle2 size={18} />
            <span>Public sources</span>
            <strong>{discovery ? `${discovery.healthy_sources}/${discovery.source_count} healthy` : "Checking"}</strong>
            <p>{discovery?.cached_sources ? `${discovery.cached_sources} unavailable source${discovery.cached_sources === 1 ? " is" : "s are"} serving cached items.` : allSourcesUnavailable ? "No cached recommendations are available. Check your connection and use Refresh to retry." : "Each source is checked independently, so one failure cannot blank your feed."}</p>
          </div>
          <div>
            <Clock3 size={18} />
            <span>Background schedule</span>
            <strong>{timeUntil(discovery?.next_refresh_unix ?? 0)}</strong>
            <p>{discovery?.last_completed_unix ? `Last completed ${timeAgo(discovery.last_completed_unix)}.` : "The first review starts automatically after the daemon is ready."}</p>
          </div>
          <div>
            <Sparkles size={18} />
            <span>Formats available</span>
            <strong>{discovery?.formats.length ? discovery.formats.join(", ") : "Collecting"}</strong>
            <p>No account, API key, YouTube handle, subreddit, or preference setup is required.</p>
          </div>
        </div>
        {discovery?.failed_sources ? (
          <div className="recommendation-runtime-note">
            <AlertTriangle size={15} />
            {allSourcesUnavailable ? "All sources are temporarily unavailable. Check your connection and use Refresh to retry." : `${discovery.failed_sources} source${discovery.failed_sources === 1 ? " is" : "s are"} temporarily unavailable. Ryn kept the remaining feed usable and scheduled an earlier retry.`}
          </div>
        ) : null}
      </Panel>

      {profile ? (
        <Panel className="recommendation-profile-panel">
          <div className="recommendation-profile-heading">
            <div>
              <span className="eyebrow">Your private recommendation profile</span>
              <h2>Tell Ryn what deserves your attention</h2>
              <p>With no choices, Ryn explores broadly. These preferences and every feedback action now drive this same feed.</p>
            </div>
            <Chip tone={profile.feedback_count ? "ok" : "muted"} icon={SlidersHorizontal}>
              {profile.feedback_count} feedback {profile.feedback_count === 1 ? "signal" : "signals"}
            </Chip>
          </div>
          <label className="recommendation-direction">
            <span>Direction</span>
            <textarea
              value={direction}
              onChange={(event) => setDirection(event.target.value)}
              placeholder="For example: More local AI and open-source research, less repetitive trend coverage."
              rows={3}
            />
          </label>
          <div className="recommendation-choice-group">
            <span>Topics</span>
            <div className="choice-chip-row">
              {profile.topic_choices.map((choice) => (
                <button
                  key={choice.id}
                  type="button"
                  className={profile.topics.includes(choice.id) ? "choice-chip active" : "choice-chip"}
                  onClick={() => toggleChoice("topics", choice.id)}
                >
                  {choice.label}
                </button>
              ))}
            </div>
          </div>
          <div className="recommendation-choice-group">
            <span>Platforms and sources</span>
            <div className="choice-chip-row">
              {profile.platform_choices.map((choice) => (
                <button
                  key={choice.id}
                  type="button"
                  className={profile.platforms.includes(choice.id) ? "choice-chip active" : "choice-chip"}
                  onClick={() => toggleChoice("platforms", choice.id)}
                >
                  {choice.label}
                </button>
              ))}
            </div>
          </div>
          <div className="button-row">
            <Button variant="primary" icon={Save} onClick={() => void saveProfile({ direction })}>
              Save direction
            </Button>
            <Button variant="ghost" onClick={() => void saveProfile({ direction: "", topics: [], platforms: [] })}>
              Explore broadly
            </Button>
          </div>
        </Panel>
      ) : null}

      <Panel title="Sources Ryn watches">
        <FeedbackSignalsPanel revision={feedbackRevision} onRefresh={refreshFeedback} />
        <SourceHealthPanel status={discovery} onRefresh={async () => {
          const [nextDigest, nextStatus] = await Promise.all([digestApi.getDigest(), digestApi.getDiscoveryStatus()]);
          setDigest(nextDigest); setDiscovery(nextStatus);
        }} />
        <p className="digest-hint digest-default-note">
          Ryn starts with a broad public catalog automatically. Add a source only when you want more from a particular channel, community, or publication.
        </p>
        <div className="digest-add-row">
          <input
            className="digest-add-input"
            placeholder="Paste a YouTube channel, @handle, r/subreddit, or RSS URL"
            value={newSource}
            onChange={(event) => setNewSource(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void addSource();
            }}
          />
          <Button icon={Plus} variant="primary" onClick={() => void addSource()} disabled={adding}>
            {adding ? "Checking…" : "Add"}
          </Button>
        </div>
        {sources.length ? (
          <div className="chip-row digest-source-chips">
            {sources.map((source) => {
              const health = digest?.sources.find((entry) => entry.id === source.id);
              return (
                <span key={source.id} className="digest-source-chip">
                  <Chip tone={health && !health.ok ? "danger" : "ok"}>
                    {source.title}
                    {source.builtin ? " · default" : ""}
                    {health && !health.ok ? " — unreachable" : ""}
                  </Chip>
                  <IconButton icon={Trash2} label={`Remove ${source.title}`} onClick={() => void removeSource(source.id)} />
                </span>
              );
            })}
          </div>
        ) : (
          <p className="digest-hint">
            The agent is restoring its default public catalog. You do not need to add anything.
          </p>
        )}
        <div className="digest-add-row digest-save-row">
          <input
            className="digest-add-input"
            placeholder="Save a link for later, or watch a page for changes"
            value={saveUrl}
            onChange={(event) => setSaveUrl(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void saveForLater();
            }}
          />
          <Button icon={Bookmark} onClick={() => void saveForLater()} disabled={saving}>
            Read later
          </Button>
          <Button icon={Eye} onClick={() => void watchPage()} disabled={saving}>
            Watch
          </Button>
        </div>
        {watchers.length ? (
          <div className="chip-row digest-source-chips">
            {watchers.map((watcher) => (
              <span key={watcher.id} className="digest-source-chip">
                <Chip tone="info">watching: {watcher.note || watcher.title}</Chip>
                <IconButton
                  icon={Trash2}
                  label={`Stop watching ${watcher.title}`}
                  onClick={() => void removeWatcher(watcher.id)}
                />
              </span>
            ))}
          </div>
        ) : null}
        {notice ? <p className="digest-hint">{notice}</p> : null}
        {error ? <p className="digest-error">{error}</p> : null}
      </Panel>

      {items.length ? (
        <div className="digest-stack">
          {items.map((item, position) => (
            <DigestCard
              key={item.item_id}
              item={item}
              onFeedback={onFeedback}
              onOpen={() => setViewer({ items, index: position })}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={NavIcons.digest}
          title={allSourcesUnavailable ? "No content available yet" : sources.length ? "The agent is reviewing fresh content" : "Starting proactive discovery"}
          body={
            allSourcesUnavailable ? "Check your connection and use Refresh to retry. No model is required." : sources.length
              ? "This page updates after the current review. You can request an immediate refresh above."
              : "Default sources are installed automatically; no setup is required."
          }
        />
      )}
      {consumption.length ? (
        <Panel title="Saved and recently opened">
          <div className="digest-stack">
            {consumption.slice(0, 8).map((record) => (
              <button
                key={record.item_id}
                type="button"
                className="digest-title"
                onClick={() => setMeshViewer(contentFromHistory(record))}
              >
                {record.item.title}
                {record.bookmarked ? " · saved" : ""}
                {record.completed ? " · completed" : record.progress ? ` · ${Math.round(record.progress * 100)}%` : ""}
              </button>
            ))}
          </div>
        </Panel>
      ) : null}
      {syncedReading ? <ContentViewer key={syncedReading.item_id} item={contentFromHistory(syncedReading)} client={client}
        onRead={() => client.recordContentConsumption(contentFromHistory(syncedReading), "opened")}
        onClose={() => { setViewer(null); void digestApi.listConsumption().then(setConsumption).catch(() => setError("Reading history could not be refreshed.")); }} /> : viewer && viewer.items[viewer.index] ? (
        <DigestViewer
          items={viewer.items}
          index={viewer.index}
          onIndexChange={(index) => setViewer((current) => current ? { ...current, index } : null)}
          onClose={() => setViewer(null)}
          onFeedback={onFeedback}
          bookmarked={Boolean(consumption.find((record) => record.item_id === viewer.items[viewer.index].item_id)?.bookmarked)}
          initialProgress={consumption.find((record) => record.item_id === viewer.items[viewer.index].item_id)?.progress ?? 0}
          onBookmark={(item, bookmarked) => updateConsumption(item, bookmarked ? "bookmark" : "unbookmark")}
          onProgress={(item, progress) => updateConsumption(item, "progress", progress)}
          onSteer={async (text) => {
            await digestApi.steer(text);
            setNotice("Got it — Ryn will use that from the next refresh.");
          }}
        />
      ) : null}
      {meshViewer ? <ContentViewer item={meshViewer} client={client} onRead={async () => {
        await client.recordContentConsumption(meshViewer, "opened");
        setConsumption(await digestApi.listConsumption());
      }} onClose={() => setMeshViewer(null)} /> : null}
    </div>
  );
}
