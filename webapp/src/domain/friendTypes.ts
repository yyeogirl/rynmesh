export interface FriendRecord {
  relationship_id: string;
  peer_id: string;
  node_name: string;
  endpoint: string;
  permissions: string[];
  status: "active" | "revoked";
  created_at: string;
  revoked_at?: string;
  revocation_delivery?: "pending" | "delivered";
}

export interface FriendInvitePreview {
  invite_id: string;
  peer_id: string;
  node_name: string;
  endpoint: string;
  permissions: string[];
  created_at: string;
  expires_at: string;
  endpoints?: string[];
  network_id?: string;
  address_category?: string;
  fingerprint?: string;
}

export interface FriendInviteResult {
  invite_uri: string;
  invite: FriendInvitePreview;
}

export interface FriendMessage {
  msg_id: string;
  dir: "in" | "out";
  from: string;
  to: string;
  text?: string;
  ts?: string;
  kind?: string;
  delivered?: boolean;
  error?: string;
  expires_at?: string;
  delivery_state?: "queued" | "sending" | "mailbox" | "delivered" | "failed" | "expired";
  attachment?: { filename: string; mime: string; size?: number };
}

export interface FriendContentCard {
  card_id: string;
  from: string;
  to?: string;
  dir?: "in" | "out";
  created_at: string;
  fetch_state?: "available" | "metadata_only" | "fetched" | "unavailable";
  fetched_library_id?: string;
  sha256_verified?: boolean;
  delivered?: boolean;
  error?: string;
  expires_at?: string;
  delivery_state?: FriendMessage["delivery_state"];
  card: {
    version?: "ryn.shared-content-card.v1";
    library_id: string;
    content_id?: string;
    title: string;
    summary: string;
    kind: string;
    source: string;
    source_url?: string;
    publisher_peer_id?: string;
    manifest_ref?: string;
    filename?: string;
    mime?: string;
    size_bytes?: number;
    sha256?: string;
    fetch_available?: boolean;
    content_truncated?: boolean;
  };
}
