import { Clock3, Network, Newspaper, RefreshCcw, Save, SlidersHorizontal, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import ContentViewer from "../components/ContentViewer";
import { Button, Chip, EmptyState, LoadingPanel, PageHeader, Panel, RecommendationCard } from "../components/ui";
import { digestApi, type DiscoveryStatus } from "../domain/digestClient";
import type { ContentItem, NodeSettings, Recommendation, RecommendationProfile } from "../domain/types";

export default function Recommendations() {
  const { client, peers, confirm, notify } = useAppContext();
  const navigate = useNavigate();
  const [items, setItems] = useState<ContentItem[]>([]);
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [profile, setProfile] = useState<RecommendationProfile | null>(null);
  const [settings, setSettings] = useState<NodeSettings | null>(null);
  const [direction, setDirection] = useState("");
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [discovery, setDiscovery] = useState<DiscoveryStatus | null>(null);
  const [viewing, setViewing] = useState<ContentItem | null>(null);
  const knownDiscoveryItems = useRef(0);

  const refresh = async () => {
    setLoading(true);
    const [content, recs, nextProfile, nodeSettings, discoveryStatus] = await Promise.all([
      client.listContent(),
      client.requestRecommendations({ limit: 6 }),
      client.getRecommendationProfile(),
      client.getSettings(),
      digestApi.getDiscoveryStatus().catch(() => null),
    ]);
    setItems(content);
    setRecommendations(recs);
    setProfile(nextProfile);
    setSettings(nodeSettings);
    setDirection(nextProfile.direction);
    setLastRefreshed(new Date());
    setDiscovery(discoveryStatus);
    knownDiscoveryItems.current = discoveryStatus?.item_count ?? 0;
    setLoading(false);
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  const refreshNow = async () => {
    setLoading(true);
    await digestApi.refreshDigest().catch(() => undefined);
    await refresh();
  };

  useEffect(() => {
    const timer = window.setInterval(() => {
      void digestApi.getDiscoveryStatus().then(async (status) => {
        setDiscovery(status);
        if (status.item_count > 0 && status.item_count !== knownDiscoveryItems.current) {
          knownDiscoveryItems.current = status.item_count;
          setRecommendations(await client.requestRecommendations({ limit: 6 }));
          setLastRefreshed(new Date());
        }
      }).catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [client]);

  if (loading) return <LoadingPanel />;

  const saveProfile = async (patch: Partial<Pick<RecommendationProfile, "direction" | "topics" | "platforms">>) => {
    const next = await client.updateRecommendationProfile(patch);
    setProfile(next);
    setDirection(next.direction);
    setRecommendations(await client.requestRecommendations({ limit: 6 }));
    notify("ok", "Recommendation direction saved locally");
  };

  const toggleChoice = (field: "topics" | "platforms", id: string) => {
    if (!profile) return;
    const current = profile[field];
    const next = current.includes(id) ? current.filter((value) => value !== id) : [...current, id];
    void saveProfile({ [field]: next, direction });
  };

  const starterCount = recommendations.filter((rec) => rec.item?.starter).length;
  const discoveryCount = recommendations.filter((rec) => rec.item?.external).length;
  const meshCount = recommendations.length - starterCount - discoveryCount;
  const otherPeerCount = peers.filter((peer) => !peer.isSelf).length;
  const showingStarters = starterCount > 0 && meshCount === 0 && discoveryCount === 0;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Personal assistant"
        title="Recommendations"
        context="See exactly what is ready now, what Ryn is waiting for, and how your local feedback changes future ranking."
        actions={
          <>
            <Chip tone={settings?.ai_provider === "local" ? "ok" : "info"}>
              {settings?.ai_model || "ranking engine"}
            </Chip>
            {lastRefreshed ? <Chip tone="muted">checked {lastRefreshed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</Chip> : null}
            <Button icon={RefreshCcw} onClick={() => void refreshNow()}>
              Refresh
            </Button>
          </>
        }
      />
      <Panel className="recommendation-status-panel">
        <div className="recommendation-status-heading">
          <div>
            <span className="eyebrow">Recommendation status</span>
            <h2>{showingStarters ? "Ryn is collecting your first real recommendations" : "Ryn has content ready for you"}</h2>
            <p>
              {showingStarters
                ? "The background agent is reviewing the built-in public source catalog now. This screen updates automatically when the first real items are ranked."
                : `${discoveryCount} public-content pick${discoveryCount === 1 ? " is" : "s are"} ready${meshCount ? `, plus ${meshCount} mesh item${meshCount === 1 ? "" : "s"}` : ""}.`}
            </p>
          </div>
          <Chip tone={showingStarters ? "warn" : "ok"}>{showingStarters ? discovery?.phase === "refreshing" ? "discovering now" : "starting agent" : "content ready"}</Chip>
        </div>
        <div className="recommendation-readiness-grid">
          <div>
            <Sparkles size={18} />
            <span>Proactive discovery</span>
            <strong>{discovery?.phase === "refreshing" ? "Reviewing now" : `${discovery?.item_count ?? discoveryCount} items ready`}</strong>
            <p>Ryn reviews built-in YouTube, Reddit, research, news, podcast, audiobook, and visual sources without requiring setup.</p>
          </div>
          <div>
            <Newspaper size={18} />
            <span>Personalization</span>
            <strong>Optional</strong>
            <p>Use feedback and written direction immediately. Adding a channel, community, or feed only expands what the agent watches.</p>
            <Button onClick={() => navigate("/digest")}>Open content feed</Button>
          </div>
          <div>
            <Network size={18} />
            <span>Mesh recommendations</span>
            <strong>{meshCount ? `${meshCount} ready` : "Waiting for published peer content"}</strong>
            <p>{otherPeerCount ? `${otherPeerCount} other node${otherPeerCount === 1 ? " is" : "s are"} visible; content appears after one publishes.` : "No fixed delay: another node must connect and publish first."}</p>
            <Button onClick={() => navigate("/peers")}>Inspect peers</Button>
          </div>
        </div>
        <div className="recommendation-runtime-note">
          <Clock3 size={15} />
          The daemon now refreshes the public catalog automatically about every 30 minutes. Refresh requests an immediate review; feedback reshapes the next ranking locally.
        </div>
      </Panel>
      {profile ? (
        <Panel className="recommendation-profile-panel">
          <div className="recommendation-profile-heading">
            <div>
              <span className="eyebrow">Personal recommendation agent</span>
              <h2>Tell Ryn what deserves your attention</h2>
              <p>
                Everything is stored on this node. With no choices, Ryn explores broadly; your direction and feedback gradually reshape the ranking.
              </p>
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
              placeholder="For example: Focus on local AI agents, open-source tools, and serious research. Avoid repetitive trend coverage."
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
            <Button
              variant="ghost"
              onClick={() => void saveProfile({ direction: "", topics: [], platforms: [] })}
            >
              Explore broadly
            </Button>
          </div>
        </Panel>
      ) : null}
      {recommendations.length ? (
        <section className="recommendation-results">
          <div className="recommendation-results-heading">
            <div>
              <span className="eyebrow">{showingStarters ? "Preparing real content" : "Ranked recommendations"}</span>
              <h2>{showingStarters ? "A live slate will appear automatically" : "Worth your attention"}</h2>
            </div>
            <Chip tone={showingStarters ? "muted" : "ok"}>{recommendations.length} shown</Chip>
          </div>
          <div className="rec-stack">
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
                onFetchPreview={async () => {
                  await client.fetchPreview(item.content_id, item.provider_peer_id);
                  notify("ok", "Preview fetch requested through local node");
                }}
                onFetchFull={() =>
                  confirm({
                    title: "Fetch full recommended item?",
                    body: "Full content fetches are high-risk because they use bandwidth and local storage. The node will verify receipts before storing bytes.",
                    risk: "high",
                    confirmLabel: "Fetch full",
                    details: [
                      { label: "Item", value: item.title },
                      { label: "Provider", value: item.provider_peer_id },
                      { label: "Size", value: item.size ?? "unknown" },
                    ],
                    onConfirm: async () => {
                      await client.fetchFullContent(item.content_id, item.provider_peer_id);
                      notify("ok", "Full fetch requested through local node");
                    },
                  })
                }
                onFeedback={async (action) => {
                  const next = await client.submitRecommendationFeedback(item.content_id, action);
                  if (item.digest_item_id) {
                    void digestApi.sendFeedback(item.digest_item_id, action === "more" ? "up" : "down").catch(() => undefined);
                  }
                  setProfile(next);
                  setRecommendations(await client.requestRecommendations({ limit: 6 }));
                  notify("ok", "Feedback saved; the local profile has been updated");
                }}
              />
            );
          })}
          </div>
        </section>
      ) : (
        <EmptyState title="No recommendations" body="Ask the curator to review visible node evidence." />
      )}
      {viewing ? <ContentViewer item={viewing} client={client} onRead={async () => {
        await client.recordContentConsumption(viewing, "opened");
        if (viewing.digest_item_id) await digestApi.sendFeedback(viewing.digest_item_id, "opened");
      }} onClose={() => setViewing(null)} /> : null}
    </div>
  );
}
