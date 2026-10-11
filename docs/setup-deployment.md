# Deploying to a VPS

## Generate the initial VPS configuration

This workflow targets a Linux VPS with systemd and an existing, running Caddy installation using `/etc/caddy/Caddyfile`. Install Caddy through its official distribution instructions first. The VPS also needs `curl`, `tar`, `useradd`, `flock`, `awk`, and `ss` (iproute2). Ports 80 and 443 must reach Caddy, and the domain must resolve to the VPS for HTTPS certificates. PocketBase listens on localhost only. Use a different localhost port for each app on the same VPS.

On a VPS with several Caddy sites and services already running, choose a new domain and app slug. The default `auto` port lookup reads active TCP listeners, Caddy's adapted routes, and systemd unit files over SSH, then chooses the first unreserved port from 8090–8999. It saves the concrete port in `deploy/config.json` and the reviewable generated service and site. It does not reserve the port: `make deploy-check` and provisioning check it again. An existing managed app keeps its saved port by default; changing that port requires a coordinated service and proxy migration. The Caddy site is added through one import at the end of the existing Caddyfile; the other site blocks are preserved.

An existing service such as `jnestr.service` under `/lib/systemd/system` is **not** adopted automatically. This workflow would otherwise shadow its unit with `/etc/systemd/system/jnestr.service` while leaving its live `/root/jnestr/pb_data` behind. The check refuses that name, even if the existing service owns the chosen port. To migrate an existing app, first inventory its unit, environment, database, uploaded files, and Caddy domain; plan a stopped, verified backup and data transfer to the dedicated `/opt/<app>/pb_data` layout, then retire the old unit and Caddy block deliberately. Use a new slug/domain/port for a separate app. No `--dev` flag or root service account is used in the generated production unit.

Connect once with your normal `ssh user@vps` command, verify the server's host key, and set up an SSH key. The deployment commands require an existing trusted host key, batch authentication, and either root or passwordless sudo. SSH passwords are never saved or requested. Your SSH config can supply keys and host aliases; custom SSH ports are supported.

```sh
make setup-vps
# Or:
make setup-vps ARGS='--non-interactive --app acme --host deploy@vps.example.com --domain app.example.com --port auto --arch amd64'
make deploy-check
```

`deploy/config.json` stores non-secret settings. `deploy/generated/` contains the systemd service, Caddy site, environment example, and complete provisioning script for review. These local files are ignored by Git. `make provision` previews the plan and makes no remote connection. To apply the reviewed configuration:

```sh
make provision ARGS='--apply'
# To install a custom backend environment on a new VPS:
make provision ARGS='--apply --env-file /path/to/production.env'
# To update supplied keys while retaining other existing server settings:
make provision ARGS='--apply --env-file /path/to/production.env --merge-env'
```

If you create `deploy/production.env`, `make provision` selects it automatically and merges its supplied keys into the server environment. It preserves server keys absent from that file. Review the private file and preview before applying. Otherwise, provisioning uses the generated environment example and preserves an existing server environment.

The command creates a dedicated service user, `/opt/acme/pb_data`, `/etc/acme/acme.env`, and `acme.service`. The service reads its environment from a root-owned file with mode 0600; do not use frontend `VITE_` variables for secrets. Custom environment files use one `KEY=value` assignment per line, without `export` or multiline values.

Provisioning backs up the existing Caddyfile, adds `import /etc/caddy/template-sites/*.caddy` once, writes an app-specific site, validates the complete configuration, and reloads Caddy. Existing sites remain in place. If validation or reload fails, the previous Caddy, service, and environment configuration is restored. It refuses to overwrite a service or site without its managed marker, to shadow an existing systemd unit, or to claim a domain or port already in use. Environment merges and replacements create a backup. Replacing the entire environment requires both `--env-file` and `--replace-env`; it removes remote keys absent from the supplied file. Provisioning does not restart a running app; deploy or explicitly restart it to load updated environment settings. The service starts after the first release. Caddy may return 502 for the new domain until then.

The generated Caddy site uses the example VPS sites' 360-second upstream read timeout and basic response headers. Its HSTS policy is scoped to the app hostname; it does not request preload or include subdomains. It sets no generic upload size limit, since applications have different file rules. If this app needs a `request_body` rule, change `caddy_file()` in `scripts/deploy.py` so the rule survives automatic regeneration, then review the generated site before provisioning.

## Release an app

```sh
make deploy                       # Preview the plan
make deploy ARGS='--apply'        # Build, upload, start, and check health
make build-deploy                # Shortcut for the same build and deploy operation
```

Each release builds the frontend from source and compiles the backend for Linux/amd64 or Linux/arm64 with CGO disabled. The upload contains only the binary and fresh `dist` assets. It never uploads a local `.env`, `pb_data`, SQLite database, or uploaded files.

On the VPS, the service stops briefly while a consistent `pb_data` backup is created under `/opt/acme/backups`. The backup directory is root-only and archives are created with private permissions. The command installs a new directory under `/opt/acme/releases`, atomically updates the `current` symlink, starts the service, and checks PocketBase's built-in `/api/health` endpoint on localhost. Previous releases and backups are retained. Set up disk monitoring and an off-server backup/retention policy for your application; this command intentionally does not delete old backups.

Startup migrations can change the production database. Review migrations before releasing. A failed startup or health check stops the new service and does not automatically restore an earlier binary or database because the previous code may be incompatible with a migrated schema. Inspect `journalctl -u acme.service`, then choose a compatible release or restore a backup while the service is stopped. The local check confirms that PocketBase serves its API; independently check the frontend, public HTTPS, and any provider integrations after deployment.

Run `make check` for local lint, syntax, build, and generated-type checks. Verify SSH, Caddy reload, systemd startup, and public certificate issuance on your VPS after deployment.

References: [Caddy file imports](https://caddyserver.com/docs/caddyfile/directives/import), [Caddy configuration validation](https://caddyserver.com/docs/command-line#caddy-validate).
