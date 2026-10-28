import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { friendsApi } from "../../domain/friendsClient";
import { LibraryCleanupError, libraryCleanup, type LibraryCleanupJob, type LibraryReview } from "../../domain/libraryCleanup";
import PrivateCopiesPanel from "./PrivateCopiesPanel";

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });
const review: LibraryReview = { scope: null, review_token: "a".repeat(64), documents: 1, files: 3, bytes: 1024 };
const finished: LibraryCleanupJob = { id: review.review_token, scope: null, sequence: 1, done: ["source", "files"], pending: [], local_copies_complete: true, remote_confirmed: false, removed: 1 };
function setup(job: LibraryCleanupJob | null = null) {
  vi.spyOn(libraryCleanup, "status").mockResolvedValue(job);
  vi.spyOn(libraryCleanup, "preview").mockResolvedValue(review);
  vi.spyOn(friendsApi, "documents").mockResolvedValue({ documents: [{ import_id: "imp", filename: "Private reading.txt", state: "ready", size_bytes: 512, created_at_unix: 0 }] });
}
it("reviews files and retained data before confirmation", async () => {
  setup();
  const begin = vi.spyOn(libraryCleanup, "begin").mockResolvedValue(finished);
  const user = userEvent.setup(); render(<PrivateCopiesPanel />);
  await screen.findByRole("button", { name: "Remove Private reading.txt" });
  await user.click(screen.getByRole("button", { name: "Review all document copies" }));
  expect(begin).not.toHaveBeenCalled();
  const request = confirm.mock.calls[0][0];
  expect(request.body).toContain("1 document copies, 3 files");
  expect(request.body).toContain("Messages, cards and reading bookmarks remain");
  vi.mocked(libraryCleanup.status).mockResolvedValue(finished);
  vi.mocked(friendsApi.documents).mockResolvedValue({ documents: [] });
  await act(() => request.onConfirm());
  expect(begin).toHaveBeenCalledExactlyOnceWith(review);
  expect(screen.getByRole("status")).toHaveTextContent("1 reviewed document copies cleared");
  expect(screen.getByRole("status")).toHaveFocus();
});
it("keeps the original scope and request after a lost response", async () => {
  setup();
  const selected = { ...review, scope: "imp" };
  vi.mocked(libraryCleanup.preview).mockResolvedValue(selected);
  const begin = vi.spyOn(libraryCleanup, "begin").mockRejectedValueOnce(new Error("Unconfirmed")).mockResolvedValue(finished);
  const user = userEvent.setup(); render(<PrivateCopiesPanel />);
  await user.click(await screen.findByRole("button", { name: "Remove Private reading.txt" }));
  expect(libraryCleanup.preview).toHaveBeenCalledWith("imp");
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.getByRole("alert")).toHaveFocus();
  expect(screen.getByRole("button", { name: "Review all document copies" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Retry original document cleanup" }));
  expect(begin.mock.calls).toEqual([[selected], [selected]]);
});
it("loads pending progress after remount and reviews remaining files", async () => {
  const pending = { ...finished, done: ["source"], pending: ["files"], local_copies_complete: false };
  setup(pending);
  vi.spyOn(libraryCleanup, "reviewFiles").mockResolvedValue({ review_token: "changed", files: 1, bytes: 42 });
  const approve = vi.spyOn(libraryCleanup, "approveFiles").mockResolvedValue(finished);
  const user = userEvent.setup(); render(<PrivateCopiesPanel />);
  await user.click(await screen.findByRole("button", { name: "Review remaining document files" }));
  expect(screen.getByRole("button", { name: "Review all document copies" })).toBeDisabled();
  expect(approve).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("1 files");
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(approve).toHaveBeenCalledExactlyOnceWith(finished.id, "changed");
});
it("drops an invalid review and allows a new explicit review", async () => {
  setup();
  vi.spyOn(libraryCleanup, "begin").mockRejectedValue(new LibraryCleanupError("library_cleanup_review_changed", "Review again"));
  const user = userEvent.setup(); render(<PrivateCopiesPanel />);
  await screen.findByRole("button", { name: "Remove Private reading.txt" });
  await user.click(screen.getByRole("button", { name: "Review all document copies" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.queryByRole("button", { name: "Retry original document cleanup" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Review all document copies" })).toBeEnabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Review again");
});
