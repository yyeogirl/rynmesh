import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { deviceSyncApi } from "../domain/deviceSync";
import type { DeviceInvite, DevicePair, DeviceStatus } from "../domain/deviceSync";
import Devices from "./Devices";

const confirm = vi.hoisted(() => vi.fn());
vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm }) }));
const identity = { name: "My laptop", peer_id: "public-key", actor: "a".repeat(64), endpoint: "http://192.168.1.2:8791" };
const preview: DeviceInvite = { id: "invite", device: identity, scopes: ["bookmarks", "reading"], created: 1, expires: 4000000000 };
const pair: DevicePair = { id: "pair", role: "inviter", status: "awaiting_owner", device: identity, review_token: "review",
  verification_code: "aaaa-bbbb-cccc-dddd-eeee-ffff", expires: 4000000000, scopes: ["bookmarks", "reading"], remote_scopes: [],
  paused: false, remote_paused: false, revision: 0, effective_scopes: [], removal_pending: false };
let state: DeviceStatus;
const show = () => render(<MemoryRouter><Devices /></MemoryRouter>);

beforeEach(() => {
  state = { pairing_available: true, reason: null, data_transfer_available: false, devices: [], invites: [] };
  vi.spyOn(deviceSyncApi, "status").mockImplementation(async () => structuredClone(state));
  vi.spyOn(deviceSyncApi, "readingConflicts").mockResolvedValue({ conflicts: [], local_actor: "local" });
  confirm.mockReset();
});
afterEach(() => vi.restoreAllMocks());

it("requires an explicit scope choice and preserves a copyable invite when clipboard fails", async () => {
  const invite = vi.spyOn(deviceSyncApi, "invite").mockImplementation(async () => {
    state.invites = [{ ...preview, status: "open", pair_id: null }];
    return { uri: "rynmesh://device-pair/signed", invite: preview };
  });
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("permission"));
  show();
  await screen.findByText("No paired devices yet.");
  const scopes = screen.getByRole("group", { name: "Offer to sync" });
  within(scopes).getAllByRole("checkbox").forEach((input) => expect(input).not.toBeChecked());
  await user.click(within(scopes).getByRole("checkbox", { name: "Saved content" }));
  await user.click(screen.getByRole("button", { name: "Create device invite" }));
  expect(invite).toHaveBeenCalledWith(["bookmarks"]);
  await user.click(await screen.findByRole("button", { name: "Copy device invite" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Select and copy");
  expect(screen.getByLabelText("Device invitation to copy")).toHaveValue("rynmesh://device-pair/signed");
});

it("discards a stale invitation preview and never silently pairs", async () => {
  let resolve!: (value: DeviceInvite) => void;
  vi.spyOn(deviceSyncApi, "inspect").mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
    .mockResolvedValue({ ...preview, device: { ...identity, name: "Other computer" } });
  const join = vi.spyOn(deviceSyncApi, "join").mockResolvedValue({ ...pair, role: "joiner", status: "awaiting_inviter" });
  const user = userEvent.setup();
  show();
  fireEvent.change(screen.getByLabelText("Paste device invitation"), { target: { value: "first" } });
  await user.click(screen.getByRole("button", { name: "Review device invite" }));
  fireEvent.change(screen.getByLabelText("Paste device invitation"), { target: { value: "second" } });
  await act(async () => resolve(preview));
  expect(screen.queryByRole("button", { name: "Request pairing" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Review device invite" }));
  expect(await screen.findByText("Other computer")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Request pairing" })).toBeDisabled();
  expect(join).not.toHaveBeenCalled();
  const scopes = screen.getByRole("group", { name: "Choose for this computer" });
  expect(within(scopes).getByRole("checkbox", { name: "Ask Ryn history" })).toBeDisabled();
  await user.click(within(scopes).getByRole("checkbox", { name: "Saved content" }));
  await user.click(screen.getByRole("checkbox", { name: "I checked this invitation is from my own computer" }));
  await user.click(screen.getByRole("button", { name: "Request pairing" }));
  await waitFor(() => expect(join).toHaveBeenCalledWith("second", ["bookmarks"]));
  expect(await screen.findByRole("status")).toHaveTextContent("Request saved");
});

it("requires inviter identity confirmation and leaves a failed approval pending", async () => {
  state.devices = [pair];
  const approve = vi.spyOn(deviceSyncApi, "approve").mockRejectedValue(new Error("Connection lost; refresh to confirm."));
  const user = userEvent.setup();
  show();
  const card = await screen.findByRole("article", { name: "Device My laptop" });
  expect(within(card).getByRole("button", { name: "Approve this device" })).toBeDisabled();
  expect(within(card).getByText(pair.verification_code)).toBeInTheDocument();
  const scopes = within(card).getByRole("group", { name: "Allow on this device" });
  within(scopes).getAllByRole("checkbox").forEach((input) => expect(input).not.toBeChecked());
  await user.click(within(scopes).getByRole("checkbox", { name: "Reading progress" }));
  await user.click(within(card).getByRole("checkbox", { name: "I checked this is my other computer" }));
  await user.click(within(card).getByRole("button", { name: "Approve this device" }));
  expect(approve).toHaveBeenCalledWith(pair, ["reading"]);
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
  expect(within(card).getByText("Review on this device")).toBeInTheDocument();
});

it("shows pairing as distinct from synced data and requires review before removal", async () => {
  const active = { ...pair, status: "active", revision: 4, effective_scopes: ["bookmarks"] as const };
  state.devices = [{ ...active, effective_scopes: ["bookmarks"] }];
  const remove = vi.spyOn(deviceSyncApi, "remove").mockImplementation(async () => {
    const removed = { ...state.devices[0], status: "revoked", removal_pending: true };
    state.devices = [removed]; return removed;
  });
  const user = userEvent.setup();
  show();
  expect(await screen.findByText("Pairing confirmed")).toBeInTheDocument();
  expect(screen.getByText(/does not mean your content has synced/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Remove device" }));
  expect(remove).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("Copies already there cannot be recalled");
  await act(async () => confirm.mock.calls[0][0].onConfirm());
  expect(remove.mock.calls[0][0].revision).toBe(4);
  expect(await screen.findByText(/Removed locally; waiting to notify/)).toBeInTheDocument();
});

it("keeps existing controls available when a network address is missing", async () => {
  state.pairing_available = false;
  state.reason = "sync_endpoint_unavailable";
  state.devices = [{ ...pair, status: "active", revision: 1 }];
  show();
  await screen.findByText("Pairing confirmed");
  expect(screen.getByRole("button", { name: "Create device invite" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Remove device" })).toBeEnabled();
  expect(screen.getByRole("status")).toHaveTextContent("Existing devices can still be removed");
});

it("uses the reviewed policy revision and preserves saved scope when pausing", async () => {
  state.devices = [{ ...pair, status: "active", revision: 2, scopes: ["bookmarks"], effective_scopes: ["bookmarks"] }];
  const configure = vi.spyOn(deviceSyncApi, "configure").mockRejectedValue(new Error("The device settings changed."));
  const user = userEvent.setup();
  show();
  await screen.findByText("Pairing confirmed");
  await user.click(within(screen.getByRole("group", { name: "Your allowed scope" })).getByRole("checkbox", { name: "Reading progress" }));
  await user.click(screen.getByRole("button", { name: "Pause" }));
  expect(configure.mock.calls[0]).toEqual([state.devices[0], ["bookmarks"], true]);
  expect(await screen.findByRole("alert")).toHaveTextContent("settings changed");
  expect(screen.queryByText("Paused on this device.")).not.toBeInTheDocument();
});

it("shows pending source changes and a lost acknowledgement without claiming success", async () => {
  state.data_transfer_available = true;
  state.devices = [{ ...pair, status: "active", revision: 1,
    sync: { state: "waiting", pending: 3, last_success_at: null, error_code: "sync_transfer_unconfirmed", conflicts: 0 } }];
  show();
  expect(await screen.findByText("3 local changes waiting for confirmation.")).toBeInTheDocument();
  expect(screen.getByText("Last transfer was not confirmed. Reconnect and retry.")).toBeInTheDocument();
  expect(screen.queryByText("Selected local changes confirmed by the other device.")).not.toBeInTheDocument();
  expect(screen.queryByText(/Personal data transfer is still/)).not.toBeInTheDocument();
});

it("shows source-confirmed status and preserves the distinction between conflicts and pending transfers", async () => {
  state.data_transfer_available = true;
  state.devices = [{ ...pair, status: "active", revision: 1,
    sync: { state: "confirmed", pending: 0, last_success_at: 1000, error_code: "", conflicts: 0 } }];
  const user = userEvent.setup();
  show();
  expect(await screen.findByText("Selected local changes confirmed by the other device.")).toBeInTheDocument();
  expect(screen.getByText(/Last confirmation across selected categories/)).toBeInTheDocument();
  state.devices[0].sync = { state: "conflict", pending: 0, last_success_at: 1000, error_code: "", conflicts: 2 };
  await user.click(screen.getByRole("button", { name: "Refresh devices" }));
  expect(await screen.findByText(/2 unresolved conflicts/)).toBeInTheDocument();
  expect(screen.queryByText("Selected local changes confirmed by the other device.")).not.toBeInTheDocument();
});
