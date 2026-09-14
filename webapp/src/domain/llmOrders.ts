import type { LLMServiceRecord } from "./nodeClient";

// Terminal task states, mirroring rynmesh/llm_package/task_protocol.py
// TERMINAL_STATES. One definition — the screens used to each carry their own.
export const LLM_TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
  "rejected",
]);

// Canonical identity for a discovered LLM service. Model aliases are not
// unique, so identity is (provider peer, package). This format is also the
// Private AI conversation-store key, so it must stay stable.
export function llmServiceKey(peerId: string, packageId: string): string {
  return `${peerId}::${packageId}`;
}

export function llmServiceRecordKey(service: LLMServiceRecord): string {
  return llmServiceKey(service.peer_id, service.service.package_id);
}

export function llmServiceAvailability(service: LLMServiceRecord): string {
  if (service.access === "self" && (!service.online || service.ready === false)) {
    return "Local model not ready — start it or check model settings";
  }
  if (!service.online) return "Availability unknown — refresh services";
  if (service.ready === false) return "Model not ready — check provider settings";
  if (service.capacity?.available === 0) return "Busy — wait or choose another service";
  return service.access === "self" ? "Ready on this device" : "Available in discovery";
}
