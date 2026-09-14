import { Bot, Compass, Settings2, Sparkles, UploadCloud } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import ContentViewer from "../components/ContentViewer";
import {
  ActivityIcon,
  Button,
  Chip,
  EmptyState,
  LoadingPanel,
  PageHeader,
  Panel,
  RecommendationCard,
} from "../components/ui";
import type { ActivityEvent, ContentItem, Recommendation } from "../domain/types";
import { digestApi } from "../domain/digestClient";
import RecommendedServices from "./components/RecommendedServices";

type HomeSource = "activity" | "recommendations" | "content";
const sourceLabels: Record<HomeSource, string> = { activity: "activity", recommendations: "recommendations", content: "content list" };

export default function Home() {
  const { client, node, peers, notify, firstSuccess, openFirstSuccess, refreshFirstSuccess } = useAppContext();
  const navigate = useNavigate();
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [items, setItems] = useState<ContentItem[]>([]);
  const [loading, setLoading] = useState<Record<HomeSource, boolean>>({ activity: true, recommendations: true, content: true });
  const [errors, setErrors] = useState<Partial<Record<HomeSource, boolean>>>({});
  const requests = useRef<Record<HomeSource, number>>({ activity: 0, recommendations: 0, content: 0 });
  const pending = useRef<Record<HomeSource, boolean>>({ activity: false, recommendations: false, content: false });
  const [viewing, setViewing] = useState<ContentItem | null>(null);
  const [availableRecommendations, setAvailableRecommendations] = useState(0);
  const knownDiscoveryItems = useRef(0);

  const load = useCallback(async (source: HomeSource) => {
    const request = ++requests.current[source];
    const current = () => requests.current[source] === request;
    pending.current[source] = true;
    setLoading((value) => ({ ...value, [source]: true }));
    try {
      if (source === "activity") {
        const value = await client.getActivity();
        if (current()) setActivity(value);
      } else if (source === "recommendations") {
        const value = await client.requestRecommendations({ limit: 2 });
        if (current()) setRecommendations(value);
      } else {
        const value = await client.listContent();
        if (current()) setItems(value);
      }
      if (!current()) return false;
      setErrors((value) => ({ ...value, [source]: false }));
      return true;
    } catch {
      if (current()) setErrors((value) => ({ ...value, [source]: true }));
      return false;
    } finally {
      if (current()) {
        pending.current[source] = false;
        setLoading((value) => ({ ...value, [source]: false }));
      }
    }
  }, [client]);

  useEffect(() => {
    setActivity([]);
    setRecommendations([]);
    setItems([]);
    setErrors({});
    setAvailableRecommendations(0);
    knownDiscoveryItems.current = 0;
    void load("activity");
    void load("recommendations");
    void load("content");
    return () => {
      for (const source of Object.keys(sourceLabels) as HomeSource[]) requests.current[source] += 1;
    };
  }, [load]);

  useEffect(() => {
    let active = true;
    const updateDiscovery = () => {
      void digestApi.getDiscoveryStatus().then(async (status) => {
        if (!active) return;
        setAvailableRecommendations(status.item_count);
        if (status.item_count > 0 && status.item_count !== knownDiscoveryItems.current && !pending.current.recommendations) {
          if (await load("recommendations") && active) knownDiscoveryItems.current = status.item_count;
        }
      }).catch(() => undefined);
    };
    updateDiscovery();
    const timer = window.setInterval(updateDiscovery, 4000);
    return () => { active = false; window.clearInterval(timer); };
  }, [load]);

  const flagged = items.filter((item) => ["flagged", "blocked"].includes(item.safety_outcome)).length;
  const showingStarters = recommendations.length > 0 && recommendations.every((rec) => rec.item?.starter);

  return (
    <div className="screen-grid home-grid">
      {(Object.keys(sourceLabels) as HomeSource[]).some((source) => errors[source]) ? (
        <Panel title="Some home information is unavailable">
          <div role="status">
            <p>Available content stays usable. Retry the affected section.</p>
            {(Object.keys(sourceLabels) as HomeSource[]).filter((source) => errors[source]).map((source) => (
              <Button key={source} disabled={loading[source]} onClick={() => void load(source)}>Retry {sourceLabels[source]}</Button>
            ))}
          </div>
        </Panel>
      ) : null}
      <RecommendedServices client={client} />
      {firstSuccess && !firstSuccess.completed ? (
        <Panel className="home-first-success">
          <div>
            <span className="eyebrow">Start here</span>
            <h2>Get your first useful result</h2>
            <p>Open one recommendation and save one choice. Ryn uses that local signal to improve what comes next.</p>
          </div>
          <Button variant="primary" icon={Sparkles} onClick={() => openFirstSuccess?.()}>Continue first reading</Button>
        </Panel>
      ) : null}
      {firstSuccess?.completed ? (
        <Panel className="home-first-success">
          <div>
            <span className="eyebrow">Optional next step</span>
            <h2>Enable private AI on this device</h2>
            <p>Ryn can recommend a model for this computer and keep prompts local. Your recommendations already work without it.</p>
          </div>
          <Button icon={Bot} onClick={() => navigate("/services/manage")}>Set up local AI</Button>
        </Panel>
      ) : null}
      <PageHeader
        eyebrow="Ryn node"
        title="Local node console"
        context="Your private assistant, recommendation profile, digest, and peer network—all mediated by the node on this machine."
        actions={
          <>
            <Chip tone="ok">daemon online</Chip>
            <Chip tone="info">{node.peer_count} peers</Chip>
          </>
        }
      />

      <Panel className="home-hero">
        <div>
          <span className="eyebrow">This node</span>
          <h2>{node.node_name}</h2>
          <p className="mono">{node.peer_id}</p>
        </div>
        <div className="hero-actions">
          <Button onClick={() => navigate("/reading")}>Continue reading</Button>
          <Button icon={Compass} onClick={() => navigate("/explore")}>
            Explore
          </Button>
          <Button icon={Sparkles} onClick={() => navigate("/ask")}>
            Ask AI
          </Button>
          <Button icon={UploadCloud} variant="primary" onClick={() => navigate("/publish")}>
            Publish
          </Button>
          <Button icon={Settings2} onClick={() => navigate("/settings")}>
            Settings
          </Button>
        </div>
      </Panel>

      <div className="stat-grid">
        <StatTile label="Local items" value={node.local_items} to="/explore?source=local" />
        <StatTile label="Fetched" value={node.fetched_items} to="/explore?source=fetched" />
        <StatTile
          label="Available recs"
          value={Math.max(availableRecommendations, recommendations.length)}
          to="/digest"
        />
        <StatTile label="Flagged" value={errors.content ? "Unavailable" : loading.content ? "Loading…" : flagged} to="/explore?safety=flagged" tone={flagged ? "warn" : "neutral"} />
      </div>

      <Panel title={showingStarters ? "Start here: teach your assistant" : "Curator Highlights"} className="home-recs">
        {recommendations.length ? (
          <>
            {showingStarters ? (
              <div className="home-starter-note">
                <div>
                  <strong>Ryn is collecting the first live recommendations.</strong>
                  <p>The background agent is reviewing its built-in public catalog now; this section updates automatically when real content is ready.</p>
                </div>
                <Button icon={Sparkles} onClick={() => navigate("/digest")}>Open For You</Button>
              </div>
            ) : null}
            <div className="rec-stack compact">
            {recommendations.map((rec) => {
              const item = rec.item ?? items.find((candidate) => candidate.content_id === rec.contentId);
              if (!item) return null;
              const publisher = peers.find((peer) => peer.id === item.publisher_peer_id);
              return (
                <RecommendationCard
                  key={rec.id}
                  rec={rec}
                  item={item}
                  publisher={publisher}
                  onInspect={() => navigate(`/items/${item.content_id}`)}
                  onOpen={() => {
                    setViewing(item);
                  }}
                  onFetchPreview={() => notify("info", "Preview fetch requested through local node")}
                  onFetchFull={() => notify("warn", "Full fetch requires confirmation from item detail")}
                  onFeedback={async (action) => {
                    await client.submitRecommendationFeedback(item.content_id, action);
                    if (item.digest_item_id) {
                      void digestApi.sendFeedback(item.digest_item_id, action === "more" ? "up" : "down").catch(() => undefined);
                    }
                    await load("recommendations");
                    notify("ok", "Your local recommendation profile learned from that feedback");
                  }}
                />
              );
            })}
            </div>
          </>
        ) : loading.recommendations ? <LoadingPanel /> : errors.recommendations ? (
          <p>Recommendations could not be loaded. Use Retry recommendations above.</p>
        ) : <EmptyState title="No recommendations yet" body="Open For You to check content sources and retry. Reading does not require a model." />}
      </Panel>
      {viewing ? <ContentViewer item={viewing} client={client} onRead={async () => {
        await client.recordContentConsumption(viewing, "opened");
        if (viewing.digest_item_id) await digestApi.sendFeedback(viewing.digest_item_id, "opened");
        await refreshFirstSuccess?.();
      }} onClose={() => setViewing(null)} /> : null}

      <Panel title="Recent Activity" className="activity-panel">
        {loading.activity ? <p role="status">Loading activity…</p> : !activity.length && !errors.activity ? <p>No activity yet.</p> : null}
        <div className="activity-list">
          {activity.map((event) => (
            <div key={`${event.t}-${event.text}`} className="activity-row">
              <span className="activity-icon">
                <ActivityIcon kind={event.kind} />
              </span>
              <span>{event.text}</span>
              <b>{event.t}</b>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function StatTile({
  label,
  value,
  to,
  tone = "neutral",
}: {
  label: string;
  value: number | string;
  to: string;
  tone?: "neutral" | "warn";
}) {
  return (
    <Link className={`stat-tile stat-${tone}`} to={to}>
      <span>{label}</span>
      <strong className="mono">{value}</strong>
    </Link>
  );
}
