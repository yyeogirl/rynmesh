import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { FeedCleanupError, feedCleanup, type FeedCleanupJob, type FeedCleanupReview } from "../../domain/feedCleanup";
import FeedCleanupPanel from "./FeedCleanupPanel";

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });
const review: FeedCleanupReview = { review_token: "a".repeat(64), publications: 3, active_publications: 2, subscriptions: 1, received_updates: 5, backup_files: 1 };
const finished: FeedCleanupJob = { id: review.review_token, sequence: 1, done: ["source", "backups"], pending: [], cancelled: false, local_copies_complete: true, remote_confirmed: false };
function empty() {
  vi.spyOn(feedCleanup, "status").mockResolvedValue(null);
  vi.spyOn(feedCleanup, "preview").mockResolvedValue(review);
}

it("reviews publication/follow counts and exclusions before confirming", async () => {
  empty();
  const begin = vi.spyOn(feedCleanup, "begin").mockResolvedValue(finished);
  const user = userEvent.setup(); render(<FeedCleanupPanel />);
  await screen.findByText('No previous friend update cleanup.');
  await user.click(screen.getByRole('button', { name: 'Review friend update data' }));
  expect(await screen.findByLabelText('Reviewed friend update scope')).toHaveFocus();
  await user.tab(); await user.keyboard('{Enter}');
  expect(begin).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain('Currently sharing: 2. Active follows: 1.');
  expect(confirm.mock.calls[0][0].body).toContain('Friend relationships, private messages, sharing cards, saved documents');
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(begin).toHaveBeenCalledExactlyOnceWith(review.review_token);
  expect(screen.getByRole('status')).toHaveFocus();
  expect(screen.getByRole('status')).toHaveTextContent('remote copies are unconfirmed');
});

it("retains the original request after an unconfirmed response", async () => {
  empty();
  const begin = vi.spyOn(feedCleanup, 'begin').mockRejectedValueOnce(new FeedCleanupError('unconfirmed', 'Not confirmed')).mockResolvedValueOnce(finished);
  const user = userEvent.setup(); render(<FeedCleanupPanel />);
  await screen.findByText('No previous friend update cleanup.');
  await user.click(screen.getByRole('button', { name: 'Review friend update data' }));
  await user.click(await screen.findByRole('button', { name: 'Clear reviewed friend update copies' }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.getByRole('alert')).toHaveFocus();
  expect(screen.getByRole('button', { name: 'Review friend update data' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Retry original friend update cleanup' }));
  expect(begin.mock.calls).toEqual([[review.review_token], [review.review_token]]);
});

it("restores a pending backup and re-reviews its new version without offering undo", async () => {
  vi.spyOn(feedCleanup, 'status').mockResolvedValue({ ...finished, done: ['source'], pending: ['backups'], local_copies_complete: false });
  vi.spyOn(feedCleanup, 'reviewBackups').mockResolvedValue({ review_token: 'b'.repeat(64), files: 1, bytes: 2048 });
  const approve = vi.spyOn(feedCleanup, 'approveBackups').mockResolvedValue(finished);
  const user = userEvent.setup(); render(<FeedCleanupPanel />);
  await user.click(await screen.findByRole('button', { name: 'Review remaining friend update backups' }));
  expect(screen.queryByRole('button', { name: /Cancel uncommitted/ })).not.toBeInTheDocument();
  expect(confirm.mock.calls[0][0].body).toContain('New publications, follows and unreviewed files remain.');
  expect(approve).not.toHaveBeenCalled();
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(approve).toHaveBeenCalledExactlyOnceWith(finished.id, 'b'.repeat(64));
});

it("removes stale review and offers another review without reusing its token", async () => {
  empty();
  vi.spyOn(feedCleanup, 'begin').mockRejectedValue(new FeedCleanupError('feed_cleanup_review_changed', 'Review changed'));
  const user = userEvent.setup(); render(<FeedCleanupPanel />);
  await screen.findByText('No previous friend update cleanup.');
  await user.click(screen.getByRole('button', { name: 'Review friend update data' }));
  await user.click(await screen.findByRole('button', { name: 'Clear reviewed friend update copies' }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.queryByLabelText('Reviewed friend update scope')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Review friend update data' })).toBeEnabled();
});
