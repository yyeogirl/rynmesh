import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import Settings from "./Settings";

afterEach(() => vi.restoreAllMocks());

it("recovers a failed initial settings request from the keyboard instead of loading forever", async () => {
  const client = makeFixtureNodeClient();
  const request = vi.spyOn(client, "getSettings").mockRejectedValueOnce(new Error("private transport details"));
  const context = { client, confirm: vi.fn(), notify: vi.fn(), refreshShell: vi.fn() } as unknown as AppOutletContext;
  render(<MemoryRouter initialEntries={["/settings"]}><Routes>
    <Route element={<Outlet context={context} />}><Route path="/settings" element={<Settings />} /></Route>
  </Routes></MemoryRouter>);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Settings could not be loaded");
  expect(alert).toHaveFocus();
  expect(screen.queryByText("private transport details")).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.tab();
  expect(screen.getByRole("button", { name: "Retry settings" })).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(await screen.findByRole("heading", { name: "Identity & storage" })).toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
