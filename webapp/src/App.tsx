import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  BellRing,
  CircleHelp,
  PanelRightClose,
  RotateCcw,
  Server,
  Sparkles,
  Users,
} from "lucide-react";
import { ConfirmDialog, Hash, IconButton, LoadingPanel, NavIcons, PeerPill, Toast, Chip } from "./components/ui";
import { RynLockup, RynMark, RynWordmark } from "./brand/RynBrand";
import OnboardingTour, { ONBOARDING_VERSION } from "./components/OnboardingTour";
import FirstSuccessFlow from "./components/FirstSuccessFlow";
import type { AppOutletContext } from "./appContext";
import type { NodeClient } from "./domain/nodeClient";
import { digestApi, type DiscoveryStatus } from "./domain/digestClient";
import { makeFixtureNodeClient } from "./domain/fixtureNodeClient";
import { makeLiveNodeClient } from "./domain/liveNodeClient";
import { nodeControlBaseUrl } from "./domain/nodeUrl";
import { installNotificationNavigation, sendDiscoveryNotification } from "./domain/notifications";
import type { ConfirmRequest, FirstSuccessStatus, NodeSettings, NodeStatus, Peer, RegistryStatus, ToastMessage } from "./domain/types";
import Home from "./screens/Home";
import Digest from "./screens/Digest";
import Explore from "./screens/Explore";
import ItemDetail from "./screens/ItemDetail";
import AskRyn, { AskRynQuickPanel } from "./screens/AskRyn";
import Publish from "./screens/Publish";
import Peers from "./screens/Peers";
import Friends from "./screens/Friends";
import Devices from "./screens/Devices";
import Search from "./screens/Search";
import FriendFeed from "./screens/FriendFeed";
import OfflineReading from "./screens/OfflineReading";
import Reading from "./screens/Reading";
import Services from "./screens/Services";
import ServicesCatalog from "./screens/ServicesCatalog";
import VideoRendering from "./screens/VideoRendering";
import SecureWebAccess from "./screens/SecureWebAccess";
import Chat from "./screens/Chat";
import Settings from "./screens/Settings";
import UnlockGate from "./screens/components/UnlockGate";

const navItems = [
  { path: "/", label: "Home", icon: NavIcons.home },
  { path: "/digest", label: "For You", icon: NavIcons.digest },
  { path: "/reading", label: "My reading", icon: NavIcons.digest },
  { path: "/explore", label: "Explore", icon: NavIcons.explore },
  { path: "/search", label: "Search", icon: NavIcons.searchAsk },
  { path: "/ask", label: "Ask Ryn", icon: NavIcons.searchAsk },
  { path: "/publish", label: "Publish", icon: NavIcons.publish },
  { path: "/peers", label: "Peers", icon: NavIcons.peers },
  { path: "/friends", label: "Friends", icon: Users },
  { path: "/friend-updates", label: "Friend updates", icon: Users },
  { path: "/offline", label: "Offline reading", icon: NavIcons.publish },
  { path: "/services", label: "Services", icon: NavIcons.services },
  { path: "/chat", label: "Chat", icon: NavIcons.chat },
  { path: "/settings", label: "Settings", icon: NavIcons.settings },
];

// Resolve the local Ryn node control-API base.
// Precedence: explicit env override > packaged-desktop default > dev default.
// - Dev (browser + Vite): undefined -> liveNodeClient uses "/api/local",
//   which the Vite dev proxy forwards to the daemon. No proxy exists in a
//   packaged build, so the Tauri shell must address the daemon directly.
function makeClient(): NodeClient {
  const params = new URLSearchParams(window.location.search);
  const explicit = params.get("client") ?? import.meta.env.VITE_RYN_NODE_CLIENT;
  if (explicit === "fixture") {
    return makeFixtureNodeClient();
  }
  return makeLiveNodeClient(nodeControlBaseUrl());
}

export default function App() {
  const navigate = useNavigate();
  const client = useMemo(makeClient, []);
  const [node, setNode] = useState<NodeStatus | null>(null);
  const [registry, setRegistry] = useState<RegistryStatus | null>(null);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [settings, setSettings] = useState<NodeSettings | null>(null);
  const [offline, setOffline] = useState(false);
  const [booting, setBooting] = useState(true);
  const [aiOpen, setAiOpen] = useState(false);
  const [tourOpen, setTourOpen] = useState(false);
  const [firstSuccess, setFirstSuccess] = useState<FirstSuccessStatus | null>(null);
  const [firstSuccessOpen, setFirstSuccessOpen] = useState(false);
  const firstSuccessEvaluated = useRef(false);
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const [discovery, setDiscovery] = useState<DiscoveryStatus | null>(null);
  const lastUnread = useRef(0);
  const firstSuccessEnabled = import.meta.env.VITE_RYN_FIRST_SUCCESS_V1_ENABLED !== "0";

  const notify = useCallback((tone: ToastMessage["tone"], text: string) => {
    const message = { id: crypto.randomUUID(), tone, text };
    setToast(message);
    window.setTimeout(() => {
      setToast((current) => (current?.id === message.id ? null : current));
    }, 3200);
  }, []);

  const refreshShell = useCallback(async () => {
    try {
      const [nodeStatus, registryStatus, peerList, nodeSettings, successStatus] = await Promise.all([
        client.getNodeStatus(),
        client.getRegistryStatus(),
        client.listPeers(),
        client.getSettings(),
        firstSuccessEnabled ? client.getFirstSuccess().catch(() => null) : Promise.resolve(null),
      ]);
      setNode(nodeStatus);
      setRegistry(registryStatus);
      setPeers(peerList);
      setSettings(nodeSettings);
      if (successStatus) setFirstSuccess(successStatus);
      if (successStatus && !firstSuccessEvaluated.current) {
        firstSuccessEvaluated.current = true;
        setFirstSuccessOpen(!successStatus.completed && !successStatus.dismissed);
      }
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, [client, firstSuccessEnabled]);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", "dark");
    let cancelled = false;
    void (async () => {
      // The packaged desktop app launches the Ryn node as a sidecar; it can
      // take ~1-2s to accept connections. Poll briefly so the app shows the
      // loading state instead of flashing the offline shell on first paint.
      const maxAttempts = 12;
      for (let attempt = 0; attempt < maxAttempts && !cancelled; attempt += 1) {
        try {
          await client.getNodeStatus();
          break;
        } catch {
          if (attempt === maxAttempts - 1) break;
          await new Promise((resolve) => setTimeout(resolve, 800));
        }
      }
      if (cancelled) return;
      await refreshShell();
      if (!cancelled) setBooting(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [client, refreshShell]);

  useEffect(() => {
    if (client.mode !== "live") return;
    let active = true;
    const update = () => {
      void digestApi.getDiscoveryStatus().then((status) => {
        if (!active) return;
        setDiscovery(status);
        if (settings && status.unread_count > lastUnread.current) {
          void sendDiscoveryNotification(settings, status.unread_count, () => navigate("/digest"));
        }
        lastUnread.current = status.unread_count;
      }).catch(() => undefined);
    };
    update();
    const timer = window.setInterval(update, 5000);
    const seen = () => update();
    window.addEventListener("ryn-discovery-seen", seen);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("ryn-discovery-seen", seen);
    };
  }, [client, navigate, settings]);

  useEffect(() => {
    let remove: () => void = () => {};
    void installNotificationNavigation(() => navigate("/digest")).then((unregister) => {
      remove = unregister;
    }).catch(() => undefined);
    return () => remove();
  }, [navigate]);

  const openDiscovery = async () => {
    navigate("/digest");
    const next = await digestApi.markDiscoverySeen().catch(() => null);
    if (next) setDiscovery(next);
  };

  if (!node || !registry || !settings) {
    if (booting) return <LoadingPanel />;
    if (offline) return <OfflineShell onRetry={refreshShell} />;
    return <LoadingPanel />;
  }

  const context: AppOutletContext = {
    client,
    node,
    registry,
    peers,
    firstSuccess,
    openFirstSuccess: () => setFirstSuccessOpen(true),
    refreshShell,
    refreshFirstSuccess: async () => {
      const next = await client.getFirstSuccess();
      setFirstSuccess(next);
      return next;
    },
    confirm: setConfirmRequest,
    notify,
  };

  return (
    <div className={`app-shell${aiOpen ? " ai-open" : ""}`}>
      <TopBar
        node={node}
        registry={registry}
        peerCount={Math.max(0, peers.filter((peer) => !peer.isSelf).length)}
        aiOpen={aiOpen}
        onToggleAi={() => setAiOpen((open) => !open)}
        onOpenTour={() => setTourOpen(true)}
        discovery={discovery}
        onOpenDiscovery={() => void openDiscovery()}
        clientMode={client.mode}
      />
      {offline ? <OfflineBanner onRetry={refreshShell} /> : null}
      <div className="app-grid">
        <Sidebar node={node} peers={peers} unreadRecommendations={discovery?.unread_count ?? 0} />
        <main className="app-main">
          <Outlet context={context} />
        </main>
        <aside className="ai-panel" aria-hidden={!aiOpen}>
          {aiOpen ? <AskRynQuickPanel context={context} /> : null}
        </aside>
      </div>
      <ConfirmDialog request={confirmRequest} onCancel={() => setConfirmRequest(null)} />
      <Toast toast={toast} onDismiss={() => setToast(null)} />
      {tourOpen ? (
        <OnboardingTour
          node={node}
          settings={settings}
          peers={peers}
          onClose={() => setTourOpen(false)}
          onComplete={async () => {
            const updated = await client.updateSettings({ onboarding_version: ONBOARDING_VERSION });
            setSettings(updated);
            setTourOpen(false);
          }}
        />
      ) : null}
      {firstSuccessOpen && firstSuccess ? (
        <FirstSuccessFlow
          client={client}
          status={firstSuccess}
          onStatusChange={setFirstSuccess}
          onClose={() => setFirstSuccessOpen(false)}
        />
      ) : null}
    </div>
  );
}

function TopBar({
  node,
  registry,
  peerCount,
  aiOpen,
  onToggleAi,
  onOpenTour,
  discovery,
  onOpenDiscovery,
  clientMode,
}: {
  node: NodeStatus;
  registry: RegistryStatus;
  peerCount: number;
  aiOpen: boolean;
  onToggleAi: () => void;
  onOpenTour: () => void;
  discovery: DiscoveryStatus | null;
  onOpenDiscovery: () => void;
  clientMode: NodeClient["mode"];
}) {
  return (
    <header className="topbar">
      <div className="brand-lockup">
        <span className="brand-mark">
          <RynMark size={30} />
        </span>
        <RynWordmark size={20} />
        <span className="mono">local console - {node.version}</span>
      </div>
      <div className="topbar-status">
        <Chip tone={node.daemon_running ? "ok" : "danger"} icon={Activity}>
          <span className="pulse-dot" />
          daemon
        </Chip>
        <Chip tone={registry.status === "connected" ? "info" : "warn"} icon={Server}>
          registry
        </Chip>
        <Chip tone="muted" icon={Users}>
          {peerCount} peers
        </Chip>
        <Chip tone={clientMode === "fixture" ? "warn" : "ok"}>{clientMode}</Chip>
        {discovery?.unread_count ? (
          <button
            type="button"
            className="discovery-notice is-unread"
            onClick={onOpenDiscovery}
            aria-label={`${discovery.unread_count} new recommendations available`}
          >
            <BellRing size={16} />
            <span>{discovery.unread_count} new</span>
          </button>
        ) : discovery?.phase === "refreshing" ? (
          <span className="discovery-notice is-working">
            <Sparkles size={15} /> discovering
          </span>
        ) : null}
        <IconButton icon={CircleHelp} label="Open getting started guide" onClick={onOpenTour} />
        <IconButton icon={aiOpen ? PanelRightClose : Sparkles} label="Toggle Ask Ryn panel" active={aiOpen} onClick={onToggleAi} />
      </div>
    </header>
  );
}

function Sidebar({ node, peers, unreadRecommendations }: { node: NodeStatus; peers: Peer[]; unreadRecommendations: number }) {
  const location = useLocation();
  const self = peers.find((peer) => peer.isSelf) ?? peers[0];
  return (
    <aside className="sidebar">
      <div className="nav-caption">Console</div>
      <nav>
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = item.path === "/explore" && location.pathname.startsWith("/items");
          return (
            <NavLink key={item.path} to={item.path} className={({ isActive }) => (isActive || active ? "active" : "")}>
              <Icon size={15} />
              {item.label}
              {item.path === "/digest" && unreadRecommendations ? <span className="nav-unread-badge">{unreadRecommendations}</span> : null}
            </NavLink>
          );
        })}
      </nav>
      <div className="this-node-card">
        <span className="eyebrow">This node</span>
        <span className="sidebar-brand">
          <RynMark size={30} />
          <RynWordmark product="" size={19} muted />
        </span>
        <PeerPill peer={self} />
        <Hash value={node.peer_id} />
        <div className="node-card-metrics">
          <span>
            <b>{node.local_items}</b>
            local
          </span>
          <span>
            <b>{node.fetched_items}</b>
            fetched
          </span>
        </div>
      </div>
    </aside>
  );
}

function OfflineBanner({ onRetry }: { onRetry: () => Promise<void> }) {
  return (
    <div className="offline-banner">
      <AlertTriangle size={16} />
      Cannot reach local Ryn node daemon.
      <button type="button" onClick={() => void onRetry()}>
        Retry
      </button>
    </div>
  );
}

function OfflineShell({ onRetry }: { onRetry: () => Promise<void> }) {
  return (
    <div className="offline-shell">
      <div className="offline-card">
        <RynLockup />
        <AlertTriangle size={28} />
        <h1>Cannot reach local Ryn node daemon</h1>
        <p>The webapp is only a control surface. Start the local Ryn node, then retry.</p>
        <button type="button" onClick={() => void onRetry()}>
          <RotateCcw size={15} />
          Retry
        </button>
      </div>
    </div>
  );
}

export function AppRoutes() {
  return (
    <UnlockGate>
      <Routes>
        <Route path="/" element={<App />}>
          <Route index element={<Home />} />
          <Route path="digest" element={<Digest />} />
          <Route path="explore" element={<Explore />} />
          <Route path="search" element={<Search />} />
          <Route path="friend-updates" element={<FriendFeed />} />
          <Route path="offline" element={<OfflineReading />} />
          <Route path="reading" element={<Reading />} />
          <Route path="items/:contentId" element={<ItemDetail />} />
          <Route path="recommendations" element={<Navigate replace to="/digest" />} />
          <Route path="ask" element={<AskRyn />} />
          <Route path="search-ask" element={<LegacyAskRedirect />} />
          <Route path="publish" element={<Publish />} />
          <Route path="peers" element={<Peers />} />
          <Route path="friends" element={<Friends />} />
          <Route path="devices" element={<Devices />} />
          <Route path="services" element={<ServicesCatalog />} />
          <Route path="services/manage" element={<Services />} />
          <Route path="services/private-ai/chat" element={<LegacyAskRedirect />} />
          <Route path="services/video-rendering" element={<VideoRendering />} />
          <Route path="services/secure-web-access" element={<SecureWebAccess />} />
          <Route path="chat" element={<Chat />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </UnlockGate>
  );
}

function LegacyAskRedirect() {
  const location = useLocation();
  return <Navigate replace to={`/ask${location.search}`} />;
}
