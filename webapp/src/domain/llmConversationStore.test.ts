import { describe, expect, it } from "vitest";
import {
  buildConversationPrompt,
  conversationStorageMode,
  createConversation,
  listConversations,
  saveConversation,
  titleFromPrompt,
  type LLMChatMessage,
} from "./llmConversationStore";

function readRawConversation(id: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("ryn-private-ai-chat");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const transaction = request.result.transaction("conversations", "readonly");
      const getRequest = transaction.objectStore("conversations").get(id);
      getRequest.onsuccess = () => resolve(getRequest.result);
      getRequest.onerror = () => reject(getRequest.error);
    };
  });
}

function writeRawConversation(record: unknown): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("ryn-private-ai-chat");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const transaction = request.result.transaction("conversations", "readwrite");
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
      transaction.objectStore("conversations").put(record);
    };
  });
}

describe("Private AI conversation storage", () => {
  it("encrypts message bodies at rest and restores them", async () => {
    expect(await conversationStorageMode()).toBe("encrypted");
    const conversation = createConversation({
      serviceKey: "peer:test::model:test",
      serviceName: "Test private model",
      providerPeerId: "peer:test",
      networkId: "rynmesh-test",
    });
    const secret = "This prompt must not be stored as plaintext";
    conversation.title = titleFromPrompt(secret);
    conversation.messages.push({
      id: "message-secret", role: "user", content: secret,
      createdAt: new Date().toISOString(), status: "complete",
    });
    await saveConversation(conversation);

    const raw = await readRawConversation(conversation.id);
    expect(JSON.stringify(raw)).not.toContain(secret);
    const restored = await listConversations(conversation.serviceKey);
    expect(restored.find((item) => item.id === conversation.id)?.messages[0].content).toBe(secret);
  });

  it("builds follow-up context without failed messages", () => {
    const messages: LLMChatMessage[] = [
      { id: "1", role: "user", content: "What is Rynmesh?", createdAt: "2026-01-01", status: "complete" },
      { id: "2", role: "assistant", content: "A private service network.", createdAt: "2026-01-01", status: "complete" },
      { id: "3", role: "assistant", content: "temporary error", createdAt: "2026-01-01", status: "failed" },
      { id: "4", role: "user", content: "Explain more.", createdAt: "2026-01-01", status: "complete" },
    ];
    const prompt = buildConversationPrompt(messages);
    expect(prompt).toContain("User: What is Rynmesh?");
    expect(prompt).toContain("Assistant: A private service network.");
    expect(prompt).toContain("User: Explain more.");
    expect(prompt).not.toContain("temporary error");
  });

  it("keeps valid history available when one encrypted record is corrupt", async () => {
    const conversation = createConversation({
      serviceKey: "peer:recovery::model:test",
      serviceName: "Recovery model",
      providerPeerId: "peer:recovery",
      networkId: "rynmesh-test",
    });
    conversation.messages.push({
      id: "message-valid", role: "user", content: "Valid conversation",
      createdAt: new Date().toISOString(), status: "complete",
    });
    await saveConversation(conversation);
    await writeRawConversation({
      id: "corrupt-record",
      serviceKey: conversation.serviceKey,
      updatedAt: new Date().toISOString(),
      iv: "not-valid-base64",
      ciphertext: "not-valid-ciphertext",
    });

    const restored = await listConversations(conversation.serviceKey);
    expect(restored.map((item) => item.id)).toContain(conversation.id);
    expect(restored.map((item) => item.id)).not.toContain("corrupt-record");
  });
});
