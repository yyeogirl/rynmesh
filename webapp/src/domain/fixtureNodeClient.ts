import type { NodeClient } from "./nodeClient";
import type {
  ActivityEvent,
  ContentFilters,
  ContentItem,
  FirstSuccessStatus,
  NodeSettings,
  Peer,
  PeerFilters,
  ProvenanceEvent,
  PublishDraft,
  Recommendation,
  RecommendationProfile,
  SearchAskResponse,
} from "./types";

const PEERS: Peer[] = [
  {
    id: "self",
    slug: "self",
    name: "My Ryn Node",
    tier: "staked",
    credits: 4820,
    weight: 0.81,
    color: "#00D084",
    endpoint: "http://127.0.0.1:8791",
    network: "rynmesh-main",
    lastSeen: "now",
    served: 412,
    fetched: 168,
    isSelf: true,
    trustedRoot: true,
  },
  {
    id: "peer-aether",
    slug: "ae",
    name: "aether.lab",
    tier: "proven",
    credits: 9120,
    weight: 0.94,
    color: "#39A9FF",
    endpoint: "https://aether.lab:8791",
    network: "rynmesh-main",
    lastSeen: "2m ago",
    served: 12840,
    fetched: 2310,
    trustedRoot: true,
  },
  {
    id: "peer-stadt",
    slug: "st",
    name: "stadt-archive",
    tier: "proven",
    credits: 7480,
    weight: 0.89,
    color: "#D4AF37",
    endpoint: "https://archive.stadt.de:8791",
    network: "rynmesh-main",
    lastSeen: "11m ago",
    served: 8412,
    fetched: 4120,
    trustedRoot: true,
  },
  {
    id: "peer-quill",
    slug: "qw",
    name: "quill-writers",
    tier: "staked",
    credits: 3215,
    weight: 0.72,
    color: "#84CC16",
    endpoint: "https://quill.collective:8791",
    network: "rynmesh-main",
    lastSeen: "34m ago",
    served: 1240,
    fetched: 880,
  },
  {
    id: "peer-mira",
    slug: "mr",
    name: "mira.studio",
    tier: "staked",
    credits: 2890,
    weight: 0.68,
    color: "#F5C542",
    endpoint: "https://mira.studio:8791",
    network: "rynmesh-main",
    lastSeen: "1h ago",
    served: 980,
    fetched: 540,
  },
  {
    id: "peer-lich",
    slug: "li",
    name: "lichtenberg-mfg",
    tier: "attested",
    credits: 1140,
    weight: 0.51,
    color: "#A8B7C7",
    endpoint: "https://lichtenberg.mfg:8791",
    network: "rynmesh-main",
    lastSeen: "4h ago",
    served: 410,
    fetched: 220,
  },
  {
    id: "peer-tomo",
    slug: "tm",
    name: "tomo-dataset",
    tier: "attested",
    credits: 820,
    weight: 0.44,
    color: "#7DD3FC",
    endpoint: "https://tomo.dataset.io:8791",
    network: "rynmesh-main",
    lastSeen: "12h ago",
    served: 220,
    fetched: 95,
  },
  {
    id: "peer-driftwd",
    slug: "dr",
    name: "driftwood-anon",
    tier: "unverified",
    credits: 60,
    weight: 0.12,
    color: "#9F8FFF",
    endpoint: "https://driftwood.anon:8791",
    network: "rynmesh-main",
    lastSeen: "3d ago",
    served: 14,
    fetched: 4,
  },
  {
    id: "peer-glass",
    slug: "gl",
    name: "glassbeach-news",
    tier: "attested",
    credits: 1480,
    weight: 0.55,
    color: "#7DD3FC",
    endpoint: "https://glassbeach.news:8791",
    network: "rynmesh-main",
    lastSeen: "20m ago",
    served: 612,
    fetched: 312,
  },
  {
    id: "peer-orbit",
    slug: "or",
    name: "orbit-music",
    tier: "staked",
    credits: 2310,
    weight: 0.66,
    color: "#FFB18A",
    endpoint: "https://orbit.music:8791",
    network: "rynmesh-main",
    lastSeen: "8m ago",
    served: 1650,
    fetched: 720,
  },
];

const ITEMS: ContentItem[] = [
  {
    content_id: "cid_8f1a23b9c4",
    manifest_hash: "mfst_3a7d0e91f2",
    title: "Urban garden design references - Vienna 2024",
    description:
      "Curated reference images and micro-essays on stepped balcony gardens, photographed plots, and generated covers.",
    tags: ["design", "urban-gardens", "reference", "photography"],
    content_kind: "image",
    content_type: "image/png+zip",
    publisher_peer_id: "peer-mira",
    provider_peer_id: "peer-mira",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_19c2e84a",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "142 MB",
    distribution_weight: 0.84,
    novelty: 0.62,
    ai_score: 0.91,
    published: "2026-05-11T09:14:00Z",
  },
  {
    content_id: "cid_2c19a4ed77",
    manifest_hash: "mfst_71b9c2d4a8",
    title: "Field-recorded forge ambience",
    description:
      "Multi-hour industrial ambient capture, post-processed into 12 loops with device provenance.",
    tags: ["audio", "field-recording", "industrial", "ambient"],
    content_kind: "audio",
    content_type: "audio/flac",
    publisher_peer_id: "peer-lich",
    provider_peer_id: "peer-lich",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_84afe123",
    fetch_status: "preview_only",
    review_basis: "preview",
    size: "2.1 GB",
    distribution_weight: 0.71,
    novelty: 0.88,
    ai_score: 0.78,
    published: "2026-05-10T19:02:00Z",
  },
  {
    content_id: "cid_71fa3b22e8",
    manifest_hash: "mfst_99213aab0c",
    title: "CRISPR base-editing visual primer",
    description:
      "Thirty-eight-slide educational deck with public paper figures, signed bibliography, and human curation.",
    tags: ["education", "biology", "crispr", "slides"],
    content_kind: "slides",
    content_type: "application/pdf",
    publisher_peer_id: "peer-quill",
    provider_peer_id: "peer-aether",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_b3122ff0",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "18.4 MB",
    distribution_weight: 0.79,
    novelty: 0.42,
    ai_score: 0.86,
    published: "2026-05-09T22:48:00Z",
  },
  {
    content_id: "cid_5418cab093",
    manifest_hash: "mfst_d54a9c10b2",
    title: "Munich tram station footage survey",
    description: "Survey video of 41 stations with stabilization and identity scrubbing logged.",
    tags: ["video", "documentary", "urban", "transport"],
    content_kind: "video",
    content_type: "video/webm",
    publisher_peer_id: "peer-stadt",
    provider_peer_id: "peer-stadt",
    safety_outcome: "pending",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "partial",
    provenance_head_hash: "prv_42ab8c10",
    fetch_status: "discovered",
    review_basis: "metadata",
    size: "8.4 GB",
    distribution_weight: 0.62,
    novelty: 0.51,
    ai_score: 0.69,
    published: "2026-05-08T11:30:00Z",
  },
  {
    content_id: "cid_3712faa9c1",
    manifest_hash: "mfst_2c1a984ec7",
    title: "Neutron-star weight on Earth - Signal50 short",
    description: "Physics short with master, audio stems, script, and signed bibliography.",
    tags: ["video", "science", "short", "signal50"],
    content_kind: "video",
    content_type: "video/mp4",
    publisher_peer_id: "peer-aether",
    provider_peer_id: "peer-aether",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_77c1a09a",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "64 MB",
    distribution_weight: 0.92,
    novelty: 0.34,
    ai_score: 0.95,
    published: "2026-05-12T05:12:00Z",
  },
  {
    content_id: "cid_ab991274c0",
    manifest_hash: "mfst_88a0d29b71",
    title: "Roman concrete dataset",
    description: "Annotated 12k-row material sample dataset with composition vectors and loader code.",
    tags: ["dataset", "history", "materials"],
    content_kind: "dataset",
    content_type: "application/parquet",
    publisher_peer_id: "peer-stadt",
    provider_peer_id: "peer-stadt",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_e29db810",
    fetch_status: "preview_only",
    review_basis: "preview",
    size: "412 MB",
    distribution_weight: 0.74,
    novelty: 0.59,
    ai_score: 0.83,
    published: "2026-05-08T08:00:00Z",
  },
  {
    content_id: "cid_44de910c22",
    manifest_hash: "mfst_71ab039c14",
    title: "Compound-interest illustrator",
    description: "Self-contained HTML calculator with deterministic test fixtures.",
    tags: ["code", "finance", "tools"],
    content_kind: "code",
    content_type: "application/zip",
    publisher_peer_id: "self",
    provider_peer_id: "self",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_b9c2381f",
    fetch_status: "local",
    review_basis: "full",
    size: "212 KB",
    distribution_weight: 0.45,
    novelty: 0.71,
    ai_score: 0.62,
    published: "2026-05-05T17:42:00Z",
  },
  {
    content_id: "cid_19a32c8e0d",
    manifest_hash: "mfst_5e213ad4b8",
    title: "Antikythera mechanism package",
    description: "Multimodal package with 3D model, animation, and source paper.",
    tags: ["model", "history", "3d", "package"],
    content_kind: "package",
    content_type: "package/multimodal",
    publisher_peer_id: "peer-stadt",
    provider_peer_id: "peer-aether",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_dd91a4c2",
    fetch_status: "discovered",
    review_basis: "metadata",
    size: "1.8 GB",
    distribution_weight: 0.68,
    novelty: 0.64,
    ai_score: 0.78,
    published: "2026-05-07T13:10:00Z",
  },
  {
    content_id: "cid_e0f1a248b3",
    manifest_hash: "mfst_a8c102b9d3",
    title: "Glassbeach News briefing - May 2026",
    description: "Weekly local-news rollup, AI-summarized and human-edited.",
    tags: ["document", "journalism", "briefing"],
    content_kind: "document",
    content_type: "application/pdf",
    publisher_peer_id: "peer-glass",
    provider_peer_id: "peer-glass",
    safety_outcome: "flagged",
    safety_scanner_id: "rynmesh-scan-1.4",
    safety_notes: "Two sources unreachable at scan time. Manual review suggested.",
    provenance_status: "signed",
    provenance_head_hash: "prv_4c1b8c19",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "4.2 MB",
    distribution_weight: 0.51,
    novelty: 0.39,
    ai_score: 0.58,
    published: "2026-05-13T07:30:00Z",
  },
  {
    content_id: "cid_b210ef99c3",
    manifest_hash: "mfst_4d99c1ab02",
    title: "Synthetic UI screenshots - 200 frames",
    description: "Rendered operator-UI states for design QA bots. No real user data.",
    tags: ["image", "design", "synthetic", "training-data"],
    content_kind: "image",
    content_type: "image/png+zip",
    publisher_peer_id: "peer-tomo",
    provider_peer_id: "peer-tomo",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "partial",
    provenance_head_hash: "prv_71a9c2eb",
    fetch_status: "discovered",
    review_basis: "metadata",
    size: "88 MB",
    distribution_weight: 0.42,
    novelty: 0.77,
    ai_score: 0.55,
    published: "2026-05-09T03:48:00Z",
  },
  {
    content_id: "cid_c01a3819e5",
    manifest_hash: "mfst_aa31c8d402",
    title: "Orbit Music - seven-track LoFi pack",
    description: "AI-composed, human-arranged stems, masters, and model fingerprint card.",
    tags: ["audio", "music", "generative"],
    content_kind: "audio",
    content_type: "audio/wav+zip",
    publisher_peer_id: "peer-orbit",
    provider_peer_id: "peer-orbit",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_d381cabf",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "512 MB",
    distribution_weight: 0.77,
    novelty: 0.66,
    ai_score: 0.83,
    published: "2026-05-11T01:08:00Z",
  },
  {
    content_id: "cid_a772ce0193",
    manifest_hash: "mfst_31c109a4b8",
    title: "Driftwood unsorted dump",
    description: "No description provided. Anonymous publisher. Use caution.",
    tags: ["document", "unsorted"],
    content_kind: "document",
    content_type: "application/octet-stream",
    publisher_peer_id: "peer-driftwd",
    provider_peer_id: "peer-driftwd",
    safety_outcome: "blocked",
    safety_scanner_id: "rynmesh-scan-1.4",
    safety_notes: "Matches a known low-quality content fingerprint cluster.",
    provenance_status: "unsigned",
    provenance_head_hash: null,
    fetch_status: "discovered",
    review_basis: "metadata",
    size: "212 MB",
    distribution_weight: 0.08,
    novelty: 0.21,
    ai_score: 0.14,
    published: "2026-05-10T22:30:00Z",
  },
  {
    content_id: "cid_8881af2901",
    manifest_hash: "mfst_b8c012aaa9",
    title: "Mitochondrial origin explainer",
    description: "Explainer video with figures, citations, co-author chain, and two-pass safety scan.",
    tags: ["video", "biology", "education"],
    content_kind: "video",
    content_type: "video/mp4",
    publisher_peer_id: "peer-quill",
    provider_peer_id: "peer-aether",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_991a0bca",
    fetch_status: "preview_only",
    review_basis: "preview",
    size: "380 MB",
    distribution_weight: 0.66,
    novelty: 0.47,
    ai_score: 0.74,
    published: "2026-05-06T15:12:00Z",
  },
  {
    content_id: "cid_7711cabd92",
    manifest_hash: "mfst_d129ba01c4",
    title: "Local-only compound-interest essay draft",
    description: "Personal draft. Not yet published. Outline plus references.",
    tags: ["document", "draft", "finance"],
    content_kind: "document",
    content_type: "text/markdown",
    publisher_peer_id: "self",
    provider_peer_id: "self",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "unsigned",
    provenance_head_hash: null,
    fetch_status: "local_draft",
    review_basis: "full",
    size: "24 KB",
    distribution_weight: 0,
    novelty: 0,
    ai_score: 0,
    published: null,
    updated: "2026-05-13T11:08:00Z",
  },
  {
    content_id: "cid_9923ab0144",
    manifest_hash: "mfst_22aabcc9e1",
    title: "Cat righting reflex - physics walkthrough",
    description: "Slow-motion footage with annotated mechanics from Aether Lab archives.",
    tags: ["video", "science", "physics"],
    content_kind: "video",
    content_type: "video/mp4",
    publisher_peer_id: "peer-aether",
    provider_peer_id: "peer-aether",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_aa12c93e",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "210 MB",
    distribution_weight: 0.81,
    novelty: 0.42,
    ai_score: 0.88,
    published: "2026-05-04T09:42:00Z",
  },
  {
    content_id: "cid_4421aedd02",
    manifest_hash: "mfst_6712ab0e94",
    title: "Dark matter operator briefing",
    description: "Fourteen-page briefing PDF with source bibliography signed by stadt-archive.",
    tags: ["document", "science", "briefing"],
    content_kind: "document",
    content_type: "application/pdf",
    publisher_peer_id: "peer-aether",
    provider_peer_id: "peer-aether",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_b0c4ad12",
    fetch_status: "preview_only",
    review_basis: "preview",
    size: "2.4 MB",
    distribution_weight: 0.74,
    novelty: 0.55,
    ai_score: 0.8,
    published: "2026-05-12T18:00:00Z",
  },
  {
    content_id: "cid_5012ff8801",
    manifest_hash: "mfst_ad41c80921",
    title: "Mira Studio micro-essays on stepped balconies",
    description: "Six illustrated 600-word essays aligned with the Vienna garden references pack.",
    tags: ["document", "design", "urban-gardens"],
    content_kind: "document",
    content_type: "text/markdown",
    publisher_peer_id: "peer-mira",
    provider_peer_id: "peer-mira",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_91cab249",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "180 KB",
    distribution_weight: 0.69,
    novelty: 0.71,
    ai_score: 0.84,
    published: "2026-05-11T14:18:00Z",
  },
  {
    content_id: "cid_a112c30c98",
    manifest_hash: "mfst_5512cbaaef",
    title: "Quill writers model card disclosure",
    description: "Disclosure manifest for the writing assistant used by Quill.",
    tags: ["document", "disclosure", "model-card"],
    content_kind: "report",
    content_type: "application/json",
    publisher_peer_id: "peer-quill",
    provider_peer_id: "peer-quill",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_c0813a90",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "48 KB",
    distribution_weight: 0.53,
    novelty: 0.32,
    ai_score: 0.61,
    published: "2026-05-02T10:00:00Z",
  },
  {
    content_id: "cid_be112c0f88",
    manifest_hash: "mfst_b81203aac2",
    title: "Tomo labeled image set - vintage signs",
    description: "14k labeled images of vintage signage for typography research.",
    tags: ["dataset", "image", "typography"],
    content_kind: "dataset",
    content_type: "application/zip",
    publisher_peer_id: "peer-tomo",
    provider_peer_id: "peer-tomo",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "partial",
    provenance_head_hash: "prv_a8c92281",
    fetch_status: "discovered",
    review_basis: "metadata",
    size: "4.1 GB",
    distribution_weight: 0.41,
    novelty: 0.62,
    ai_score: 0.52,
    published: "2026-05-09T20:00:00Z",
  },
  {
    content_id: "cid_72ab2cf001",
    manifest_hash: "mfst_8c12290bba",
    title: "Aether Lab model fingerprint registry",
    description: "Public registry of model fingerprints used by aether.lab outputs, updated weekly.",
    tags: ["report", "registry", "disclosure"],
    content_kind: "report",
    content_type: "application/json",
    publisher_peer_id: "peer-aether",
    provider_peer_id: "peer-aether",
    safety_outcome: "passed",
    safety_scanner_id: "rynmesh-scan-1.4",
    provenance_status: "signed",
    provenance_head_hash: "prv_f12ab0c9",
    fetch_status: "fetched_full",
    review_basis: "full",
    size: "212 KB",
    distribution_weight: 0.88,
    novelty: 0.18,
    ai_score: 0.71,
    published: "2026-05-13T00:00:00Z",
  },
  {
    content_id: "cid_ee9920a101",
    manifest_hash: "mfst_d1cb204122",
    title: "Small object detector model card",
    description: "Model card and evaluation bundle for a lightweight object detector.",
    tags: ["model", "vision", "disclosure"],
    content_kind: "model",
    content_type: "application/json",
    publisher_peer_id: "peer-tomo",
    provider_peer_id: "peer-tomo",
    safety_outcome: "unscanned",
    provenance_status: "broken",
    provenance_head_hash: "prv_broken_model",
    fetch_status: "discovered",
    review_basis: "metadata",
    size: "64 KB",
    distribution_weight: 0.29,
    novelty: 0.58,
    ai_score: 0.31,
    published: "2026-05-06T03:20:00Z",
  },
];

const PROVENANCE: Record<string, ProvenanceEvent[]> = {
  prv_77c1a09a: [
    {
      t: "2026-05-12T05:12:00Z",
      label: "Published",
      actor: "peer-aether",
      kind: "publish",
      hash: "evt_a812c0",
      notes: "Manifest signed; preview hash and content hash bound.",
    },
    {
      t: "2026-05-12T05:11:00Z",
      label: "Safety scan",
      actor: "rynmesh-scan-1.4",
      kind: "scan",
      hash: "evt_aa1b29",
      notes: "No flags. Visual, audio, and bibliography checks passed.",
    },
    {
      t: "2026-05-12T05:08:00Z",
      label: "Render complete",
      actor: "aether-render-7",
      kind: "render",
      hash: "evt_b81229",
      notes: "Final video master and audio stems written.",
    },
    {
      t: "2026-05-12T04:51:00Z",
      label: "Script approved",
      actor: "peer-aether",
      kind: "edit",
      hash: "evt_c01ab0",
      notes: "Human editor approved assistant-drafted script.",
    },
  ],
  prv_19c2e84a: [
    {
      t: "2026-05-12T03:22:00Z",
      label: "Published",
      actor: "peer-mira",
      kind: "publish",
      hash: "evt_881ab0",
      notes: "Manifest signed by mira.studio.",
    },
    {
      t: "2026-05-12T03:18:00Z",
      label: "Safety scan",
      actor: "rynmesh-scan-1.4",
      kind: "scan",
      hash: "evt_91c0ab",
      notes: "Visual moderation passed; identity scrubbing pass logged.",
    },
    {
      t: "2026-05-11T18:42:00Z",
      label: "Curation complete",
      actor: "peer-mira",
      kind: "edit",
      hash: "evt_a12c0a",
      notes: "84 references kept from 412 candidates.",
    },
  ],
  prv_4c1b8c19: [
    {
      t: "2026-05-13T07:30:00Z",
      label: "Published",
      actor: "peer-glass",
      kind: "publish",
      hash: "evt_111ab0",
      notes: "Briefing manifest signed.",
    },
    {
      t: "2026-05-13T07:14:00Z",
      label: "Safety scan flagged",
      actor: "rynmesh-scan-1.4",
      kind: "scan",
      hash: "evt_221bca",
      notes: "Two source URLs unreachable at scan time. Accepted with review note.",
      flagged: true,
    },
    {
      t: "2026-05-13T06:42:00Z",
      label: "Bibliography pinned",
      actor: "peer-glass",
      kind: "cite",
      hash: "evt_331cde",
      notes: "41 sources cited; 39 verified.",
    },
  ],
};

const ACTIVITY: ActivityEvent[] = [
  {
    t: "2m ago",
    kind: "fetch",
    text: 'Fetched preview of "Neutron-star weight on Earth" from aether.lab',
    itemId: "cid_3712faa9c1",
  },
  {
    t: "4m ago",
    kind: "scan",
    text: 'Safety scan passed: "Aether Lab model fingerprint registry"',
    itemId: "cid_72ab2cf001",
  },
  { t: "11m ago", kind: "credit", text: 'Earned 12 credits for serving "Urban garden design references"' },
  { t: "22m ago", kind: "rec", text: "3 new AI recommendations available" },
  { t: "34m ago", kind: "peer", text: "New peer attested: glassbeach-news" },
  { t: "1h ago", kind: "verify", text: "Provenance chain re-verified for 14 local items" },
  { t: "1h ago", kind: "publish", text: 'Published "Compound-interest illustrator"' },
  { t: "2h ago", kind: "fetch", text: 'Fetched full content: "Orbit Music - seven-track LoFi pack"' },
  { t: "4h ago", kind: "flag", text: 'Safety scanner flagged "Glassbeach News briefing - May 2026"' },
];

function fixtureEvidence(
  contentId: string,
  basis: Recommendation["review_basis"],
): Recommendation["evidence_packet"] {
  return {
    version: 1,
    content_id: contentId,
    review_basis: basis,
    reviewed_at_unix: Date.parse("2026-05-13T12:00:00Z") / 1000,
    source: { name: "Fixture mesh publisher", platform: "rynmesh", url: "" },
    signals: [],
    observations: [{ field: "title", label: "Title", value: "Fixture content metadata" }],
    citations: [],
    limitations:
      basis === "metadata"
        ? ["Ryn reviewed metadata only, not the full content."]
        : basis === "preview"
          ? ["Ryn reviewed a preview, not the complete content."]
          : [],
  };
}

const RECOMMENDATIONS: Recommendation[] = [
  {
    id: "rec_a01",
    contentId: "cid_5012ff8801",
    priority: 0.92,
    reason:
      "Pairs strongly with your fetched urban garden references. Same publisher, complementary form, and signed provenance.",
    evidence: ["content_match", "publisher_match", "safety_passed", "provenance_signed"],
    novelty: "High overlap with active reading context.",
    uncertainty: null,
    review_basis: "preview",
    evidence_packet: fixtureEvidence("cid_5012ff8801", "preview"),
  },
  {
    id: "rec_a02",
    contentId: "cid_4421aedd02",
    priority: 0.88,
    reason:
      "You asked about dark matter twice this week. Aether Lab is proven tier and the bibliography is signed.",
    evidence: ["query_match", "peer_trust", "safety_passed"],
    novelty: "Different angle than the explainers you already have.",
    uncertainty: null,
    review_basis: "preview",
    evidence_packet: fixtureEvidence("cid_4421aedd02", "preview"),
  },
  {
    id: "rec_a03",
    contentId: "cid_8881af2901",
    priority: 0.76,
    reason:
      "Continuation of the biology reading thread. Quill is staked tier and co-authored with aether.lab.",
    evidence: ["content_match", "peer_reputation"],
    novelty: "Adds video form to a text-heavy thread.",
    uncertainty: "Preview-only; full fetch is 380 MB and should be confirmed.",
    review_basis: "preview",
    evidence_packet: fixtureEvidence("cid_8881af2901", "preview"),
  },
  {
    id: "rec_a04",
    contentId: "cid_19a32c8e0d",
    priority: 0.69,
    reason:
      "Adjacent to your interest in Roman concrete. The package has 3D model, paper, and animation.",
    evidence: ["content_match", "peer_trust"],
    novelty: "Different format: multimodal package.",
    uncertainty: "Metadata-only review. Preview not yet fetched.",
    review_basis: "metadata",
    evidence_packet: fixtureEvidence("cid_19a32c8e0d", "metadata"),
  },
  {
    id: "rec_a05",
    contentId: "cid_c01a3819e5",
    priority: 0.61,
    reason:
      "Diversity injection. Your fetched library is text-heavy this week; orbit-music is staked tier.",
    evidence: ["diversity", "peer_reputation", "safety_passed"],
    novelty: "Pulls library balance toward audio.",
    uncertainty: null,
    review_basis: "full",
    evidence_packet: fixtureEvidence("cid_c01a3819e5", "full"),
  },
  {
    id: "rec_a06",
    contentId: "cid_be112c0f88",
    priority: 0.42,
    reason: "Loose match on typography interest. Tomo is attested only and preview is not fetched.",
    evidence: ["tag_match"],
    novelty: "Different domain than your usual reads.",
    uncertainty: "Attested-tier source; metadata review only.",
    review_basis: "metadata",
    evidence_packet: fixtureEvidence("cid_be112c0f88", "metadata"),
  },
];

let settings: NodeSettings = {
  node_name: "My Ryn Node",
  node_storage: "~/.ryn/store",
  peer_http_host: "0.0.0.0",
  peer_http_port: 8791,
  public_endpoint: "https://ryn.local:8791",
  registry_url: "https://registry.rynmesh.org",
  trusted_roots: ["peer-aether", "peer-stadt"],
  safety_policy: "standard",
  ai_provider: "local",
  ai_model: "gpt-5.3-codex-spark (local)",
  cloud_access: false,
  rank_default: "weight",
  publish_visibility: "network",
  fetch_budget_mb: 8192,
  fetch_used_mb: 4324,
  fetch_timeout_s: 60,
  onboarding_version: 0,
  notifications_enabled: true,
  notification_frequency: "immediate",
  notification_quiet_start: 22,
  notification_quiet_end: 8,
  desktop_managed: true,
  network_id: "rynmesh-main",
};

let recommendationProfile: RecommendationProfile = {
  version: 1,
  direction: "",
  topics: [],
  platforms: [],
  feedback: {},
  updated_at: "",
  topic_choices: [
    { id: "ai-agents", label: "AI agents", tags: ["ai", "agents", "automation"] },
    { id: "open-source", label: "Open source", tags: ["open-source", "software", "code"] },
    { id: "research", label: "Research", tags: ["research", "science", "papers"] },
    { id: "creative", label: "Creative work", tags: ["art", "design", "music"] },
  ],
  platform_choices: [
    { id: "rynmesh", label: "RynMesh" },
    { id: "github", label: "GitHub" },
    { id: "arxiv", label: "arXiv" },
    { id: "youtube", label: "YouTube" },
    { id: "rss", label: "RSS & blogs" },
  ],
  learned_signals: 0,
  feedback_count: 0,
};

const delay = async () => new Promise((resolve) => window.setTimeout(resolve, 110));

export function peerById(id: string): Peer | undefined {
  return PEERS.find((peer) => peer.id === id);
}

function withResolvedPeer(item: ContentItem): ContentItem {
  const peer = peerById(item.publisher_peer_id);
  return {
    ...item,
    source_peer_name: peer?.name,
    identity_tier: peer?.tier,
    credit_score: peer?.credits,
  };
}

function filterContent(filters: ContentFilters | undefined): ContentItem[] {
  let result = ITEMS.map(withResolvedPeer);
  if (!filters) return result;
  if (filters.source && filters.source !== "all") {
    if (filters.source === "local") result = result.filter((item) => item.publisher_peer_id === "self");
    else if (filters.source === "fetched")
      result = result.filter((item) => ["fetched_full", "preview_only", "local"].includes(item.fetch_status));
    else if (filters.source === "discovered")
      result = result.filter((item) => item.fetch_status === "discovered");
    else result = result.filter((item) => item.publisher_peer_id === filters.source);
  }
  if (filters.kind && filters.kind !== "all")
    result = result.filter((item) => item.content_kind === filters.kind);
  if (filters.safety && filters.safety !== "all")
    result = result.filter((item) => item.safety_outcome === filters.safety);
  if (filters.tier && filters.tier !== "all")
    result = result.filter((item) => item.identity_tier === filters.tier);
  if (filters.provenance && filters.provenance !== "all")
    result = result.filter((item) => item.provenance_status === filters.provenance);
  if (filters.search) {
    const q = filters.search.toLowerCase();
    result = result.filter(
      (item) =>
        item.title.toLowerCase().includes(q) ||
        item.description.toLowerCase().includes(q) ||
        item.tags.some((tag) => tag.toLowerCase().includes(q)),
    );
  }
  const rank = filters.rank ?? "weight";
  return [...result].sort((a, b) => {
    if (rank === "newest") return String(b.published ?? b.updated).localeCompare(String(a.published ?? a.updated));
    if (rank === "trusted") return (b.credit_score ?? 0) - (a.credit_score ?? 0);
    if (rank === "ai") return (b.ai_score ?? 0) - (a.ai_score ?? 0);
    if (rank === "novelty") return (b.novelty ?? 0) - (a.novelty ?? 0);
    return b.distribution_weight - a.distribution_weight;
  });
}

function filterPeers(filters: PeerFilters | undefined): Peer[] {
  let result = [...PEERS];
  if (!filters) return result;
  if (filters.tier && filters.tier !== "all")
    result = result.filter((peer) => peer.tier === filters.tier);
  if (filters.quarantined !== undefined)
    result = result.filter((peer) => Boolean(peer.quarantined) === filters.quarantined);
  if (filters.search) {
    const q = filters.search.toLowerCase();
    result = result.filter(
      (peer) =>
        peer.name.toLowerCase().includes(q) ||
        peer.id.toLowerCase().includes(q) ||
        peer.endpoint.toLowerCase().includes(q),
    );
  }
  return result;
}

export function makeFixtureNodeClient(): NodeClient {
  let egress: import("./types").EgressStatus = {
    region: "CN", connected: false, providerPeerId: "", providerNodeName: "",
    socksPort: null, exitIp: null, loc: "", locVerified: false,
    ttlExpiresAt: "", priceCredits: 0, lastError: null,
    connectedAt: null, uptimeSeconds: null,
  };
  let _autoUpdate = true;
  let firstSuccess: FirstSuccessStatus = {
    version: "ryn.first-success.v1",
    phase: "ready",
    completed: false,
    dismissed: false,
    node_ready: true,
    content_ready: true,
    item_count: RECOMMENDATIONS.length,
    healthy_sources: 4,
    source_count: 4,
    failed_sources: 0,
    degraded: false,
    using_cache: false,
    first_item_opened: false,
    first_signal_recorded: false,
    milestones: { node_ready: Date.now() / 1000, content_ready: Date.now() / 1000 },
    safe_error: null,
    recoverable_actions: [],
  };

  return {
    mode: "fixture",
    async getNodeStatus() {
      await delay();
      return {
        node_name: settings.node_name,
        peer_id: "peer:7d2b9c4f-self",
        daemon_running: true,
        desktop_managed: true,
        registry: "connected",
        peer_count: PEERS.length - 1,
        local_items: ITEMS.filter((item) => item.publisher_peer_id === "self").length,
        fetched_items: ITEMS.filter((item) =>
          ["fetched_full", "preview_only", "local"].includes(item.fetch_status),
        ).length,
        pending_recs: RECOMMENDATIONS.length,
        version: "ryn-node 0.4.2",
        uptime_seconds: 84320,
      };
    },
    async getRegistryStatus() {
      await delay();
      return { status: "connected", url: settings.registry_url };
    },
    async listJobCapacities() {
      await delay();
      return [
        {
          peer_id: "peer:m4-mini-veo",
          node_name: "M4-mini",
          provider_name: "M4-mini",
          capabilities: ["signal50.veo_motion.v1"],
          network_id: "rynmesh-home-qa",
          capacity_units: 1,
          max_concurrent: 1,
          price_credits: { "signal50.veo_motion.v1": 20 },
          polling_interval_sec: 30,
          updated_at: new Date().toISOString(),
          metadata: {
            service: "Signal50 Veo rendering",
            route: "m4_mini_hammerspoon_chrome_cdp",
            operation: "signal50.remote_action.complete_flow_video_veo_motion_clips",
          },
        },
      ];
    },
    async submitWorkOrder(req) {
      await delay();
      const work_order_id = `wo_fixture_${Math.random().toString(16).slice(2, 10)}`;
      return {
        work_order_id,
        order: {
          work_order_id,
          requester_peer_id: "peer:7d2b9c4f-self",
          provider_peer_id: req.provider_peer_id,
          capability: req.capability,
          operation: req.operation,
          params: req.params,
          network_id: req.network_id ?? "rynmesh-home-qa",
          max_credit_cost: req.max_credit_cost ?? 0,
          created_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 6 * 3600 * 1000).toISOString(),
        },
      };
    },
    async listWorkResults() {
      await delay();
      return [];
    },
    async listLLMServices() {
      await delay();
      return [{
        peer_id: "peer:fixture-llm-provider",
        node_name: "Fixture LLM provider",
        online: true,
        capacity: { available: 1, max_concurrent: 1, running: 0 },
        benchmark: { latency_ms: 12, tokens_per_second: 42 },
        service: {
          package_id: "fixture-local-llm",
          model_alias: "fixture-private-model",
          capabilities: ["text-generation"],
          context_window: 2048,
          max_output_tokens: 128,
          pricing: {
            currency: "DEV_TASK_BALANCE",
            input_per_1k: 0.001,
            output_per_1k: 0.002,
            minimum: 0.001,
            maximum_per_task: 1,
          },
          privacy: { policy_text: "Fixture only; compute node sees plaintext.", compute_node_sees_plaintext: true },
          risk_labels: ["fixture"],
        },
      }];
    },
    async getLLMServiceStatus() {
      await delay();
      return { configured: false, online: false };
    },
    async publishLLMService() {
      await delay();
      return { ok: true };
    },
    async pauseLLMService() {
      await delay();
      return { configured: true, online: false, publication_enabled: false };
    },
    async setupLLMService() {
      await delay();
      return { configured: true, publication_enabled: false };
    },
    async startLLMSetup() {
      await delay();
      return { job_id: "setup_fixture", state: "succeeded", stage: "completed", progress: 100 };
    },
    async getLLMSetupStatus() {
      await delay();
      return { job_id: "setup_fixture", state: "succeeded", stage: "completed", progress: 100 };
    },
    async cancelLLMSetup(jobId) {
      await delay();
      return { job_id: jobId, state: "cancelled", stage: "cancelled", progress: 0 };
    },
    async getLLMHardware() {
      await delay();
      return {
        // The fixture node has no LLM configured yet, so the bundled runtime
        // is downloadable (`available`) but not installed (`present`). The
        // two fields are deliberately different: the setup panel must report
        // what is on the device, not what could be fetched.
        hardware: { native_runtime_available: true, native_runtime_present: false },
        recommendations: [
          {
            profile: "light", can_run: true, display_name: "Light",
            estimated_memory_mb: 2048, estimated_disk_mb: 3200, recommended: false,
          },
          {
            profile: "balanced", can_run: true, display_name: "Balanced",
            estimated_memory_mb: 4096, estimated_disk_mb: 6400, recommended: true,
          },
          {
            profile: "quality", can_run: true, display_name: "Quality",
            estimated_memory_mb: 8192, estimated_disk_mb: 12800, recommended: false,
          },
        ],
      };
    },
    async runLLMServiceAction(action) {
      await delay();
      return { ok: true, action };
    },
    async getTaskBalance() {
      await delay();
      return { currency: "DEV_TASK_BALANCE", available: 100, held: 0, earned: 0 };
    },
    async submitLLMOrder(req) {
      await delay();
      return {
        task_id: `task_fixture_${Math.random().toString(16).slice(2, 10)}`,
        state: "succeeded",
        output: `Fixture response for: ${req.prompt.slice(0, 40)}`,
        model_alias: "fixture-private-model",
        input_tokens: 8,
        output_tokens: 8,
        duration_ms: 12,
        amount: 0.001,
        transport: req.transport === "relay" ? "encrypted_relay"
          : req.transport === "p2p" ? "ice_udp_direct" : "peer_http_direct",
      };
    },
    async getLLMOrder(taskId) {
      await delay();
      return { task_id: taskId, state: "succeeded", output: "Fixture completed response" };
    },
    async cancelLLMOrder(taskId) {
      await delay();
      return { task_id: taskId, state: "cancelled" };
    },
    async listLLMOrders() {
      await delay();
      return [];
    },
    async getLLMPrivacy() {
      await delay();
      return {
        result_retention_seconds: 3600 as const, plaintext_persisted: false,
        stored_results_encrypted: true, compute_node_sees_plaintext: true,
      };
    },
    async updateLLMPrivacy(resultRetentionSeconds) {
      await delay();
      return {
        result_retention_seconds: resultRetentionSeconds, plaintext_persisted: false,
        stored_results_encrypted: true, compute_node_sees_plaintext: true,
      };
    },
    async clearLLMOrders() {
      await delay();
      return { ok: true, removed: 0 };
    },
    async discoverPeers() {
      await delay();
      return PEERS;
    },
    async listPeers(filters) {
      await delay();
      return filterPeers(filters);
    },
    async listContent(filters) {
      await delay();
      return filterContent(filters);
    },
    async getContentItem(id) {
      await delay();
      const item = ITEMS.find((candidate) => candidate.content_id === id);
      if (!item) throw new Error(`Unknown content item: ${id}`);
      return withResolvedPeer(item);
    },
    async getContentBody(id) {
      await delay();
      const item = ITEMS.find((candidate) => candidate.content_id === id);
      if (!item) throw new Error(`Unknown content item: ${id}`);
      return {
        ok: true,
        content_id: id,
        content_type: item.content_type,
        size: item.size ?? "",
        truncated: false,
        text: `# ${item.title}\n\n${item.description}\n\nThis fixture body represents verified local content.`,
      };
    },
    async getProvenance(headHash) {
      await delay();
      return headHash ? (PROVENANCE[headHash] ?? []) : [];
    },
    async fetchPreview(contentId) {
      await delay();
      const item = ITEMS.find((candidate) => candidate.content_id === contentId);
      if (item && item.fetch_status === "discovered") item.fetch_status = "preview_only";
      return { ok: true, size: item?.size ?? "preview" };
    },
    async fetchFullContent(contentId) {
      await delay();
      const item = ITEMS.find((candidate) => candidate.content_id === contentId);
      if (item) item.fetch_status = "fetched_full";
      return { ok: true, size: item?.size ?? "content" };
    },
    async requestRecommendations(req) {
      await delay();
      const limit = req?.limit ?? RECOMMENDATIONS.length;
      return RECOMMENDATIONS.slice(0, limit);
    },
    async getFirstSuccess() {
      await delay();
      return { ...firstSuccess, milestones: { ...firstSuccess.milestones } };
    },
    async dismissFirstSuccess() {
      await delay();
      firstSuccess = { ...firstSuccess, dismissed: true };
      return firstSuccess;
    },
    async resetFirstSuccess() {
      await delay();
      firstSuccess = {
        ...firstSuccess,
        phase: "ready",
        completed: false,
        dismissed: false,
        first_item_opened: false,
        first_signal_recorded: false,
        milestones: { node_ready: Date.now() / 1000, content_ready: Date.now() / 1000 },
      };
      return firstSuccess;
    },
    async recordContentConsumption(_item, action) {
      await delay();
      const now = Date.now() / 1000;
      if (action === "opened") {
        firstSuccess = {
          ...firstSuccess,
          phase: firstSuccess.first_signal_recorded ? "completed" : "awaiting_signal",
          first_item_opened: true,
          completed: firstSuccess.first_signal_recorded,
          milestones: { ...firstSuccess.milestones, first_item_opened: now },
        };
      } else if (action === "bookmark" || action === "completed") {
        firstSuccess = {
          ...firstSuccess,
          phase: firstSuccess.first_item_opened ? "completed" : firstSuccess.phase,
          first_signal_recorded: true,
          completed: firstSuccess.first_item_opened,
          milestones: {
            ...firstSuccess.milestones,
            first_signal_recorded: now,
            ...(firstSuccess.first_item_opened ? { completed: now } : {}),
          },
        };
      }
    },
    async getRecommendationProfile() {
      await delay();
      return recommendationProfile;
    },
    async updateRecommendationProfile(patch) {
      await delay();
      recommendationProfile = {
        ...recommendationProfile,
        ...patch,
        updated_at: new Date().toISOString(),
      };
      return recommendationProfile;
    },
    async submitRecommendationFeedback(contentId, action) {
      await delay();
      const feedback = { ...recommendationProfile.feedback };
      if (action === "neutral") delete feedback[contentId];
      else feedback[contentId] = { action };
      recommendationProfile = {
        ...recommendationProfile,
        feedback,
        feedback_count: Object.keys(feedback).length,
        updated_at: new Date().toISOString(),
      };
      return recommendationProfile;
    },
    async submitSearchAsk(req): Promise<SearchAskResponse> {
      await delay();
      const lower = req.text.toLowerCase();
      const cites = lower.includes("urban")
        ? ["cid_8f1a23b9c4", "cid_5012ff8801"]
        : ["cid_3712faa9c1", "cid_4421aedd02"];
      return {
        routing: {
          text:
            "Routed through the local Ryn node. The webapp did not contact peers or the registry directly.",
          operations: [
            { name: "listContent(filters)", risk: "low", status: "done" },
            { name: "requestRecommendations(top 6)", risk: "low", status: "done" },
            { name: "rankContent(local policy)", risk: "low", status: "done" },
          ],
        },
        assistant: {
          text:
            "I found a small set worth reviewing. The strongest items have signed provenance, passing safety receipts, and publishers with attested or better identity tiers.",
          cites,
          suggests: ["Fetch previews for the top 3", "Show only proven peers", "More like this"],
        },
      };
    },
    async preparePublish(req: PublishDraft) {
      await delay();
      return {
        draft_id: `draft_${Math.random().toString(16).slice(2, 10)}`,
        content_hash: `sha256_${req.title.toLowerCase().replace(/[^a-z0-9]+/g, "_").slice(0, 18)}_c0ffee`,
        preview_hash: "prev_8c3b6c21",
        manifest_hash: "mfst_9c4a210bb7",
        safety: { outcome: "passed", scanner: "rynmesh-scan-1.4" },
        provenance_head: "prv_publish_draft",
      };
    },
    async confirmPublish() {
      await delay();
      return { ok: true, content_id: "cid_newly_published" };
    },
    async getCreditScoreboard(filters) {
      await delay();
      const peers = filters?.peerId ? PEERS.filter((peer) => peer.id === filters.peerId) : PEERS;
      return { peers: peers.map(({ id, name, credits, weight }) => ({ id, name, credits, weight })) };
    },
    async getSettings() {
      await delay();
      return settings;
    },
    async updateSettings(patch) {
      await delay();
      settings = { ...settings, ...patch };
      return settings;
    },
    async getActivity() {
      await delay();
      return ACTIVITY;
    },
    async getPrivacyStatus() {
      await delay();
      return {
        storage_root: "~/.rynmesh",
        reading_history_items: 12,
        feedback_items: recommendationProfile.feedback_count,
        learned_signals: recommendationProfile.learned_signals,
        cached_discovery_items: 84,
        reader_cache_files: 3,
        audit_events: ACTIVITY.length,
        cloud_ai_enabled: settings.cloud_access,
      };
    },
    async exportPersonalData() {
      await delay();
      return {
        version: 1,
        exported_at: new Date().toISOString(),
        storage: "local" as const,
        recommendation_profile: recommendationProfile,
        assistant_audit: ACTIVITY,
      };
    },
    async erasePersonalData(scopes) {
      await delay();
      if (scopes.includes("profile")) {
        recommendationProfile = {
          ...recommendationProfile,
          direction: "",
          topics: [],
          platforms: [],
          feedback: {},
          feedback_count: 0,
          learned_signals: 0,
        };
      }
      return { ok: true, erased: scopes };
    },
    async updatesStatus() {
      return { currentVersion: "0.2.0", availableVersion: null, autoUpdate: _autoUpdate, lastCheck: new Date().toISOString(), lastError: null };
    },
    async updatesCheck() {
      return { currentVersion: "0.2.0", availableVersion: null, autoUpdate: _autoUpdate, lastCheck: new Date().toISOString(), lastError: null };
    },
    async updatesApply() { return { applied: true, version: "0.3.0" }; },
    async setAutoUpdate(on: boolean) { _autoUpdate = on; },
    async peersHealth() {
      await delay();
      return PEERS.map((p) => ({ peerId: p.id, online: true, checkedAt: new Date().toISOString() }));
    },
    async egressStatus() {
      return egress;
    },
    async egressConnect() {
      egress = {
        ...egress, connected: true, providerNodeName: "sz-egress", providerPeerId: "sz",
        socksPort: 2080, exitIp: "192.0.2.20", loc: "CN", locVerified: true,
        priceCredits: 1, ttlExpiresAt: new Date(Date.now() + 3600_000).toISOString(),
        lastError: null,
        connectedAt: new Date().toISOString(), uptimeSeconds: 0,
      };
      return egress;
    },
    async egressLaunch() {
      return { launched: true, count: 8 };
    },
    async egressDisconnect() {
      egress = {
        ...egress, connected: false, socksPort: null, exitIp: null,
        loc: "", locVerified: false, ttlExpiresAt: "", priceCredits: 0,
        connectedAt: null, uptimeSeconds: null,
      };
      return egress;
    },
    async listMessages() {
      await delay();
      return [];
    },
    async sendMessage(req) {
      await delay();
      return {
        msg_id: `msg_fixture_${Math.random().toString(16).slice(2, 10)}`,
        dir: "out",
        from: "self",
        to: req.peer_id,
        text: req.text ?? "",
        kind: req.attachment ? "file" : "text",
        ts: new Date().toISOString(),
        delivered: false,
      };
    },
    messagesStreamUrl() {
      return "/api/local/messages/stream";
    },
  };
}
