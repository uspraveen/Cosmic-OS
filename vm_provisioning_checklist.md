# COSMIC User VM Provisioning Checklist

This is the final reusable production checklist for provisioning a new user VM.

Use this when you:
- create a new VM
- assign a public hostname for that VM
- register the VM in Supabase
- bootstrap the backend on the VM
- bring up the HTTPS/WSS edge with Caddy

This document assumes:
- the base domain is `thelearnchain.com`
- each user VM gets a unique subdomain such as `<user_id>.thelearnchain.com`
- shared provider API keys already exist in Supabase Vault
- the corrected Supabase functions from [supabase_config.txt](C:/Users/Praveen%20Raj%20U%20S/Cosmic-OS/supabase_config.txt) are already installed

## Token Map

Before you start, know exactly where each token/value comes from:

| Value | Source | Used For |
|---|---|---|
| `user_id` | `public.users.id` in Supabase | Used in the VM hostname and user mapping |
| Cosmic API key | `public.users.api_key` | User enters this in the desktop app to log in |
| `GATEWAY_LOCAL_API_TOKEN` | Generated/preserved by `public.provision_user_vm(...)` and stored in `public.user_vms.api_token` | Desktop -> Gateway auth |
| `COSMIC_BOOTSTRAP_TOKEN` | Returned by `app_private.issue_vm_bootstrap_token(...)` | One-time VM bootstrap auth |
| Anthropic / Perplexity / Deepgram / Groq keys | Supabase Vault | Injected into VM env files during bootstrap |
| `gateway_url` | `public.user_vms.gateway_url` | Desktop uses this as the public HTTPS/WSS endpoint |
| `vm_dns` | `public.user_vms.vm_dns` | Bootstrap maps this to `GATEWAY_PUBLIC_HOST` for Caddy/TLS |

Important:
- Never invent `GATEWAY_LOCAL_API_TOKEN` on the VM.
- Never manually consume the bootstrap token in SQL before the VM uses it.
- The bootstrap token is one-time and short-lived.

## Required Inputs Per New VM

Collect these before you begin:

- `user_email`
- `user_id`
- `vm_public_ip`
- `vm_region`
- `hostname = <user_id>.thelearnchain.com`
- SSH key path for the VM

Example hostname:

```text
c2ece0ad-4b2d-4af4-ae65-1b07660550dc.thelearnchain.com
```

## Final Production Flow

### 1. Create the VM

Create the VM normally in AWS.

Make sure you know:
- public IP
- SSH key
- region

### 2. Open AWS Inbound Rules Before Bootstrap

On the EC2 instance's attached security group, allow:

- `TCP 80` from `0.0.0.0/0`
- `TCP 443` from `0.0.0.0/0`
- if IPv6 is enabled:
  - `TCP 80` from `::/0`
  - `TCP 443` from `::/0`

Optional during rollout:
- keep `TCP 8080` open temporarily if you want a rollback/debug path

Why:
- Caddy needs public `80/443` reachability for ACME certificate issuance

### 3. Add the DNS Record in Squarespace

In Squarespace DNS for `thelearnchain.com`, add:

- `Type`: `A`
- `Host`: `<user_id>`
- `Value`: `<vm_public_ip>`

Example:

- `Host`: `c2ece0ad-4b2d-4af4-ae65-1b07660550dc`
- `Value`: `3.137.194.119`

Then verify locally:

```powershell
nslookup <user_id>.thelearnchain.com
```

Example:

```powershell
nslookup c2ece0ad-4b2d-4af4-ae65-1b07660550dc.thelearnchain.com
```

Do not continue until it resolves to the VM public IP.

### 4. Create or Update the VM Row in Supabase

Run this in Supabase SQL Editor:

```sql
select *
from public.provision_user_vm(
  '<user_email>',
  'https://<user_id>.thelearnchain.com',
  '<vm_public_ip>',
  '<user_id>.thelearnchain.com',
  '<vm_region>'
);
```

Example:

```sql
select *
from public.provision_user_vm(
  'uspraveenraj@gmail.com',
  'https://c2ece0ad-4b2d-4af4-ae65-1b07660550dc.thelearnchain.com',
  '3.137.194.119',
  'c2ece0ad-4b2d-4af4-ae65-1b07660550dc.thelearnchain.com',
  'us-east-2'
);
```

What this does:
- creates or updates `public.user_vms`
- preserves the current `api_token` if the row already exists
- returns the canonical desktop-facing Gateway token

This returned `out_gateway_api_token` is the VM's `GATEWAY_LOCAL_API_TOKEN`.

### 5. Verify the Supabase VM Row

Run this in Supabase SQL Editor:

```sql
select
  id,
  user_id,
  gateway_url,
  api_token,
  vm_ip,
  vm_dns,
  vm_region,
  status,
  updated_at
from public.user_vms
where user_id = '<user_id>';
```

Check that:
- `gateway_url = https://<user_id>.thelearnchain.com`
- `vm_dns = <user_id>.thelearnchain.com`
- `vm_ip = <vm_public_ip>`
- `api_token` is non-empty

### 6. Mint the One-Time Bootstrap Token

Run this in Supabase SQL Editor:

```sql
select *
from app_private.issue_vm_bootstrap_token('<user_email>', 20);
```

Example:

```sql
select *
from app_private.issue_vm_bootstrap_token('uspraveenraj@gmail.com', 20);
```

Copy the returned `raw_token`.

This `raw_token` is the `COSMIC_BOOTSTRAP_TOKEN`.

Important:
- do not run `consume_bootstrap_token(...)` manually
- do not let this token expire before the VM uses it

### 7. Copy `Backend/` to the VM

From your local machine:

```powershell
scp -i "<path-to-pem>" -r "C:\Users\Praveen Raj U S\Cosmic-OS\Backend" ubuntu@<vm_public_ip>:~/Cosmic-OS/
```

Example:

```powershell
scp -i "C:\Users\Praveen Raj U S\Downloads\cosmic-vpc-feb-2026.pem" -r "C:\Users\Praveen Raj U S\Cosmic-OS\Backend" ubuntu@3.137.194.119:~/Cosmic-OS/
```

After this, the VM should have:

```text
~/Cosmic-OS/Backend
```

### 8. Bootstrap the VM

SSH to the VM:

```powershell
ssh -i "<path-to-pem>" ubuntu@<vm_public_ip>
```

Then run:

```bash
cd ~/Cosmic-OS/Backend
export COSMIC_BOOTSTRAP_TOKEN='<raw_token>'
python3 bootstrap.py provision-vm
```

What this does:
- fetches env payload from Supabase using the bootstrap token
- writes repo env files and `/etc/cosmic/*.env`
- installs Python deps
- installs WhatsApp bridge deps
- installs systemd units
- starts backend services
- installs/configures Caddy
- attempts TLS certificate issuance automatically

### 9. Verify Services on the VM

Run on the VM:

```bash
systemctl is-active cosmic-gateway
systemctl is-active cosmic-model-router
systemctl is-active cosmic-orchestrator
systemctl is-active caddy
```

All should return:

```text
active
```

### 10. Verify Public HTTPS

Run locally:

```powershell
curl.exe -sS -D - https://<user_id>.thelearnchain.com/health
```

Example:

```powershell
curl.exe -sS -D - https://c2ece0ad-4b2d-4af4-ae65-1b07660550dc.thelearnchain.com/health
```

Expected:
- `HTTP/1.1 200 OK`
- JSON health payload from the Gateway via Caddy

### 11. Verify Secure WebSocket

Desktop app behavior:
- desktop login uses `gateway_url` from Supabase
- if `gateway_url` is `https://...`, Electron automatically upgrades to `wss://.../ws`

So a normal desktop login/logout test is enough.

If you want a manual check from the repo root:

```powershell
node -e "const WebSocket=require('./node_modules/ws'); const ws=new WebSocket('wss://<user_id>.thelearnchain.com/ws',{headers:{Authorization:'Bearer <GATEWAY_LOCAL_API_TOKEN>','X-Device-Id':'desktop-verifier'}}); ws.on('open',()=>ws.send(JSON.stringify({type:'ping'}))); ws.on('message',(data)=>{console.log(data.toString()); ws.close();}); ws.on('error',(err)=>{console.error(err.message); process.exit(1);});"
```

The `GATEWAY_LOCAL_API_TOKEN` for that check comes from:
- `public.user_vms.api_token`
- or the `out_gateway_api_token` returned by `public.provision_user_vm(...)`

## If Certificate Issuance Is Delayed

If DNS or security-group ingress became correct only after Caddy had already failed ACME and entered retry backoff, run on the VM:

```bash
sudo systemctl restart caddy
```

Then verify again:

```bash
sudo journalctl -u caddy -n 40 --no-pager
```

Look for:

```text
certificate obtained successfully
```

## Fallback Path If DNS or 80/443 Are Not Ready Yet

Only use this if you intentionally want a temporary non-TLS rollout first.

On the VM:

```bash
cd ~/Cosmic-OS/Backend
export COSMIC_BOOTSTRAP_TOKEN='<raw_token>'
python3 bootstrap.py provision-vm --skip-edge
```

Later, after DNS and `80/443` are ready:

```bash
python3 bootstrap.py setup-edge
```

For this fallback path, if the desktop must work before TLS is ready, you must temporarily keep Supabase `gateway_url` on raw HTTP:

```sql
update public.user_vms
set gateway_url = 'http://<vm_public_ip>:8080',
    updated_at = now()
where user_id = '<user_id>';
```

Then switch it back to HTTPS after the edge is working:

```sql
update public.user_vms
set gateway_url = 'https://<user_id>.thelearnchain.com',
    updated_at = now()
where user_id = '<user_id>';
```

## Later: Switching to an Elastic IP

When you attach an Elastic IP later:

1. update the Squarespace `A` record to the Elastic IP
2. update `public.user_vms.vm_ip`

You do not need to change:
- the hostname
- Supabase `gateway_url`
- the desktop app
- the Caddy hostname model

## Final Sanity Checklist

Before handing the VM to the user, confirm:

- DNS resolves: `<user_id>.thelearnchain.com -> <vm_public_ip>`
- `public.user_vms.gateway_url` is `https://<user_id>.thelearnchain.com`
- `public.user_vms.vm_dns` is `<user_id>.thelearnchain.com`
- `public.user_vms.api_token` is non-empty
- `cosmic-gateway` is active
- `cosmic-model-router` is active
- `cosmic-orchestrator` is active
- `caddy` is active
- `https://<user_id>.thelearnchain.com/health` returns `200`
- desktop login works with the user’s Cosmic API key
- desktop chat works over `wss://.../ws`
