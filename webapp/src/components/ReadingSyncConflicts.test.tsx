import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { deviceSyncApi } from "../domain/deviceSync";
import type { ReadingConflict } from "../domain/deviceSync";
import ReadingSyncConflicts from "./ReadingSyncConflicts";

const issue: ReadingConflict = { id: "article", scope: "reading", revision: "original-review", item: { title: "Reading sample", source_title: "Journal" }, candidates: [
  { choice_id: "local:1", value: { progress: .2, completed: false, content_version: "version-one" } },
  { choice_id: "remote:1", value: { progress: .8, completed: false, content_version: "version-two" } },
] };
let conflicts: ReadingConflict[];
const refreshParent = vi.fn().mockResolvedValue(undefined);
beforeEach(() => {
  conflicts = [structuredClone(issue)];
  refreshParent.mockClear();
  vi.spyOn(deviceSyncApi, "readingConflicts").mockImplementation(async () => ({ conflicts: structuredClone(conflicts), local_actor: "local" }));
});
afterEach(() => vi.restoreAllMocks());
const show = () => render(<ReadingSyncConflicts devices={[]} onResolved={refreshParent} />);

it("shows both versions with no automatic choice and saves the reviewed earlier position", async () => {
  const resolve = vi.spyOn(deviceSyncApi, "resolveReading").mockImplementation(async () => { conflicts = []; });
  const user = userEvent.setup();
  show();
  expect(await screen.findByText("Reading sample")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Use this choice" })).toBeDisabled();
  screen.getAllByRole("radio").forEach((radio) => expect(radio).not.toBeChecked());
  expect(screen.getByText("Content version: version-one")).toBeInTheDocument();
  expect(screen.getByText("Content version: version-two")).toBeInTheDocument();
  await user.click(screen.getByRole("radio", { name: /20% read/ }));
  await user.click(screen.getByRole("button", { name: "Use this choice" }));
  expect(resolve).toHaveBeenCalledWith(issue, "local:1");
  expect(await screen.findByText("No reading conflicts to resolve.")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Choice saved on this device");
  await waitFor(() => expect(refreshParent).toHaveBeenCalledTimes(1));
});

it("keeps an unconfirmed choice available for an identical retry", async () => {
  const resolve = vi.spyOn(deviceSyncApi, "resolveReading").mockRejectedValueOnce(new Error("Response lost; refresh or retry."))
    .mockImplementationOnce(async () => { conflicts = []; });
  const user = userEvent.setup();
  show();
  await user.click(await screen.findByRole("radio", { name: /80% read/ }));
  await user.click(screen.getByRole("button", { name: "Use this choice" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Response lost");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Use this choice" }));
  expect(resolve.mock.calls[0]).toEqual(resolve.mock.calls[1]);
  expect(await screen.findByText("No reading conflicts to resolve.")).toBeInTheDocument();
});

it("drops the selection when refresh reveals a newer review revision", async () => {
  const user = userEvent.setup();
  show();
  await user.click(await screen.findByRole("radio", { name: /20% read/ }));
  conflicts = [{ ...issue, revision: "new-review" }];
  await user.click(screen.getByRole("button", { name: "Refresh reading changes" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Use this choice" })).toBeDisabled());
  screen.getAllByRole("radio").forEach((radio) => expect(radio).not.toBeChecked());
});

it("labels a deleted position explicitly and renders version labels as text", async () => {
  conflicts = [{ ...issue, candidates: [{ choice_id: "removed:1", value: null },
    { choice_id: "remote:1", value: { progress: .8, completed: true, content_version: "<img src=x onerror=alert(1)>" } }] }];
  show();
  expect(await screen.findByRole("radio", { name: /Remove synced reading position/ })).not.toBeChecked();
  expect(screen.getByRole("radio", { name: /80% read · completed/ })).not.toBeChecked();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

it("reports unavailable source state and allows a reload", async () => {
  vi.mocked(deviceSyncApi.readingConflicts).mockRejectedValueOnce(new Error("Unavailable"));
  show();
  expect(await screen.findByRole("alert")).toHaveTextContent("Reading changes could not be loaded");
  fireEvent.click(screen.getByRole("button", { name: "Refresh reading changes" }));
  expect(await screen.findByText("Reading sample")).toBeInTheDocument();
});
