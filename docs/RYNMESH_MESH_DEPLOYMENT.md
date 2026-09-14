# RynMesh 6-Node Deployment Runbook

## 1. Overview & Topology

Six nodes form the mesh, all registering to `https://registry.rynmesh.ai` under
`RYNMESH_NETWORK_ID=rynmesh-main`. Reachability strategy differs by zone:

```
                        registry.rynmesh.ai
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
  ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
  │  Office LAN   │   │   HK server   │   │   SZ node     │
  │  (NAT/DHCP)   │   │  203.0.113.10   │   │  (no public   │
  │               │   │  port 8791    │   │   IP)         │
  │  your-mac     │   │  hk-server    │   │  sz-egress    │
  │  ms-2         │   └──────┬────────┘   └──────┬────────┘
  │  m4-mini      │          │ autossh            │
  │  m2-mini      │          │ reverse tunnel     │
  │  port 8791    │          │ HK:8792 ← SZ:8791  │
  └───────────────┘          └────────────────────┘
```

- **Office Macs** — four nodes on the same LAN, each binds `0.0.0.0:8791`; their
  `RYNMESH_PEER_ENDPOINT` points to a LAN IP so they are only reachable by peers
  on the same subnet.
- **HK server** — public IP `203.0.113.10`, port 8791 open to the internet.
- **SZ egress** — no direct inbound; an autossh reverse tunnel from SZ to HK
  exposes SZ's peer as `203.0.113.10:8792`. The sz-egress `RYNMESH_PEER_ENDPOINT`
  is `http://203.0.113.10:8792`.

End-to-end verification: `scripts/verify_mesh.sh` (run from the office Mac).

---

## 2. Prerequisites

### All nodes

Install rynmesh so `rynmesh-peer` is on PATH:

```bash
# Option A — pipx (recommended for isolation)
pipx install /path/to/rynmesh/repo

# Option B — venv
python3 -m venv /opt/rynmesh/venv
/opt/rynmesh/venv/bin/pip install /path/to/rynmesh/repo
```

> **systemd path note:** `deploy/systemd/rynmesh-peer.service` has
> `ExecStart=/usr/local/bin/rynmesh-peer`. If you installed into a venv
> (e.g. `/opt/rynmesh/venv/bin/rynmesh-peer`), edit that line before
> copying the unit to `/etc/systemd/system/`.

### SZ node only

```bash
sudo apt-get install -y autossh
```

### Shared network key

Pick one strong secret and store it in your password manager:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use this value as `RYNMESH_NETWORK_KEY` on HK, SZ, and any office Mac you want
in the same authenticated mesh.

---

## 3. Office Macs (your-mac, ms-2, m4-mini, m2-mini)

Run on each Mac, substituting the correct node name:

```bash
./deploy/bin/bringup-office-mac.zsh your-mac
# or: ms-2, m4-mini, m2-mini
```

The script writes a config file to `~/.config/rynmesh/office-mac.env` (chmod
600) and loads the launchd agent.

To include a network key, copy the template, fill it in, and pass as a second
argument:

```bash
cp deploy/env/office-mac.env.example ~/.config/rynmesh/office-mac.env.filled
# edit ~/.config/rynmesh/office-mac.env.filled — uncomment RYNMESH_NETWORK_KEY
./deploy/bin/bringup-office-mac.zsh your-mac ~/.config/rynmesh/office-mac.env.filled
```

Verify:

```bash
launchctl list | grep rynmesh
tail -f ~/Library/Logs/rynmesh-peer.log
```

---

## 4. HK Server (203.0.113.10)

```bash
# Copy and fill the template — keep outside deploy/ so the path check ignores it
cp deploy/env/hk-server.env.example /etc/rynmesh/hk-server.env
# Set RYNMESH_NETWORK_KEY in /etc/rynmesh/hk-server.env

sudo ./deploy/bin/bringup-linux-node.sh hk /etc/rynmesh/hk-server.env

# Open the firewall
sudo ufw allow 8791/tcp
```

Verify:

```bash
curl -s http://127.0.0.1:8791/api/local/node/status
systemctl status rynmesh-peer
```

---

## 5. One-Time HK SSH Change for the SZ Reverse Tunnel

The autossh tunnel from SZ uses a remote-bind port on HK. OpenSSH must be told
to allow that:

```bash
# On HK — Set GatewayPorts clientspecified (append if absent, replace if present):
if grep -qE '^[#[:space:]]*GatewayPorts' /etc/ssh/sshd_config; then
  sudo sed -i 's/^[#[:space:]]*GatewayPorts.*/GatewayPorts clientspecified/' /etc/ssh/sshd_config
else
  echo 'GatewayPorts clientspecified' | sudo tee -a /etc/ssh/sshd_config
fi
sudo systemctl reload sshd

# Open the tunnel's listener port
sudo ufw allow 8792/tcp
```

Pre-seed the host key on the SZ box (as user `ops`) so autossh does not hang on
a TOFU prompt:

```bash
# On SZ, as ops:
ssh-keyscan -H 203.0.113.10 >> /home/ops/.ssh/known_hosts
# Or do a one-time interactive login and accept the fingerprint:
#   ssh rynmesh@203.0.113.10
```

---

## 6. SZ Node

```bash
# Copy templates to working files outside deploy/
cp deploy/env/sz-node.env.example   /etc/rynmesh/sz-node.env
cp deploy/env/sz-tunnel.env.example /etc/rynmesh/sz-tunnel.env
```

In `/etc/rynmesh/sz-tunnel.env` confirm:

```
SSH_KEY=/home/ops/.ssh/rynmesh_example_key
HK_SSH_TARGET=rynmesh@203.0.113.10
```

> The `deploy/systemd/rynmesh-sz-tunnel.service` unit runs as `User=ops`.
> The SSH key and `~/.ssh/known_hosts` must therefore be owned by `ops`.

Install and start both services:

```bash
sudo ./deploy/bin/bringup-linux-node.sh sz /etc/rynmesh/sz-node.env /etc/rynmesh/sz-tunnel.env
```

Verify the reverse bind on HK:

```bash
# On HK:
ss -ltnp | grep 8792   # expect a LISTEN entry

# On SZ:
systemctl status rynmesh-sz-tunnel
systemctl status rynmesh-peer
```

---

## 7. Verification

From your Mac on the office LAN (peer already running):

```bash
./scripts/verify_mesh.sh
```

For accurate reachability of the key-protected HK and SZ nodes, pass the
shared network key in the environment:

```bash
RYNMESH_NETWORK_KEY=<the-shared-secret> ./scripts/verify_mesh.sh
```

(Discovery works either way; the network key only affects the
`↳ reachable/UNREACHABLE` probe lines for HK and SZ.)

Expected output:

```
✓ discovered your-mac
✓ discovered ms-2
✓ discovered m4-mini
✓ discovered m2-mini
✓ discovered hk-server
✓ discovered sz-egress
ALL 6 NODES PRESENT
```

Also check the webapp Peers screen at `http://localhost:8080/peers` (or wherever
your local node's web UI is bound).

---

## 8. Troubleshooting

- **Node not discovered** — confirm `RYNMESH_AUTO_REGISTER=1`, the registry URL
  is `https://registry.rynmesh.ai`, and every node has `RYNMESH_NETWORK_ID=rynmesh-main`.

- **SZ unreachable** — run `systemctl status rynmesh-sz-tunnel` on SZ; confirm
  HK has `GatewayPorts clientspecified` in sshd_config and firewall port 8792
  open; `ss -ltnp | grep 8792` on HK should show a listener.

- **Public node returns 404 to probes** — `RYNMESH_NETWORK_KEY` must match
  across all nodes in the mesh; also note that the `/api/local/*` control
  surface is loopback-only by design — a remote 404 on that path is expected,
  not a fault.

- **Office node not visible off-LAN** — expected; office endpoints are LAN IPs,
  reachable only from the same subnet. The office Mac queries the registry
  (which is public), so it discovers all six nodes regardless.

- **Peer fails to start under systemd** — check that the `ExecStart` path in
  `deploy/systemd/rynmesh-peer.service` matches where `rynmesh-peer` is actually
  installed (venv vs `/usr/local/bin`). The unit sets `NoNewPrivileges=true`,
  which is fine because the peer binds port 8791 (>1024) and needs no extra
  capabilities.

---

## 9. Registry Mailbox Storage

A registry also carries store-and-forward mail for peers that cannot reach each
other directly (`docs/PEER_MAILBOX.md`). The spool lives under
`$RYNMESH_REGISTRY_DIR/mailbox`, sharded by a hash of the recipient's peer id,
one JSON file per pending message at mode 0600 in 0700 directories.

**What the operator can and cannot see.** Bodies are **ciphertext only**, sealed
to the recipient's X25519 messaging key, so the operator cannot read mail. The
envelope around them is *not* encrypted, and that is what routing needs: the
operator sees the sender and recipient peer ids, the message `kind` (a plaintext
routing label such as `peer.message.v1`), the message id, the created/expiry
timestamps, and the size of each message. That is enough to build a traffic
graph — who talks to whom, how often, and in which kind of exchange — so the
registry is a metadata observer even though it is never a party to the content.

**What the operator can do.** A hostile registry cannot forge, alter, or read
mail (every envelope is Ed25519-signed by its sender and sealed to its
recipient), but it *can* withhold, delay, or reorder messages, and nothing in
the protocol lets a client detect that. Clients must therefore treat mailbox
delivery as best-effort with an expiry, never as a guarantee, and must not
depend on the order two messages arrive in.

**Admission control is the network key.** Everything below is a per-box quota,
not an identity check: any peer holding `RYNMESH_NETWORK_KEY` may deposit. The
key is what decides who can use the mailbox at all; the quotas only stop one
such peer from crowding out the others.

Sizing is bounded by construction: 64 KiB per envelope, 256 pending messages per
recipient, **16 pending per (sender, recipient) pair** so one peer cannot fill
someone else's box, 120 deposits per minute per sender, and a maximum 24-hour
TTL (one hour by default). The worst case is therefore about 16 MiB of pending
mail per recipient, plus up to 2048 ack tombstones per box (a few hundred bytes
each, so well under 1 MiB) — call it ~17 MiB per active recipient. Expired
messages and tombstones are swept lazily — on poll, and every 50th deposit — and
the tombstone count is additionally capped, oldest evicted first. A registry that
goes completely idle reclaims disk late but never serves expired mail. No extra
configuration or open port is needed: the routes are
`POST /api/v1/mailbox/{deposit,poll}` on the registry's existing listener,
hidden as 404 to callers without `RYNMESH_NETWORK_KEY`.
