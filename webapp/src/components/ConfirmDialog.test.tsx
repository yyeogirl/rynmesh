import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, it, vi } from "vitest";
import type { ConfirmRequest } from "../domain/types";
import { ConfirmDialog } from "./ui";

it("keeps failed operations reviewable and prevents duplicate submissions", async () => {
  let finish!: () => void;
  const operation = vi.fn().mockRejectedValueOnce(new Error("Storage unavailable. Retry."))
    .mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; }));
  function Harness() {
    const [request, setRequest] = useState<ConfirmRequest | null>({ title: "Clear copies?", body: "Bookmarks remain.", risk: "high", confirmLabel: "Clear copies", onConfirm: operation });
    return <ConfirmDialog request={request} onCancel={() => setRequest(null)} />;
  }
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "Clear copies" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Storage unavailable");
  expect(screen.getByRole("alert")).toHaveFocus();
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  const button = screen.getByRole("button", { name: "Clear copies" });
  fireEvent.click(button); fireEvent.click(button);
  await waitFor(() => expect(operation).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await act(async () => finish());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("moves focus into the review, contains tab navigation, and restores the trigger on Escape", async () => {
  const operation = vi.fn();
  function Harness() {
    const [request, setRequest] = useState<ConfirmRequest | null>(null);
    return <><button onClick={() => setRequest({ title: "Clear download?", body: "One local copy.", risk: "medium", onConfirm: operation })}>Review cleanup</button>
      <ConfirmDialog request={request} onCancel={() => setRequest(null)} /></>;
  }
  const user = userEvent.setup(); render(<Harness />);
  const trigger = screen.getByRole("button", { name: "Review cleanup" });
  await user.click(trigger);
  expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  await user.tab({ shift: true }); expect(screen.getByRole("button", { name: "Confirm" })).toHaveFocus();
  await user.tab(); expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(trigger).toHaveFocus();
  expect(operation).not.toHaveBeenCalled();
});

it("preserves an operation’s focused result when its confirmation closes", async () => {
  function Harness() {
    const [request, setRequest] = useState<ConfirmRequest | null>(null);
    const operation = async () => {
      screen.getByRole('alert').focus();
      await Promise.resolve();
    };
    return <div className="app-main"><button onClick={() => setRequest({ title: "Clear?", body: "Reviewed copies", risk: "high", onConfirm: operation })}>Review</button>
      <p role="alert" tabIndex={-1}>Backup cleanup remains unfinished.</p>
      <ConfirmDialog request={request} onCancel={() => setRequest(null)} /></div>;
  }
  const user = userEvent.setup(); render(<Harness />);
  await user.click(screen.getByRole('button', { name: 'Review' }));
  await user.click(screen.getByRole('button', { name: 'Confirm' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveFocus();
});
