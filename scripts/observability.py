#!/usr/bin/env python3
"""Optional self-hosted app journal analytics with Grafana, Alloy, and Loki."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import secrets as secret_tokens
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

import deploy
from common import ROOT, prompt, write_atomic


DIRECTORY = ROOT / "deploy/observability"
GRAFANA_IMAGE = "grafana/grafana:13.2.3"
ALLOY_IMAGE = "grafana/alloy:v1.20.1"
LOKI_IMAGE = "grafana/loki:3.7.8"


def errors_helper():
    try:
        return importlib.import_module("observability_errors")
    except ModuleNotFoundError as error:
        if error.name != "observability_errors":
            raise
        return None


def validate_config(values: dict, deployment: dict) -> dict:
    deployment = deploy.validate_config(deployment)
    if values.get("app", deployment["app"]) != deployment["app"]:
        raise ValueError("The observability app differs from deploy/config.json; configure it again for this app.")
    domain = str(values.get("grafana_domain", "")).strip()
    if domain:
        domain = deploy.validate_config(dict(deployment, domain=domain))["domain"]
        if domain == deployment["domain"]:
            raise ValueError("Grafana needs a different domain from the application.")
    try:
        port, retention = int(values.get("grafana_port", 3000)), int(values.get("retention_days", 14))
    except (ValueError, TypeError) as error:
        raise ValueError("Grafana port and retention days must be integers.") from error
    if not 1024 <= port <= 65535 or port == deployment["port"]:
        raise ValueError("Grafana needs a distinct unprivileged port between 1024 and 65535.")
    if not 1 <= retention <= 90:
        raise ValueError("Log retention must be between 1 and 90 days.")
    user = str(values.get("admin_user", "admin"))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", user):
        raise ValueError("Grafana admin user must start with a letter and contain letters, numbers, dots, hyphens or underscores.")
    config = {"version": 1, "app": deployment["app"], "grafana_domain": domain, "grafana_port": port, "retention_days": retention, "admin_user": user}
    helper = errors_helper()
    if helper:
        config.update(helper.validate(dict(values, **config), deployment))
    elif values.get("error_domain"):
        raise ValueError("The optional error tracker helper is unavailable in this checkout.")
    return config


def json_file(value: dict) -> str:
    # JSON is valid YAML, and avoids adding a YAML package to project setup.
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def alloy_file(config: dict) -> str:
    app = config["app"]
    return f'''// Read only this app's systemd unit. No Docker socket or other units are collected.
logging {{
  level  = "warn"
  format = "json"
}}

loki.source.journal "app" {{
  path       = "/var/log/journal"
  matches    = "_SYSTEMD_UNIT={app}.service"
  max_age    = "24h"
  labels     = {{ service = "{app}", unit = "{app}.service" }}
  forward_to = [loki.process.app.receiver]
}}

loki.process "app" {{
  stage.json {{
    expressions = {{ level = "level", type = "type", request_id = "request_id" }}
  }}
  stage.labels {{
    values = {{ level = "", type = "" }}
  }}
  // Correlation IDs stay out of Loki's indexed labels.
  stage.structured_metadata {{
    values = {{ request_id = "" }}
  }}
  forward_to = [loki.write.local.receiver]
}}

loki.write "local" {{
  endpoint {{
    url = "http://loki:3100/loki/api/v1/push"
  }}
}}
'''


def loki_file(config: dict) -> str:
    hours = config["retention_days"] * 24
    return json_file({
        "auth_enabled": False,
        "server": {"http_listen_port": 3100, "log_level": "warn"},
        "common": {"path_prefix": "/loki", "instance_addr": "127.0.0.1", "replication_factor": 1, "ring": {"kvstore": {"store": "inmemory"}}, "storage": {"filesystem": {"chunks_directory": "/loki/chunks", "rules_directory": "/loki/rules"}}},
        "schema_config": {"configs": [{"from": "2024-01-01", "store": "tsdb", "object_store": "filesystem", "schema": "v13", "index": {"prefix": "app_", "period": "24h"}}]},
        "storage_config": {"tsdb_shipper": {"active_index_directory": "/loki/index", "cache_location": "/loki/index-cache"}},
        "compactor": {"working_directory": "/loki/compactor", "compaction_interval": "10m", "retention_enabled": True, "retention_delete_delay": "2h", "retention_delete_worker_count": 10, "delete_request_store": "filesystem"},
        "limits_config": {"retention_period": f"{hours}h", "max_query_lookback": f"{hours}h", "allow_structured_metadata": True, "ingestion_rate_mb": 2, "ingestion_burst_size_mb": 4, "max_query_parallelism": 2},
        "querier": {"max_concurrent": 2},
        "analytics": {"reporting_enabled": False},
    })


def dashboard_file(config: dict) -> str:
    selector = '{service="' + config["app"] + '"}'
    datasource = {"type": "loki", "uid": "app-loki"}
    panels = [
        {"id": 1, "title": "Requests / second", "type": "timeseries", "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8}, "targets": [{"refId": "A", "expr": f'sum(rate({selector} | json | type="request" [$__auto]))', "queryType": "range"}]},
        {"id": 2, "title": "Errors in the last 5 minutes", "type": "stat", "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8}, "targets": [{"refId": "A", "expr": 'sum(count_over_time({service="' + config["app"] + '",level="ERROR"}[5m]))', "queryType": "instant"}]},
        {"id": 3, "title": "Request duration p95 (ms)", "type": "timeseries", "gridPos": {"x": 0, "y": 8, "w": 24, "h": 8}, "targets": [{"refId": "A", "expr": f'quantile_over_time(0.95, {selector} | json | type="request" | unwrap duration_ms | __error__="" [$__auto])', "queryType": "range"}]},
        {"id": 4, "title": "Structured app logs (expand a row to inspect request_id)", "type": "logs", "gridPos": {"x": 0, "y": 16, "w": 24, "h": 12}, "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True}, "targets": [{"refId": "A", "expr": selector + ' | json', "queryType": "range"}]},
    ]
    for panel in panels:
        panel["datasource"] = datasource
        for target in panel["targets"]:
            target["datasource"] = datasource
    return json_file({"uid": "app-overview", "title": config["app"] + " application", "schemaVersion": 41, "version": 1, "editable": False, "tags": ["pocketbase", "application"], "time": {"from": "now-1h", "to": "now"}, "refresh": "30s", "panels": panels})


def alert_file(config: dict) -> str:
    expression = 'sum(count_over_time({service="' + config["app"] + '",level="ERROR"}[5m]))'
    return json_file({"apiVersion": 1, "groups": [{"orgId": 1, "name": "application-errors", "folder": "Application", "interval": "1m", "rules": [{
        "uid": "app-errors", "title": config["app"] + " has errors", "condition": "C", "for": "1m", "noDataState": "OK", "execErrState": "Error", "isPaused": False,
        "labels": {"service": config["app"], "severity": "error"}, "annotations": {"summary": "Application errors appeared in the last five minutes.", "description": "Open the application log dashboard and inspect request_id. Configure a Grafana contact point to receive external notifications.", "__dashboardUid__": "app-overview", "__panelId__": "2"},
        "data": [
            {"refId": "A", "datasourceUid": "app-loki", "relativeTimeRange": {"from": 300, "to": 0}, "model": {"refId": "A", "expr": expression, "queryType": "instant", "instant": True, "intervalMs": 1000, "maxDataPoints": 43200}},
            {"refId": "B", "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0}, "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}},
            {"refId": "C", "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0}, "model": {"refId": "C", "type": "threshold", "expression": "B", "conditions": [{"type": "query", "evaluator": {"type": "gt", "params": [0]}, "operator": {"type": "and"}, "query": {"params": ["C"]}, "reducer": {"type": "last", "params": []}}]}},
        ],
    }]}]})


def core_compose(config: dict) -> dict:
    port, domain = config["grafana_port"], config["grafana_domain"]
    shared = {"restart": "unless-stopped", "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"], "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}
    grafana = dict(shared, image=GRAFANA_IMAGE, user="472:472", mem_limit="512m", cpus=0.5, ports=[f"127.0.0.1:{port}:3000"], networks=["private", "egress"], volumes=["grafana-data:/var/lib/grafana", "./grafana/provisioning:/etc/grafana/provisioning:ro", "./grafana/dashboards:/etc/grafana/dashboards:ro"], environment={
        "GF_SECURITY_ADMIN_USER": config["admin_user"], "GF_SECURITY_ADMIN_PASSWORD": "${GRAFANA_ADMIN_PASSWORD:?Missing Grafana password}", "GF_SECURITY_SECRET_KEY": "${GRAFANA_SECRET_KEY:?Missing Grafana secret key}", "GF_AUTH_ANONYMOUS_ENABLED": "false", "GF_USERS_ALLOW_SIGN_UP": "false", "GF_SECURITY_COOKIE_SECURE": "true" if domain else "false", "GF_SERVER_ROOT_URL": f"https://{domain}" if domain else f"http://localhost:{port}", "GF_ANALYTICS_REPORTING_ENABLED": "false", "GF_ANALYTICS_CHECK_FOR_UPDATES": "false", "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false", "GF_LOG_MODE": "console", "GF_LOG_LEVEL": "warn",
    })
    loki = dict(shared, image=LOKI_IMAGE, user="10001:10001", read_only=True, mem_limit="768m", cpus=1, networks=["private"], volumes=["loki-data:/loki", "./loki/config.yaml:/etc/loki/config.yaml:ro"], command=["-config.file=/etc/loki/config.yaml"], tmpfs=["/tmp:uid=10001,gid=10001,mode=1777,size=64m"])
    alloy = dict(shared, image=ALLOY_IMAGE, user="0:0", read_only=True, mem_limit="256m", cpus=0.5, networks=["private"], volumes=["alloy-data:/var/lib/alloy/data", "./alloy/config.alloy:/etc/alloy/config.alloy:ro", {"type": "bind", "source": "${JOURNAL_DIRECTORY:-/var/log/journal}", "target": "/var/log/journal", "read_only": True, "bind": {"create_host_path": False}}, "/etc/machine-id:/etc/machine-id:ro"], command=["run", "--disable-reporting", "--server.http.enable-pprof=false", "--server.http.listen-addr=0.0.0.0:12345", "--storage.path=/var/lib/alloy/data", "/etc/alloy/config.alloy"], tmpfs=["/tmp:mode=1777,size=32m"], depends_on=["loki"])
    return {"name": config["app"] + "-observability", "services": {"grafana": grafana, "loki": loki, "alloy": alloy}, "volumes": {"grafana-data": {}, "loki-data": {}, "alloy-data": {}}, "networks": {"private": {"internal": True}, "egress": {}}}


def read_secrets(directory: Path) -> dict[str, str]:
    path = directory / ".env"
    if not path.exists():
        return {}
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("#"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", line) or any(ord(char) < 32 for char in line):
                raise ValueError(f"Invalid private environment assignment on line {number}.")
            key, value = line.split("=", 1)
            value = value.strip()
            if value.startswith("'"):
                if not value.endswith("'"):
                    raise ValueError(f"Invalid private environment quoting on line {number}.")
                value = value[1:-1].replace("\\'", "'")
            elif value.startswith('"'):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as error:
                    raise ValueError(f"Invalid private environment quoting on line {number}.") from error
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            values[key.strip()] = value
    return values


def env_literal(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Secret environment values must be single printable lines.")
    if re.fullmatch(r"[A-Za-z0-9_./:@+=?-]+", value):
        return value
    # Compose single quotes disable interpolation of passwords containing '$'.
    return "'" + value.replace("'", "\\'") + "'"


def render_files(config: dict, values: dict[str, str]) -> dict[str, str]:
    values.setdefault("GRAFANA_ADMIN_PASSWORD", secret_tokens.token_urlsafe(32))
    values.setdefault("GRAFANA_SECRET_KEY", secret_tokens.token_urlsafe(48))
    if not values["GRAFANA_ADMIN_PASSWORD"] or len(values["GRAFANA_SECRET_KEY"]) < 32:
        raise ValueError("Grafana requires a nonempty admin password and a secret key of at least 32 characters.")
    compose = core_compose(config)
    files = {
        "alloy/config.alloy": alloy_file(config), "loki/config.yaml": loki_file(config), "grafana/dashboards/app.json": dashboard_file(config),
        "grafana/provisioning/datasources/loki.yaml": json_file({"apiVersion": 1, "datasources": [{"name": "Application logs", "uid": "app-loki", "type": "loki", "access": "proxy", "url": "http://loki:3100", "isDefault": True, "editable": False, "jsonData": {"maxLines": 1000}}]}),
        "grafana/provisioning/dashboards/app.yaml": json_file({"apiVersion": 1, "providers": [{"name": "application", "orgId": 1, "folder": "Application", "type": "file", "disableDeletion": True, "editable": False, "updateIntervalSeconds": 30, "options": {"path": "/etc/grafana/dashboards"}}]}),
        "grafana/provisioning/alerting/errors.yaml": alert_file(config),
    }
    if config["grafana_domain"]:
        files["caddy/" + config["app"] + "-grafana.caddy"] = f"# Managed by pb-template for app: {config['app']}\n{config['grafana_domain']} {{\n    encode zstd gzip\n    reverse_proxy 127.0.0.1:{config['grafana_port']}\n}}\n"
    helper = errors_helper()
    if helper:
        additions = helper.render(config, values)
        for key in ("services", "volumes"):
            overlap = set(compose[key]) & set(additions.get(key, {}))
            if overlap:
                raise ValueError(f"Error tracker addon collides with {key}: {', '.join(sorted(overlap))}")
            compose[key].update(additions.get(key, {}))
        files.update(additions.get("files", {}))
        values.update({key: value for key, value in additions.get("secrets", {}).items() if key not in values})
        files.update({"caddy/" + filename: content for filename, content in additions.get("caddy_sites", {}).items()})
    for section in ("services", "volumes", "networks"):
        for item in compose[section].values():
            item.setdefault("labels", {})["io.pb-template.app"] = config["app"]
            item["labels"]["io.pb-template.stack"] = "observability"
    for filename in files:
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Addon files must stay inside the observability directory.")
    files["compose.yaml"] = json_file(compose)
    files[".env"] = "# Generated secrets: preserve this file and back it up privately.\n" + "\n".join(f"{key}={env_literal(value)}" for key, value in sorted(values.items()) if key != "JOURNAL_DIRECTORY") + "\n"
    return files


def render(config: dict, directory: Path | None = None) -> dict[str, str]:
    directory = directory or DIRECTORY
    files = render_files(config, read_secrets(directory))
    # Remove only obsolete generated files, retaining the private environment.
    manifest = directory / "manifest.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else []
    for filename in previous:
        path = directory / filename
        if filename not in files and not Path(filename).is_absolute() and ".." not in Path(filename).parts and path.is_file():
            path.unlink()
    for filename, content in files.items():
        write_atomic(directory / filename, content, 0o600 if filename == ".env" else 0o644)
    write_atomic(manifest, json.dumps(sorted(files), indent=2) + "\n")
    return files


def remote_script(config: dict, deployment: dict, caddy_files: list[str]) -> str:
    app, port = config["app"], config["grafana_port"]
    if any(not re.fullmatch(r"[a-z0-9-]+\.caddy", Path(name).name) for name in caddy_files):
        raise ValueError("Managed Caddy filenames must contain lowercase letters, numbers and hyphens.")
    site_names = " ".join(shlex.quote(Path(name).name) for name in caddy_files)
    port_checks = ""
    for service, host_port, container_port in [("grafana", port, 3000)] + ([("glitchtip", config["error_port"], 8000)] if config.get("error_domain") else []):
        port_checks += f'''listeners=$(ss -H -ltn 'sport = :{host_port}')
if [ -n "$listeners" ]; then
    owner=$(docker ps -q --filter "label=com.docker.compose.project=$project" --filter label=com.docker.compose.service={service})
    test -n "$owner" && docker port "$owner" {container_port}/tcp | grep -Fqx '127.0.0.1:{host_port}' || {{ echo 'Localhost port {host_port} belongs to another service; reconfigure the optional stack.' >&2; exit 1; }}
fi
'''
    bootstrap = f'''# Create the owner account before publishing the error service in Caddy.
attempt=0
while ! curl --fail --silent --max-time 2 http://127.0.0.1:{config.get('error_port', 8000)}/_health/ >/dev/null; do
    attempt=$((attempt + 1)); test "$attempt" -lt 180 || {{ echo 'GlitchTip failed its health check.' >&2; exit 1; }}; sleep 1
done
compose --profile setup run --rm glitchtip-init
''' if config.get("error_domain") else ""
    return f'''#!/bin/sh
{deploy.preflight_script(deployment)}
umask 077
stage=$1
test -d "$stage" && printf '%s' "$stage" | grep -Eq '^/tmp/pb-template\\.[A-Za-z0-9]{{10}}$' || {{ echo 'Invalid staging directory.' >&2; exit 1; }}
command -v docker >/dev/null || {{ echo 'Install Docker Engine and its Compose plugin first.' >&2; exit 1; }}
docker compose version >/dev/null
docker info >/dev/null
case "$(docker info --format '{{{{json .SecurityOptions}}}}')" in *rootless*|*userns*) echo 'Host journal collection requires a rootful Docker daemon without user namespace remapping.' >&2; exit 1;; esac
exec 9>/run/lock/pb-template-{app}-observability.lock
flock -n 9 || {{ echo 'Another observability apply is running.' >&2; exit 1; }}
exec 8>/run/lock/pb-template-caddy.lock
flock -n 8 || {{ echo 'Another Caddy provisioning command is running.' >&2; exit 1; }}
base=/opt/{app}-observability
env_dir=/etc/{app}-observability
env_file=$env_dir/secrets.env
project={app}-observability
unset GRAFANA_ADMIN_PASSWORD GRAFANA_SECRET_KEY GLITCHTIP_SECRET_KEY GLITCHTIP_DB_PASSWORD GLITCHTIP_ADMIN_PASSWORD GLITCHTIP_EMAIL_URL GLITCHTIP_FROM_EMAIL JOURNAL_DIRECTORY
if [ -e "$base" ]; then
    test -f "$base/.managed" && grep -Fqx '# Managed by pb-template for app: {app}' "$base/.managed" || {{ echo 'Refusing to replace an unmanaged observability directory.' >&2; exit 1; }}
fi
# Every resource belonging to this Compose project must carry this template's owner label.
resources=$(docker ps -aq --filter "label=com.docker.compose.project=$project")
for resource in $resources; do
    test "$(docker inspect --format '{{{{index .Config.Labels "io.pb-template.app"}}}}' "$resource")" = {app} || {{ echo 'Compose project contains an unmanaged container.' >&2; exit 1; }}
done
resources=$(docker volume ls -q --filter "label=com.docker.compose.project=$project")
for resource in $resources; do
    test "$(docker volume inspect --format '{{{{index .Labels "io.pb-template.app"}}}}' "$resource")" = {app} || {{ echo 'Compose project contains an unmanaged data volume.' >&2; exit 1; }}
done
resources=$(docker network ls -q --filter "label=com.docker.compose.project=$project")
for resource in $resources; do
    test "$(docker network inspect --format '{{{{index .Labels "io.pb-template.app"}}}}' "$resource")" = {app} || {{ echo 'Compose project contains an unmanaged network.' >&2; exit 1; }}
done
{port_checks}
printf '%s\\n' {site_names} >"$stage/sites.txt"
if [ -d "$base/caddy" ]; then
    for old_site in "$base"/caddy/*.caddy; do
        test -f "$old_site" || continue
        basename "$old_site" >>"$stage/sites.txt"
    done
fi
sort -u "$stage/sites.txt" | sed '/^$/d' >"$stage/all-sites.txt"
sites=$(cat "$stage/all-sites.txt")
for filename in $sites; do
    printf '%s' "$filename" | grep -Eq '^[a-z0-9-]+\\.caddy$' || {{ echo 'Invalid managed Caddy filename.' >&2; exit 1; }}
    existing=/etc/caddy/template-sites/$filename
    if [ -e "$existing" ]; then grep -Fqx '# Managed by pb-template for app: {app}' "$existing" || {{ echo 'Refusing to replace an unmanaged Caddy site.' >&2; exit 1; }}; fi
done
# Preserve the host's journald policy; bind an existing persistent or runtime journal.
if [ -d /var/log/journal ]; then journal=/var/log/journal; elif [ -d /run/log/journal ]; then journal=/run/log/journal; else echo 'No host journal directory exists.' >&2; exit 1; fi
tar -xzf "$stage/observability.tar.gz" -C "$stage"
candidate=$stage/files
install -d -m 0700 "$env_dir"
if [ -e "$env_file" ]; then
    awk -F= 'NR==FNR {{ if ($0 !~ /^#/ && NF > 1) keys[$1]=1; print; next }} !($1 in keys) && $1 != "JOURNAL_DIRECTORY" {{ print }}' "$env_file" "$candidate/.env" >"$stage/merged.env"
else cp "$candidate/.env" "$stage/merged.env"; fi
# Runtime journal directory is not a secret and is refreshed for this host.
awk -F= '$1 != "JOURNAL_DIRECTORY" {{ print }}' "$stage/merged.env" >"$stage/runtime.env"
printf 'JOURNAL_DIRECTORY=%s\\n' "$journal" >>"$stage/runtime.env"
chmod 0600 "$stage/runtime.env"
docker compose --project-name "$project" --env-file "$stage/runtime.env" -f "$candidate/compose.yaml" config --quiet
docker compose --project-name "$project" --env-file "$stage/runtime.env" -f "$candidate/compose.yaml" pull
# Validate the exact pinned component configurations before starting services.
docker run --rm --network none -v "$candidate/alloy/config.alloy:/etc/alloy/config.alloy:ro" {ALLOY_IMAGE} validate /etc/alloy/config.alloy
docker run --rm --network none -v "$candidate/loki/config.yaml:/etc/loki/config.yaml:ro" {LOKI_IMAGE} -config.file=/etc/loki/config.yaml -verify-config=true
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/etc/caddy/Caddyfile.{app}-observability.$stamp.bak
test ! -e "$backup" || {{ echo 'Backup timestamp collision; retry.' >&2; exit 1; }}
cp -p /etc/caddy/Caddyfile "$backup"
had_base=no
if [ -d "$base" ]; then cp -a "$base" "$stage/previous-files"; had_base=yes; fi
had_env=no
if [ -f "$env_file" ]; then cp -p "$env_file" "$stage/previous.env"; had_env=yes; fi
for filename in $sites; do
    if [ -f /etc/caddy/template-sites/$filename ]; then cp -p /etc/caddy/template-sites/$filename "$stage/previous-$filename"; fi
done
committed=no
cleanup() {{
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$committed" != yes ]; then
        docker compose --project-name "$project" --env-file "$env_file" -f "$base/compose.yaml" down >/dev/null 2>&1 || true
        cp -p "$backup" /etc/caddy/Caddyfile
        for filename in $sites; do
            if [ -f "$stage/previous-$filename" ]; then cp -p "$stage/previous-$filename" /etc/caddy/template-sites/$filename; else rm -f /etc/caddy/template-sites/$filename; fi
        done
        rm -rf "$base"
        if [ "$had_base" = yes ]; then cp -a "$stage/previous-files" "$base"; fi
        if [ "$had_env" = yes ]; then cp -p "$stage/previous.env" "$env_file"; else rm -f "$env_file"; fi
        caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && systemctl reload caddy || true
        echo 'Observability apply failed; restored previous configuration and stopped the stack. Named data volumes were retained; database migrations are not rolled back.' >&2
    fi
    rm -rf "$stage"
    exit "$status"
}}
trap cleanup EXIT HUP INT TERM
rm -rf "$base"
install -d -m 0755 "$base" /etc/caddy/template-sites
cp -a "$candidate/." "$base/"
rm -f "$base/.env"
find "$base" -type d -exec chmod 0755 {{}} +
find "$base" -type f -exec chmod 0644 {{}} +
printf '# Managed by pb-template for app: {app}\\n' >"$base/.managed"
install -m 0600 "$stage/runtime.env" "$env_file"
compose() {{ docker compose --project-name "$project" --env-file "$env_file" -f "$base/compose.yaml" "$@"; }}
compose up -d --remove-orphans
attempt=0
while [ "$attempt" -lt 60 ]; do
    loki_id=$(compose ps --all -q loki)
    alloy_id=$(compose ps --all -q alloy)
    loki_ip=$(docker inspect --format '{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}}{{{{end}}}}' "$loki_id")
    alloy_ip=$(docker inspect --format '{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}}{{{{end}}}}' "$alloy_id")
    if [ -n "$loki_ip" ] && [ -n "$alloy_ip" ] && curl --fail --silent --max-time 2 http://127.0.0.1:{port}/api/health >/dev/null && curl --fail --silent --max-time 2 "http://$loki_ip:3100/ready" >/dev/null && curl --fail --silent --max-time 2 "http://$alloy_ip:12345/-/healthy" >/dev/null; then break; fi
    attempt=$((attempt + 1)); sleep 1
done
test "$attempt" -lt 60 || {{ echo 'Grafana, Loki, or Alloy failed its health check.' >&2; exit 1; }}
{bootstrap}
# Reconcile only this addon stack's managed sites after its services and admin are ready.
for filename in $sites; do
    if [ -f "$candidate/caddy/$filename" ]; then install -m 0644 "$candidate/caddy/$filename" /etc/caddy/template-sites/$filename; else rm -f /etc/caddy/template-sites/$filename; fi
done
if [ -n "$sites" ] && ! grep -Fqx '{deploy.IMPORT_LINE}' /etc/caddy/Caddyfile; then printf '\\n{deploy.IMPORT_LINE}\\n' >>/etc/caddy/Caddyfile; fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
committed=yes
echo 'Self-hosted observability started. Secrets remain in the root-only environment file; existing secret values were preserved.'
'''


def load_config(deployment: dict) -> dict:
    path = DIRECTORY / "config.json"
    if not path.exists():
        raise ValueError("Run observability.py configure first. Observability is disabled by default.")
    return validate_config(json.loads(path.read_text()), deployment)


def configure(args: argparse.Namespace, deployment: dict) -> dict:
    existing = load_config(deployment) if (DIRECTORY / "config.json").exists() else {}
    defaults = {"grafana_domain": "", "grafana_port": 3000, "retention_days": 14, "admin_user": "admin", "error_domain": "", "error_admin_email": "", "error_port": 8000, "error_retention_days": 30}
    labels = {"grafana_domain": "Grafana domain (blank initially, or '-' keeps it private via SSH tunnel)", "grafana_port": "Grafana localhost port", "retention_days": "Log retention days (1–90)", "admin_user": "Initial Grafana admin user", "error_domain": "Optional GlitchTip domain (blank initially, or '-' disables error tracker)", "error_admin_email": "Initial GlitchTip administrator email", "error_port": "GlitchTip localhost port", "error_retention_days": "Error event retention days (1–365)"}
    values = {}
    for key, default in defaults.items():
        supplied = getattr(args, key)
        if supplied is not None:
            values[key] = supplied
        elif args.non_interactive or (key.startswith("error_") and (errors_helper() is None or key != "error_domain" and not values.get("error_domain"))):
            values[key] = existing.get(key, default)
        else:
            values[key] = prompt(labels[key], str(existing.get(key, default)))
        if key in {"grafana_domain", "error_domain"} and values[key] == "-":
            values[key] = ""
    config = validate_config(values, deployment)
    write_atomic(DIRECTORY / "config.json", json_file(config), 0o600)
    files = render(config)
    write_atomic(DIRECTORY / "apply.sh", remote_script(config, deployment, sorted(filename for filename in files if filename.startswith("caddy/"))), 0o700)
    print("Generated deploy/observability/. Grafana credentials are in its private .env file and were not printed.")
    print("Review generated configuration; run check to validate Compose, then apply --apply to install remotely.")
    return config


def check(directory: Path, *, runtime: bool = False) -> None:
    if not shutil.which("docker"):
        raise ValueError("Install the Docker CLI and Compose plugin to validate the generated stack.")
    subprocess.run(["docker", "compose", "--env-file", str(directory / ".env"), "-f", str(directory / "compose.yaml"), "config", "--quiet"], check=True)
    print("Docker Compose configuration passed.")
    if runtime:
        # Explicitly requested: may pull only the pinned validator images.
        subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", str(directory / "alloy/config.alloy") + ":/etc/alloy/config.alloy:ro", ALLOY_IMAGE, "validate", "/etc/alloy/config.alloy"], check=True)
        subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", str(directory / "loki/config.yaml") + ":/etc/loki/config.yaml:ro", LOKI_IMAGE, "-config.file=/etc/loki/config.yaml", "-verify-config=true"], check=True)
        print("Pinned Alloy and Loki runtime configuration validation passed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure", help="Generate an optional local observability stack")
    for flag in ("grafana-domain", "grafana-port", "retention-days", "admin-user", "error-domain", "error-admin-email", "error-port", "error-retention-days"):
        setup.add_argument("--" + flag)
    setup.add_argument("--non-interactive", action="store_true")
    commands.add_parser("render")
    commands.add_parser("status", help="Read-only SSH status of the optional Compose services")
    validation = commands.add_parser("check", help="Validate locally without making remote changes")
    validation.add_argument("--runtime", action="store_true", help="Also validate pinned Alloy/Loki using a local Docker daemon")
    apply = commands.add_parser("apply", help="Preview or explicitly apply the generated stack")
    apply.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        deployment = deploy.load_config()
        if args.command == "configure":
            configure(args, deployment)
            return 0
        config = load_config(deployment)
        if args.command == "status":
            script = f'''set -eu
base=/opt/{config['app']}-observability
test -f "$base/.managed" && grep -Fqx '# Managed by pb-template for app: {config['app']}' "$base/.managed" || {{ echo 'No managed observability stack is installed.' >&2; exit 1; }}
docker compose --project-name {config['app']}-observability --env-file /etc/{config['app']}-observability/secrets.env -f "$base/compose.yaml" ps
'''
            deploy.run_remote(deployment, script)
            return 0
        files = render(config)
        caddy_files = sorted(filename for filename in files if filename.startswith("caddy/"))
        script = remote_script(config, deployment, caddy_files)
        write_atomic(DIRECTORY / "apply.sh", script, 0o700)
        if args.command == "render":
            print("Regenerated observability files; existing local secrets were retained.")
        elif args.command == "check":
            check(DIRECTORY, runtime=args.runtime)
        else:
            url = "https://" + config["grafana_domain"] if config["grafana_domain"] else f"http://localhost:{config['grafana_port']} via SSH tunnel"
            print(f"Observability plan: {deployment['host']}; Grafana {url}; only {config['app']}.service journal; retention {config['retention_days']} days.")
            print("Pinned containers, durable named volumes, private Loki/Alloy, and existing Grafana secrets preserved. No app/database changes.")
            if not args.apply:
                print("Review deploy/observability/apply.sh; apply remotely with apply --apply.")
                return 0
            check(DIRECTORY)
            with tempfile.TemporaryDirectory(prefix="pb-template-observability-") as temporary:
                archive = Path(temporary) / "observability.tar.gz"
                with tarfile.open(archive, "w:gz") as handle:
                    for filename in files:
                        handle.add(DIRECTORY / filename, arcname="files/" + filename)
                stage = deploy.upload(deployment, [archive])
                try:
                    deploy.run_remote(deployment, script, [stage])
                finally:
                    subprocess.run(deploy.remote_prefix(deployment) + ["rm -rf " + shlex.quote(stage)], check=False)
        return 0
    except (ValueError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"Observability setup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
