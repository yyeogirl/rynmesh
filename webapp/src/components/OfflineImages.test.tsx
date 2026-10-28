import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { offlineApi, type OfflineBody } from "../domain/offlineReading";
import OfflineImages from "./OfflineImages";

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it("uses local blobs, reports decoder failures, and revokes images on close", async () => {
  const create = vi.fn(() => "blob:local-copy"); const revoke = vi.fn();
  vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: create, revokeObjectURL: revoke }));
  vi.spyOn(offlineApi, "image").mockResolvedValue(new Blob(["image"], { type: "image/png" }));
  const body = { job_id: "version", images: [{ index: 0, state: "verified", alt: "Saved chart" }] } as OfflineBody;
  const view = render(<OfflineImages body={body} itemKey="key" />);
  const image = await screen.findByRole("img");
  expect(image).toHaveAttribute("src", "blob:local-copy");
  expect(offlineApi.image).toHaveBeenCalledWith("key", "version", 0, expect.any(AbortSignal));
  fireEvent.error(image);
  expect(await screen.findByText(/Image unavailable.*Saved chart/)).toBeInTheDocument();
  view.unmount();
  await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:local-copy"));
});

it("settles an unresponsive local image request so position recovery cannot wait forever", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.spyOn(offlineApi, "image").mockImplementation(() => new Promise(() => undefined));
  const body = { job_id: "version", images: [{ index: 0, state: "verified", alt: "Delayed chart" }] } as OfflineBody;
  const ready = vi.fn();
  render(<OfflineImages body={body} itemKey="key" onReady={ready} />);
  expect(ready).not.toHaveBeenCalled();
  await act(async () => { vi.advanceTimersByTime(15000); });
  expect(screen.getByText(/Image unavailable.*Delayed chart/)).toBeInTheDocument();
  expect(ready).toHaveBeenCalledExactlyOnceWith("version");
  expect(vi.mocked(offlineApi.image).mock.calls[0][3].aborted).toBe(true);
});
