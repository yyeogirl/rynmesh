import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { askHistory } from "../domain/askHistory";
import { friendsApi } from "../domain/friendsClient";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { createConversation, type LLMConversation } from "../domain/llmConversationStore";
import type { LLMServiceRecord } from "../domain/nodeClient";
import AskRyn, { AskRynQuickPanel, conversationUrl } from "./AskRyn";

beforeEach(() => {
  vi.spyOn(askHistory, "syncConflicts").mockResolvedValue([]);
  vi.spyOn(askHistory, "draft").mockResolvedValue({ text: "", revision: 0 });
  vi.spyOn(askHistory, "saveDraft").mockImplementation(async (text, revision) => ({ text, revision: revision + 1 }));
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [] });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function mockDownload() {
  const create = vi.fn(() => "blob:ask-export");
  vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: create, revokeObjectURL: vi.fn() }));
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  return { create, click };
}

it("exports the latest unsaved draft once and reports only a requested download", async () => {
  const download = mockDownload();
  let persisted = "", exported = "";
  let finish!: () => void;
  let finishSave!: () => void;
  vi.mocked(askHistory.saveDraft).mockImplementation((text, revision) => new Promise((resolve) => {
    finishSave = () => { persisted = text; resolve({ text, revision: revision + 1 }); };
  }));
  const exportRequest = vi.spyOn(askHistory, "export").mockImplementation(() => {
    exported = persisted;
    return new Promise((resolve) => { finish = () => resolve({ version: "ryn.ask-export.v1", conversations: [] }); });
  });
  const { user } = mount();
  await waitFor(() => expect(screen.getByLabelText("Your draft")).toBeEnabled());
  await user.click(screen.getByLabelText("Your draft"));
  await user.paste("Latest unsaved export draft");
  await user.click(screen.getByRole("button", { name: "Export conversations and draft" }));
  await waitFor(() => expect(askHistory.saveDraft).toHaveBeenCalledTimes(1));
  expect(exportRequest).not.toHaveBeenCalled();
  await act(async () => finishSave());
  await waitFor(() => expect(exportRequest).toHaveBeenCalledTimes(1));
  expect(exported).toBe("Latest unsaved export draft");
  const busy = screen.getByRole("button", { name: "Preparing export…" });
  expect(busy).toBeDisabled();
  await user.click(busy);
  expect(exportRequest).toHaveBeenCalledTimes(1);
  await act(async () => finish());
  const result = await screen.findByText(/Export prepared and download requested/);
  expect(result).toHaveFocus();
  expect(download.click).toHaveBeenCalledTimes(1);
});

it.each(["save", "export"])("keeps the draft and supports retry when %s fails", async (failure) => {
  const download = mockDownload();
  const exportRequest = vi.spyOn(askHistory, "export").mockResolvedValue({ version: "ryn.ask-export.v1", conversations: [] });
  if (failure === "save") vi.mocked(askHistory.saveDraft).mockRejectedValue(new Error("Synthetic write failure"));
  else exportRequest.mockRejectedValueOnce(new Error("Synthetic connection failure"));
  const { user } = mount();
  await waitFor(() => expect(screen.getByLabelText("Your draft")).toBeEnabled());
  await user.click(screen.getByLabelText("Your draft"));
  await user.paste("Keep this draft through export failure");
  await user.click(screen.getByRole("button", { name: "Export conversations and draft" }));
  const error = await screen.findByText(/Export was not completed/);
  expect(error).toHaveFocus();
  expect(screen.getByLabelText("Your draft")).toHaveValue("Keep this draft through export failure");
  expect(download.create).not.toHaveBeenCalled();
  if (failure === "save") expect(exportRequest).not.toHaveBeenCalled();
  vi.mocked(askHistory.saveDraft).mockImplementation(async (text, revision) => ({ text, revision: revision + 1 }));
  await user.click(screen.getByRole("button", { name: "Export conversations and draft" }));
  expect(await screen.findByText(/Export prepared and download requested/)).toHaveFocus();
  expect(download.click).toHaveBeenCalledTimes(1);
});

function mount(initial = "/ask", services: LLMServiceRecord[] = [], rows: LLMConversation[] = []) {
  const client = makeFixtureNodeClient(); client.mode = "live";
  vi.spyOn(client, "listLLMServices").mockResolvedValue(services);
  const submit = vi.spyOn(client, "submitLLMOrder");
  const saved = new Map(rows.map((row) => [row.id, row]));
  vi.spyOn(askHistory, "list").mockImplementation(async (key) => [...saved.values()].filter((row) => !key || row.serviceKey === key));
  const save = vi.spyOn(askHistory, "save").mockImplementation(async (row) => { const result = { ...row, revision: (row.revision ?? 0) + 1 }; saved.set(row.id, result); return result; });
  const confirm = vi.fn();
  const context = { client, confirm, node: { peer_id: "owner" }, notify: vi.fn() } as unknown as AppOutletContext;
  const rendered = render(<MemoryRouter initialEntries={[initial]}><Routes><Route element={<Outlet context={context} />}>
    <Route path="/ask" element={<AskRyn />} /><Route path="/services/manage" element={<h1>Local model setup</h1>} />
  </Route></Routes></MemoryRouter>);
  return { ...rendered, submit, save, confirm, context, user: userEvent.setup() };
}

it("shows no invented history and keeps an unsent draft without a model", async () => {
  const { user, submit } = mount();
  expect(await screen.findByText("No conversations yet.")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("Your draft")).toBeEnabled());
  await user.type(screen.getByLabelText("Your draft"), "还没有模型，也要保留的问题");
  await user.click(screen.getByRole("button", { name: "Save draft" }));
  expect(await screen.findByText(/Draft saved on your node/)).toBeInTheDocument();
  expect(askHistory.saveDraft).toHaveBeenCalledWith("还没有模型，也要保留的问题", 0);
  expect(submit).not.toHaveBeenCalled();
  expect(screen.queryByText(/urban gardens/)).not.toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: "Set up a local model" }));
  expect(screen.getByRole("heading", { name: "Local model setup" })).toBeInTheDocument();
});

it("opens saved history and the same sidebar link when its model is unavailable", async () => {
  const row = { ...createConversation({ serviceKey: "old::model", serviceName: "Old model", providerPeerId: "old", networkId: "rynmesh-main" }), title: "My saved question",
    messages: [{ id: "m", role: "assistant" as const, content: "An actual saved response", createdAt: new Date().toISOString(), status: "complete" as const }], revision: 1 };
  const { context, submit } = mount(conversationUrl(row), [], [row]);
  expect(await screen.findByText("An actual saved response")).toBeInTheDocument();
  expect(screen.getByText(/original service is unavailable/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Continue with original provider" })).not.toBeInTheDocument();
  const quick = render(<MemoryRouter><AskRynQuickPanel context={context} /></MemoryRouter>);
  expect(await within(quick.container).findByRole("link", { name: row.title })).toHaveAttribute("href", conversationUrl(row));
  expect(submit).not.toHaveBeenCalled();
});

it("reviews the recipient and opens a separate provider conversation without forwarding history", async () => {
  const base = (await makeFixtureNodeClient().listLLMServices())[0];
  const other = { ...base, peer_id: "provider-b", node_name: "Provider B", service: { ...base.service, model_alias: "Model B" } };
  const old = { ...createConversation({ serviceKey: "provider-a::model-a", serviceName: "Model A", providerPeerId: "provider-a", networkId: "rynmesh-main" }), title: "Private history with A", revision: 1 };
  const { user, confirm, save, submit } = mount(conversationUrl(old), [other], [old]);
  await user.click(await screen.findByRole("button", { name: "Choose Model B" }));
  expect(save).not.toHaveBeenCalled();
  const request = confirm.mock.calls[0][0];
  expect(request.body).toContain("provider-b");
  await act(() => request.onConfirm());
  await screen.findByLabelText("Message Private AI");
  const opened = save.mock.calls[0][0];
  expect(opened.providerPeerId).toBe("provider-b");
  expect(opened.messages).toEqual([]);
  expect(opened.id).not.toBe(old.id);
  expect(submit).not.toHaveBeenCalled();
});

it("keeps the supplied article excerpt recoverable in archived history without a model", async () => {
  const row = { ...createConversation({ serviceKey: "old::model", serviceName: "Old model", providerPeerId: "old", networkId: "rynmesh-main" }),
    messages: [{ id: "answer", role: "assistant" as const, content: "Three trees", createdAt: new Date().toISOString(), status: "complete" as const,
      contextIds: ["import:source"], contextBytes: [3] }], revision: 1 };
  const context = vi.spyOn(askHistory, "context").mockRejectedValueOnce(new Error("Source temporarily unavailable"))
    .mockResolvedValue({ library_id: "import:source", title: "Garden notebook", source_url: "https://example.test/garden",
      sha256: "a".repeat(64), extraction_truncated: false, text_bytes: 11, text: "abcEXCLUDED" });
  const { user, submit } = mount(conversationUrl(row), [], [row]);
  await screen.findByText("Three trees");
  await user.click(screen.getByText("Sources supplied for this answer"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Source temporarily unavailable");
  await user.click(screen.getByRole("button", { name: "Retry source" }));
  expect(await screen.findByText(/Supplied excerpt: 3 bytes of 11/)).toHaveTextContent("Truncated for the context budget");
  await user.click(screen.getByText("Read supplied excerpt"));
  expect(screen.getByText("abc")).toBeInTheDocument();
  expect(screen.queryByText(/EXCLUDED/)).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "https://example.test/garden" })).toHaveAttribute("href", "https://example.test/garden");
  expect(context).toHaveBeenCalledTimes(2);
  expect(submit).not.toHaveBeenCalled();
});

it("does not replace an explicitly requested missing provider with a working one", async () => {
  const base = (await makeFixtureNodeClient().listLLMServices())[0];
  const { submit } = mount("/ask?peer=missing&service=missing-model", [base]);
  expect(await screen.findByRole("heading", { name: "The selected provider is unavailable" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Message Private AI")).not.toBeInTheDocument();
  expect(submit).not.toHaveBeenCalled();
});

it("reports retained and skipped migration records without claiming they were imported", async () => {
  vi.spyOn(askHistory, "importLegacy").mockResolvedValue({ imported: 0, alreadyPresent: 1, skippedDeleted: 1, retained: 2 });
  const { user, confirm } = mount();
  await user.click(await screen.findByRole("button", { name: "Import older browser conversations" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(await screen.findByRole("status")).toHaveTextContent("0 imported; 1 already on node; 1 skipped because previously deleted; 2 need recovery. Browser originals kept.");
});

it("opens a recovered branch as readable history without a model and focuses it", async () => {
  const value = { ...createConversation({ serviceKey: "old::model", serviceName: "Old model", providerPeerId: "old", networkId: "rynmesh-main" }),
    title: "Recovered garden answer", messages: [{ id: "m", role: "assistant" as const, content: "The retained answer from the other computer.", createdAt: new Date().toISOString(), status: "complete" as const }] };
  vi.mocked(askHistory.syncConflicts).mockResolvedValue([{ id: value.id, revision: "a".repeat(64), conflict: true, deleted: true, erased: false, deferred: false,
    common_messages: [], branches: [], recovery: [{ choice_id: "b".repeat(64) + ":2", value }] }]);
  const { user, save, submit } = mount();
  vi.spyOn(askHistory, "restoreBranch").mockImplementation(async (_id, _choice, newId) => save({ ...value, id: newId }));
  await user.click(await screen.findByRole("button", { name: "Keep branch 1 as a separate conversation" }));
  expect(await screen.findByText(value.messages[0].content)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Selected conversation" })).toHaveFocus();
  expect(screen.getByText(/original service is unavailable/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Continue with original provider" })).not.toBeInTheDocument();
  expect(submit).not.toHaveBeenCalled();
});
