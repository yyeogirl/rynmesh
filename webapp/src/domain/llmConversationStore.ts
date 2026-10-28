/**
 * Device-local conversation persistence for the Private AI experience.
 *
 * Message bodies are encrypted before IndexedDB writes and are never copied to
 * localStorage. This protects against casual plaintext inspection, but it is
 * not a secure enclave: code running under the same origin, a compromised
 * browser profile, or the inference provider can still access plaintext.
 */
export type LLMChatRole = "user" | "assistant";
export type LLMChatMessageStatus = "complete" | "failed" | "cancelled" | "queued" | "running" | "cancel_requested" | "interrupted";

export interface LLMChatMessage {
  id: string;
  role: LLMChatRole;
  content: string;
  createdAt: string;
  status: LLMChatMessageStatus;
  taskId?: string;
  inputTokens?: number;
  outputTokens?: number;
  cost?: number;
  contextIds?: string[];
  contextBytes?: number[];
  promptSha256?: string;
}

export interface LLMConversation {
  id: string;
  title: string;
  serviceKey: string;
  serviceName: string;
  providerPeerId: string;
  networkId: string;
  createdAt: string;
  updatedAt: string;
  messages: LLMChatMessage[];
  revision?: number;
  draft?: string;
  contextIds?: string[];
  sync?: { revision: string; conflict: boolean; deleted: boolean; erased: boolean; branch_count: number; recovery_count: number; deferred: boolean };
}

interface EncryptedConversationRecord {
  id: string;
  serviceKey: string;
  updatedAt: string;
  iv: string;
  ciphertext: string;
}

interface StoredKey {
  id: "primary";
  key: CryptoKey;
}

const DB_NAME = "ryn-private-ai-chat";
const DB_VERSION = 2;
const KEY_STORE = "keys";
const CONVERSATION_STORE = "conversations";
const ERASURE_STORE = "erasure";
// Never fall back to plaintext persistence when Web Crypto or IndexedDB fails.
const memoryFallback = new Map<string, LLMConversation>();
let databasePromise: Promise<IDBDatabase> | null = null;
let keyPromise: Promise<CryptoKey> | null = null;
let browserWriter = Promise.resolve();
const erasedIds = new Set<string>();

function serializeWrite<T>(operation: () => Promise<T>): Promise<T> {
  const next = browserWriter.then(operation, operation);
  browserWriter = next.then(() => undefined, () => undefined);
  return next;
}

export class BrowserErasureError extends Error {}

function cloneConversation(conversation: LLMConversation): LLMConversation {
  return JSON.parse(JSON.stringify(conversation)) as LLMConversation;
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}

function supportsEncryptedPersistence() {
  return typeof indexedDB !== "undefined" && typeof crypto !== "undefined" && Boolean(crypto.subtle);
}

function openDatabase(): Promise<IDBDatabase> {
  if (!supportsEncryptedPersistence()) return Promise.reject(new Error("Encrypted persistence is unavailable"));
  if (databasePromise) return databasePromise;
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    let blocked = false;
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(KEY_STORE)) database.createObjectStore(KEY_STORE, { keyPath: "id" });
      if (!database.objectStoreNames.contains(CONVERSATION_STORE)) database.createObjectStore(CONVERSATION_STORE, { keyPath: "id" });
      if (!database.objectStoreNames.contains(ERASURE_STORE)) database.createObjectStore(ERASURE_STORE, { keyPath: "id" });
    };
    request.onblocked = () => { blocked = true; reject(new BrowserErasureError("Close other Ryn tabs using older browser storage, then retry. No browser cleanup was confirmed.")); };
    request.onsuccess = () => {
      if (blocked) { request.result.close(); return; }
      request.result.onversionchange = () => { request.result.close(); databasePromise = null; keyPromise = null; };
      resolve(request.result);
    };
    request.onerror = () => reject(request.error ?? new Error("Unable to open encrypted conversation storage"));
  });
  databasePromise = databasePromise.catch((cause) => { databasePromise = null; throw cause; });
  return databasePromise;
}

async function getEncryptionKey(): Promise<CryptoKey> {
  if (keyPromise) return keyPromise;
  keyPromise = (async () => {
    const database = await openDatabase();
    const readTransaction = database.transaction(KEY_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const stored = await requestResult(readTransaction.objectStore(KEY_STORE).get("primary")) as StoredKey | undefined;
    await readDone;
    if (stored?.key) return stored.key;

    // IndexedDB can structured-clone a non-extractable CryptoKey, so raw key
    // bytes never need to be serialized into JavaScript strings or storage.
    const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    const writeTransaction = database.transaction(KEY_STORE, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    writeTransaction.objectStore(KEY_STORE).put({ id: "primary", key } satisfies StoredKey);
    await writeDone;
    return key;
  })();
  return keyPromise;
}

function bytesToBase64(bytes: Uint8Array) {
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary);
}

function base64ToBytes(value: string) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

async function encryptConversation(conversation: LLMConversation): Promise<EncryptedConversationRecord> {
  const key = await getEncryptionKey();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plaintext = new TextEncoder().encode(JSON.stringify(conversation));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plaintext);
  return {
    id: conversation.id,
    serviceKey: conversation.serviceKey,
    updatedAt: conversation.updatedAt,
    iv: bytesToBase64(iv),
    ciphertext: bytesToBase64(new Uint8Array(ciphertext)),
  };
}

async function decryptConversation(record: EncryptedConversationRecord): Promise<LLMConversation> {
  const key = await getEncryptionKey();
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: base64ToBytes(record.iv) },
    key,
    base64ToBytes(record.ciphertext),
  );
  return JSON.parse(new TextDecoder().decode(plaintext)) as LLMConversation;
}

export function createConversation(input: {
  serviceKey: string;
  serviceName: string;
  providerPeerId: string;
  networkId: string;
}): LLMConversation {
  const now = new Date().toISOString();
  return {
    id: typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `conversation_${Date.now()}_${Math.random().toString(16).slice(2)}`,
    title: "New conversation",
    serviceKey: input.serviceKey,
    serviceName: input.serviceName,
    providerPeerId: input.providerPeerId,
    networkId: input.networkId,
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}

export function titleFromPrompt(prompt: string) {
  const compact = prompt.trim().replace(/\s+/g, " ");
  if (!compact) return "New conversation";
  return compact.length > 46 ? `${compact.slice(0, 46).trimEnd()}…` : compact;
}

export function buildConversationPrompt(messages: LLMChatMessage[]) {
  // The order API accepts one prompt rather than a message array. Rebuild the
  // transcript from successful messages only so failures never become context.
  const complete = messages.filter((message) => message.status === "complete" && message.content.trim());
  if (complete.length <= 1 && complete[0]?.role === "user") return complete[0].content;
  const transcript = complete.map((message) => `${message.role === "user" ? "User" : "Assistant"}: ${message.content}`).join("\n\n");
  return `Continue the conversation below. Answer the latest user message directly.\n\n${transcript}\n\nAssistant:`;
}

export async function saveConversation(conversation: LLMConversation) {
  const snapshot = cloneConversation(conversation);
  return serializeWrite(async () => { try {
    if (erasedIds.has(snapshot.id)) throw new BrowserErasureError("This browser conversation was erased. Start a new conversation.");
    const database = await openDatabase();
    // Check before encryption as well as in the write transaction: a failed
    // encryption after reload must not turn an erased ID into a memory copy.
    const guard = database.transaction(ERASURE_STORE, "readonly");
    const guardDone = transactionDone(guard);
    const alreadyErased = await requestResult(guard.objectStore(ERASURE_STORE).get(`deleted:${snapshot.id}`));
    await guardDone;
    if (alreadyErased) { erasedIds.add(snapshot.id); throw new BrowserErasureError("This browser conversation was erased. Start a new conversation."); }
    const record = await encryptConversation(snapshot);
    const transaction = database.transaction([CONVERSATION_STORE, ERASURE_STORE], "readwrite");
    const done = transactionDone(transaction);
    const erased = await requestResult(transaction.objectStore(ERASURE_STORE).get(`deleted:${snapshot.id}`));
    if (erased) { await done; erasedIds.add(snapshot.id); throw new BrowserErasureError("This browser conversation was erased. Start a new conversation."); }
    transaction.objectStore(CONVERSATION_STORE).put(record);
    await done;
  } catch (cause) {
    if (cause instanceof BrowserErasureError || erasedIds.has(snapshot.id)) throw cause;
    // Session memory is the only safe degradation path: private content must
    // never be persisted unencrypted merely to preserve convenience.
    memoryFallback.set(snapshot.id, snapshot);
  } });
}

interface BrowserErasureState {
  id: "state"; version: 1; revision: number;
  last?: { token: string; ids: string[]; removed: number };
}
export interface BrowserErasureReview { token: string; copies: number; memoryCopies: number }
export interface BrowserErasureResult { removed: number; reviewed_copies_cleared: true }
interface BrowserReviewSnapshot { records: string; memory: string; revision: number; ids: string[] }
const browserReviews = new Map<string, BrowserReviewSnapshot>();
function erasureState(value: BrowserErasureState | undefined): BrowserErasureState {
  if (value === undefined) return { id: "state", version: 1, revision: 0 };
  if (value.version !== 1 || !Number.isSafeInteger(value.revision) || value.revision < 0
    || (value.last && (!Array.isArray(value.last.ids) || value.last.ids.length > 10000
      || value.last.ids.some((id) => typeof id !== "string" || id.length > 512)
      || new Set(value.last.ids).size !== value.last.ids.length || value.last.removed !== value.last.ids.length
      || typeof value.last.token !== "string" || !/^[a-f0-9]{64}$/.test(value.last.token)))) {
    throw new BrowserErasureError("Browser cleanup records need a compatible version of Ryn. No cleanup was confirmed.");
  }
  return value;
}
const memorySnapshot = () => JSON.stringify([...memoryFallback.entries()].sort(([a], [b]) => a.localeCompare(b)));
const storedSnapshot = (rows: EncryptedConversationRecord[]) => JSON.stringify(rows);

/** Review encrypted records even when their old decryption key is unavailable. */
export async function reviewBrowserConversations(): Promise<BrowserErasureReview> {
  const database = await openDatabase();
  const tx = database.transaction([CONVERSATION_STORE, ERASURE_STORE], "readonly");
  const done = transactionDone(tx);
  const [rows, rawState] = await Promise.all([
    requestResult(tx.objectStore(CONVERSATION_STORE).getAll()) as Promise<EncryptedConversationRecord[]>,
    requestResult(tx.objectStore(ERASURE_STORE).get("state")) as Promise<BrowserErasureState | undefined>,
  ]);
  await done;
  const state = erasureState(rawState);
  const ids = [...new Set([...rows.map((row) => row.id), ...memoryFallback.keys()])].sort();
  if (ids.length > 10000 || ids.some((id) => typeof id !== "string" || id.length > 512)) throw new BrowserErasureError("There are too many or unsupported browser records to review. No cleanup was confirmed.");
  const snapshot = { records: storedSnapshot(rows), memory: memorySnapshot(), revision: state.revision, ids };
  const memoryCopies = memoryFallback.size;
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(snapshot)));
  const token = [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  browserReviews.set(token, snapshot);
  if (browserReviews.size > 4) browserReviews.delete(browserReviews.keys().next().value!);
  return { token, copies: ids.length, memoryCopies };
}

/** Commit reviewed deletion markers and records in one IndexedDB transaction. */
export async function eraseReviewedBrowserConversations(token: string): Promise<BrowserErasureResult> {
  return serializeWrite(async () => {
    const database = await openDatabase();
    const tx = database.transaction([CONVERSATION_STORE, ERASURE_STORE], "readwrite");
    const done = transactionDone(tx);
    try {
      const [rows, rawState, keys] = await Promise.all([
        requestResult(tx.objectStore(CONVERSATION_STORE).getAll()) as Promise<EncryptedConversationRecord[]>,
        requestResult(tx.objectStore(ERASURE_STORE).get("state")) as Promise<BrowserErasureState | undefined>,
        requestResult(tx.objectStore(ERASURE_STORE).getAllKeys()),
      ]);
      const state = erasureState(rawState);
      if (state.last?.token === token) {
        await done;
        state.last.ids.forEach((id) => { erasedIds.add(id); memoryFallback.delete(id); });
        return { removed: state.last.removed, reviewed_copies_cleared: true };
      }
      const review = browserReviews.get(token);
      if (!review || review.records !== storedSnapshot(rows) || review.memory !== memorySnapshot() || review.revision !== state.revision) {
        throw new BrowserErasureError("Browser copies changed after review. Review them again before clearing anything.");
      }
      const markers = new Set([...keys.map(String).filter((key) => key.startsWith("deleted:")), ...review.ids.map((id) => `deleted:${id}`)]);
      if (markers.size > 10000 || state.revision >= Number.MAX_SAFE_INTEGER) throw new BrowserErasureError("Browser deletion records are full. No cleanup was confirmed.");
      review.ids.forEach((id) => tx.objectStore(ERASURE_STORE).put({ id: `deleted:${id}`, version: 1 }));
      tx.objectStore(CONVERSATION_STORE).clear();
      tx.objectStore(ERASURE_STORE).put({ ...state, revision: state.revision + 1, last: { token, ids: review.ids, removed: review.ids.length } });
      await done;
      review.ids.forEach((id) => { erasedIds.add(id); memoryFallback.delete(id); });
      browserReviews.delete(token);
      return { removed: review.ids.length, reviewed_copies_cleared: true };
    } catch (cause) {
      try { tx.abort(); } catch { /* The transaction may already have aborted. */ }
      await done.catch(() => undefined);
      throw cause;
    }
  });
}

export async function listConversations(serviceKey: string) {
  try {
    const database = await openDatabase();
    const transaction = database.transaction(CONVERSATION_STORE, "readonly");
    const done = transactionDone(transaction);
    const records = await requestResult(transaction.objectStore(CONVERSATION_STORE).getAll()) as EncryptedConversationRecord[];
    await done;
    // A partial/corrupt write must not hide every other conversation. Skip only
    // the records that fail authenticated decryption and keep valid history.
    const decryptedResults = await Promise.allSettled(
      records.filter((record) => record.serviceKey === serviceKey).map(decryptConversation),
    );
    const decrypted = decryptedResults.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
    return decrypted.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  } catch {
    return [...memoryFallback.values()]
      .filter((conversation) => conversation.serviceKey === serviceKey)
      .map(cloneConversation)
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }
}

export async function deleteConversation(conversationId: string) {
  memoryFallback.delete(conversationId);
  try {
    const database = await openDatabase();
    const transaction = database.transaction(CONVERSATION_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(CONVERSATION_STORE).delete(conversationId);
    await done;
  } catch {
    // The in-memory record was already removed.
  }
}

export async function clearConversations(serviceKey: string) {
  [...memoryFallback.values()].forEach((conversation) => {
    if (conversation.serviceKey === serviceKey) memoryFallback.delete(conversation.id);
  });
  try {
    const database = await openDatabase();
    const readTransaction = database.transaction(CONVERSATION_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const records = await requestResult(readTransaction.objectStore(CONVERSATION_STORE).getAll()) as EncryptedConversationRecord[];
    await readDone;
    const writeTransaction = database.transaction(CONVERSATION_STORE, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    const store = writeTransaction.objectStore(CONVERSATION_STORE);
    records.filter((record) => record.serviceKey === serviceKey).forEach((record) => store.delete(record.id));
    await writeDone;
  } catch {
    // Nothing persisted outside the session fallback.
  }
}

export async function conversationStorageMode(): Promise<"encrypted" | "session-only"> {
  try {
    await getEncryptionKey();
    return "encrypted";
  } catch {
    return "session-only";
  }
}

/** Read old encrypted history for explicit migration. Never erase originals here. */
export async function readLegacyConversations(): Promise<{ conversations: LLMConversation[]; unreadable: number }> {
  const database = await openDatabase();
  const transaction = database.transaction(CONVERSATION_STORE, "readonly");
  const done = transactionDone(transaction);
  const records = await requestResult(transaction.objectStore(CONVERSATION_STORE).getAll()) as EncryptedConversationRecord[];
  await done;
  const decoded = await Promise.allSettled(records.map(decryptConversation));
  const conversations = new Map<string, LLMConversation>();
  for (const result of decoded) if (result.status === "fulfilled") conversations.set(result.value.id, result.value);
  for (const [id, value] of memoryFallback) if (!conversations.has(id)) conversations.set(id, cloneConversation(value));
  return { conversations: [...conversations.values()], unreadable: decoded.filter((result) => result.status === "rejected").length };
}
