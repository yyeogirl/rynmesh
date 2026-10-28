import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { aiAccess, type FriendAISnapshot } from "../../domain/aiAccess";
import type { FriendRecord } from "../../domain/friendTypes";
import FriendAI from "./FriendAI";

const mocks = vi.hoisted(() => ({ confirm: vi.fn(), client: { getLLMServiceStatus: vi.fn(), publishLLMService: vi.fn() } }));
vi.mock("../../appContext", () => ({ useAppContext: () => mocks }));
const friend: FriendRecord = { peer_id: "alice", relationship_id: "a".repeat(32), node_name: "Alice",
  endpoint: "http://192.168.1.2:8791", permissions: [], status: "active", created_at: "2026-09-11T00:00:00Z" };
const service = { package_id: "model-x", model_alias: "Local model", capabilities: ["chat"], context_window: 4096, max_output_tokens: 256,
  pricing: { currency: "DEV", input_per_1k: 0, output_per_1k: 0, minimum: 0, maximum_per_task: 1 }, privacy: {} };
const snapshot = (): FriendAISnapshot => ({ peer_id: friend.peer_id, relationship_id: friend.relationship_id,
  checked_at: Date.now() / 1000, status: "authorized", services: [{ peer_id: friend.peer_id, online: true, service, capacity: { available: 1 } }] });
beforeEach(() => {
  mocks.confirm.mockReset();
  mocks.client.getLLMServiceStatus.mockResolvedValue({ online: true, ready: true, publication_enabled: true, service });
  vi.spyOn(aiAccess, "list").mockResolvedValue({ grants: [] });
  vi.spyOn(aiAccess, "friends").mockResolvedValue({ friends: [] });
});
afterEach(() => vi.restoreAllMocks());
const open = () => render(<MemoryRouter><FriendAI friends={[friend]} /></MemoryRouter>);

it("requires confirmation and waits for a committed grant before showing allowed", async () => {
  const set = vi.spyOn(aiAccess, "set").mockResolvedValue({ grant: { service_id: "model-x", relationship_id: friend.relationship_id,
    peer_id: friend.peer_id, allowed: true, effective: true, revision: 1 }, cancellation: "not_requested" });
  open(); const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Allow AI access" }));
  expect(set).not.toHaveBeenCalled();
  expect(screen.getByText("Your Local model: Not allowed")).toBeInTheDocument();
  await act(async () => { await mocks.confirm.mock.calls[0][0].onConfirm(); });
  expect(set).toHaveBeenCalledWith("model-x", friend.relationship_id, true, 0);
  expect(await screen.findByText("Your Local model: Allowed")).toBeInTheDocument();
});

it("keeps a failed authorization visibly unconfirmed", async () => {
  vi.spyOn(aiAccess, "set").mockRejectedValue(new Error("Permission changed. Refresh first."));
  open(); const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Allow AI access" }));
  await act(async () => { await mocks.confirm.mock.calls[0][0].onConfirm(); });
  expect(await screen.findByRole("alert")).toHaveTextContent("Refresh first");
  expect(screen.getByText("Your Local model: Not allowed")).toBeInTheDocument();
});

it("opens only a checked friend's model and clears it after a failed refresh", async () => {
  vi.mocked(aiAccess.friends).mockResolvedValue({ friends: [snapshot()] });
  vi.spyOn(aiAccess, "refresh").mockRejectedValue(new Error("Friend unreachable"));
  open(); const user = userEvent.setup();
  const link = await screen.findByRole("link", { name: "Ask Alice's AI" });
  expect(link).toHaveAttribute("href", "/ask?peer=alice&service=model-x&network=rynmesh-main");
  await user.click(screen.getByRole("button", { name: "Check Alice's AI" }));
  await waitFor(() => expect(screen.queryByRole("link", { name: "Ask Alice's AI" })).not.toBeInTheDocument());
  expect(screen.getByRole("alert")).toHaveTextContent("unreachable");
});

it("shows revocation as saved without claiming running computation stopped", async () => {
  const grant = { service_id: "model-x", relationship_id: friend.relationship_id, peer_id: friend.peer_id,
    allowed: true, effective: true, revision: 4 };
  vi.mocked(aiAccess.list).mockResolvedValue({ grants: [grant] });
  vi.spyOn(aiAccess, "set").mockResolvedValue({ grant: { ...grant, allowed: false, effective: false, revision: 5 }, cancellation: "pending" });
  open(); const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Revoke AI access" }));
  await act(async () => { await mocks.confirm.mock.calls[0][0].onConfirm(); });
  expect(aiAccess.set).toHaveBeenCalledWith("model-x", friend.relationship_id, false, 4);
  expect(await screen.findByRole("status")).toHaveTextContent("Running computation may continue");
  expect(screen.getByText("Your Local model: Not allowed")).toBeInTheDocument();
});
