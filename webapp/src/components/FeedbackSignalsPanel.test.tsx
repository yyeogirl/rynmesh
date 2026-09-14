import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { digestApi, type FeedbackHistory } from "../domain/digestClient";
import FeedbackSignalsPanel from "./FeedbackSignalsPanel";

const history: FeedbackHistory = { total: 1, offset: 0, limit: 20, items: [{
  event_id: "event-1", content_id: "article", title: "Science article", action: "hide",
  tags: ["science"], platform: "rss", publisher: "source:one", updated_at: "2026-09-10T09:00:00Z",
  undone_at: "", active: true, migrated: false,
}] };

afterEach(() => vi.restoreAllMocks());

it("shows the action and its signals, and confirms one undo before changing the state", async () => {
  const user = userEvent.setup();
  const load = vi.spyOn(digestApi, "feedbackHistory").mockResolvedValue(history);
  let finish!: (value: Awaited<ReturnType<typeof digestApi.undoFeedback>>) => void;
  const undo = vi.spyOn(digestApi, "undoFeedback").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const refresh = vi.fn().mockResolvedValue(undefined);
  render(<FeedbackSignalsPanel revision={0} onRefresh={refresh} />);
  expect(await screen.findByText("Science article")).toBeInTheDocument();
  expect(screen.getByText(/Topics: science/)).toHaveTextContent("Platform: rss · Source: source:one");
  await user.click(screen.getByRole("button", { name: "Undo this feedback" }));
  expect(screen.queryByText("Undone")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Undoing…" })).toBeDisabled();
  load.mockResolvedValue({ ...history, items: [{ ...history.items[0], undone_at: "2026-09-10T10:00:00Z", active: false }] });
  finish({} as Awaited<ReturnType<typeof digestApi.undoFeedback>>);
  expect(await screen.findByText("Undone")).toBeInTheDocument();
  expect(undo).toHaveBeenCalledExactlyOnceWith("event-1");
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
});

it("keeps a failed undo retryable and hides private error details", async () => {
  vi.spyOn(digestApi, "feedbackHistory").mockResolvedValue(history);
  const undo = vi.spyOn(digestApi, "undoFeedback").mockRejectedValue(new Error("private-token"));
  render(<FeedbackSignalsPanel revision={0} onRefresh={vi.fn()} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Undo this feedback" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Retry is safe");
  expect(screen.queryByText("Undone")).not.toBeInTheDocument();
  expect(screen.queryByText("private-token")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Undo this feedback" }));
  expect(undo).toHaveBeenCalledTimes(2);
});

it("reloads history after a recoverable load failure", async () => {
  vi.spyOn(digestApi, "feedbackHistory").mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValue({ ...history, items: [], total: 0 });
  render(<FeedbackSignalsPanel revision={0} onRefresh={vi.fn()} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Reload feedback" }));
  expect(await screen.findByText(/No feedback yet/)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
