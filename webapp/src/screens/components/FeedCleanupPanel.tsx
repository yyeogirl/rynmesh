import { FeedCleanupError, feedCleanup, type FeedCleanupReview } from "../../domain/feedCleanup";
import ReviewedCleanupPanel, { type CleanupConfig } from "./ReviewedCleanupPanel";

const config: CleanupConfig<FeedCleanupReview> = {
  noun: "friend update", title: "Friend update data",
  scope: "Stops your reviewed publications, clears their drafts and received updates, and turns off your current follows. Reviewed feed backups are included. Friend relationships, private messages, sharing cards, saved documents, reading bookmarks and downloaded articles remain. Copies already saved by other people or browsers cannot be recalled. You can follow again or create a new publication after cleanup.",
  result: "Reviewed friend update data and backups cleared on this node. Friendships and independently saved copies remain; remote copies are unconfirmed.",
  backupScope: "New publications, follows and unreviewed files remain.",
  labels: { source: "Publications, follows and received updates", backups: "Reviewed feed backups" },
  counts: (value) => `Publications and drafts: ${value.publications}. Currently sharing: ${value.active_publications}. Active follows: ${value.subscriptions}. Received updates: ${value.received_updates}. Backup files: ${value.backup_files}.`,
  invalidReview: (cause) => cause instanceof FeedCleanupError && ["feed_cleanup_review_changed", "feed_cleanup_pending"].includes(cause.code),
};

export default function FeedCleanupPanel() {
  return <ReviewedCleanupPanel api={feedCleanup} config={config} />;
}
