import { screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { renderDigest } from "../test/digestScenario";
import { makeConsumptionRecord, makeDigestItem } from "../test/fixtures";

vi.mock("../components/ContentViewer", () => ({ default: ({ item }: { item: { title: string } }) =>
  <div role="dialog" aria-label={`Reading review: ${item.title}`} /> }));

it.each(["recommendation", "history"])("uses the position-aware reader for a synced %s entry", async (entry) => {
  const item = makeDigestItem();
  const { user } = renderDigest({ consumption: [makeConsumptionRecord(item, { progress: .8,
    sync_revisions: { reading: "review-revision" }, sync_conflicts: { reading: true } })] });
  const label = entry === "recommendation" ? item.title : `${item.title} · 80%`;
  await user.click(await screen.findByRole("button", { name: label }));
  expect(await screen.findByRole("dialog", { name: `Reading review: ${item.title}` })).toBeInTheDocument();
});
