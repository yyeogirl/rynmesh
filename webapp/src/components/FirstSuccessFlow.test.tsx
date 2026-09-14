import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { FirstSuccessStatus } from "../domain/types";
import FirstSuccessFlow from "./FirstSuccessFlow";
import { makeDigestItem } from "../test/fixtures";

const ready: FirstSuccessStatus = {
  version: "ryn.first-success.v1",
  phase: "ready",
  completed: false,
  dismissed: false,
  node_ready: true,
  content_ready: true,
  item_count: 6,
  healthy_sources: 4,
  source_count: 4,
  failed_sources: 0,
  degraded: false,
  using_cache: false,
  first_item_opened: false,
  first_signal_recorded: false,
  milestones: { node_ready: 1, content_ready: 1 },
  safe_error: null,
  recoverable_actions: [],
};

afterEach(() => vi.restoreAllMocks());

describe("FirstSuccessFlow", () => {
  it("restores the article actually read after a restart, and recovers a failed bookmark", async () => {
    const client = { ...makeFixtureNodeClient(), mode: "live" as const };
    vi.spyOn(digestApi, "listConsumption").mockResolvedValue([{
      item_id: "earlier", last_opened_unix: 100, progress: 0.4,
      first_opened_unix: 100, last_activity_unix: 100, open_count: 1, bookmarked: false, completed: false,
      item: makeDigestItem({ item_id: "earlier", title: "Earlier real article", link: "https://example.test/earlier", content_kind: "document", tags: [], source_title: "Earlier source" }),
    } as ConsumptionRecord]);
    const bookmark = vi.spyOn(client, "recordContentConsumption")
      .mockRejectedValueOnce(new Error("disk full")).mockResolvedValue(undefined);
    vi.spyOn(client, "getFirstSuccess").mockResolvedValue({ ...ready, phase: "completed", completed: true, first_item_opened: true, first_signal_recorded: true });
    function Harness() {
      const [status, setStatus] = useState<FirstSuccessStatus>({ ...ready, phase: "awaiting_signal", first_item_opened: true });
      return <MemoryRouter><FirstSuccessFlow client={client} status={status} onStatusChange={setStatus} onClose={vi.fn()} /></MemoryRouter>;
    }
    const user = userEvent.setup();
    render(<Harness />);
    expect(await screen.findByText("Earlier real article")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save for later" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not save");
    expect(screen.queryByRole("heading", { name: "Your first read is saved" })).not.toBeInTheDocument();
    expect(bookmark.mock.calls[0][0].digest_item_id).toBe("earlier");
    await user.click(screen.getByRole("button", { name: "Save for later" }));
    expect(await screen.findByRole("heading", { name: "Your first read is saved" })).toBeInTheDocument();
    expect(bookmark).toHaveBeenCalledTimes(2);
  });
  it("moves from a real recommendation to a saved local signal", async () => {
    const client = makeFixtureNodeClient();
    vi.spyOn(client, "getContentBody").mockResolvedValue({ ok: true, content_id: "article", content_type: "text/plain", size: "40", truncated: false, text: "The real article body is ready to read." });
    const user = userEvent.setup();

    function Harness() {
      const [status, setStatus] = useState(ready);
      return (
        <MemoryRouter>
          <FirstSuccessFlow client={client} status={status} onStatusChange={setStatus} onClose={vi.fn()} />
        </MemoryRouter>
      );
    }

    render(<Harness />);
    expect(screen.getByRole("heading", { name: "Open one real recommendation" })).toBeInTheDocument();

    const item = await screen.findByRole("button", { name: /Mira Studio micro-essays/ });
    await user.click(item);
    expect(await screen.findByText("The real article body is ready to read.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close content viewer" }));
    expect(await screen.findByRole("heading", { name: "Save your first read" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save for later" }));
    expect(await screen.findByRole("heading", { name: "Your first read is saved" })).toBeInTheDocument();
    expect(screen.getByText("Open one").closest("span")).toHaveClass("done");
    expect(screen.getByText("Save a choice").closest("span")).toHaveClass("done");
  });

  it("does not count a failed body fetch as reading and lets the user retry", async () => {
    const client = makeFixtureNodeClient();
    const read = vi.spyOn(client, "getContentBody").mockRejectedValueOnce(new Error("offline"));
    read.mockResolvedValue({ ok: true, content_id: "article", content_type: "text/plain", size: "24", truncated: false, text: "Recovered article body." });
    const record = vi.spyOn(client, "recordContentConsumption");
    const user = userEvent.setup();
    render(<MemoryRouter><FirstSuccessFlow client={client} status={ready} onStatusChange={vi.fn()} onClose={vi.fn()} /></MemoryRouter>);
    await user.click(await screen.findByRole("button", { name: /Mira Studio micro-essays/ }));
    await screen.findByRole("button", { name: "Retry reading" });
    expect(record).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry reading" }));
    expect(await screen.findByText("Recovered article body.")).toBeInTheDocument();
    await waitFor(() => expect(record).toHaveBeenCalledTimes(1));
    expect(record.mock.calls[0][1]).toBe("opened");
  });

  it("keeps the dialog open when dismiss cannot be saved", async () => {
    const client = makeFixtureNodeClient();
    vi.spyOn(client, "dismissFirstSuccess").mockRejectedValue(new Error("disk"));
    const close = vi.fn();
    const user = userEvent.setup();
    render(<MemoryRouter><FirstSuccessFlow client={client} status={ready} onStatusChange={vi.fn()} onClose={close} /></MemoryRouter>);
    await user.click(screen.getAllByRole("button", { name: "Continue later" })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("could not save");
    expect(close).not.toHaveBeenCalled();
  });

  it("explains a recoverable source failure without asking for a model or peer", () => {
    const status: FirstSuccessStatus = {
      ...ready,
      phase: "needs_action",
      content_ready: false,
      item_count: 0,
      safe_error: "discovery_unavailable",
      recoverable_actions: ["retry_discovery"],
    };
    render(
      <MemoryRouter>
        <FirstSuccessFlow client={makeFixtureNodeClient()} status={status} onStatusChange={vi.fn()} onClose={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "The sources need another try" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry discovery" })).toBeEnabled();
    expect(screen.queryByText(/AI model/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/connect a peer/i)).not.toBeInTheDocument();
  });
});
