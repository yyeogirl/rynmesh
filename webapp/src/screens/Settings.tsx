import { Activity, BellRing, Cloud, Download, DownloadCloud, HardDrive, History, Network, Save, ShieldCheck, SlidersHorizontal, Sparkles, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, Chip, KV, LoadingPanel, PageHeader, Panel } from "../components/ui";
import type { NodeClient } from "../domain/nodeClient";
import { requestDesktopNotificationPermission, sendTestNotification } from "../domain/notifications";
import AccessPanel from "./components/AccessPanel";
import LocalModelPicker from "./components/LocalModelPicker";
import PrivateCopiesPanel from "./components/PrivateCopiesPanel";
import ConversationCleanupPanel from "./components/ConversationCleanupPanel";
import BrowserConversationCleanup from "./components/BrowserConversationCleanup";
import ProductExportPanel from "./components/ProductExportPanel";
import ReadingCleanupPanel from "./components/ReadingCleanupPanel";
import FeedCleanupPanel from "./components/FeedCleanupPanel";
import type { ActivityEvent, NodeSettings, PrivacyEraseScope, PrivacyStatus, UpdateStatus } from "../domain/types";

const sections = [
  "Identity & storage",
  "Network",
  "Trust & safety",
  "AI curator",
  "Notifications",
  "Privacy & data",
  "Ranking & publish",
  "Fetch limits",
  "Software updates",
] as const;

export default function Settings() {
  const { client, confirm, notify, refreshShell } = useAppContext();
  const [active, setActive] = useState<(typeof sections)[number]>("Identity & storage");
  const [settings, setSettings] = useState<NodeSettings | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const loadResult = useRef<HTMLParagraphElement>(null);
  const [privacyRevision, setPrivacyRevision] = useState(0);
  const readingChanged = useCallback(() => setPrivacyRevision((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setSettings(null);
    setLoadError("");
    void client.getSettings().then((value) => {
      if (active) setSettings(value);
    }).catch(() => {
      if (active) setLoadError("Settings could not be loaded. Check the local node connection, then retry.");
    });
    return () => { active = false; };
  }, [client, loadAttempt]);

  useEffect(() => { if (loadError) loadResult.current?.focus(); }, [loadError]);

  if (!settings) return loadError ? <Panel>
    <p ref={loadResult} tabIndex={-1} role="alert">{loadError}</p>
    <Button onClick={() => { setLoadError(""); setLoadAttempt((value) => value + 1); }}>Retry settings</Button>
  </Panel> : <LoadingPanel />;

  const update = async (patch: Partial<NodeSettings>) => {
    const next = await client.updateSettings(patch);
    setSettings(next);
    await refreshShell();
    notify("ok", "Settings updated through local node");
  };

  const confirmUpdate = (patch: Partial<NodeSettings>, title: string, body: string) =>
    confirm({
      title,
      body,
      risk: "high",
      confirmLabel: "Apply change",
      onConfirm: () => update(patch),
    });

  return (
    <div className="settings-layout">
      <PageHeader
        eyebrow="Settings"
        title="Node policy"
        context="Configuration and personal-assistant data belong to this local node."
        actions={<Chip tone="info">local control API</Chip>}
      />
      <aside className="settings-rail">
        <Link to="/devices">My devices</Link>
        {sections.map((section) => (
          <button key={section} type="button" className={active === section ? "active" : ""} onClick={() => setActive(section)}>
            {section}
          </button>
        ))}
      </aside>
      <Panel className="settings-panel">
        {active === "Identity & storage" ? <IdentitySection settings={settings} onUpdate={update} /> : null}
        {active === "Network" ? <NetworkSection settings={settings} /> : null}
        {active === "Trust & safety" ? (
          <TrustSection settings={settings} onConfirmUpdate={confirmUpdate} />
        ) : null}
        {active === "AI curator" ? (
          <AISection settings={settings} onUpdate={update} onConfirmUpdate={confirmUpdate} />
        ) : null}
        {active === "Notifications" ? (
          <NotificationsSection settings={settings} onUpdate={update} notify={notify} />
        ) : null}
        {active === "Privacy & data" ? (
          <><PrivacySection client={client} confirm={confirm} notify={notify} revision={privacyRevision} />{client.mode === "live" ? <><ProductExportPanel /><ReadingCleanupPanel onChange={readingChanged} /><FeedCleanupPanel /></> : null}<ConversationCleanupPanel /><BrowserConversationCleanup /></>
        ) : null}
        {active === "Ranking & publish" ? <RankingSection settings={settings} onUpdate={update} /> : null}
        {active === "Fetch limits" ? <FetchSection settings={settings} onUpdate={update} /> : null}
        {active === "Software updates" ? <UpdatesSection client={client} /> : null}
      </Panel>
    </div>
  );
}

export function PrivacySection({
  client,
  confirm,
  notify,
  revision,
}: {
  client: NodeClient;
  confirm: ReturnType<typeof useAppContext>["confirm"];
  notify: (tone: "ok" | "warn" | "danger", text: string) => void;
  revision: number;
}) {
  const [status, setStatus] = useState<PrivacyStatus | null>(null);
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [exportNotice, setExportNotice] = useState("");
  const exportPending = useRef(false);
  const exportResult = useRef<HTMLParagraphElement>(null);

  const reload = useCallback(async () => {
    setLoading(true); setLoadError("");
    try {
      const [nextStatus, nextEvents] = await Promise.all([
        client.getPrivacyStatus(), client.getActivity(),
      ]);
      setStatus(nextStatus); setEvents(nextEvents);
    } catch {
      setLoadError("The local data summary could not be refreshed. Check the node connection and retry.");
    } finally { setLoading(false); }
  }, [client]);

  useEffect(() => {
    void reload();
  }, [reload, revision]);

  useEffect(() => { if (exportError || exportNotice) exportResult.current?.focus(); }, [exportError, exportNotice]);

  const download = async () => {
    if (exportPending.current) return;
    exportPending.current = true;
    setExporting(true); setExportError(""); setExportNotice("");
    try {
      const payload = await client.exportPersonalData();
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const revoke = URL.revokeObjectURL.bind(URL);
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `ryn-personal-data-${new Date().toISOString().slice(0, 10)}.json`;
        anchor.click();
      } finally { window.setTimeout(() => revoke(url), 1000); }
      setExportNotice("Reading and preferences export prepared; download requested. Check your browser downloads.");
    } catch {
      setExportError("The node did not return a complete reading and preferences export. Check the connection and retry.");
    } finally { exportPending.current = false; setExporting(false); }
  };

  const erase = (scopes: PrivacyEraseScope[], title: string, body: string) => {
    confirm({
      title,
      body,
      risk: "high",
      confirmLabel: "Erase local data",
      onConfirm: async () => {
        try { await client.erasePersonalData(scopes); }
        catch { throw new Error("The selected data could not be fully erased. Check the node connection and storage access, then retry. Some steps may already have completed."); }
        await reload();
        notify("ok", "Selected personal data erased from this node");
      },
    });
  };

  return (
    <Section title="Privacy & local data" icon={<ShieldCheck size={22} />}>
      <div className="privacy-local-callout">
        <HardDrive size={18} />
        <span>
          <b>Manage this node's profile, reading history, cached content and audit trail.</b>
          <small>Discovery contacts your chosen source sites. Sharing, device sync and AI requests have their own permissions.</small>
        </span>
      </div>
      {status ? (
        <KV rows={[
          { label: "Storage root", value: status.storage_root },
          { label: "Reading history", value: `${status.reading_history_items} items` },
          { label: "Recommendation learning", value: `${status.feedback_items} feedback · ${status.learned_signals} signals` },
          { label: "Discovery cache", value: `${status.cached_discovery_items} items · ${status.reader_cache_files} reader pages` },
          { label: "Assistant audit", value: `${status.audit_events} events` },
          { label: "Cloud AI", value: status.cloud_ai_enabled ? "enabled" : "disabled" },
        ]} />
      ) : loading ? <p className="muted">Inspecting local data…</p> : null}
      {loadError ? <p role="alert">{loadError} <Button disabled={loading} onClick={() => void reload()}>Retry data summary</Button></p> : null}
      <p ref={exportResult} tabIndex={-1} role={exportError ? "alert" : "status"}>{exportError || exportNotice}</p>
      <div className="button-row">
        <Button icon={Download} variant="primary" disabled={exporting} onClick={() => void download()}>
          {exporting ? "Preparing reading export…" : "Export reading & preferences (JSON)"}
        </Button>
        {client.mode !== "live" ? <Button icon={History} onClick={() => erase(["history"], "Clear reading history?", "This erases opened items, bookmarks, playback position, and reading progress from this node.")}>
          Clear history
        </Button> : null}
        <Button icon={Trash2} onClick={() => erase(["profile"], "Reset recommendation learning?", "This erases your direction, topic and platform choices, and all more/less/hide feedback.")}>
          Reset learning
        </Button>
        <Button icon={Trash2} onClick={() => erase(["cache"], "Clear downloaded discovery cache?", "This erases feed results and locally extracted reader pages. Default sources remain and will refill the feed on the next discovery cycle.")}>
          Clear cache
        </Button>
        <Button icon={Trash2} variant="danger" onClick={() => erase(["audit"], "Clear assistant audit?", "This permanently erases the local record of assistant activity.")}>
          Clear audit
        </Button>
      </div>
      <div className="privacy-audit">
        {client.mode === "live" ? <PrivateCopiesPanel /> : null}
        <div className="privacy-audit-head"><Activity size={16} /><b>Recent assistant activity</b></div>
        {events.slice(0, 8).map((event) => (
          <div className="privacy-audit-row" key={event.id ?? `${event.t}-${event.text}`}>
            <span>{event.text}</span>
            <small>{event.t ? new Date(event.t).toLocaleString() : ""}</small>
          </div>
        ))}
        {!events.length ? <p className="muted">No assistant activity recorded yet.</p> : null}
      </div>
    </Section>
  );
}

function NotificationsSection({
  settings,
  onUpdate,
  notify,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
  notify: (tone: "ok" | "warn" | "danger", text: string) => void;
}) {
  const enable = async (enabled: boolean) => {
    if (enabled) {
      const permission = await requestDesktopNotificationPermission();
      if (permission !== "granted") {
        notify("warn", "Desktop notification permission was not granted");
        await onUpdate({ notifications_enabled: false });
        return;
      }
    }
    await onUpdate({ notifications_enabled: enabled });
  };

  return (
    <Section title="Notifications" icon={<BellRing size={22} />}>
      <label className="toggle-row">
        <input
          type="checkbox"
          checked={settings.notifications_enabled}
          onChange={(event) => void enable(event.target.checked)}
        />
        Notify me when new recommendations are ready
      </label>
      <div className="setting-row">
        <span>
          <b>Frequency</b>
          <small>The in-app badge always updates immediately</small>
        </span>
        <select
          value={settings.notification_frequency}
          onChange={(event) => void onUpdate({
            notification_frequency: event.target.value as NodeSettings["notification_frequency"],
          })}
        >
          <option value="immediate">Immediate</option>
          <option value="hourly">At most hourly</option>
          <option value="daily">At most daily</option>
        </select>
      </div>
      <div className="setting-row">
        <span>
          <b>Quiet hours</b>
          <small>Local time, start inclusive and end exclusive</small>
        </span>
        <div className="setting-edit">
          <input
            type="number"
            min="0"
            max="23"
            value={settings.notification_quiet_start}
            onChange={(event) => void onUpdate({ notification_quiet_start: Number(event.target.value) })}
            aria-label="Quiet hours start"
          />
          <span>to</span>
          <input
            type="number"
            min="0"
            max="23"
            value={settings.notification_quiet_end}
            onChange={(event) => void onUpdate({ notification_quiet_end: Number(event.target.value) })}
            aria-label="Quiet hours end"
          />
        </div>
      </div>
      <Button
        icon={BellRing}
        onClick={async () => {
          const sent = await sendTestNotification(() => window.location.assign("/digest"));
          notify(sent ? "ok" : "warn", sent ? "Test notification sent" : "Notification permission is unavailable");
        }}
      >
        Send test notification
      </Button>
    </Section>
  );
}

function IdentitySection({
  settings,
  onUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
}) {
  return (
    <Section title="Identity & storage" icon={<HardDrive size={22} />}>
      <SettingInput label="Node name" value={settings.node_name} onSave={(value) => onUpdate({ node_name: value })} />
      <ReadOnly label="Storage" value={settings.node_storage} />
      <ReadOnly label="Trusted roots" value={settings.trusted_roots.join(", ")} />
    </Section>
  );
}

function NetworkSection({
  settings,
}: {
  settings: NodeSettings;
}) {
  return (
    <Section title="Network" icon={<Network size={22} />}>
      <div className="alert-callout">
        Desktop networking is configured automatically at startup. Advanced operators can override RYNMESH_* environment variables before launch.
      </div>
      <ReadOnly label="Mode" value={settings.desktop_managed ? "automatic desktop" : "operator managed"} />
      <ReadOnly label="Network" value={settings.network_id ?? "rynmesh-main"} />
      <ReadOnly label="Registry URL" value={settings.registry_url} />
      <ReadOnly label="Peer HTTP host" value={settings.peer_http_host} />
      <ReadOnly label="Public endpoint" value={settings.public_endpoint} />
      <ReadOnly label="Peer HTTP port" value={String(settings.peer_http_port)} />
    </Section>
  );
}

function TrustSection({
  settings,
  onConfirmUpdate,
}: {
  settings: NodeSettings;
  onConfirmUpdate: (patch: Partial<NodeSettings>, title: string, body: string) => void;
}) {
  return (
    <Section title="Trust & safety" icon={<ShieldCheck size={22} />}>
      <div className="setting-row">
        <span>
          <b>Safety policy</b>
          <small>High-risk local policy change</small>
        </span>
        <div className="segmented">
          {(["permissive", "standard", "strict"] as const).map((policy) => (
            <button
              key={policy}
              type="button"
              className={settings.safety_policy === policy ? "active" : ""}
              onClick={() =>
                onConfirmUpdate(
                  { safety_policy: policy },
                  "Change safety policy?",
                  "This affects what the local node will fetch, propagate, and publish.",
                )
              }
            >
              {policy}
            </button>
          ))}
        </div>
      </div>
      <ReadOnly label="Trusted root peer IDs" value={settings.trusted_roots.join(", ")} />
      <AccessPanel />
    </Section>
  );
}

function AISection({
  settings,
  onUpdate,
  onConfirmUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
  onConfirmUpdate: (patch: Partial<NodeSettings>, title: string, body: string) => void;
}) {
  return (
    <Section title="AI curator" icon={<Sparkles size={22} />}>
      <div className="setting-row">
        <span>
          <b>Provider</b>
          <small>Local model is the privacy-preserving default</small>
        </span>
        <div className="segmented">
          <button type="button" className={settings.ai_provider === "local" ? "active" : ""} onClick={() => void onUpdate({ ai_provider: "local", cloud_access: false })}>
            local
          </button>
          <button
            type="button"
            className={settings.ai_provider === "cloud" ? "active" : ""}
            onClick={() =>
              onConfirmUpdate(
                { ai_provider: "cloud", cloud_access: true },
                "Enable cloud model access?",
                "Bounded metadata can leave the machine. Full local files still require separate confirmation.",
              )
            }
          >
            cloud
          </button>
        </div>
      </div>
      <div className="setting-row setting-row-stacked">
        <span>
          <b>Local model</b>
          <small>Runs on this machine — nothing leaves it</small>
        </span>
      </div>
      <LocalModelPicker />
      <ReadOnly label="Cloud access" value={settings.cloud_access ? "enabled" : "disabled"} icon={<Cloud size={14} />} />
    </Section>
  );
}

function RankingSection({
  settings,
  onUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
}) {
  return (
    <Section title="Ranking & publish" icon={<SlidersHorizontal size={22} />}>
      <div className="setting-row">
        <span>
          <b>Default rank</b>
          <small>Used by Explore and recommendations</small>
        </span>
        <select value={settings.rank_default} onChange={(event) => void onUpdate({ rank_default: event.target.value as NodeSettings["rank_default"] })}>
          {["weight", "newest", "trusted", "ai", "novelty"].map((rank) => (
            <option key={rank}>{rank}</option>
          ))}
        </select>
      </div>
      <div className="setting-row">
        <span>
          <b>Publish visibility</b>
          <small>Default for new publish drafts</small>
        </span>
        <select value={settings.publish_visibility} onChange={(event) => void onUpdate({ publish_visibility: event.target.value as NodeSettings["publish_visibility"] })}>
          {["network", "trusted", "local"].map((visibility) => (
            <option key={visibility}>{visibility}</option>
          ))}
        </select>
      </div>
    </Section>
  );
}

function FetchSection({
  settings,
  onUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
}) {
  const pct = Math.round((settings.fetch_used_mb / settings.fetch_budget_mb) * 100);
  return (
    <Section title="Fetch limits" icon={<HardDrive size={22} />}>
      <KV
        rows={[
          { label: "Daily budget", value: `${settings.fetch_budget_mb} MB` },
          { label: "Used today", value: `${settings.fetch_used_mb} MB` },
          { label: "Timeout", value: `${settings.fetch_timeout_s}s` },
        ]}
      />
      <div className={`budget-bar${pct > 80 ? " budget-warn" : ""}`}>
        <span style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
      <SettingInput
        label="Fetch timeout seconds"
        value={String(settings.fetch_timeout_s)}
        onSave={(value) => onUpdate({ fetch_timeout_s: Number(value) || settings.fetch_timeout_s })}
      />
    </Section>
  );
}

function UpdatesSection({ client }: { client: NodeClient }) {
  const [upd, setUpd] = useState<UpdateStatus | null>(null);

  useEffect(() => {
    let alive = true;
    void client
      .updatesStatus()
      .then((s) => {
        if (alive) setUpd(s);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [client]);

  return (
    <Section title="Software updates" icon={<DownloadCloud size={22} />}>
      <p className="muted">
        Current version: <span className="mono">{upd?.currentVersion ?? "…"}</span>
      </p>
      <label className="toggle-row">
        <input
          type="checkbox"
          checked={upd?.autoUpdate ?? true}
          onChange={async (event) => {
            await client.setAutoUpdate(event.target.checked);
            setUpd(await client.updatesCheck());
          }}
        />
        Automatically install updates
      </label>
      {upd?.availableVersion ? (
        <div className="update-banner">
          <span>Version {upd.availableVersion} available.</span>
          <Button onClick={() => void client.updatesApply()}>Update now</Button>
        </div>
      ) : null}
      {upd?.lastError ? <p className="service-status error">⚠ {upd.lastError}</p> : null}
    </Section>
  );
}

function Section({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div className="settings-section">
      <div className="section-title">
        {icon}
        <div>
          <span className="eyebrow">Settings</span>
          <h2>{title}</h2>
        </div>
      </div>
      {children}
    </div>
  );
}

function ReadOnly({ label, value, icon }: { label: string; value: string; icon?: ReactNode }) {
  return (
    <div className="setting-row">
      <span>
        <b>{label}</b>
        <small>Read from local node</small>
      </span>
      <code>
        {icon}
        {value}
      </code>
    </div>
  );
}

function SettingInput({
  label,
  value,
  onSave,
}: {
  label: string;
  value: string;
  onSave: (value: string) => Promise<void>;
}) {
  const [current, setCurrent] = useState(value);
  return (
    <div className="setting-row">
      <span>
        <b>{label}</b>
        <small>Saved through local node settings</small>
      </span>
      <div className="setting-edit">
        <input value={current} onChange={(event) => setCurrent(event.target.value)} />
        <Button icon={Save} onClick={() => void onSave(current)}>
          Save
        </Button>
      </div>
    </div>
  );
}
