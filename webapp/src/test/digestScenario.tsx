import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse, type HttpHandler } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import type {
  ConsumptionRecord,
  Digest,
  DigestSource,
  DiscoveryStatus,
  ReaderArticle,
  Watcher,
} from "../domain/digestClient";
import type { NodeClient } from "../domain/nodeClient";
import type { RecommendationProfile } from "../domain/types";
import DigestScreen from "../screens/Digest";
import {
  ARTICLE_FIXTURE,
  DEFAULT_SOURCES,
  FIXED_UNIX,
  makeConsumptionRecord,
  makeDigest,
  makeDiscoveryStatus,
  makeProfile,
  TEST_API_BASE,
} from "./fixtures";
import { server } from "./server";

interface DigestScenarioOptions {
  digest?: Digest;
  discovery?: DiscoveryStatus;
  sources?: DigestSource[];
  watchers?: Watcher[];
  consumption?: ConsumptionRecord[];
  profile?: RecommendationProfile;
  article?: ReaderArticle;
  handlers?: HttpHandler[];
}

type ProfilePatch = Partial<Pick<RecommendationProfile, "direction" | "topics" | "platforms">>;

export function createDigestScenario(options: DigestScenarioOptions = {}) {
  let digest = options.digest ?? makeDigest();
  let discovery = options.discovery ?? makeDiscoveryStatus({ item_count: digest.items.length });
  let profile = options.profile ?? makeProfile();
  let consumption = [...(options.consumption ?? [])];
  const sources = options.sources ?? DEFAULT_SOURCES;
  const watchers = options.watchers ?? [];
  const article = options.article ?? ARTICLE_FIXTURE;

  const requests = {
    feedback: [] as Array<{ item_id: string; action: string }>,
    consumption: [] as Array<{ item: Digest["items"][number]; action: string; progress?: number }>,
    profilePatches: [] as ProfilePatch[],
    refreshes: 0,
  };

  const getRecommendationProfile = vi.fn(async () => profile);
  const updateRecommendationProfile = vi.fn(async (patch: ProfilePatch) => {
    requests.profilePatches.push(patch);
    profile = { ...profile, ...patch, version: profile.version + 1 };
    return profile;
  });
  const profileClient: Pick<
    NodeClient,
    "mode" | "getRecommendationProfile" | "updateRecommendationProfile"
  > = {
    mode: "fixture",
    getRecommendationProfile,
    updateRecommendationProfile,
  };
  const client = profileClient as NodeClient;

  const handlers: HttpHandler[] = [
    http.post(`${TEST_API_BASE}/offline-reading/resolve`, () => HttpResponse.json(null)),
    http.get(`${TEST_API_BASE}/recommendations/signals`, () => HttpResponse.json({ items: [], total: 0, offset: 0, limit: 20 })),
    http.get(`${TEST_API_BASE}/digest`, () => HttpResponse.json(digest)),
    http.get(`${TEST_API_BASE}/sources`, () => HttpResponse.json(sources)),
    http.get(`${TEST_API_BASE}/watchers`, () => HttpResponse.json(watchers)),
    http.get(`${TEST_API_BASE}/ai/status`, () => HttpResponse.json({ provider: null, model: null })),
    http.post(`${TEST_API_BASE}/discovery/seen`, () => HttpResponse.json(discovery)),
    http.post(`${TEST_API_BASE}/digest/refresh`, () => {
      requests.refreshes += 1;
      return HttpResponse.json({ refresh: { new_items: digest.items.length }, digest, status: discovery });
    }),
    http.post(`${TEST_API_BASE}/digest/feedback`, async ({ request }) => {
      const body = (await request.json()) as { item_id: string; action: string };
      requests.feedback.push(body);
      if (body.action === "hide") digest = { ...digest, items: digest.items.filter((item) => item.item_id !== body.item_id) };
      return HttpResponse.json({ ok: true });
    }),
    http.get(`${TEST_API_BASE}/consumption`, () => HttpResponse.json(consumption)),
    http.post(`${TEST_API_BASE}/consumption`, async ({ request }) => {
      const body = (await request.json()) as {
        item: Digest["items"][number];
        action: string;
        progress?: number;
      };
      requests.consumption.push(body);
      const existing = consumption.find((record) => record.item_id === body.item.item_id);
      const next = {
        ...(existing ?? makeConsumptionRecord(body.item)),
        last_activity_unix: FIXED_UNIX,
        bookmarked:
          body.action === "bookmark"
            ? true
            : body.action === "unbookmark"
              ? false
              : existing?.bookmarked ?? false,
        progress: body.progress ?? existing?.progress ?? 0,
        completed: body.action === "completed" || existing?.completed === true,
      };
      consumption = [...consumption.filter((record) => record.item_id !== body.item.item_id), next];
      return HttpResponse.json(next);
    }),
    http.get(`${TEST_API_BASE}/reader`, () => HttpResponse.json(article)),
  ];

  return {
    handlers,
    requests,
    client,
    get profile() {
      return profile;
    },
    setDigest(next: Digest) {
      digest = next;
    },
    setDiscovery(next: DiscoveryStatus) {
      discovery = next;
    },
  };
}

export function renderDigest(options: DigestScenarioOptions = {}) {
  const scenario = createDigestScenario(options);
  server.use(...scenario.handlers);
  if (options.handlers) server.use(...options.handlers);

  const notify = vi.fn();
  const context: AppOutletContext = {
    client: scenario.client,
    node: {
      node_name: "Test Ryn",
      peer_id: "peer:test",
      daemon_running: true,
      registry: "connected",
      peer_count: 0,
      local_items: 0,
      fetched_items: 0,
      pending_recs: 0,
      version: "test",
      uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [],
    refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(),
    notify,
  };

  const result = render(
    <MemoryRouter>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route index element={<DigestScreen />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  return { ...result, user: userEvent.setup(), scenario, notify };
}
