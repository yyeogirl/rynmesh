import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { friendsApi } from "../domain/friendsClient";
import ShareContentButton from "./ShareContentButton";

afterEach(() => vi.restoreAllMocks());

it("requires recipient confirmation and preserves a failed share identity", async () => {
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [{ peer_id: "friend", node_name: "Alice", relationship_id: "rel", endpoint: "http://192.168.1.2:8791", permissions: [], status: "active", created_at: "" }] });
  const share = vi.spyOn(friendsApi, "share").mockRejectedValueOnce(new Error("Reply lost"))
    .mockResolvedValue({ card_id: "card", from: "me", created_at: "", delivery_state: "mailbox", card: { library_id: "doc", title: "Article", summary: "", kind: "document", source: "" } });
  const user = userEvent.setup();
  render(<MemoryRouter><ShareContentButton itemId="article" title="Article" offlineJobId={"a".repeat(32)} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "Share with a friend" }));
  await screen.findByRole("option", { name: "Alice" });
  expect(share).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toHaveTextContent("Copies they save cannot be recalled");
  await user.selectOptions(screen.getByLabelText("Share recipient"), "friend");
  expect(share).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Send content card" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Reply lost");
  expect(screen.getByLabelText("Share recipient")).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Retry this share" }));
  await waitFor(() => expect(share).toHaveBeenCalledTimes(2));
  expect(share.mock.calls[0]).toEqual(share.mock.calls[1]);
  expect(share.mock.calls[0][0]).toMatchObject({ item_id: "article", peer_id: "friend", offline_job_id: "a".repeat(32) });
  expect(await screen.findByRole("status")).toHaveTextContent("waiting for confirmation");
});

it("Escape closes only the sharing dialog and returns focus to the opener", async () => {
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [] });
  const user = userEvent.setup();
  const parentEscape = vi.fn();
  window.addEventListener("keydown", parentEscape);
  try {
    render(<MemoryRouter><ShareContentButton itemId="article" title="Article" /></MemoryRouter>);
    const opener = screen.getByRole("button", { name: "Share with a friend" });
    await user.click(opener);
    await screen.findByText(/Add a friend in/);
    parentEscape.mockClear();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(parentEscape).not.toHaveBeenCalled();
    expect(opener).toHaveFocus();
  } finally { window.removeEventListener("keydown", parentEscape); }
});
