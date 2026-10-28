import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import SecureWebAccess from "./SecureWebAccess";
import VideoRendering from "./VideoRendering";

function makeContext() {
  const client = makeFixtureNodeClient();
  const context: AppOutletContext = {
    client,
    node: {
      node_name: "Test Ryn", peer_id: "peer:test", daemon_running: true,
      registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [], refreshShell: vi.fn(async () => undefined), confirm: vi.fn(), notify: vi.fn(),
  };
  return { client, context };
}

function renderExperience(path: string, element: React.ReactNode) {
  const { client, context } = makeContext();
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path={path} element={element} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { ...result, client, user: userEvent.setup() };
}

describe("Service-specific experiences", () => {
  it("submits a video workflow without asking the user to choose a provider", async () => {
    const { client, user } = renderExperience("/services/video-rendering", <VideoRendering />);
    const submit = vi.spyOn(client, "submitWorkOrder");
    expect(await screen.findByText("Renderer ready")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Video project ID"), "launch-project-7");
    await user.click(screen.getByRole("button", { name: /Start rendering/ }));

    expect(await screen.findByText("Render request submitted")).toBeInTheDocument();
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      provider_peer_id: "peer:m4-mini-veo",
      params: expect.objectContaining({ video_id: "launch-project-7" }),
    }));
  });

  it("connects, launches, and disconnects the secure web route", async () => {
    const { client, user } = renderExperience("/services/secure-web-access", <SecureWebAccess />);
    const connect = vi.spyOn(client, "egressConnect");
    const launch = vi.spyOn(client, "egressLaunch");
    const disconnect = vi.spyOn(client, "egressDisconnect");

    await screen.findByRole("button", { name: /Connect securely/ });
    await user.click(screen.getByRole("button", { name: /Connect securely/ }));
    expect(await screen.findByText("Your secure route is active")).toBeInTheDocument();
    expect(connect).toHaveBeenCalledWith({ region: "CN" });

    await user.click(screen.getByRole("button", { name: /Open secure browser/ }));
    expect(launch).toHaveBeenCalledWith({ region: "CN" });
    await user.click(screen.getByRole("button", { name: /Disconnect/ }));
    expect(disconnect).toHaveBeenCalledWith({ region: "CN" });
    expect(await screen.findByText("Connect when you are ready")).toBeInTheDocument();
  });
});
