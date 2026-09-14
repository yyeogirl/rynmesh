import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { digestApi } from "../domain/digestClient";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { Recommendation } from "../domain/types";
import { makeDiscoveryStatus } from "../test/fixtures";
import Home from "./Home";

let context: AppOutletContext;
let recommendation: Recommendation;
vi.mock("../appContext", () => ({ useAppContext: () => context }));

beforeEach(async () => {
  const client = makeFixtureNodeClient();
  const [node, registry, items, recs] = await Promise.all([
    client.getNodeStatus(), client.getRegistryStatus(), client.listContent(), client.requestRecommendations({ limit: 1 }),
  ]);
  const item = { ...items.find((row) => row.content_id === recs[0].contentId)!,
    title: "Available reading", content_kind: "document" as const, external: true };
  recommendation = { ...recs[0], item };
  vi.spyOn(client, "getActivity").mockResolvedValue([{ kind: "publish", text: "Saved local activity", t: "now" }]);
  vi.spyOn(client, "requestRecommendations").mockResolvedValue([recommendation]);
  vi.spyOn(client, "listContent").mockResolvedValue([item]);
  vi.spyOn(client, "listJobCapacities").mockResolvedValue([]);
  vi.spyOn(digestApi, "getDiscoveryStatus").mockResolvedValue(makeDiscoveryStatus({ item_count: 0 }));
  context = { client, node, registry, peers: [], notify: vi.fn(), confirm: vi.fn(), refreshShell: vi.fn() };
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

function showHome() { return render(<MemoryRouter><Home /></MemoryRouter>); }

it("keeps recommendations usable while an unrelated activity request is still pending", async () => {
  vi.mocked(context.client.getActivity).mockImplementation(() => new Promise(() => undefined));
  showHome();
  expect(await screen.findByRole("heading", { name: "Available reading" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Read" })).toBeEnabled();
});

it("shows an activity failure without losing content and retries only activity", async () => {
  vi.mocked(context.client.getActivity).mockRejectedValueOnce(new Error("private diagnostic"));
  const user = userEvent.setup();
  showHome();
  expect(await screen.findByRole("heading", { name: "Available reading" })).toBeInTheDocument();
  expect(screen.queryByText("private diagnostic")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Retry activity" }));
  expect(await screen.findByText("Saved local activity")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry activity" })).not.toBeInTheDocument();
  expect(context.client.requestRecommendations).toHaveBeenCalledTimes(1);
  expect(context.client.listContent).toHaveBeenCalledTimes(1);
});

it("distinguishes unavailable recommendations from an empty list and recovers", async () => {
  vi.mocked(context.client.requestRecommendations).mockRejectedValueOnce(new Error("unavailable"));
  const user = userEvent.setup();
  showHome();
  expect(await screen.findByText("Saved local activity")).toBeInTheDocument();
  expect(screen.queryByText("No recommendations yet")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Retry recommendations" }));
  expect(await screen.findByRole("heading", { name: "Available reading" })).toBeInTheDocument();
  expect(context.client.getActivity).toHaveBeenCalledTimes(1);
});

it("keeps embedded recommendation content when the separate content list fails", async () => {
  vi.mocked(context.client.listContent).mockRejectedValueOnce(new Error("unavailable"));
  showHome();
  expect(await screen.findByRole("heading", { name: "Available reading" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry content list" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("link", { name: /Flagged/ })).toHaveTextContent("Unavailable"));
});

it("preserves displayed reading through a failed refresh and retries an unchanged discovery count", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  vi.mocked(digestApi.getDiscoveryStatus).mockResolvedValueOnce(makeDiscoveryStatus({ item_count: 0 }))
    .mockResolvedValue(makeDiscoveryStatus({ item_count: 3 }));
  vi.mocked(context.client.requestRecommendations).mockResolvedValueOnce([recommendation])
    .mockRejectedValueOnce(new Error("temporary failure"))
    .mockResolvedValue([{ ...recommendation, item: { ...recommendation.item!, title: "Updated reading" } }]);
  await act(async () => { showHome(); });
  expect(screen.getByRole("heading", { name: "Available reading" })).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(screen.getByRole("heading", { name: "Available reading" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry recommendations" })).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(screen.getByRole("heading", { name: "Updated reading" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry recommendations" })).not.toBeInTheDocument();
});

it("opens the existing reader even when activity is unavailable", async () => {
  vi.mocked(context.client.getActivity).mockRejectedValue(new Error("unavailable"));
  vi.spyOn(context.client, "getContentBody").mockResolvedValue({ ok: true,
    content_id: recommendation.contentId, content_type: "text/plain", size: "29",
    truncated: false, text: "Readable while activity fails." });
  const user = userEvent.setup();
  showHome();
  await user.click(await screen.findByRole("button", { name: "Read" }));
  expect(await screen.findByRole("dialog", { name: "Available reading" })).toBeInTheDocument();
  expect(await screen.findByText("Readable while activity fails.")).toBeInTheDocument();
});
