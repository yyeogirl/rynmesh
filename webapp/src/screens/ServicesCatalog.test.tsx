import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import ServicesCatalog from "./ServicesCatalog";

function context(): AppOutletContext {
  return {
    client: makeFixtureNodeClient(),
    node: {
      node_name: "Test Ryn", peer_id: "peer:test", daemon_running: true,
      registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [], refreshShell: vi.fn(async () => undefined), confirm: vi.fn(), notify: vi.fn(),
  };
}

function renderCatalog() {
  return render(
    <MemoryRouter initialEntries={["/services"]}>
      <Routes>
        <Route element={<Outlet context={context()} />}>
          <Route path="/services" element={<ServicesCatalog />} />
          <Route path="/services/private-ai/chat" element={<h1>Private AI destination</h1>} />
          <Route path="/services/video-rendering" element={<h1>Video destination</h1>} />
          <Route path="/services/secure-web-access" element={<h1>Secure web destination</h1>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Services catalog", () => {
  it("shows type-specific actions and filters without infrastructure jargon", async () => {
    const user = userEvent.setup();
    renderCatalog();

    expect(await screen.findByRole("heading", { name: "Choose a service" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open chat/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Create video/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Connect/ })).toBeEnabled();
    expect(screen.queryByText(/peer:fixture/)).not.toBeInTheDocument();
    expect(screen.queryByText(/DEV_TASK_BALANCE/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "AI" }));
    expect(screen.getByRole("heading", { name: "Private AI" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Video rendering" })).not.toBeInTheDocument();
  });

  it("opens the selected language model in its chat experience", async () => {
    const user = userEvent.setup();
    renderCatalog();
    await user.click(await screen.findByRole("button", { name: /Open chat/ }));
    expect(screen.getByRole("heading", { name: "Private AI destination" })).toBeInTheDocument();
  });

  it("opens each non-chat service in its own experience", async () => {
    const user = userEvent.setup();
    renderCatalog();
    await user.click(await screen.findByRole("button", { name: /Create video/ }));
    expect(screen.getByRole("heading", { name: "Video destination" })).toBeInTheDocument();
  });
});
