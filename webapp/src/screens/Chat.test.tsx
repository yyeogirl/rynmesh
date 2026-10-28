import { render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { Peer } from "../domain/types";
import Chat from "./Chat";

// jsdom has no EventSource; Chat opens one on mount. The stream is not what
// these tests are about, so a no-op stand-in is enough.
beforeAll(() => {
  class FakeEventSource {
    onmessage: ((event: MessageEvent) => void) | null = null;
    close() {}
  }
  Object.defineProperty(window, "EventSource", {
    configurable: true,
    value: FakeEventSource,
  });
});

const PEER: Peer = {
  id: "peer:friend", slug: "friend", name: "Friend", endpoint: "http://friend:8791",
  network: "rynmesh-main", tier: "attested", credits: 0, weight: 1,
  lastSeen: "2026-09-03T10:00:00Z", served: 0, fetched: 0,
};

function renderChat(history: Record<string, unknown>[]) {
  const client = makeFixtureNodeClient();
  vi.spyOn(client, "listMessages").mockResolvedValue(history);
  const context: AppOutletContext = {
    client,
    node: {
      node_name: "Test Ryn", peer_id: "peer:self", daemon_running: true,
      registry: "connected", peer_count: 1, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [PEER], refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(), notify: vi.fn(),
  };
  return render(
    <MemoryRouter initialEntries={["/chat"]}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/chat" element={<Chat />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function outbound(extra: Record<string, unknown>) {
  return {
    msg_id: "m1", dir: "out", from: "peer:self", to: PEER.id,
    text: "hello", ts: "2026-09-03T10:00:00Z", kind: "text", ...extra,
  };
}

describe("Chat delivery markers", () => {
  it("marks a mailbox-queued message as queued, not delivered", async () => {
    renderChat([outbound({ delivered: false, via: "mailbox" })]);

    const marker = await screen.findByText("queued");
    expect(marker).toHaveAttribute(
      "title",
      "Queued via the network mailbox; delivered when the peer comes online",
    );
    expect(screen.queryByText("✓")).not.toBeInTheDocument();
  });

  it("keeps the check mark for a directly delivered message", async () => {
    renderChat([outbound({ delivered: true, via: "direct" })]);

    expect(await screen.findByText("✓")).toBeInTheDocument();
    expect(screen.queryByText("queued")).not.toBeInTheDocument();
  });

  it("shows neither marker when nothing took the message", async () => {
    renderChat([outbound({ delivered: false })]);

    expect(await screen.findByText("hello")).toBeInTheDocument();
    expect(screen.queryByText("queued")).not.toBeInTheDocument();
    expect(screen.queryByText("✓")).not.toBeInTheDocument();
  });

  it("never marks an inbound message as queued", async () => {
    renderChat([
      { msg_id: "m2", dir: "in", from: PEER.id, to: "peer:self", text: "hi back",
        ts: "2026-09-03T10:01:00Z", kind: "text", delivered: false, via: "mailbox" },
    ]);

    expect(await screen.findByText("hi back")).toBeInTheDocument();
    expect(screen.queryByText("queued")).not.toBeInTheDocument();
  });
});
