import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import Reading from "./Reading";

vi.mock("../appContext", () => ({ useAppContext: () => ({ client: { mode: "live" } }) }));
vi.mock("../components/ContentViewer", () => ({ default: ({ item, onClose }: { item: { title: string }; onClose: () => void }) =>
  <div role="dialog" aria-label={item.title}><button onClick={onClose}>Close reading</button></div> }));
const row = (id: string, extra: Partial<ConsumptionRecord> = {}) => ({ item_id: id, item: { item_id: id, title: id, source_title: "Journal", link: "https://example.test/article" },
  progress: .4, completed: false, bookmarked: true, open_count: 0, last_opened_unix: 0, sync_reading_available: true, ...extra }) as ConsumptionRecord;
let records: ConsumptionRecord[];
beforeEach(() => {
  records = [row("Remote article"), row("Finished", { completed: true }), row("Unread saved", { progress: 0, sync_reading_available: false }),
    row("Conflict", { sync_conflicts: { reading: true } })];
  vi.spyOn(digestApi, "listConsumption").mockImplementation(async () => records);
});
afterEach(() => vi.restoreAllMocks());
const mount = () => { render(<MemoryRouter><Reading /></MemoryRouter>); return userEvent.setup(); };

it("includes synced positions without a local open date and distinguishes saved-only and completed records", async () => {
  const user = mount();
  await screen.findByRole("heading", { name: "Remote article" });
  expect(screen.queryByRole("heading", { name: "Unread saved" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Finished" })).not.toBeInTheDocument();
  expect(screen.getAllByText(/Saved position; not yet opened on this device/)).toHaveLength(2);
  expect(screen.getByRole("link", { name: "Review reading choices" })).toHaveAttribute("href", "/devices#reading-sync-conflicts");
  expect(screen.getByText(/do not include article text/)).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("Reading list"), "saved");
  expect(screen.getByRole("heading", { name: "Unread saved" })).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("Reading list"), "history");
  expect(screen.getByRole("heading", { name: "Finished" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Unread saved" })).not.toBeInTheDocument();
});

it("opens a source only on request and refreshes after reading", async () => {
  records = [row("Article")];
  const user = mount();
  await screen.findByRole("heading", { name: "Article" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Open reading view" }));
  expect(screen.getByRole("dialog", { name: "Article" })).toBeInTheDocument();
  records = [];
  await user.click(screen.getByRole("button", { name: "Close reading" }));
  expect(await screen.findByText(/No items in this list yet/)).toBeInTheDocument();
});

it("recovers a failed load and preserves the existing list during a failed refresh", async () => {
  vi.mocked(digestApi.listConsumption).mockRejectedValueOnce(new Error("node unavailable"));
  const user = mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be refreshed");
  await user.click(screen.getByRole("button", { name: "Refresh reading list" }));
  await screen.findByRole("heading", { name: "Remote article" });
  vi.mocked(digestApi.listConsumption).mockRejectedValueOnce(new Error("disk unreadable"));
  await user.click(screen.getByRole("button", { name: "Refresh reading list" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("heading", { name: "Remote article" })).toBeInTheDocument();
});

it("limits rendered records and lets the owner reveal more without losing a saved position", async () => {
  records = Array.from({ length: 25 }, (_, i) => row(`Article ${i}`));
  const user = mount();
  await screen.findByRole("heading", { name: "Article 0" });
  expect(screen.queryByRole("heading", { name: "Article 24" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Show more reading items" }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "Article 24" })).toBeInTheDocument());
});
