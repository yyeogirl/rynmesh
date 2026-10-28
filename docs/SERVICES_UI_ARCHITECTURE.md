# Services UI architecture

Status: implemented on `feature/local-llm-dual-node`.

This document describes the user-facing Services catalog and its typed service
experiences. It records current behavior and the boundaries contributors must
preserve when extending the UI.

## Product contract

`/services` is a task-first catalog. Users choose what they want to do; provider
nodes, package IDs, networks, and transport details remain behind optional
details or the legacy management screen.

Each service type opens the interaction suited to its lifecycle:

| Route | Experience | Node-client boundary |
|---|---|---|
| `/services` | Searchable and filterable service catalog | `listLLMServices`, `listJobCapacities` |
| `/services/private-ai/chat` | Multi-conversation language-model chat | `submitLLMOrder`, `getLLMOrder`, `cancelLLMOrder` |
| `/services/video-rendering` | Bounded render workflow | `submitWorkOrder`, `listWorkResults` |
| `/services/secure-web-access` | Connect, launch, and disconnect lifecycle | `egressStatus`, `egressConnect`, `egressLaunch`, `egressDisconnect` |
| `/services/manage` | Advanced provider/package administration | Existing Services APIs |
| `/chat` | Direct peer-to-peer messaging | Messaging APIs; intentionally unchanged |

The catalog currently maps discovered language-model and video capabilities to
three curated product experiences. Adding generic service-manifest rendering is
separate protocol work; contributors should not expose raw manifest fields as a
user workflow without an accepted design.

## Data flow

```text
Services experience
        |
        v
typed NodeClient method
        |
        v
local Ryn node control API
        |
        v
provider discovery / transport / settlement
```

The webapp does not contact providers or the registry directly. The local node
remains the enforcement point for discovery, transport, cancellation, balance,
and result retention.

## Local state and privacy

The catalog stores up to three recently opened service IDs and timestamps in
`localStorage` under `ryn.services.recent.v1`. It does not store prompts,
results, provider IDs, or routes there.

Private AI conversations use the `ryn-private-ai-chat` IndexedDB database:

- one non-extractable AES-GCM 256 `CryptoKey` is stored through IndexedDB
  structured cloning;
- each conversation receives a fresh 96-bit IV and is written as authenticated
  ciphertext;
- the storage key is the compound provider peer ID plus package ID, because
  display aliases are not unique;
- if IndexedDB, Web Crypto, key storage, or authenticated decryption is
  unavailable, new content falls back to session memory rather than plaintext
  persistence;
- a corrupt record is skipped without hiding other valid conversations.

This is encrypted local persistence, not confidential computing or an OS-bound
secure enclave. JavaScript running under the same origin and a compromised
browser profile can use the stored key. The selected inference provider also
necessarily sees plaintext while generating a response. The UI states this in
its Details panel.

Node-side LLM order history remains governed by the result-retention setting
and does not store prompt bodies. Clearing Private AI history removes the local
encrypted conversations and requests deletion of retained terminal order
results from the local node.

## Compatibility rules

- Keep `/chat` reserved for direct node messaging.
- Keep `/services/manage` available until its advanced provider and package
  controls have dedicated replacements.
- Route all service actions through `NodeClient`; do not call peers directly.
- Preserve the `client=fixture` query parameter in fixture navigation so UI
  tests do not fall through to a live node.
- Do not put conversation or result bodies in URLs, logs, `localStorage`, or
  catalog analytics.
- Infrastructure identifiers may appear in explicit Details sections, but not
  as required inputs for ordinary users.

## Contributor workflow

When adding or changing a service experience:

1. Add or reuse a typed `NodeClient` method and implement both live and fixture
   clients.
2. Keep the catalog card action-oriented and provide unavailable/error states.
3. Add component tests for the full action lifecycle, including failure or
   cancellation where applicable.
4. Run `npm test`, `npm run lint`, and `npm run build` from `webapp/`.
5. Browser-test the affected route and attach screenshots to the pull request.
6. Update this document when routes, persistence, privacy, or compatibility
   boundaries change.

Browser acceptance evidence for the current implementation is stored in
[`acceptance/services-ui-browser/`](acceptance/services-ui-browser/README.md).
