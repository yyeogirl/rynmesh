import { ReadingCleanupError, readingCleanup, type ReadingCleanupReview } from "../../domain/readingCleanup";
import ReviewedCleanupPanel, { type CleanupConfig } from "./ReviewedCleanupPanel";

const config: CleanupConfig<ReadingCleanupReview> = {
  noun: "reading", title: "Reading data",
  scope: "Clears reviewed reading history, bookmarks and progress, their local sync records, known migration backups and interrupted-write files, and rebuilds search. Shared sync/search backups may also contain other metadata. Saved documents, downloaded articles, browser copies and other devices are outside this cleanup. New work after the source cleanup remains. Unseen edits on another device may later require conflict review.",
  result: "Reviewed reading copies on this node cleared. Other devices and saved or downloaded content remain outside this result.",
  backupScope: "New reading records and unreviewed files remain.",
  labels: { source: "Reading history and bookmarks", replica: "Local sync copies", backups: "Reviewed backups", search: "Search index" },
  counts: (value) => `Local items: ${value.local_items}. Sync entries: ${value.source_entities}. Replica entries: ${value.replica_entities}. Backup files: ${value.backup_files}.`,
  invalidReview: (cause) => cause instanceof ReadingCleanupError && ["reading_privacy_review_changed", "reading_cleanup_pending"].includes(cause.code),
};

export default function ReadingCleanupPanel({ onChange }: { onChange?: () => void }) {
  return <ReviewedCleanupPanel api={readingCleanup} config={config} onChange={onChange} />;
}
