import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { digestApi } from "../domain/digestClient";
import { feedApi, type FeedPublication, type FeedSnapshot } from "../domain/friendFeed";
import { friendsApi } from "../domain/friendsClient";
import type { ConfirmRequest } from "../domain/types";
import FriendFeed from "./FriendFeed";

const mocks = vi.hoisted(() => ({ confirm: vi.fn(), record: vi.fn() }));
vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm: mocks.confirm, client: { recordContentConsumption: mocks.record } }) }));
vi.mock("../domain/friendFeed", () => ({ feedApi: { snapshot: vi.fn(), publications: vi.fn(), draft: vi.fn(), publish: vi.fn(),
  stop: vi.fn(), subscribe: vi.fn(), refresh: vi.fn(), fetch: vi.fn(), read: vi.fn() } }));
vi.mock("../domain/friendsClient", () => ({ friendsApi: { list: vi.fn() } }));
vi.mock("../domain/digestClient", () => ({ digestApi: { listConsumption: vi.fn() } }));
vi.mock("../components/ContentViewer", () => ({ default: () => <p>Verified saved content viewer</p> }));

const friend = { relationship_id: "a".repeat(32), peer_id: "alice", node_name: "Alice", endpoint: "", status: "active" as const, permissions: [], created_at: "" };
const card = { title: "Private story", summary: "Metadata only", source: "Original journal", kind: "document", library_id: "import:document" };
const draft: FeedPublication = { id: "b".repeat(32), revision: 1, stopped: false, draft: { card, audience: { mode: "all_friends", relationship_ids: [] } }, published: null };
const saved = { item_id: "article", item: { title: "Saved article", link: "https://example.test" }, bookmarked: true };
let snapshot: FeedSnapshot;

beforeEach(() => {
  vi.clearAllMocks();
  snapshot = { subscriptions: [], timeline: [] };
  vi.mocked(feedApi.snapshot).mockImplementation(async () => snapshot);
  vi.mocked(feedApi.publications).mockResolvedValue({ publications: [] });
  vi.mocked(friendsApi.list).mockResolvedValue({ friends: [friend] });
  vi.mocked(digestApi.listConsumption).mockResolvedValue([saved as never]);
  vi.mocked(feedApi.draft).mockResolvedValue(draft);
});
function mount() { render(<MemoryRouter><FriendFeed /></MemoryRouter>); return userEvent.setup(); }

it("starts without subscriptions or automatic downloads and recovers an offline first refresh", async () => {
  vi.mocked(feedApi.subscribe).mockImplementation(async () => {
    const subscription = { relationship_id: friend.relationship_id, peer_id: friend.peer_id, revision: 1, enabled: true };
    snapshot = { ...snapshot, subscriptions: [subscription] };
    return subscription;
  });
  vi.mocked(feedApi.refresh).mockRejectedValue(new Error("Friend is offline. Retry later."));
  const user = mount();
  expect(await screen.findByRole("button", { name: "Follow Alice" })).toBeEnabled();
  expect(feedApi.subscribe).not.toHaveBeenCalled(); expect(feedApi.fetch).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Follow Alice" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  expect(await screen.findByRole("button", { name: "Unfollow Alice" })).toBeEnabled();
  expect(feedApi.subscribe).toHaveBeenCalledWith(friend.relationship_id, true, 0);
});

it("saves a draft without publishing and requires an explicit future-friend review", async () => {
  const user = mount();
  await screen.findByRole("button", { name: "Follow Alice" });
  await user.selectOptions(screen.getByLabelText("Publication content"), "article");
  await user.selectOptions(screen.getByLabelText("Publication audience"), "all_friends");
  await user.click(screen.getByRole("button", { name: "Save draft for review" }));
  expect(await screen.findByRole("heading", { name: "Saved draft preview" })).toBeInTheDocument();
  expect(feedApi.publish).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Review and publish" }));
  const confirmation = mocks.confirm.mock.calls[0][0] as ConfirmRequest;
  expect(confirmation.body).toContain("All current and future friends");
  expect(feedApi.publish).not.toHaveBeenCalled();
  vi.mocked(feedApi.publish).mockResolvedValue({ ...draft, revision: 2 });
  await confirmation.onConfirm();
  expect(feedApi.publish).toHaveBeenCalledWith(draft.id, expect.objectContaining({ expected_revision: 1, confirm_all_friends: true }));
  expect(await screen.findByRole("status")).toHaveTextContent("does not mean anyone has received or read it");
});

it("restores a saved draft for editing and prevents publishing unsaved audience changes", async () => {
  vi.mocked(feedApi.publications).mockResolvedValue({ publications: [draft] });
  const user = mount();
  await user.click(await screen.findByRole("button", { name: "Edit draft and audience" }));
  expect(screen.getByLabelText("Publication content")).toHaveValue("import:document");
  expect(screen.getByRole("button", { name: "Review and publish" })).toBeEnabled();
  await user.selectOptions(screen.getByLabelText("Publication audience"), "selected");
  expect(screen.getByRole("button", { name: "Review and publish" })).toBeDisabled();
  expect(feedApi.publish).not.toHaveBeenCalled();
});

it("keeps edits after a failed draft save and retries the same operation identity", async () => {
  vi.mocked(feedApi.draft).mockRejectedValueOnce(new Error("Save could not be confirmed")).mockResolvedValue(draft);
  const user = mount();
  await screen.findByRole("button", { name: "Follow Alice" });
  fireEvent.change(screen.getByLabelText("Publication content"), { target: { value: "article" } });
  fireEvent.change(screen.getByLabelText("Publication audience"), { target: { value: "all_friends" } });
  await user.click(screen.getByRole("button", { name: "Save draft for review" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be confirmed");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save draft for review" })).toBeEnabled());
  expect(screen.getByLabelText("Publication content")).toHaveValue("article");
  await user.click(screen.getByRole("button", { name: "Save draft for review" }));
  expect(vi.mocked(feedApi.draft).mock.calls[1]).toEqual(vi.mocked(feedApi.draft).mock.calls[0]);
});

it("shows source, last check and unread revision, and never treats a rejected download as read", async () => {
  snapshot.timeline = [{ relationship_id: friend.relationship_id, peer_id: "alice", node_name: "Alice", subscription_revision: 1,
    checked_at: 1000, next_cursor: "", error_code: "feed_friend_unreachable", rows: [{ id: draft.id, revision: 4, published_at: 1,
      updated_at: 2, card: { ...card, source_url: "https://example.test/source" }, read: false }] }];
  vi.mocked(feedApi.fetch).mockRejectedValue(new Error("Access was removed. Refresh updates."));
  const user = mount();
  await screen.findByRole("heading", { name: "Private story" });
  expect(screen.getByText(/Unread · Version 4 · Updated/)).toBeInTheDocument();
  expect(screen.getByText("Source: https://example.test/source")).toBeInTheDocument();
  expect(feedApi.fetch).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Save copy and read Private story" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Access was removed");
  expect(feedApi.read).not.toHaveBeenCalled();
  expect(screen.queryByText("Verified saved content viewer")).not.toBeInTheDocument();
});

it("recovers an initial local loading failure and offers pairing for a clean node", async () => {
  vi.mocked(feedApi.snapshot).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(snapshot);
  vi.mocked(friendsApi.list).mockResolvedValue({ friends: [] });
  const user = mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not refresh");
  await user.click(screen.getByRole("button", { name: "Refresh local feed" }));
  expect(await screen.findByRole("link", { name: "Invite a friend" })).toHaveAttribute("href", "/friends");
  expect(screen.getByText("No publications or saved drafts.")).toBeInTheDocument();
});

it("clears a stale connection error after the automatic poll reconnects", async () => {
  let poll: (() => Promise<void>) | undefined;
  const nativeTimer = window.setInterval.bind(window);
  const timer = vi.spyOn(window, "setInterval").mockImplementation((callback, delay, ...args) => {
    if (delay === 5000) poll = callback as () => Promise<void>;
    return nativeTimer(callback, delay, ...args) as unknown as ReturnType<typeof window.setInterval>;
  });
  try {
    vi.mocked(feedApi.snapshot).mockRejectedValueOnce(new Error("disconnected")).mockResolvedValue(snapshot);
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not refresh");
    await act(async () => { await poll!(); });
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Follow Alice" })).toBeEnabled();
  } finally { timer.mockRestore(); }
});
