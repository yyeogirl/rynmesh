import { IDBFactory, IDBObjectStore } from "fake-indexeddb";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

let store: typeof import("./llmConversationStore");
beforeEach(async () => {
  vi.resetModules();
  vi.stubGlobal("indexedDB", new IDBFactory());
  store = await import("./llmConversationStore");
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function conversation() {
  const row = store.createConversation({ serviceKey: "peer::model", serviceName: "Model", providerPeerId: "peer", networkId: "network" });
  row.messages.push({ id: "message", role: "user", content: "Private legacy question", createdAt: row.createdAt, status: "complete" });
  return row;
}
function open(version?: number): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("ryn-private-ai-chat", version);
    request.onupgradeneeded = () => {
      for (const name of ["keys", "conversations"]) if (!request.result.objectStoreNames.contains(name)) request.result.createObjectStore(name, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function raw(name: string, value?: unknown): Promise<unknown[]> {
  const db = await open();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(name, value === undefined ? "readonly" : "readwrite");
    const target = tx.objectStore(name);
    if (value !== undefined) target.put(value);
    const request = target.getAll();
    tx.oncomplete = () => { db.close(); resolve(request.result); };
    tx.onabort = tx.onerror = () => { db.close(); reject(tx.error); };
  });
}

it("clears reviewed records, survives reload and leaves later new conversations intact", async () => {
  const first = conversation();
  await store.saveConversation(first);
  const review = await store.reviewBrowserConversations();
  expect(review.copies).toBe(1);
  expect(await store.eraseReviewedBrowserConversations(review.token)).toEqual({ removed: 1, reviewed_copies_cleared: true });
  expect(await raw("conversations")).toEqual([]);
  const next = conversation();
  await store.saveConversation(next);
  vi.resetModules();
  const restarted = await import("./llmConversationStore");
  expect((await restarted.eraseReviewedBrowserConversations(review.token)).removed).toBe(1);
  expect((await restarted.listConversations(next.serviceKey)).map((row) => row.id)).toEqual([next.id]);
  expect(JSON.stringify(await raw("erasure"))).not.toContain("Private legacy question");
  expect(await raw("keys")).toHaveLength(1);
});

it("rejects a changed review and keeps both the changed conversation and new copy", async () => {
  const first = conversation();
  await store.saveConversation(first);
  const review = await store.reviewBrowserConversations();
  first.title = "Changed after review";
  await store.saveConversation(first);
  await store.saveConversation(conversation());
  const before = await raw("conversations");
  await expect(store.eraseReviewedBrowserConversations(review.token)).rejects.toThrow("changed after review");
  expect(await raw("conversations")).toEqual(before);
});

it("a writer in another loaded module cannot restore an erased identity", async () => {
  const first = conversation();
  await store.saveConversation(first);
  vi.resetModules();
  const otherTab = await import("./llmConversationStore");
  await otherTab.listConversations(first.serviceKey);
  const review = await store.reviewBrowserConversations();
  await store.eraseReviewedBrowserConversations(review.token);
  const encryption = vi.spyOn(crypto.subtle, 'encrypt').mockRejectedValueOnce(new Error('encryption unavailable'));
  await expect(otherTab.saveConversation(first)).rejects.toThrow("was erased");
  expect(encryption).not.toHaveBeenCalled();
  encryption.mockRestore();
  expect(await raw("conversations")).toEqual([]);
  expect((await otherTab.readLegacyConversations()).conversations).toEqual([]);
});

it("an aborted clear rolls back deletion markers and does not claim success", async () => {
  const first = conversation();
  await store.saveConversation(first);
  const review = await store.reviewBrowserConversations();
  const before = await raw("conversations");
  const clear = IDBObjectStore.prototype.clear;
  const failure = vi.spyOn(IDBObjectStore.prototype, "clear").mockImplementation(function (this: IDBObjectStore) {
    const request = clear.call(this);
    this.transaction.abort();
    return request;
  });
  await expect(store.eraseReviewedBrowserConversations(review.token)).rejects.toThrow();
  expect(await raw("conversations")).toEqual(before);
  expect(await raw("erasure")).toEqual([]);
  failure.mockRestore();
  expect((await store.eraseReviewedBrowserConversations(review.token)).removed).toBe(1);
});

it("upgrades v1 without rewriting ciphertext and can review unreadable old copies", async () => {
  const old = await open(1);
  old.close();
  const corrupt = { id: "unreadable", serviceKey: "peer::old", ciphertext: "old unreadable ciphertext", iv: "old iv", extra: { preserve: true } };
  await raw("conversations", corrupt);
  const review = await store.reviewBrowserConversations();
  expect(await raw("conversations")).toEqual([corrupt]);
  expect(review.copies).toBe(1);
  await store.eraseReviewedBrowserConversations(review.token);
  expect(await raw("conversations")).toEqual([]);
});

it("reports an upgrade blocked by an older tab and allows retry after it closes", async () => {
  const old = await open(1);
  await expect(store.reviewBrowserConversations()).rejects.toThrow("Close other Ryn tabs");
  old.close();
  expect((await store.reviewBrowserConversations()).copies).toBe(0);
});

it("does not overwrite a future cleanup record", async () => {
  await store.saveConversation(conversation());
  await raw("erasure", { id: "state", version: 99, unknown: "preserve" });
  const before = await raw("conversations");
  await expect(store.reviewBrowserConversations()).rejects.toThrow("compatible version");
  await expect(store.eraseReviewedBrowserConversations("a".repeat(64))).rejects.toThrow("compatible version");
  expect(await raw("conversations")).toEqual(before);
  expect(await raw("erasure")).toEqual([{ id: "state", version: 99, unknown: "preserve" }]);
});

it("includes session fallback copies after a failed encrypted write and clears them only on commit", async () => {
  const first = conversation();
  const failedEncryption = vi.spyOn(crypto.subtle, 'encrypt').mockRejectedValueOnce(new Error('temporary crypto failure'));
  await store.saveConversation(first);
  failedEncryption.mockRestore();
  expect(await raw('conversations')).toEqual([]);
  const review = await store.reviewBrowserConversations();
  expect(review.copies).toBe(1);
  expect(review.memoryCopies).toBe(1);
  expect((await store.readLegacyConversations()).conversations).toHaveLength(1);
  await store.eraseReviewedBrowserConversations(review.token);
  expect((await store.readLegacyConversations()).conversations).toEqual([]);
  await expect(store.saveConversation(first)).rejects.toThrow('was erased');
});
