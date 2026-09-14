import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { digestApi, type DiscoveryStatus } from "../domain/digestClient";
import SourceHealthPanel from "./SourceHealthPanel";

const status = {
  source_health: [
    { id: "one", title: "Working feed", ok: true, status: "healthy", error: "", item_count: 4, last_checked_unix: 100, last_success_unix: 100, consecutive_failures: 0, using_cached_items: false },
    { id: "two", title: "Cached feed", ok: false, status: "cached", error: "source_fetch_failed", item_count: 3, last_checked_unix: 120, last_success_unix: 90, consecutive_failures: 2, using_cached_items: true },
  ],
} as DiscoveryStatus;

afterEach(() => vi.restoreAllMocks());

describe("Source health", () => {
  it("distinguishes sources never checked from a failed first check without inventing success or cache", () => {
    const fresh = {
      ...status,
      source_health: ["Built-in feed", "Custom feed"].map((title, index) => ({
        ...status.source_health[0], id: `fresh-${index}`, title, ok: false, status: "not_checked",
        item_count: 0, last_checked_unix: 0, last_success_unix: 0, consecutive_failures: 0,
      })),
    } as DiscoveryStatus;
    const { rerender } = render(<SourceHealthPanel status={fresh} onRefresh={vi.fn()} />);
    for (const row of screen.getAllByRole("listitem")) {
      expect(within(row).getByText("Not checked")).toBeInTheDocument();
      expect(within(row).getByText(/0 items · Last checked: Never · Last success: Never · Consecutive failures: 0/)).toBeInTheDocument();
    }
    rerender(<SourceHealthPanel status={{ ...fresh, source_health: fresh.source_health.map((source) => ({
      ...source, status: "failed", error: "source_fetch_failed", last_checked_unix: 120, consecutive_failures: 1,
    })) }} onRefresh={vi.fn()} />);
    for (const row of screen.getAllByRole("listitem")) {
      expect(within(row).getByText("Unavailable")).toBeInTheDocument();
      expect(within(row).getByText(/Last success: Never · Consecutive failures: 1/)).toBeInTheDocument();
      expect(within(row).queryByText(/Last checked: Never/)).not.toBeInTheDocument();
      expect(within(row).getByRole("button", { name: /^Retry / })).toBeEnabled();
    }
    expect(screen.queryByText("Available")).not.toBeInTheDocument();
    expect(screen.queryByText("Using cached content")).not.toBeInTheDocument();
  });

  it("shows healthy and cached source details and retries only the selected source", async () => {
    const retry = vi.spyOn(digestApi, "retrySource").mockResolvedValue({ status } as Awaited<ReturnType<typeof digestApi.retrySource>>);
    const all = vi.spyOn(digestApi, "refreshDigest");
    const refresh = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<SourceHealthPanel status={status} onRefresh={refresh} />);
    expect(screen.getByText("Available")).toBeInTheDocument();
    expect(screen.getByText("Using cached content")).toBeInTheDocument();
    expect(screen.getByText(/Consecutive failures: 2/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry Cached feed" }));
    expect(retry).toHaveBeenCalledExactlyOnceWith("two");
    expect(all).not.toHaveBeenCalled();
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });

  it("offers recovery when a retry fails, preserving the source list", async () => {
    vi.spyOn(digestApi, "retrySource").mockRejectedValue(new Error("private token"));
    const user = userEvent.setup();
    render(<SourceHealthPanel status={status} onRefresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Retry Cached feed" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("wait and retry");
    expect(screen.queryByText("private token")).not.toBeInTheDocument();
    expect(screen.getByText("Working feed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry Cached feed" })).toBeEnabled();
  });

  it("distinguishes loading and empty states", () => {
    const { rerender } = render(<SourceHealthPanel status={null} onRefresh={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking");
    rerender(<SourceHealthPanel status={{ ...status, source_health: [] }} onRefresh={vi.fn()} />);
    expect(screen.getByText("No sources have been configured.")).toBeInTheDocument();
  });
});
