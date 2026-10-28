import type { ComponentType, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BadgeCheck,
  BookMarked,
  Bot,
  Box,
  Check,
  CheckCircle,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  ClipboardList,
  CloudCog,
  Code2,
  Coins,
  Compass,
  Copy,
  Database,
  Download,
  Eye,
  FileEdit,
  FileText,
  Globe,
  HardDrive,
  Hash as HashIcon,
  Hexagon,
  Image,
  Link2,
  Loader2,
  LucideProps,
  MessageSquareText,
  Music,
  Newspaper,
  Package,
  PanelRightClose,
  Pencil,
  Presentation,
  Search,
  Server,
  Settings2,
  Shield,
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
  ShieldX,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Unlink,
  UploadCloud,
  Users,
  Video,
  X,
} from "lucide-react";
import type {
  ActionRisk,
  ActivityEvent,
  ContentItem,
  ContentKind,
  FetchStatus,
  IdentityTier,
  Peer,
  ProvenanceEvent,
  ProvenanceStatus,
  Recommendation,
  RecommendationEvidence,
  RecommendationEvidencePacket,
  ReviewBasis,
  SafetyOutcome,
} from "../domain/types";

type IconComponent = ComponentType<LucideProps>;

export function EvidenceDetails({ packet }: { packet: RecommendationEvidencePacket }) {
  const reviewed = packet.observations.map((observation) => observation.label).join(", ");
  return (
    <details className="evidence-details">
      <summary>
        Evidence reviewed · {packet.review_basis === "metadata" ? "metadata only" : packet.review_basis}
      </summary>
      <div className="evidence-details-body">
        <p><strong>Reviewed:</strong> {reviewed || "No fields were available."}</p>
        {packet.limitations.map((limitation) => (
          <p className="evidence-limitation" key={limitation}>
            <AlertTriangle size={13} /> {limitation}
          </p>
        ))}
        {packet.citations.map((citation) => (
          <a key={citation.url} href={citation.url} target="_blank" rel="noreferrer noopener">
            <Link2 size={13} /> {citation.label}
          </a>
        ))}
      </div>
    </details>
  );
}

const kindIcons: Record<ContentKind, IconComponent> = {
  video: Video,
  image: Image,
  audio: Music,
  document: FileText,
  slides: Presentation,
  dataset: Database,
  code: Code2,
  report: ClipboardList,
  package: Package,
  model: Box,
};

const activityIcons: Record<ActivityEvent["kind"], IconComponent> = {
  fetch: Download,
  scan: ShieldCheck,
  credit: Coins,
  rec: Sparkles,
  peer: Users,
  verify: CheckCircle2,
  publish: UploadCloud,
  flag: AlertTriangle,
};

const eventIcons: Record<ProvenanceEvent["kind"], IconComponent> = {
  publish: UploadCloud,
  scan: ShieldCheck,
  render: Sparkles,
  edit: Pencil,
  cite: BookMarked,
  generate: Bot,
};

const safetyMeta: Record<SafetyOutcome, { tone: Tone; icon: IconComponent; label: string }> = {
  passed: { tone: "ok", icon: ShieldCheck, label: "Passed" },
  pending: { tone: "warn", icon: Shield, label: "Pending" },
  flagged: { tone: "warn", icon: ShieldAlert, label: "Flagged" },
  blocked: { tone: "danger", icon: ShieldX, label: "Blocked" },
  unscanned: { tone: "muted", icon: ShieldOff, label: "Unscanned" },
};

const provenanceMeta: Record<ProvenanceStatus, { tone: Tone; icon: IconComponent; label: string }> = {
  signed: { tone: "ok", icon: BadgeCheck, label: "Signed" },
  partial: { tone: "warn", icon: Link2, label: "Partial" },
  unsigned: { tone: "muted", icon: CircleDashed, label: "Unsigned" },
  broken: { tone: "danger", icon: Unlink, label: "Broken" },
};

const fetchMeta: Record<FetchStatus, { tone: Tone; icon: IconComponent; label: string }> = {
  local: { tone: "info", icon: HardDrive, label: "Local" },
  local_draft: { tone: "muted", icon: FileEdit, label: "Draft" },
  fetched_full: { tone: "ok", icon: CheckCircle, label: "Fetched" },
  preview_only: { tone: "neutral", icon: Eye, label: "Preview" },
  discovered: { tone: "muted", icon: Globe, label: "Discovered" },
  fetching: { tone: "info", icon: Loader2, label: "Fetching" },
};

const tierIndex: Record<IdentityTier, number> = {
  unverified: 0,
  attested: 1,
  staked: 2,
  proven: 3,
};

const tierLabel: Record<IdentityTier, string> = {
  unverified: "Unverified",
  attested: "Attested",
  staked: "Staked",
  proven: "Proven",
};

const tierClass: Record<IdentityTier, string> = {
  unverified: "tier-unverified",
  attested: "tier-attested",
  staked: "tier-staked",
  proven: "tier-proven",
};

const evidenceLabel: Record<RecommendationEvidence, string> = {
  content_match: "Content overlap",
  publisher_match: "Trusted publisher",
  peer_trust: "Peer trust",
  peer_reputation: "Peer reputation",
  query_match: "Query match",
  tag_match: "Tag match",
  diversity: "Diversity",
  safety_passed: "Safety passed",
  provenance_signed: "Provenance signed",
};

export const NavIcons = {
  home: Hexagon,
  digest: Newspaper,
  explore: Compass,
  recommendations: Sparkles,
  searchAsk: MessageSquareText,
  chat: MessageSquareText,
  publish: UploadCloud,
  peers: Users,
  services: CloudCog,
  settings: Settings2,
  aiPanel: PanelRightClose,
  search: Search,
  server: Server,
};

export type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "muted";

export function Chip({
  children,
  tone = "neutral",
  icon: Icon,
  mono = false,
}: {
  children: ReactNode;
  tone?: Tone;
  icon?: IconComponent;
  mono?: boolean;
}) {
  return (
    <span className={`chip chip-${tone}${mono ? " mono" : ""}`}>
      {Icon ? <Icon size={12} /> : null}
      {children}
    </span>
  );
}

export function TierBadge({ tier, compact = false }: { tier: IdentityTier; compact?: boolean }) {
  const index = tierIndex[tier];
  return (
    <span className={`tier-badge ${tierClass[tier]}`}>
      <span className="tier-ladder" aria-hidden="true">
        {[0, 1, 2, 3].map((bar) => (
          <span key={bar} className={bar <= index ? "tier-on" : ""} />
        ))}
      </span>
      {compact ? null : tierLabel[tier]}
    </span>
  );
}

export function SafetyBadge({ outcome }: { outcome: SafetyOutcome }) {
  const meta = safetyMeta[outcome];
  return (
    <Chip tone={meta.tone} icon={meta.icon}>
      {meta.label}
    </Chip>
  );
}

export function ProvBadge({ status }: { status: ProvenanceStatus }) {
  const meta = provenanceMeta[status];
  return (
    <Chip tone={meta.tone} icon={meta.icon}>
      {meta.label}
    </Chip>
  );
}

export function FetchChip({ status }: { status: FetchStatus }) {
  const meta = fetchMeta[status];
  return (
    <Chip tone={meta.tone} icon={meta.icon}>
      {meta.label}
    </Chip>
  );
}

export function KindIcon({ kind, size = 16 }: { kind: ContentKind; size?: number }) {
  const Icon = kindIcons[kind];
  return <Icon size={size} />;
}

export function ActivityIcon({ kind }: { kind: ActivityEvent["kind"] }) {
  const Icon = activityIcons[kind];
  return <Icon size={16} />;
}

export function EventIcon({ kind }: { kind: ProvenanceEvent["kind"] }) {
  const Icon = eventIcons[kind];
  return <Icon size={13} />;
}

export function Hash({ value }: { value: string | null | undefined }) {
  const text = value ?? "none";
  const short = text.length > 16 ? `${text.slice(0, 8)}...${text.slice(-6)}` : text;
  const copy = async () => {
    if (value && navigator.clipboard) await navigator.clipboard.writeText(value);
  };
  return (
    <button className="hash-button mono" type="button" onClick={copy} aria-label={`Copy ${text}`}>
      <HashIcon size={12} />
      {short}
    </button>
  );
}

export function Button({
  children,
  variant = "standard",
  icon: Icon,
  onClick,
  type = "button",
  disabled = false,
}: {
  children: ReactNode;
  variant?: "standard" | "primary" | "danger" | "ghost";
  icon?: IconComponent;
  onClick?: () => void;
  type?: "button" | "submit";
  disabled?: boolean;
}) {
  return (
    <button className={`btn btn-${variant}`} type={type} onClick={onClick} disabled={disabled}>
      {Icon ? <Icon size={15} /> : null}
      {children}
    </button>
  );
}

export function IconButton({
  icon: Icon,
  label,
  onClick,
  active = false,
  disabled = false,
}: {
  icon: IconComponent;
  label: string;
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      className={`icon-button${active ? " is-active" : ""}`}
      type="button"
      onClick={onClick}
      aria-label={label}
      disabled={disabled}
    >
      <Icon size={17} />
    </button>
  );
}

export function Panel({
  children,
  title,
  action,
  className = "",
}: {
  children: ReactNode;
  title?: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title || action ? (
        <div className="panel-head">
          {title ? <h2>{title}</h2> : <span />}
          {action}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function PageHeader({
  eyebrow,
  title,
  context,
  actions,
}: {
  eyebrow: string;
  title: string;
  context: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{context}</p>
      </div>
      <div className="page-actions">{actions}</div>
    </header>
  );
}

export function KV({ rows }: { rows: Array<{ label: string; value: ReactNode }> }) {
  return (
    <dl className="kv">
      {rows.map((row) => (
        <div key={row.label}>
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function WeightBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <span className="weight-bar">
      <span className="weight-track">
        <span style={{ width: `${pct}%` }} />
      </span>
      <b className="mono">{value.toFixed(2)}</b>
    </span>
  );
}

export function PeerPill({ peer }: { peer: Peer | undefined }) {
  if (!peer) return <span className="muted">unknown peer</span>;
  return (
    <span className="peer-pill">
      <span className="peer-avatar" style={{ background: peer.color ?? "var(--accent-blue)" }}>
        {peer.slug.slice(0, 2).toUpperCase()}
      </span>
      <span>{peer.name}</span>
      <TierBadge tier={peer.tier} compact />
    </span>
  );
}

export function ReviewBasisBadge({ basis }: { basis: ReviewBasis }) {
  const active = basis === "full" ? 3 : basis === "preview" ? 2 : 1;
  return (
    <span className="review-basis">
      <span className="basis-dots" aria-hidden="true">
        {[1, 2, 3].map((dot) => (
          <span key={dot} className={dot <= active ? "basis-on" : ""} />
        ))}
      </span>
      {basis}
    </span>
  );
}

export function EmptyState({
  title,
  body,
  icon: Icon = CircleDashed,
  action,
}: {
  title: string;
  body: string;
  icon?: IconComponent;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <Icon size={24} />
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function ContentRow({
  item,
  publisher,
  provider,
  selected,
  onSelect,
  onOpen,
}: {
  item: ContentItem;
  publisher: Peer | undefined;
  provider: Peer | undefined;
  selected: boolean;
  onSelect: () => void;
  onOpen: () => void;
}) {
  return (
    <tr className={item.fetch_status === "discovered" ? "ghost-row" : ""} onClick={onOpen}>
      <td onClick={(event) => event.stopPropagation()}>
        <input aria-label={`Select ${item.title}`} type="checkbox" checked={selected} onChange={onSelect} />
      </td>
      <td className="content-title-cell">
        <span className="kind-box">
          <KindIcon kind={item.content_kind} />
        </span>
        <div>
          <strong>{item.title}</strong>
          <small>{item.description}</small>
        </div>
      </td>
      <td className="mono kind-text">{item.content_kind}</td>
      <td>
        <span>{publisher?.name ?? "unknown"}</span>
        {provider && provider.id !== publisher?.id ? <small>via {provider.name}</small> : null}
      </td>
      <td>{publisher ? <TierBadge tier={publisher.tier} compact /> : null}</td>
      <td>
        <SafetyBadge outcome={item.safety_outcome} />
      </td>
      <td>
        <ProvBadge status={item.provenance_status} />
      </td>
      <td>
        <FetchChip status={item.fetch_status} />
      </td>
      <td>
        <WeightBar value={item.distribution_weight} />
      </td>
    </tr>
  );
}

export function RecommendationCard({
  rec,
  item,
  publisher,
  onInspect,
  onFetchPreview,
  onFetchFull,
  onFeedback,
  onOpen,
}: {
  rec: Recommendation;
  item: ContentItem;
  publisher: Peer | undefined;
  onInspect: () => void;
  onFetchPreview: () => void;
  onFetchFull: () => void;
  onFeedback?: (action: "more" | "less" | "hide") => void;
  onOpen?: () => void;
}) {
  const openLabel = item.content_kind === "video"
    ? "Watch"
    : item.content_kind === "audio"
      ? "Listen"
      : item.content_kind === "image"
        ? "View"
        : "Read";
  return (
    <article className="recommendation-card">
      <div className="recommendation-main">
        {item.thumbnail_url ? <img className="recommendation-thumbnail" src={item.thumbnail_url} alt="" loading="lazy" /> : null}
        <div className="content-kicker">
          <KindIcon kind={item.content_kind} />
          <span>{item.content_kind}</span>
          <ReviewBasisBadge basis={rec.review_basis} />
        </div>
        <h2>{item.title}</h2>
        <p>{item.description}</p>
        <div className="why-box">
          <strong>Why</strong>
          <span>{rec.reason}</span>
        </div>
        {rec.uncertainty ? (
          <div className="limitation">
            <AlertTriangle size={15} />
            {rec.uncertainty}
          </div>
        ) : null}
        <div className="chip-row">
          {rec.evidence.map((evidence) => (
            <Chip key={evidence} tone="muted">
              {evidenceLabel[evidence]}
            </Chip>
          ))}
        </div>
        <EvidenceDetails packet={rec.evidence_packet} />
        {item.starter ? (
          <div className="button-row recommendation-feedback">
            <Button variant="primary" icon={ThumbsUp} onClick={() => onFeedback?.("more")}>
              More like this
            </Button>
            <Button icon={ThumbsDown} onClick={() => onFeedback?.("less")}>
              Less like this
            </Button>
            <Button variant="ghost" onClick={() => onFeedback?.("hide")}>
              Hide
            </Button>
          </div>
        ) : item.external ? (
          <>
            <div className="button-row">
              <Button variant="primary" icon={Eye} onClick={onOpen}>
                {openLabel}
              </Button>
              {item.external_url ? (
                <Button onClick={() => window.open(item.external_url, "_blank", "noopener,noreferrer")}>
                  Open original
                </Button>
              ) : null}
            </div>
            {onFeedback ? (
              <div className="button-row recommendation-feedback">
                <Button variant="ghost" icon={ThumbsUp} onClick={() => onFeedback("more")}>
                  More like this
                </Button>
                <Button variant="ghost" icon={ThumbsDown} onClick={() => onFeedback("less")}>
                  Less
                </Button>
                <Button variant="ghost" onClick={() => onFeedback("hide")}>
                  Hide
                </Button>
              </div>
            ) : null}
          </>
        ) : (
          <>
            <div className="button-row">
              <Button icon={Eye} onClick={onInspect}>
                Inspect
              </Button>
              <Button icon={Download} onClick={onFetchPreview}>
                Fetch Preview
              </Button>
              <Button variant="primary" icon={Download} onClick={onFetchFull}>
                Fetch Full
              </Button>
            </div>
            {onFeedback ? (
              <div className="button-row recommendation-feedback">
                <Button variant="ghost" icon={ThumbsUp} onClick={() => onFeedback("more")}>
                  More like this
                </Button>
                <Button variant="ghost" icon={ThumbsDown} onClick={() => onFeedback("less")}>
                  Less
                </Button>
                <Button variant="ghost" onClick={() => onFeedback("hide")}>
                  Hide
                </Button>
              </div>
            ) : null}
          </>
        )}
      </div>
      <aside className="receipt-mini">
        <span className="eyebrow">Receipts</span>
        <KV
          rows={[
            {
              label: item.starter ? "Source choice" : item.external ? "Source" : "Publisher",
              value: item.starter || item.external ? item.source_peer_name ?? item.source_platform ?? "starter" : <PeerPill peer={publisher} />,
            },
            { label: "Safety", value: <SafetyBadge outcome={item.safety_outcome} /> },
            { label: "Provenance", value: <ProvBadge status={item.provenance_status} /> },
            { label: "Weight", value: <WeightBar value={item.distribution_weight} /> },
            { label: "Priority", value: <span className="mono">{Math.round(rec.priority * 100)}%</span> },
          ]}
        />
      </aside>
    </article>
  );
}

export function ProvenanceTimeline({
  events,
  peersById,
}: {
  events: ProvenanceEvent[];
  peersById: Map<string, Peer>;
}) {
  if (!events.length) {
    return (
      <EmptyState
        icon={Link2}
        title="No provenance chain"
        body="This item has no signed history attached to its manifest."
      />
    );
  }
  return (
    <div className="timeline">
      {events.map((event) => {
        const peer = peersById.get(event.actor);
        return (
          <div className="timeline-event" key={event.hash}>
            <span className={`timeline-node${event.flagged ? " timeline-warn" : ""}`}>
              <EventIcon kind={event.kind} />
            </span>
            <div>
              <div className="timeline-title">
                <strong>{event.label}</strong>
                {event.flagged ? (
                  <Chip tone="warn" icon={AlertTriangle}>
                    Flagged
                  </Chip>
                ) : null}
              </div>
              <div className="timeline-meta">
                <span className="mono">{new Date(event.t).toLocaleString()}</span>
                <span>{peer?.name ?? event.actor}</span>
                <Hash value={event.hash} />
              </div>
              {event.notes ? <p>{event.notes}</p> : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function ConfirmDialog({
  request,
  onCancel,
}: {
  request: import("../domain/types").ConfirmRequest | null;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(request);
  active.current = request;
  const running = useRef(false);
  const dialog = useRef<HTMLDivElement>(null);
  const failure = useRef<HTMLParagraphElement>(null);
  const cancel = useRef(onCancel);
  cancel.current = onCancel;
  useEffect(() => {
    if (!request) return;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (!dialog.current || Array.from(document.querySelectorAll('[role="dialog"]')).at(-1) !== dialog.current) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!running.current) cancel.current(); }
      if (event.key !== "Tab") return;
      const buttons = Array.from(dialog.current.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
      if (!buttons.length) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === buttons[0] || !dialog.current.contains(document.activeElement))) { event.preventDefault(); buttons.at(-1)?.focus(); }
      else if (!event.shiftKey && (document.activeElement === buttons.at(-1) || !dialog.current.contains(document.activeElement))) { event.preventDefault(); buttons[0]?.focus(); }
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      document.removeEventListener("keydown", keyboard);
      const focused = document.activeElement as HTMLElement | null;
      // An operation may focus its persistent result before this dialog closes.
      // Keep that destination instead of jumping to a removed/disabled trigger.
      if (focused?.isConnected && focused.matches('[role="alert"], [role="status"]') && !dialog.current?.contains(focused)) return;
      if (previous?.isConnected && !previous.matches(":disabled")) previous.focus();
      else document.querySelector<HTMLElement>(".app-main button:not(:disabled), .app-main select")?.focus();
    };
  }, [request]);
  useEffect(() => { running.current = false; setBusy(false); setError(""); }, [request]);
  useEffect(() => { if (error) failure.current?.focus(); }, [error]);
  if (!request) return null;
  const run = async () => {
    if (running.current) return;
    const current = request;
    running.current = true; setBusy(true); setError("");
    try {
      await current.onConfirm();
      if (active.current === current) onCancel();
    } catch (cause) {
      if (active.current === current) setError(cause instanceof Error ? cause.message : "This operation could not be confirmed. Retry.");
    } finally {
      if (active.current === current) { running.current = false; setBusy(false); }
    }
  };
  return (
    <div className="modal-backdrop" role="presentation">
      <div ref={dialog} className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <div className="dialog-risk">
          <Chip tone={request.risk === "high" ? "danger" : request.risk === "medium" ? "warn" : "info"}>
            {request.risk} risk
          </Chip>
        </div>
        <h2 id="confirm-title">{request.title}</h2>
        <p>{request.body}</p>
        {error ? <p ref={failure} tabIndex={-1} role="alert">{error}</p> : null}
        {request.details?.length ? <KV rows={request.details} /> : null}
        <div className="dialog-actions">
          <Button variant="ghost" disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
          <Button variant={request.risk === "high" ? "danger" : "primary"} disabled={busy} onClick={run}>
            {busy ? "Working…" : request.confirmLabel ?? "Confirm"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function Toast({
  toast,
  onDismiss,
}: {
  toast: import("../domain/types").ToastMessage | null;
  onDismiss: () => void;
}) {
  if (!toast) return null;
  return (
    <button className={`toast toast-${toast.tone}`} type="button" onClick={onDismiss}>
      {toast.tone === "ok" ? <Check size={16} /> : toast.tone === "danger" ? <X size={16} /> : <ChevronRight size={16} />}
      {toast.text}
    </button>
  );
}

export function LoadingPanel({ label = "Loading from local Ryn node" }: { label?: string }) {
  return (
    <div className="loading-panel">
      <Loader2 size={18} className="spin" />
      {label}
    </div>
  );
}
