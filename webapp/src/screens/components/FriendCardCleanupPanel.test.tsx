import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { friendsApi } from "../../domain/friendsClient";
import FriendCardCleanupPanel from "./FriendCardCleanupPanel";

const reviewed = { review_token: "a".repeat(64), cards: 2, legacy_files: 1, resuming: false };
afterEach(() => { vi.restoreAllMocks(); });

it("reviews by keyboard, cancels without writing, and confirms only the reviewed scope", async () => {
  vi.spyOn(friendsApi, "reviewCardCleanup").mockResolvedValue(reviewed);
  const clear = vi.spyOn(friendsApi, "clearCards").mockResolvedValue({ cards: 2, complete: true, remote_confirmed: false });
  const changed = vi.fn();
  render(<FriendCardCleanupPanel onChanged={changed} />);
  const user = userEvent.setup();
  await user.tab(); await user.keyboard("{Enter}");
  expect(await screen.findByRole("region", { name: "Card cleanup review and result" })).toHaveFocus();
  await user.tab(); await user.tab(); await user.keyboard("{Enter}");
  expect(clear).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Review card history cleanup" })).toHaveFocus();
  await user.keyboard("{Enter}");
  await waitFor(() => expect(screen.getByRole("region", { name: "Card cleanup review and result" })).toHaveFocus());
  await user.tab(); await user.keyboard("{Enter}");
  expect(await screen.findByRole("status")).toHaveTextContent("2 reviewed card entries cleared locally");
  expect(screen.getByRole("status")).toHaveTextContent("Other devices are not confirmed cleared");
  expect(screen.getByRole("status")).toHaveFocus();
  expect(clear).toHaveBeenCalledExactlyOnceWith(reviewed.review_token);
  expect(changed).toHaveBeenCalledOnce();
});

it("retains the operation after an unconfirmed response and retries the same identity", async () => {
  vi.spyOn(friendsApi, "reviewCardCleanup").mockResolvedValue(reviewed);
  const clear = vi.spyOn(friendsApi, "clearCards").mockRejectedValueOnce(new Error("Response lost; retry"))
    .mockResolvedValue({ cards: 2, complete: true, remote_confirmed: false });
  render(<FriendCardCleanupPanel onChanged={vi.fn()} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Review card history cleanup" }));
  await user.click(await screen.findByRole("button", { name: "Clear reviewed card history" }));
  expect(await screen.findByRole("alert")).toHaveFocus();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Retry reviewed card cleanup" }));
  expect(await screen.findByRole("status")).toHaveFocus();
  expect(clear.mock.calls.map(([token]) => token)).toEqual([reviewed.review_token, reviewed.review_token]);
});

it("recovers unfinished cleanup from a remounted page without erasing later cards", async () => {
  vi.spyOn(friendsApi, "reviewCardCleanup").mockResolvedValue({ ...reviewed, resuming: true });
  const clear = vi.spyOn(friendsApi, "clearCards");
  render(<FriendCardCleanupPanel onChanged={vi.fn()} />);
  await userEvent.setup().click(screen.getByRole("button", { name: "Review card history cleanup" }));
  expect(await screen.findByText(/Continue the unfinished cleanup; cards created afterwards are kept/)).toBeInTheDocument();
  expect(clear).not.toHaveBeenCalled();
});
