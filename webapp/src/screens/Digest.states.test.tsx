import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { renderDigest } from "../test/digestScenario";
import {
  makeDigest,
  makeDigestItem,
  makeDiscoveryStatus,
  TEST_API_BASE,
} from "../test/fixtures";

describe("For You states", () => {
  it.each([false, true])("shows all-source failure truthfully with cached content=%s", async (cached) => {
    renderDigest({
      digest: makeDigest(cached ? [makeDigestItem()] : []),
      discovery: makeDiscoveryStatus({ item_count: cached ? 1 : 0, source_count: 14,
        healthy_sources: 0, failed_sources: 14, cached_sources: cached ? 14 : 0, degraded: true }),
    });
    expect(await screen.findByText(cached ? "using cached content" : "waiting for connection")).toBeInTheDocument();
    expect(screen.queryByText("using healthy sources")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
    if (!cached) expect(screen.getByRole("heading", { name: "No content available yet" })).toBeInTheDocument();
    else expect(screen.getByRole("button", { name: "A local-first assistant worth reading" })).toBeEnabled();
  });

  it("shows a ready first load with ranked recommendations", async () => {
    renderDigest();

    expect(await screen.findByRole("heading", { name: "For You" })).toBeInTheDocument();
    expect(screen.getByText("1 items are ready")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "A local-first assistant worth reading" })).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
  });

  it("shows discovery that is already refreshing without hiding existing items", async () => {
    renderDigest({
      discovery: makeDiscoveryStatus({ phase: "refreshing", message: "Reviewing sources now." }),
    });

    expect(await screen.findByText("agent discovering")).toBeInTheDocument();
    expect(screen.getByText("reviewing now")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "A local-first assistant worth reading" })).toBeInTheDocument();
  });

  it("keeps healthy recommendations usable when one source is degraded", async () => {
    renderDigest({
      discovery: makeDiscoveryStatus({
        degraded: true,
        source_count: 2,
        healthy_sources: 1,
        failed_sources: 1,
        cached_sources: 1,
        message: "One source is temporarily unavailable.",
      }),
    });

    expect(await screen.findByText("using healthy sources")).toBeInTheDocument();
    expect(screen.getByText(/1 source is temporarily unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/1 unavailable source is serving cached items/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "A local-first assistant worth reading" })).toBeEnabled();
  });

  it("shows an actionable empty state when no recommendations are ready", async () => {
    renderDigest({
      digest: makeDigest([]),
      discovery: makeDiscoveryStatus({ item_count: 0, new_items: 0, unread_count: 0, formats: [] }),
    });

    expect(await screen.findByRole("heading", { name: "The agent is reviewing fresh content" })).toBeInTheDocument();
    expect(screen.getByText("Ryn is collecting your first items")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it("reports a daemon error without crashing the screen", async () => {
    renderDigest({
      handlers: [
        http.get(`${TEST_API_BASE}/digest`, () =>
          HttpResponse.json({ detail: "Node daemon is unavailable" }, { status: 503 }),
        ),
      ],
    });

    expect(await screen.findByText("Node daemon is unavailable")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "For You" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it("disables refresh until the updated ranking arrives", async () => {
    let finishRefresh: (() => void) | undefined;
    const pendingRefresh = new Promise<void>((resolve) => {
      finishRefresh = resolve;
    });
    const refreshedItem = makeDigestItem({ item_id: "item-refreshed", title: "A freshly ranked item" });
    const refreshedDigest = makeDigest([refreshedItem]);
    const refreshedStatus = makeDiscoveryStatus({ message: "Refresh complete." });

    const { user } = renderDigest({
      handlers: [
        http.post(`${TEST_API_BASE}/digest/refresh`, async () => {
          await pendingRefresh;
          return HttpResponse.json({
            refresh: { new_items: 1 },
            digest: refreshedDigest,
            status: refreshedStatus,
          });
        }),
      ],
    });

    await screen.findByRole("button", { name: "A local-first assistant worth reading" });
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByRole("button", { name: "Refreshing…" })).toBeDisabled();

    finishRefresh?.();

    expect(await screen.findByRole("button", { name: "A freshly ranked item" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled());
  });
});
