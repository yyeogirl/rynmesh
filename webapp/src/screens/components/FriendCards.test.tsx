import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { friendsApi } from "../../domain/friendsClient";
import { libraryCleanup } from "../../domain/libraryCleanup";
import FriendCards from "./FriendCards";

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });

it("shows card metadata without fetching private bytes and recovers a denied download", async () => {
  vi.spyOn(friendsApi, "cards").mockResolvedValue({ cards: [{ card_id: "card", from: "Alice", dir: "in", created_at: "", fetch_state: "available",
    card: { library_id: "doc", title: "Private reading", summary: "A metadata summary", source: "Original publisher", source_url: "https://example.test/article", kind: "document", fetch_available: true, size_bytes: 42 } }] });
  const download = vi.spyOn(friendsApi, "fetchCard").mockRejectedValue(new Error("Friend access was removed."));
  const user = userEvent.setup();
  render(<FriendCards />);
  await screen.findByRole("heading", { name: "Private reading" });
  expect(download).not.toHaveBeenCalled();
  expect(screen.getByText("Source: https://example.test/article")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Download and read (42 bytes)" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("access was removed");
  expect(screen.queryByRole("button", { name: "Read saved copy" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Download and read (42 bytes)" })).toBeEnabled();
});

it("reviews the selected saved copy and directs failed cleanup to durable settings progress", async () => {
  vi.spyOn(friendsApi, 'cards').mockResolvedValue({ cards: [{ card_id: 'card', from: 'Alice', dir: 'in', created_at: '', fetch_state: 'fetched', fetched_library_id: 'import:imp_selected',
    card: { library_id: 'doc', title: 'Saved reading', summary: '', source: '', source_url: '', kind: 'document', fetch_available: true } }] });
  const reviewed = { scope: 'imp_selected', review_token: 'token', documents: 1, files: 4, bytes: 1000 };
  vi.spyOn(libraryCleanup, 'preview').mockResolvedValue(reviewed);
  const begin = vi.spyOn(libraryCleanup, 'begin').mockRejectedValue(new Error('File is occupied.'));
  const user = userEvent.setup(); render(<FriendCards />);
  await user.click(await screen.findByRole('button', { name: 'Remove local copy' }));
  expect(libraryCleanup.preview).toHaveBeenCalledExactlyOnceWith('imp_selected');
  expect(begin).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain('4 files');
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(begin).toHaveBeenCalledExactlyOnceWith(reviewed);
  expect(screen.getByRole('alert')).toHaveTextContent('Check document cleanup progress in Settings');
});
