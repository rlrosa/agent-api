# Networking & IP Whitelisting Guide

## Access Paths

`agent-api` handles requests originating across three distinct network topologies:

1. **Trusted Local Area Network (LAN)**: Subnet `192.168.87.0/24` (direct access without API key required).
2. **Tailscale VPN Overlay**: Subnet `100.64.0.0/10` (direct encrypted access without API key required).
3. **Cloudflare Tunnel (Public Ingress)**: Arrives at `127.0.0.1:8090` from `cloudflared` via `https://agent-api.rela.uy`.

## Loopback & Cloudflare Tunnel Protection

Local loopback subnets (`127.0.0.1/32`, `::1/128`, `127.0.0.0/8`) are included in `TRUSTED_NETWORKS` by default to allow local host applications to access `agent-api` without an API key.

> [!NOTE]
> **Cloudflare Tunnel Protection:**
> When public HTTP traffic arrives via Cloudflare Tunnel (`cloudflared`) to `127.0.0.1:8090`, requests contain Cloudflare headers (`CF-Connecting-IP`, `CF-Ray`, `CF-Visitor`). `agent-api` detects these headers (`has_cf_header`) and forces mandatory `X-API-Key` authentication even though the peer IP is `127.0.0.1`. Direct local requests without Cloudflare headers safely bypass authentication.

## How to Whitelist an IP Range

To tighten or modify the trusted network ranges (e.g., restricting access to a specific office subnet or VPN pool):

### Step 1: Update Environment Configuration
Edit `~/.config/agent-api/env`:

```bash
# Example: Adding specific CIDR subnets
TRUSTED_NETWORKS="192.168.87.0/24,100.64.0.0/10,172.16.50.0/24"
```

### Step 2: Restart the Service
```bash
systemctl --user restart agent-api.service
```

### Step 3: Verify Configuration
Test request from an IP within the whitelisted range without sending `X-API-Key`:

```bash
curl -s http://192.168.87.132:8090/v1/stats/summary | jq .
```

Verify that requests from non-whitelisted IPs or Cloudflare tunnel header requests return `HTTP 401 Unauthorized`.
