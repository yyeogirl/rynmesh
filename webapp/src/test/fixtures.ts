import type {
  ConsumptionRecord,
  Digest,
  DigestItem,
  DigestSource,
  DiscoveryStatus,
  ReaderArticle,
} from "../domain/digestClient";
import type { RecommendationProfile } from "../domain/types";

export const TEST_API_BASE = "http://localhost/api/local";
export const FIXED_UNIX = 1_786_950_000;

export function makeDigestItem(overrides: Partial<DigestItem> = {}): DigestItem {
  const itemId = overrides.item_id ?? "item-article";
  const link = overrides.link ?? "https://example.test/article";
  return {
    item_id: itemId,
    source_id: "source-rss",
    source_title: "Ryn Research",
    source_kind: "rss",
    title: "A local-first assistant worth reading",
    link,
    summary: "A deterministic summary for the recommendation card.",
    ai_summary: "",
    ai_summary_grounding_version: 0,
    author: "Rynmesh",
    thumbnail: "",
    media_url: "",
    content_kind: "document",
    content_type: "text/html",
    tags: ["local-first", "open-source"],
    published_unix: FIXED_UNIX - 3600,
    score: 0.91,
    reasons: ["Matches local-first"],
    review_basis: "preview",
    safety_outcome: "unscanned",
    provenance_status: "unsigned",
    evidence_packet: {
      version: 1,
      content_id: itemId,
      review_basis: "preview",
      reviewed_at_unix: FIXED_UNIX,
      source: { name: "Ryn Research", platform: "rss", url: link },
      signals: [{ kind: "topic", label: "Local-first" }],
      observations: [{ field: "title", label: "Title", value: "local-first assistant" }],
      citations: [{ kind: "source", label: "Original", url: link }],
      limitations: ["Fixture metadata only"],
    },
    ...overrides,
  };
}

export function makeDigest(items: DigestItem[] = [makeDigestItem()]): Digest {
  return {
    generated_at_unix: FIXED_UNIX,
    brief: "A deterministic daily briefing.",
    ai: null,
    items,
    sources: [
      {
        id: "source-rss",
        title: "Ryn Research",
        ok: true,
        error: "",
        item_count: items.length,
        last_checked_unix: FIXED_UNIX,
        last_success_unix: FIXED_UNIX,
        consecutive_failures: 0,
        using_cached_items: false,
      },
    ],
  };
}

export function makeDiscoveryStatus(overrides: Partial<DiscoveryStatus> = {}): DiscoveryStatus {
  return {
    phase: "ready",
    message: "Your latest local ranking is ready.",
    last_started_unix: FIXED_UNIX - 30,
    last_completed_unix: FIXED_UNIX,
    next_refresh_unix: FIXED_UNIX + 1800,
    new_items: 1,
    unread_count: 1,
    item_count: 1,
    source_count: 1,
    formats: ["article"],
    healthy_sources: 1,
    failed_sources: 0,
    cached_sources: 0,
    degraded: false,
    offline_ready: true,
    source_health: [],
    ...overrides,
  };
}

export function makeProfile(overrides: Partial<RecommendationProfile> = {}): RecommendationProfile {
  return {
    version: 1,
    direction: "",
    topics: [],
    platforms: [],
    feedback: {},
    updated_at: "2026-08-17T08:00:00Z",
    topic_choices: [
      { id: "local-ai", label: "Local AI" },
      { id: "open-source", label: "Open source" },
    ],
    platform_choices: [
      { id: "rss", label: "RSS" },
      { id: "youtube", label: "YouTube" },
    ],
    learned_signals: 0,
    feedback_count: 0,
    ...overrides,
  };
}

export const DEFAULT_SOURCES: DigestSource[] = [
  {
    id: "source-rss",
    kind: "rss",
    feed_url: "https://example.test/feed.xml",
    title: "Ryn Research",
    tags: ["research"],
    weight: 1,
    builtin: true,
    content_kind: "document",
  },
];

export const ARTICLE_FIXTURE: ReaderArticle = {
  url: "https://example.test/article",
  title: "A local-first assistant worth reading",
  byline: "Rynmesh Test Author",
  lead_image: "",
  blocks: [
    { tag: "p", text: "This article body came from the mocked local node." },
    { tag: "h2", text: "A deterministic section" },
  ],
  word_count: 12,
  cached: true,
};

export function makeConsumptionRecord(
  item: DigestItem,
  overrides: Partial<ConsumptionRecord> = {},
): ConsumptionRecord {
  return {
    item_id: item.item_id,
    item,
    first_opened_unix: FIXED_UNIX,
    last_opened_unix: FIXED_UNIX,
    last_activity_unix: FIXED_UNIX,
    open_count: 1,
    bookmarked: false,
    progress: 0,
    completed: false,
    ...overrides,
  };
}
