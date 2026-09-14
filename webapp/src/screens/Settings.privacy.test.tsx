import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { PrivacySection } from "./Settings";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("does not report erasure success when the node rejects the selected reset", async () => {
  const client = makeFixtureNodeClient();
  vi.spyOn(client, "erasePersonalData").mockRejectedValue(new Error("private disk path"));
  const confirm = vi.fn(); const notify = vi.fn();
  render(<PrivacySection client={client} confirm={confirm} notify={notify} revision={0} />);
  await userEvent.setup().click(screen.getByRole("button", { name: "Reset learning" }));
  await expect(confirm.mock.calls[0][0].onConfirm()).rejects.toThrow("could not be fully erased");
  expect(notify).not.toHaveBeenCalled();
});

it("reports a failed reading export, prevents duplicate requests, and retries with a download notice", async () => {
  const client = makeFixtureNodeClient();
  let reject!: (error: Error) => void;
  const request = vi.spyOn(client, "exportPersonalData").mockImplementationOnce(() =>
    new Promise((_, fail) => { reject = fail; }));
  const create = vi.fn(() => "blob:reading-export");
  vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: vi.fn() });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  render(<PrivacySection client={client} confirm={vi.fn()} notify={vi.fn()} revision={0} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Export reading & preferences (JSON)" }));
  await user.click(screen.getByRole("button", { name: "Preparing reading export…" }));
  expect(request).toHaveBeenCalledTimes(1);
  reject(new Error("private transport diagnostic"));
  const error = await screen.findByText(/did not return a complete reading/);
  expect(error).toHaveFocus();
  expect(screen.queryByText(/private transport diagnostic/)).not.toBeInTheDocument();
  expect(create).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Export reading & preferences (JSON)" }));
  expect(await screen.findByText(/export prepared; download requested/)).toHaveFocus();
  expect(click).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledTimes(2);
});

it("offers a retry when the privacy summary cannot load", async () => {
  const client = makeFixtureNodeClient();
  const request = vi.spyOn(client, "getPrivacyStatus").mockRejectedValueOnce(new Error("offline"));
  render(<PrivacySection client={client} confirm={vi.fn()} notify={vi.fn()} revision={0} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Retry data summary" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Retry data summary" })).not.toBeInTheDocument());
  expect(request).toHaveBeenCalledTimes(2);
  expect(await screen.findByText("Recommendation learning")).toBeInTheDocument();
});
