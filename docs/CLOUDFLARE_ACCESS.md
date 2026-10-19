# Shared camera access with Cloudflare Access policies

Cas Cams keeps a **single Blink login** (yours). Friends never sign into Blink.

**Cloudflare Access policies** decide who may open the site (email OTP, Google,
IdP, Access Groups, etc.). After they pass the policy, Cas Cams reads their
email from the Access JWT and shows only the cameras you assigned.

| Layer | Who controls it | What it does |
|-------|-----------------|--------------|
| Cloudflare Access **policy** | Zero Trust dashboard | Login / who can reach the hostname |
| Cas Cams camera allowlist | Settings → Shared camera access | Which cameras each email may view |

## Local mode vs Cloudflare mode

| Cloudflare team domain + AUD | Behavior |
|------------------------------|----------|
| Both empty (default) | Local single-user: anyone with the page is **admin** (same as before). |
| Both set | Every API/media request needs a **verified Access JWT**. Guests are view-only on allowlisted cameras. |

**Warning:** do not save a team domain and AUD until you reach Cas Cams
**through** the Access application hostname. Enabling it on raw
`http://127.0.0.1:8765` will return “Cloudflare Access login required.”

## Set up an Access policy (friends log in here)

1. Open [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → **Access** →
   **Applications** → **Add an application** → **Self-hosted**.
2. **Application domain**: the public hostname that proxies to Cas Cams
   (Tunnel or reverse proxy), e.g. `cams.example.com`. Friends must use this
   hostname so the Access policy runs.
3. **Add a policy** (this is the Access policy friends use):
   - Action: **Allow**
   - Include: emails (`you@example.com`, `friend@example.com`, …) and/or an
     **Access Group**, emails ending in `@yourdomain.com`, IdP groups, etc.
   - Identity providers: One-time PIN (email), Google, Microsoft, etc.
4. After creating the app, copy from the application settings:
   - **Team domain** (e.g. `myteam.cloudflareaccess.com`)
   - **Application Audience (AUD)** tag
5. In Cas Cams → **Settings → Access & automation → Shared camera access**:
   - Paste **team domain** and **AUD**
   - Confirm admin emails include `you@example.com`
   - Add each friend email and check the cameras they may view
   - **Save access** (no full server restart required)
6. Put your public hostname in **Trusted host** (same Settings tab) so
   `Host` / `Origin` checks accept Cloudflare’s hostname, and expose the
   origin with Tunnel/`BLINK_BIND` as needed.

### Flow for a friend

1. They open `https://cams.example.com`.
2. Cloudflare shows the **Access login** (policy).
3. After success, Access injects `Cf-Access-Jwt-Assertion` (and the
   `CF_Authorization` cookie). Cas Cams verifies the JWT against your team’s
   JWKS and AUD.
4. If you already assigned cameras → they see those live views/clips only.
5. If not yet assigned → they are signed in but see an empty list until you
   grant cameras (Access policy still controls login).

## What guests can and cannot do

**Allowed (view-only, allowlisted cameras only):**

- Live Views / All Live Views / Live View Rotation
- Detection clips, live recordings, daily/weekly/monthly, pictures
- Thumbnails and media playback for those cameras

**Denied:**

- Settings, Blink login/2FA, arm/disarm, wake, snapshot command, record-to-disk
- Clip exclude/delete, mute alerts, auto-delete, update/restart/stop
- Any camera not on their allowlist (API returns 403 even if the URL is guessed)

## Network requirements

- Cas Cams must sit behind Cloudflare (Tunnel recommended) so Access can
  enforce the policy and forward the JWT.
- `trusted_host` / `BLINK_BIND` alone do **not** authenticate users.
- Direct LAN IP that bypasses Cloudflare also bypasses the Access policy —
  close that path when Shared access is enabled.

## Config file

Server file: `blink_access.json` under the app data directory (`runtime.app_dir()`):

```json
{
  "admin_emails": ["you@example.com"],
  "cloudflare": {
    "team_domain": "myteam.cloudflareaccess.com",
    "aud": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  },
  "users": [
    {
      "email": "friend@example.com",
      "cameras": ["Bedroom", "camera-key-…"],
      "permissions": ["view_live", "view_clips", "view_pictures"]
    }
  ]
}
```

## APIs

- `GET /api/me` — `{ email, role, cameras, pending_allowlist, … }`
- `GET|POST /api/access` — admin only; read/write allowlists + Cloudflare fields
