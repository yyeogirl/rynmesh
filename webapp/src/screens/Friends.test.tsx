import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { friendsApi, invitationText, extractInvite } from "../domain/friendsClient";
import type { FriendInvitePreview, FriendRecord } from "../domain/friendTypes";
import Friends from "./Friends";
import { MemoryRouter } from "react-router-dom";
import FriendConversation from "./components/FriendConversation";

vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm: vi.fn() }) }));
vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,AA==") } }));
vi.mock("./components/FriendAI", () => ({ default: () => null }));
const friend: FriendRecord = { peer_id: "alice", relationship_id: "r1", node_name: "Alice", endpoint: "http://192.168.1.2:8791", permissions: ["friend.message"], status: "active", created_at: "2026-09-10T00:00:00Z" };
const preview: FriendInvitePreview = { ...friend, invite_id: "i1", expires_at: "2026-09-10T00:15:00Z" };

beforeEach(() => {
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [] });
  vi.spyOn(friendsApi, "invites").mockResolvedValue({ invites: [] });
  vi.spyOn(friendsApi, "cards").mockResolvedValue({ cards: [] });
  vi.spyOn(friendsApi, "history").mockResolvedValue({ messages: [] });
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

it("clears stale friend and card load warnings when background polling recovers", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  vi.mocked(friendsApi.list).mockRejectedValueOnce(new Error("offline"));
  vi.mocked(friendsApi.cards).mockRejectedValueOnce(new Error("offline"));
  render(<MemoryRouter><Friends /></MemoryRouter>);
  expect(await screen.findByText(/Could not load friends/)).toBeInTheDocument();
  expect(await screen.findByText(/Could not refresh shared content/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.queryByText(/Could not load friends/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Could not refresh shared content/)).not.toBeInTheDocument();
  expect(screen.getByText(/No friends yet/)).toBeInTheDocument();
});

it("clears a recovered message load failure without hiding an unconfirmed send", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  vi.mocked(friendsApi.history).mockRejectedValueOnce(new Error("offline"));
  vi.spyOn(friendsApi, "send").mockRejectedValue(new Error("Send unconfirmed"));
  const user = userEvent.setup();
  render(<FriendConversation friend={friend} />);
  expect(await screen.findByText(/Could not refresh messages/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.queryByText(/Could not refresh messages/)).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Message"), "Pending text");
  await user.click(screen.getByRole("button", { name: "Send message" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Send unconfirmed");
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.getByRole("alert")).toHaveTextContent("Send unconfirmed");
  expect(screen.getByRole("button", { name: "Retry this send" })).toBeEnabled();
});

describe("Friend pairing recovery", () => {
  it("never accepts a stale preview after the pasted invite changes", async () => {
    let resolve!: (value: FriendInvitePreview) => void;
    vi.spyOn(friendsApi, "inspect").mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockResolvedValue({ ...preview, node_name: "Bob" });
    const join = vi.spyOn(friendsApi, "join").mockResolvedValue(friend);
    const user = userEvent.setup();
    render(<MemoryRouter><Friends /></MemoryRouter>);
    await user.type(screen.getByLabelText("Friend invite"), "rynmesh://join/alice");
    await user.click(screen.getByRole("button", { name: "Review invite" }));
    fireEvent.change(screen.getByLabelText("Friend invite"), { target: { value: "rynmesh://join/bob" } });
    await act(async () => resolve(preview));
    expect(screen.queryByRole("button", { name: "Add this friend" })).not.toBeInTheDocument();
    expect(join).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Review invite" }));
    expect(await screen.findByText("Bob")).toBeInTheDocument();
    expect(join).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Add this friend" }));
    await waitFor(() => expect(join).toHaveBeenCalledWith("rynmesh://join/bob"));
  });

  it("keeps installation instructions copyable when clipboard permission fails", async () => {
    const value = { invite_uri: "rynmesh://join/invite", invite: preview };
    vi.spyOn(friendsApi, "createInvite").mockResolvedValue(value);
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Clipboard unavailable. Select and copy the invitation text."));
    render(<MemoryRouter><Friends /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Create invite" }));
    await user.click(await screen.findByRole("button", { name: "Copy invitation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Clipboard unavailable");
    expect(screen.getByLabelText("Invitation and installation instructions")).toHaveValue(invitationText(value));
    expect(extractInvite(invitationText(value))).toBe(value.invite_uri);
  });
});

describe("Friend delivery recovery", () => {
  it("explains a full mailbox and clears the warning only after confirmed recovery", async () => {
    const failed = { msg_id: "id", dir: "out" as const, from: "me", to: "alice", text: "hello",
      delivery_state: "failed" as const, error: "recipient_full" };
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [failed] });
    const retry = vi.spyOn(friendsApi, "retry").mockImplementation(async () => {
      vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ ...failed, delivery_state: "delivered", error: "" }] });
      return {};
    });
    const user = userEvent.setup();
    render(<FriendConversation friend={friend} />);
    expect(await screen.findByText(/Your friend's mailbox is full/)).toBeInTheDocument();
    expect(screen.queryByText("Delivered · confirmed by your friend")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry next pending message" }));
    expect(retry).toHaveBeenCalledWith("alice");
    expect(await screen.findByText("Delivered · confirmed by your friend")).toBeInTheDocument();
    expect(screen.queryByText(/Your friend's mailbox is full/)).not.toBeInTheDocument();
  });

  it("does not claim non-delivery or resend automatically after unconfirmed expiry", async () => {
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ msg_id: "id", dir: "out", from: "me", to: "alice",
      text: "hello", delivery_state: "expired", error: "message_expired" }] });
    const send = vi.spyOn(friendsApi, "send");
    render(<FriendConversation friend={friend} />);
    expect(await screen.findByText(/Your friend may already have received it/)).toBeInTheDocument();
    expect(screen.queryByText("Expired · not delivered")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry next pending message" })).not.toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
  });

  it("retries an unconfirmed send with the same identity and content", async () => {
    const send = vi.spyOn(friendsApi, "send").mockRejectedValueOnce(new Error("Response lost"))
      .mockResolvedValue({ msg_id: "id", dir: "out", from: "me", to: "alice", delivery_state: "mailbox" });
    const user = userEvent.setup();
    render(<FriendConversation friend={friend} />);
    await screen.findByText("Send your first message.");
    await user.type(screen.getByLabelText("Message"), "第一次分享");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Response lost");
    expect(screen.getByLabelText("Message")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry this send" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(send.mock.calls[0]).toEqual(send.mock.calls[1]);
    expect(send.mock.calls[0][1].message_id).toMatch(/^[a-f0-9]{32}$/);
  });

  it("shows mailbox delivery as unconfirmed and blocks oversized files before sending", async () => {
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ msg_id: "id", dir: "out", from: "me", to: "alice", text: "hello", delivery_state: "mailbox" }] });
    const send = vi.spyOn(friendsApi, "send");
    render(<FriendConversation friend={friend} />);
    expect(await screen.findByText("In encrypted mailbox · waiting for confirmation")).toBeInTheDocument();
    const file = new File([new Uint8Array(5 * 1024 * 1024 + 1)], "large.bin");
    fireEvent.change(screen.getByLabelText("Attachment"), { target: { files: [file] } });
    expect(screen.getByRole("alert")).toHaveTextContent("exceeds 5 MiB");
    expect(send).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });
});
