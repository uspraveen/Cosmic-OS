# WhatsApp Debug Handoff

This report is for the next agent taking over COSMIC WhatsApp/Baileys debugging.

## Current Baseline

- Repo root: `C:\Users\Praveen Raj U S\Cosmic-OS`
- VM repo path: `/home/ubuntu/Cosmic-OS`
- Current synced commit baseline when this handoff was prepared: `b07beb64eabae912b158e9c40a500ed56f2bbb16`
- Local branch: `main`
- Remote: `origin/main`
- VM branch: `main`

Important:
- The only dirty VM state observed before this handoff was runtime data under `Backend/bridges/whatsapp_bridge/store/`.
- `.gitignore` was updated to ignore `Backend/bridges/*/store/` so runtime bridge state does not keep the VM repo dirty.

## Hard Git Sync Rule

This must be followed strictly.

There are always three places to keep aligned:

1. Local repo on this Windows machine
2. GitHub `origin/main`
3. VM repo at `/home/ubuntu/Cosmic-OS`

Protocol:

1. Before doing substantive work, check:
   - local: `git status --short --branch && git rev-parse HEAD`
   - VM: same commands over SSH
2. Do not continue if there is unexpected local or VM drift in tracked files.
3. Make code changes locally first unless the task is explicitly VM-only operational work.
4. After local changes:
   - commit locally
   - push to GitHub
   - pull on the VM
5. After VM operational work:
   - verify VM repo is still clean
   - if a tool modifies tracked files on the VM, stop and sync properly instead of leaving drift
6. If authenticated push/fetch is done through an explicit HTTPS URL, refresh `origin/main` tracking refs afterwards so `git status` is truthful.
7. Never leave uncommitted tracked changes on the VM while continuing local work.
8. Runtime files, auth state, `.env`, and generated bridge state must stay untracked.

## VM Connection Process

### Paramiko pattern

Use this pattern from the Windows machine:

```python
import paramiko

host = "ec2-3-137-194-119.us-east-2.compute.amazonaws.com"
user = "ubuntu"
key_path = r"C:\Users\Praveen Raj U S\Downloads\cosmic-vpc-feb-2026.pem"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    hostname=host,
    username=user,
    key_filename=key_path,
    look_for_keys=False,
    allow_agent=False,
    timeout=20,
    banner_timeout=20,
    auth_timeout=20,
)
```

Known VM identity:

- hostname: `ip-172-31-22-170`
- user: `ubuntu`
- repo path: `/home/ubuntu/Cosmic-OS`

### Important VM services

These services were provisioned through `Backend/bootstrap.py` and `systemd`:

- `cosmic-gateway.service`
- `cosmic-model-router.service`
- `cosmic-whatsapp-bridge.service`
- `cosmic-backend.target`

Useful checks:

```bash
systemctl is-active cosmic-gateway.service cosmic-model-router.service cosmic-whatsapp-bridge.service
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8742/health
journalctl -u cosmic-whatsapp-bridge.service -n 80 --no-pager
```

### Important VM env locations

Do not commit secrets. Real values live on the VM only.

- `/etc/cosmic/gateway.env`
- `/etc/cosmic/model-router.env`
- `/etc/cosmic/whatsapp-bridge.env`

Persistent WhatsApp auth state:

- `/var/lib/cosmic/whatsapp/auth`

## What Was Built

### 1. Backend architecture migration

WhatsApp/Baileys was moved out of the old root prototype and into the backend architecture:

- Bridge:
  - `Backend/bridges/whatsapp_bridge/src/index.js`
- Gateway adapter:
  - `Backend/gateway/channels/whatsapp.py`
- Gateway routes:
  - `Backend/gateway/channels/routes.py`
- Gateway runtime:
  - `Backend/gateway/runtime.py`

### 2. Gateway control plane

Implemented control/data plane routes in the Gateway:

- `GET /channels`
- `GET /channels/{platform}/status`
- `POST /channels/whatsapp/pairing/qr`
- `DELETE /channels/whatsapp/session`
- `GET /channels/whatsapp/config`
- `POST /channels/whatsapp/config`
- `POST /internal/channels/whatsapp/incoming`

### 3. Desktop WhatsApp settings UI

Implemented desktop UI and IPC path for WhatsApp:

- `src/WhatsAppIntegrationSettings.tsx`
- `src/Settings.tsx`
- `electron/main.ts`
- `electron/preload.ts`
- `src/vite-env.d.ts`

The settings screen now supports:

- Gateway URL
- Gateway local API token
- Allowed user phone number
- Optional built-in SSH tunnel for private VM access

### 4. Built-in SSH tunnel support

The direct desktop-to-VM public `:8080` path was not reliable from the laptop, so desktop-side SSH tunneling was added in Electron main.

Important files:

- `electron/main.ts`
- `electron/ssh2.d.ts`
- `vite.config.ts`
- root `package.json`

Important detail:

- `ssh2` must remain externalized from the Electron main bundle
- `vite.config.ts` now marks `ssh2` as external for the main build

### 5. Allowed-user-number restriction

The assistant is intended to only talk to the user’s own WhatsApp number.

This is now implemented end to end:

- desktop settings can save the number
- Gateway forwards config to the bridge
- bridge persists config
- bridge ignores inbound messages from other numbers
- bridge rejects outbound sends to other numbers

Bridge config currently persists in:

- `Backend/bridges/whatsapp_bridge/store/bridge-config.json`

This is runtime state and must remain untracked.

## Current User-Facing Working State

### What works

1. Desktop -> Gateway path works
2. Gateway -> bridge path works
3. WhatsApp status/config/session routes work
4. Built-in SSH tunnel support was added to avoid requiring public port `8080`
5. Gateway health and bridge health on the VM are good

### What does not work

Fresh WhatsApp QR pairing still fails on the VM.

Current user-visible error:

- `Error: Connection failure (405)`

This is surfaced by:

- desktop -> Gateway -> bridge -> Baileys pairing attempt

## Exact Current Failure

The failure is not the desktop route layer anymore.

What VM logs show repeatedly:

1. Baileys connects to WhatsApp successfully
2. Baileys logs `not logged in, attempting registration...`
3. Then it errors with `Connection Failure`
4. Gateway surfaces that as `Connection failure (405)`

That means:

- desktop transport is working
- Gateway API is working
- bridge route handling is working
- the failure is in the fresh WhatsApp registration handshake done by Baileys

## Baileys Work Already Tried

These changes were already made:

1. Surfaced bridge pairing errors clearly instead of generic 500s
2. Increased pairing request timeout handling in the Gateway adapter
3. Added explicit browser config:
   - `Browsers.macOS('Google Chrome')`
4. Disabled some extra sync behavior:
   - `markOnlineOnConnect: false`
   - `shouldSyncHistoryMessage: () => false`
5. Pinned Baileys from old `7.0.0-rc.9` to `6.7.21`
6. Retested on the VM after reinstalling bridge dependencies

The QR pairing failure still remained after those changes.

## Relevant Files To Inspect Next

Primary:

- `Backend/bridges/whatsapp_bridge/src/index.js`
- `Backend/bridges/whatsapp_bridge/package.json`
- `Backend/gateway/channels/whatsapp.py`
- `Backend/gateway/channels/routes.py`
- `Backend/gateway/runtime.py`
- `src/WhatsAppIntegrationSettings.tsx`
- `electron/main.ts`
- `vite.config.ts`

Operational:

- `/etc/cosmic/whatsapp-bridge.env`
- `/var/lib/cosmic/whatsapp/auth`
- `journalctl -u cosmic-whatsapp-bridge.service -n 100 --no-pager`

## Known External References

These were already checked and are relevant:

- Baileys docs:
  - https://baileys.wiki/docs/socket/configuration
  - https://baileys.wiki/docs/socket/connecting/
- Baileys issue patterns matching this failure:
  - https://github.com/WhiskeySockets/Baileys/issues/1939
  - https://github.com/WhiskeySockets/Baileys/issues/1947
  - https://github.com/WhiskeySockets/Baileys/issues/1914
  - https://github.com/WhiskeySockets/Baileys/issues/1427

## Strong Working Hypothesis

The remaining failure is not normal app-route breakage. It is an upstream-style Baileys fresh-registration failure during QR-based linking on the VM.

In other words:

- our layers mostly work
- fresh WhatsApp Web registration through Baileys is what still fails

## Recommended Next Debugging Path

If keeping QR is mandatory, the next agent should focus only on the registration handshake and environment comparison.

Recommended sequence:

1. Compare the exact local environment where QR reportedly worked versus the current VM:
   - Node version
   - Baileys version
   - auth store behavior
   - browser/device fingerprint
   - OpenSSL/runtime differences
2. Try reproducing the same bridge locally with the current pinned version and current code.
3. If local works and VM fails:
   - treat it as environment-specific
   - compare remote target behavior, DNS, runtime, and VM platform
4. If local also fails:
   - treat it as an upstream Baileys/WhatsApp QR regression
5. Avoid reworking the desktop/Gateway layers unless a new concrete symptom points there.

## Known Good Desktop Configuration

For the current desktop app, the intended settings are:

- Gateway Base URL: `http://127.0.0.1:8080`
- Gateway Local API Token: stored in `/etc/cosmic/gateway.env`
- Use SSH tunnel: enabled
- SSH Host: `ec2-3-137-194-119.us-east-2.compute.amazonaws.com`
- SSH Port: `22`
- SSH Username: `ubuntu`
- SSH Private Key Path: `C:\Users\Praveen Raj U S\Downloads\cosmic-vpc-feb-2026.pem`

Important:

- if the app still tries to use `127.0.0.1:18080`, that is stale old config or an old build/run still in memory
- full app restart is required after Electron main-process changes

## What Not To Do

1. Do not commit secrets, tokens, or private keys
2. Do not leave VM-only tracked file drift
3. Do not assume `405` means our FastAPI route method is wrong
4. Do not keep thrashing unrelated desktop code once status/config transport is verified
5. Do not destroy existing auth state under `/var/lib/cosmic/whatsapp/auth` without a reason

## Final Summary

The desktop transport path, Gateway control plane, bridge config persistence, and allowed-number enforcement are built.

The unresolved problem is the Baileys fresh QR registration handshake on the VM, which still fails as `Connection failure (405)` even after version pinning and socket-config hardening.
