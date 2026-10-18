# factory-dashboard

## Accounts and access

The board reads its accounts and its session secret from a configuration folder outside this
repository: `~/.config/factory-dashboard/` on the host (set `FACTORY_DASH_CONFIG_DIR` before
`docker compose up` to use another folder). Compose mounts it read-only at `/config`. The board
refuses to start, with one sentence saying why, if the secret or the accounts file is missing,
and compose refuses to start it if the folder itself is missing.

1. Create the folder and a session secret (at least 32 characters):

   ```sh
   mkdir -p ~/.config/factory-dashboard && cd ~/.config/factory-dashboard
   python3 -c 'import secrets;print(secrets.token_urlsafe(48))' > session_secret
   chmod 600 session_secret
   ```

   Changing the secret signs everyone out.

2. Add an account. From this repository, make a password hash (it asks for the password twice and
   prints one line):

   ```sh
   uv run python -m backend.accounts hash
   ```

   Copy `accounts.example.yaml` to `~/.config/factory-dashboard/accounts.yaml`, then fill in the
   username, the display name and the printed hash (`chmod 600` it too). Leave `tenant` out (it
   defaults to `operator`, which sees the factory pages). The board notices edits to the file by
   itself; removing an account signs that person out, and a file it cannot read lets nobody in.

3. Start or recreate the board: `docker compose up -d board`.

After five wrong passwords for one username within 15 minutes, that username must wait 15
minutes. A restart clears the count.

**Reaching it from another laptop or phone (tailnet only).** The board listens on
`127.0.0.1:8090` only. To offer it over HTTPS to devices on the tailnet, on the Dell:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:8090
```

It is then at `https://<this machine's tailnet name>:8443/`. To stop offering it:

```sh
tailscale serve --https=8443 off
```

Never use Tailscale Funnel for this; that would put it on the public internet.

The session cookie is marked Secure, HttpOnly and SameSite=Lax, and lasts 14 days. A browser sends
a Secure cookie only over HTTPS, except to the loopback address itself, so the existing SSH tunnel
to `http://127.0.0.1:8090` keeps working (checked in Chrome on 18 October 2026). `FACTORY_DASH_INSECURE_COOKIE=1`
drops the Secure flag; it is for the test suite only, and compose never sets it.
