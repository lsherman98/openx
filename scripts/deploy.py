#!/usr/bin/env python3
"""Generate and explicitly apply a Caddy/systemd deployment over key-based SSH."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

from common import ROOT, pnpm_command, prompt, validate_slug, write_atomic


CONFIG_PATH = ROOT / "deploy/config.json"
GENERATED = ROOT / "deploy/generated"
RESERVED_USERS = {"root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "nobody", "caddy", "sshd", "systemd-network", "systemd-resolve"}
IMPORT_LINE = "import /etc/caddy/template-sites/*.caddy"


def validate_config(config: dict) -> dict:
    app = validate_slug(str(config.get("app", "")))
    if app in RESERVED_USERS:
        raise ValueError("Choose an app slug that is not an existing system account name.")
    host = str(config.get("host", ""))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}@[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("SSH host must be user@hostname or user@IPv4-address (IPv6 is not supported).")
    domain = str(config.get("domain", "")).lower().rstrip(".")
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("Domain must be a hostname such as app.example.com, without a scheme, port or path.")
    try:
        port, ssh_port = int(config.get("port", 8090)), int(config.get("ssh_port", 22))
    except (ValueError, TypeError) as error:
        raise ValueError("Ports must be integers.") from error
    if not 1024 <= port <= 65535 or not 1 <= ssh_port <= 65535:
        raise ValueError("App port must be 1024–65535; SSH port must be 1–65535.")
    arch = str(config.get("arch", "amd64"))
    if arch not in {"amd64", "arm64"}:
        raise ValueError("Linux architecture must be amd64 or arm64.")
    return {"version": 1, "app": app, "host": host, "domain": domain, "port": port, "ssh_port": ssh_port, "arch": arch}


def service_file(config: dict) -> str:
    app, port = config["app"], config["port"]
    return f"""# Managed by pb-template for app: {app}
[Unit]
Description={app} PocketBase
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User={app}
Group={app}
LimitNOFILE=4096
WorkingDirectory=/opt/{app}/current
EnvironmentFile=/etc/{app}/{app}.env
ExecStart=/opt/{app}/current/server serve --http=127.0.0.1:{port} --dir=/opt/{app}/pb_data --publicDir=/opt/{app}/current/pb_public
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/{app}/pb_data
StandardOutput=journal
StandardError=journal
SyslogIdentifier={app}

[Install]
WantedBy=multi-user.target
"""


def caddy_file(config: dict) -> str:
    return f"""# Managed by pb-template for app: {config['app']}
{config['domain']} {{
    encode zstd gzip
    reverse_proxy 127.0.0.1:{config['port']} {{
        transport http {{
            read_timeout 360s
        }}
    }}
    header {{
        Strict-Transport-Security "max-age=31536000"
        X-Frame-Options "DENY"
        X-Content-Type-Options "nosniff"
        Content-Security-Policy "upgrade-insecure-requests"
    }}
}}
"""


def environment_file(config: dict) -> str:
    return f"""# Installed only if /etc/{config['app']}/{config['app']}.env does not exist.
# Add backend secrets here or supply --env-file; never use a frontend VITE_ variable for a secret.
APP_ENV=production
LOG_SERVICE={config['app']}
LOG_LEVEL=info
LOG_JSON=false
PUBLIC_URL=https://{config['domain']}
"""


def remote_prefix(config: dict) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-p", str(config["ssh_port"]), config["host"]]


def root_command(config: dict, command: str) -> str:
    return command if config["host"].split("@", 1)[0] == "root" else "sudo -n " + command


def target_preflight_script(config: dict) -> str:
    app = config["app"]
    return f"""unit=/etc/systemd/system/{app}.service
site=/etc/caddy/template-sites/{app}.caddy
marker='# Managed by pb-template for app: {app}'
# systemd also loads units from /lib and /usr/lib. An /etc unit would shadow them.
fragment=$(systemctl show --property=FragmentPath --value '{app}.service' 2>/dev/null || true)
if [ -n "$fragment" ] && [ "$fragment" != "$unit" ]; then
    echo "Refusing to replace existing service at $fragment. Migrate its data and unit manually first." >&2
    exit 1
fi
for managed in "$unit" "$site"; do
    if [ -e "$managed" ]; then
        grep -Fqx "$marker" "$managed" || {{ echo "Refusing to replace unmanaged file: $managed" >&2; exit 1; }}
    fi
done
if [ -f "$unit" ] && ! grep -Fq 'serve --http=127.0.0.1:{config['port']}' "$unit"; then
    echo 'Changing the port of a provisioned service requires a coordinated migration; refusing to reload Caddy first.' >&2
    exit 1
fi
# Check the existing Caddyfile and sibling managed sites before adding this host.
for candidate in /etc/caddy/Caddyfile /etc/caddy/template-sites/*.caddy; do
    [ -f "$candidate" ] || continue
    [ "$candidate" = "$site" ] && continue
    if awk -v domain='{config['domain']}' '
        /^[[:space:]]*#/ {{ next }}
        index($0, "{{") {{
            line=$0
            sub(/#.*/, "", line)
            sub(/[{{].*/, "", line)
            gsub(/,/, " ", line)
            count=split(line, names, /[[:space:]]+/)
            for (i=1; i<=count; i++) if (names[i] == domain) found=1
        }}
        END {{ exit !found }}
    ' "$candidate"; then
        echo "Refusing to add {config['domain']}; it is already declared in $candidate." >&2
        exit 1
    fi
done
port_listeners=$(ss -H -ltnp 'sport = :{config['port']}')
if [ -n "$port_listeners" ]; then
    own_pid=$(systemctl show --property=MainPID --value '{app}.service' 2>/dev/null || true)
    test -n "$own_pid" && test "$own_pid" != 0 || {{ echo 'The chosen app port is already in use; configure a different port.' >&2; exit 1; }}
    foreign_listeners=$(printf '%s\\n' "$port_listeners" | grep -Fv "pid=$own_pid," || true)
    test -z "$foreign_listeners" || {{ echo 'The chosen app port belongs to another service; configure a different port.' >&2; exit 1; }}
fi
"""


def preflight_script(config: dict, *, release: bool = False, provision: bool = False) -> str:
    app, arch = config["app"], "x86_64" if config["arch"] == "amd64" else "aarch64"
    script = f"""set -eu
test "$(uname -s)" = Linux || {{ echo 'This deployment requires Linux.' >&2; exit 1; }}
test "$(uname -m)" = {arch} || {{ echo 'Remote architecture differs from deploy/config.json.' >&2; exit 1; }}
test "$(id -u)" -eq 0 || {{ echo 'Root or passwordless sudo is required.' >&2; exit 1; }}
for command in systemctl caddy curl tar install getent useradd flock ss awk; do
    command -v "$command" >/dev/null || {{ echo "Missing remote prerequisite: $command" >&2; exit 1; }}
done
test -f /etc/caddy/Caddyfile || {{ echo 'Install and configure Caddy first.' >&2; exit 1; }}
systemctl is-active --quiet caddy || {{ echo 'Caddy must already be running.' >&2; exit 1; }}
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
"""
    if provision:
        script += target_preflight_script(config)
    if release:
        script += f"""test -f /etc/systemd/system/{app}.service || {{ echo 'Run provision --apply before releasing.' >&2; exit 1; }}
test -f /etc/{app}/{app}.env || {{ echo 'Missing application environment file.' >&2; exit 1; }}
grep -Fqx '# Managed by pb-template for app: {app}' /etc/systemd/system/{app}.service || {{ echo 'Refusing to deploy through an unmanaged service.' >&2; exit 1; }}
"""
    return script


def provision_script(config: dict) -> str:
    app = config["app"]
    return f"""#!/bin/sh
# Generated for {app}. Run through scripts/deploy.py provision --apply.
# Arguments: staging directory; replace environment (yes/no); merge supplied keys (yes/no).
{preflight_script(config)}
umask 077
stage=$1
replace_env=${{2:-no}}
merge_env=${{3:-no}}
app={app}
test -d "$stage" && printf '%s' "$stage" | grep -Eq '^/tmp/pb-template\\.[A-Za-z0-9]{{10}}$' || {{ echo 'Invalid staging directory.' >&2; exit 1; }}
exec 9>/run/lock/pb-template-$app.lock
flock -n 9 || {{ echo 'Another provisioning/release command is already running.' >&2; exit 1; }}
exec 8>/run/lock/pb-template-caddy.lock
flock -n 8 || {{ echo 'Another Caddy provisioning command is already running.' >&2; exit 1; }}
{target_preflight_script(config)}
base=/opt/$app
site=/etc/caddy/template-sites/$app.caddy
unit=/etc/systemd/system/$app.service
env_file=/etc/$app/$app.env
marker='# Managed by pb-template for app: {app}'
for managed in "$site" "$unit"; do
    if [ -e "$managed" ]; then
        grep -Fqx "$marker" "$managed" || {{ echo "Refusing to replace unmanaged file: $managed" >&2; exit 1; }}
    fi
done
if getent passwd "$app" >/dev/null; then
    test "$(id -u "$app")" -ne 0 || {{ echo 'The app user cannot be root.' >&2; exit 1; }}
    test "$(getent passwd "$app" | cut -d: -f6)" = "$base" || {{ echo 'Existing user belongs to a different application.' >&2; exit 1; }}
else
    useradd --system --user-group --home-dir "$base" --no-create-home --shell /usr/sbin/nologin "$app"
fi
install -d -m 0755 -o root -g root "$base" "$base/releases" /etc/$app /etc/caddy/template-sites
install -d -m 0700 -o root -g root "$base/backups"
install -d -m 0700 -o "$app" -g "$app" "$base/pb_data"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/etc/caddy/Caddyfile.$app.$stamp.bak
test ! -e "$backup" || {{ echo 'A backup with this timestamp already exists; retry in a second.' >&2; exit 1; }}
cp -p /etc/caddy/Caddyfile "$backup"
had_site=no
if [ -e "$site" ]; then cp -p "$site" "$stage/previous.caddy"; had_site=yes; fi
had_unit=no
if [ -e "$unit" ]; then cp -p "$unit" "$stage/previous.service"; had_unit=yes; fi
had_env=no
if [ -e "$env_file" ]; then cp -p "$env_file" "$stage/previous.env"; had_env=yes; fi
was_enabled=no
if systemctl is-enabled --quiet "$app.service"; then was_enabled=yes; fi
committed=no
cleanup() {{
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$committed" != yes ]; then
        cp -p "$backup" /etc/caddy/Caddyfile
        if [ "$had_site" = yes ]; then cp -p "$stage/previous.caddy" "$site"; else rm -f "$site"; fi
        if [ "$was_enabled" != yes ]; then systemctl disable "$app.service" >/dev/null 2>&1 || true; fi
        if [ "$had_unit" = yes ]; then cp -p "$stage/previous.service" "$unit"; else rm -f "$unit"; fi
        if [ "$had_env" = yes ]; then cp -p "$stage/previous.env" "$env_file"; else rm -f "$env_file"; fi
        systemctl daemon-reload || true
        caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && systemctl reload caddy || true
        echo "Provisioning failed. Restored Caddy, service, and environment configuration; backup: $backup" >&2
    fi
    rm -rf "$stage"
    exit "$status"
}}
trap cleanup EXIT HUP INT TERM
install -m 0644 "$stage/$app.caddy" "$site"
if ! grep -Fqx '{IMPORT_LINE}' /etc/caddy/Caddyfile; then
    printf '\\n# Application sites managed by the project deploy command\\n{IMPORT_LINE}\\n' >>/etc/caddy/Caddyfile
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
install -m 0644 "$stage/$app.service" "$unit"
if [ ! -e "$env_file" ] || [ "$replace_env" = yes ]; then
    if [ -e "$env_file" ]; then cp -p "$env_file" "$env_file.$stamp.bak"; fi
    install -m 0600 -o root -g root "$stage/$app.env" "$env_file"
elif [ "$merge_env" = yes ]; then
    cp -p "$env_file" "$env_file.$stamp.bak"
    # Treat both files as data: preserve unrelated settings and collapse updated duplicates.
    awk '
        FILENAME == ARGV[1] {{
            if ($0 ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/) {{
                key=$0; sub(/=.*/, "", key); sub(/^[[:space:]]+/, "", key); sub(/[[:space:]]+$/, "", key)
                if (!(key in replacement)) order[++count]=key
                replacement[key]=$0
            }}
            next
        }}
        {{
            key=$0; sub(/=.*/, "", key); sub(/^[[:space:]]+/, "", key); sub(/[[:space:]]+$/, "", key)
            if ($0 ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ && key in replacement) {{
                if (!(key in written)) print replacement[key]
                written[key]=1
            }} else print
        }}
        END {{ for (i=1; i<=count; i++) if (!(order[i] in written)) print replacement[order[i]] }}
    ' "$stage/$app.env" "$env_file" >"$stage/merged.env"
    install -m 0600 -o root -g root "$stage/merged.env" "$env_file"
fi
systemctl daemon-reload
systemctl enable "$app.service"
systemctl reload caddy
committed=yes
echo "Provisioned $app. Caddy backup: $backup"
echo "Environment: $env_file (supplied keys merged only when selected; unrelated existing values preserved)."
echo 'Provisioning does not restart a running app. Release or restart the service to load updated environment settings.'
echo 'Run release --apply to upload a build and start the application.'
"""


def release_script(config: dict, release_id: str) -> str:
    app, port = config["app"], config["port"]
    return f"""#!/bin/sh
# Generated for {app}. No database or environment files are uploaded.
{preflight_script(config, release=True)}
umask 077
stage=$1
app={app}
test -d "$stage" && printf '%s' "$stage" | grep -Eq '^/tmp/pb-template\\.[A-Za-z0-9]{{10}}$' || {{ echo 'Invalid staging directory.' >&2; exit 1; }}
exec 9>/run/lock/pb-template-$app.lock
flock -n 9 || {{ echo 'Another provisioning/release command is already running.' >&2; exit 1; }}
base=/opt/$app
release=$base/releases/{release_id}
backup=$base/backups/{release_id}.tar.gz
test ! -e "$release" && test ! -e "$backup" || {{ echo 'Release identifier already exists.' >&2; exit 1; }}
install -d -m 0755 -o root -g root "$release"
tar -xzf "$stage/release.tar.gz" -C "$release"
test -x "$release/server" && test -d "$release/pb_public"
chown -R root:root "$release"
chmod 0755 "$release/server"
find "$release/pb_public" -type d -exec chmod 0755 {{}} +
find "$release/pb_public" -type f -exec chmod 0644 {{}} +
was_active=no
if systemctl is-active --quiet "$app.service"; then was_active=yes; fi
switched=no
cleanup() {{
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$status" -ne 0 ] && [ "$switched" = no ] && [ "$was_active" = yes ]; then systemctl start "$app.service" || true; fi
    if [ "$status" -ne 0 ] && [ "$switched" = yes ]; then systemctl stop "$app.service" || true; fi
    rm -f "$base/.current.{release_id}"
    rm -rf "$stage"
    exit "$status"
}}
trap cleanup EXIT HUP INT TERM
systemctl stop "$app.service"
listeners=$(ss -H -ltn 'sport = :{port}')
if [ -n "$listeners" ]; then
    echo 'The configured TCP port is still in use by another service; choose a free port.' >&2
    exit 1
fi
# The service is stopped so SQLite and uploaded files form a consistent backup.
tar -czf "$backup" -C "$base" pb_data
chmod 0600 "$backup"
ln -s "$release" "$base/.current.{release_id}"
mv -Tf "$base/.current.{release_id}" "$base/current"
switched=yes
systemctl start "$app.service"
attempt=0
while [ "$attempt" -lt 30 ]; do
    if systemctl is-active --quiet "$app.service" && curl --fail --silent --max-time 2 http://127.0.0.1:{port}/api/health >/dev/null; then
        echo "Released {release_id}. Database backup: $backup"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done
echo "PocketBase health check failed. Inspect: journalctl -u $app.service" >&2
echo "Database backup: $backup. No automatic rollback; startup migrations may have changed the schema." >&2
exit 1
"""


def render(config: dict, output: Path = GENERATED) -> None:
    app = config["app"]
    for filename, content in {
        f"{app}.service": service_file(config),
        f"{app}.caddy": caddy_file(config),
        f"{app}.env.example": environment_file(config),
        "provision.sh": provision_script(config),
    }.items():
        write_atomic(output / filename, content, 0o600 if filename.endswith(".env.example") else 0o700 if filename.endswith(".sh") else 0o644)


def validate_env(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", line) or "\x00" in line or line.endswith("\\"):
            raise ValueError(f"Environment file line {number} must be a single KEY=value assignment (no export or multiline values).")
        try:
            shlex.split(line, comments=True)
        except ValueError as error:
            raise ValueError(f"Environment file line {number} has invalid quoting.") from error


def run_remote(config: dict, script: str, arguments: list[str] | None = None, *, capture: bool = False) -> subprocess.CompletedProcess:
    command = root_command(config, "sh -s --")
    if arguments:
        command += " " + shlex.join(arguments)
    return subprocess.run(remote_prefix(config) + [command], input=script, text=True, check=True, capture_output=capture)


def auto_port_script(config: dict) -> str:
    return preflight_script(config) + """# Read the active Caddy routes so stopped apps keep their reserved upstream ports.
configured=$(caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile 2>/dev/null)
candidate=8090
while [ "$candidate" -le 8999 ]; do
    if [ -z "$(ss -H -ltn "sport = :$candidate")" ] &&
       ! printf '%s\\n' "$configured" | grep -Fq "127.0.0.1:$candidate" &&
       ! printf '%s\\n' "$configured" | grep -Fq "localhost:$candidate" &&
       ! grep -RqsF ":$candidate" /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system 2>/dev/null; then
        printf '%s\\n' "$candidate"
        exit 0
    fi
    candidate=$((candidate + 1))
done
echo 'No unreserved localhost port found between 8090 and 8999.' >&2
exit 1
"""


def resolve_config(values: dict) -> dict:
    if str(values.get("port", "")).lower() != "auto":
        return validate_config(values)
    preliminary = validate_config(dict(values, port=8090))
    try:
        result = run_remote(preliminary, auto_port_script(preliminary), capture=True)
    except (OSError, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip().splitlines()[-1] if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        raise ValueError(f"Automatic port discovery failed: {detail}. Check trusted SSH access and Caddy, or supply a numeric --port.") from error
    found = result.stdout.strip()
    if not re.fullmatch(r"[0-9]{4,5}", found):
        raise ValueError("Automatic port discovery returned an invalid port; supply a numeric --port.")
    config = validate_config(dict(preliminary, port=int(found)))
    print(f"Found available VPS localhost port {config['port']} (read-only SSH); verify again before provisioning.")
    return config


def upload(config: dict, files: list[Path]) -> str:
    result = subprocess.run(remote_prefix(config) + ["mktemp -d /tmp/pb-template.XXXXXXXXXX"], text=True, check=True, capture_output=True)
    stage = result.stdout.strip()
    if not re.fullmatch(r"/tmp/pb-template\.[A-Za-z0-9]{10}", stage):
        raise ValueError("Remote mktemp returned an unexpected staging directory.")
    try:
        subprocess.run(["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-P", str(config["ssh_port"]), *[str(path) for path in files], f"{config['host']}:{stage}/"], check=True)
    except (OSError, subprocess.CalledProcessError):
        subprocess.run(remote_prefix(config) + ["rm -rf " + shlex.quote(stage)], check=False)
        raise
    return stage


def build_release(config: dict, destination: Path) -> Path:
    if not shutil.which("go"):
        raise ValueError("Missing local prerequisite: go")
    subprocess.run([*pnpm_command(), "build"], cwd=ROOT, check=True)
    public = ROOT / "dist"
    if not (public / "index.html").is_file():
        raise ValueError("Frontend build produced no dist/index.html.")
    if any(path.is_symlink() for path in public.rglob("*")):
        raise ValueError("Frontend build contains symlinks; only regular public assets may be deployed.")
    destination.mkdir(parents=True, exist_ok=True)
    binary = destination / "server"
    build_env = dict(os.environ, GOOS="linux", GOARCH=config["arch"], CGO_ENABLED="0")
    subprocess.run(["go", "build", "-trimpath", "-o", str(binary), "."], cwd=ROOT / "pocketbase", env=build_env, check=True)
    os.chmod(binary, 0o755)
    archive = destination / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(binary, arcname="server")
        handle.add(public, arcname="pb_public")
    return archive


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise ValueError("No deploy/config.json. Run configure first.")
    return validate_config(json.loads(CONFIG_PATH.read_text()))


def configure(args: argparse.Namespace) -> dict:
    existing = load_config() if CONFIG_PATH.exists() else {}
    package = json.loads((ROOT / "package.json").read_text())
    values = {"app": args.app, "host": args.host, "domain": args.domain, "port": args.port, "ssh_port": args.ssh_port, "arch": args.arch}
    defaults = {"app": package.get("name", "my-app"), "host": "deploy@your-vps", "domain": "app.example.com", "port": "auto", "ssh_port": 22, "arch": "amd64"}
    labels = {"app": "App/service slug", "host": "SSH user@hostname", "domain": "Public domain", "port": "PocketBase localhost port (auto discovers a free port over SSH)", "ssh_port": "SSH port", "arch": "Linux CPU architecture (amd64/arm64)"}
    for key in values:
        if values[key] is None:
            if args.non_interactive:
                if key in {"app", "host", "domain"} and key not in existing and (key != "app" or not package.get("name")):
                    raise ValueError(f"--non-interactive requires --{key}.")
                values[key] = existing.get(key, defaults[key])
            else:
                values[key] = prompt(labels[key], str(existing.get(key, defaults[key])))
    config = resolve_config(values)
    write_atomic(CONFIG_PATH, json.dumps(config, indent=2) + "\n", 0o600)
    render(config)
    print(f"Saved deploy/config.json and reviewable files in deploy/generated/ for {config['app']}.")
    print("Review provision.sh, the Caddy site, the service unit, and the environment defaults before applying.")
    return config


def print_plan(config: dict, command: str) -> None:
    app = config["app"]
    print(f"{command.capitalize()} plan: {config['host']} (SSH {config['ssh_port']}), {config['domain']} → 127.0.0.1:{config['port']}")
    if command == "provision":
        print(f"Create dedicated user {app}; /opt/{app}/pb_data; /etc/{app}/{app}.env; {app}.service.")
        print("Back up the Caddyfile, add a managed site import, validate, then reload Caddy. Preserve an existing environment file.")
        print("The app starts after the first release. Review generated files; apply with provision --apply.")
    else:
        print(f"Build fresh public assets and Linux/{config['arch']} Go binary; upload server and pb_public only.")
        print("Stop the service, back up pb_data on the VPS, switch release, start, and check PocketBase's /api/health.")
        print("Retain earlier releases and database backups. Apply with release --apply.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("configure", help="Prompt and generate local deployment configuration")
    for key in ("app", "host", "domain", "port", "ssh-port", "arch"):
        setup.add_argument("--" + key)
    setup.add_argument("--non-interactive", action="store_true")
    subparsers.add_parser("render", help="Regenerate review files from saved config")
    subparsers.add_parser("check", help="Read-only SSH/Caddy/systemd prerequisite check")
    provision = subparsers.add_parser("provision", help="Preview provisioning or apply it explicitly")
    provision.add_argument("--apply", action="store_true")
    provision.add_argument("--env-file", type=Path, help="Single-line backend KEY=value environment file")
    environment_mode = provision.add_mutually_exclusive_group()
    environment_mode.add_argument("--replace-env", action="store_true", help="Explicitly replace and back up an existing remote environment; requires --env-file")
    environment_mode.add_argument("--merge-env", action="store_true", help="Merge reviewed supplied keys and back up existing env; preserves unrelated server secrets")
    release = subparsers.add_parser("release", help="Preview a release or build/upload/start it explicitly")
    release.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            configure(args)
            return 0
        config = load_config()
        if args.command == "check":
            run_remote(config, preflight_script(config, provision=True))
            print("Remote prerequisites and target service, domain, and port checks passed. App health is a separate check.")
            return 0
        render(config)
        if args.command == "render":
            print("Regenerated deploy/generated/.")
            return 0
        if args.command == "provision":
            if args.replace_env and args.env_file is None:
                raise ValueError("--replace-env requires --env-file.")
            production = ROOT / "deploy/production.env"
            env_source = args.env_file or (production if production.is_file() else GENERATED / f"{config['app']}.env.example")
            merge_env = args.merge_env or args.env_file is None and production.is_file()
            validate_env(env_source)
        print_plan(config, args.command)
        if args.command == "provision":
            print(f"Environment source: {env_source}; existing server environment: {'replace' if args.replace_env else 'merge supplied keys' if merge_env else 'preserve'}.")
            print("Review this private file before apply. Provisioning does not restart a running app; release or restart it to load changes.")
        if not args.apply:
            return 0
        # Verify prerequisites before uploading any configuration or rebuilding the app.
        run_remote(config, preflight_script(config, release=args.command == "release", provision=args.command == "provision"))
        with tempfile.TemporaryDirectory(prefix="pb-template-deploy-") as temporary:
            work = Path(temporary)
            if args.command == "provision":
                env_target = work / f"{config['app']}.env"
                shutil.copyfile(env_source, env_target)
                os.chmod(env_target, 0o600)
                files = [GENERATED / f"{config['app']}.service", GENERATED / f"{config['app']}.caddy", env_target]
                stage = upload(config, files)
                try:
                    run_remote(config, provision_script(config), [stage, "yes" if args.replace_env else "no", "yes" if merge_env else "no"])
                finally:
                    subprocess.run(remote_prefix(config) + ["rm -rf " + shlex.quote(stage)], check=False)
            else:
                archive = build_release(config, work)
                release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                script = release_script(config, release_id)
                write_atomic(GENERATED / "release.sh", script, 0o700)
                stage = upload(config, [archive])
                try:
                    run_remote(config, script, [stage])
                finally:
                    subprocess.run(remote_prefix(config) + ["rm -rf " + shlex.quote(stage)], check=False)
        return 0
    except (ValueError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
