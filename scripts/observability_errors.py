"""Optional small Sentry-compatible server for the observability Compose bundle.

GlitchTip is MIT licensed. Its Sentry SDK endpoint can also be replaced by an
existing GlitchTip or full Sentry installation without deploying these services.
"""

from __future__ import annotations

import re
import secrets as random_secrets

from deploy import validate_config


GLITCHTIP_IMAGE = "glitchtip/glitchtip:6.2.6"
POSTGRES_IMAGE = "postgres:18.6-alpine"


def validate(config: dict, deploy_config: dict) -> dict:
    config = dict(config)
    domain = str(config.get("error_domain") or "").strip()
    if not domain:
        config.update(error_domain="", error_port=8000, error_retention_days=30, error_admin_email="")
        return config
    domain = validate_config(dict(deploy_config, domain=domain))["domain"]
    if domain in {deploy_config["domain"], config.get("grafana_domain")}:
        raise ValueError("Error reporting needs a different domain from the app and Grafana.")
    try:
        port = int(config.get("error_port", 8000))
        retention = int(config.get("error_retention_days", 30))
    except (TypeError, ValueError) as error:
        raise ValueError("Error reporting port and retention must be integers.") from error
    occupied = {deploy_config["port"], int(config.get("grafana_port", 3000)), 3100, 12345}
    if not 1024 <= port <= 65535 or port in occupied:
        raise ValueError("Error reporting needs a distinct localhost port between 1024 and 65535.")
    if not 1 <= retention <= 365:
        raise ValueError("Error retention must be between 1 and 365 days.")
    email = str(config.get("error_admin_email") or "").strip()
    if len(email) > 254 or not re.fullmatch(r"[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+\.[^\s@\x00-\x1f\x7f]+", email):
        raise ValueError("Provide --error-admin-email for the initial GlitchTip administrator.")
    config.update(error_domain=domain, error_port=port, error_retention_days=retention, error_admin_email=email)
    return config


def render(config: dict, secrets: dict) -> dict:
    if not config.get("error_domain"):
        return {"services": {}, "volumes": {}, "files": {}, "secrets": {}, "caddy_sites": {}}
    # Return defaults only; the caller preserves both existing local and remote
    # keys. Rotating SECRET_KEY/passwords during a render would break live data.
    defaults = {
        "GLITCHTIP_SECRET_KEY": random_secrets.token_hex(32),
        "GLITCHTIP_DB_PASSWORD": random_secrets.token_hex(32),
        "GLITCHTIP_ADMIN_PASSWORD": random_secrets.token_urlsafe(32),
        "GLITCHTIP_EMAIL_URL": "consolemail://",
        "GLITCHTIP_FROM_EMAIL": config["error_admin_email"],
    }
    for key in ("GLITCHTIP_SECRET_KEY", "GLITCHTIP_DB_PASSWORD", "GLITCHTIP_ADMIN_PASSWORD"):
        if key in secrets and not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", secrets[key]):
            raise ValueError(f"{key} must contain 24–128 URL-safe characters.")
    environment = {
        "DATABASE_URL": "postgres://glitchtip:${GLITCHTIP_DB_PASSWORD:?Missing GlitchTip DB password}@glitchtip-db:5432/glitchtip",
        "VALKEY_URL": "",
        "SECRET_KEY": "${GLITCHTIP_SECRET_KEY:?Missing GlitchTip secret}",
        "GLITCHTIP_DOMAIN": f"https://{config['error_domain']}",
        "EMAIL_URL": "${GLITCHTIP_EMAIL_URL:-consolemail://}",
        "DEFAULT_FROM_EMAIL": "${GLITCHTIP_FROM_EMAIL:?Missing GlitchTip sender}",
        "SERVER_ROLE": "all_in_one",
        "ALLOWED_HOSTS": f"{config['error_domain']},localhost,127.0.0.1",
        "CSRF_TRUSTED_ORIGINS": f"https://{config['error_domain']}",
        "ENABLE_USER_REGISTRATION": "False",
        "ENABLE_SOCIAL_APPS_USER_REGISTRATION": "False",
        "ENABLE_ORGANIZATION_CREATION": "False",
        "ENABLE_ADMIN": "True",
        "ENABLE_OPENAPI": "False",
        "GLITCHTIP_ENABLE_MCP": "False",
        "GLITCHTIP_ENABLE_LOGS": "False",
        "GLITCHTIP_ENABLE_UPTIME": "False",
        "GLITCHTIP_ENABLE_DUCKDB": "False",
        "GLITCHTIP_RETENTION_DAYS": str(config["error_retention_days"]),
        "GRANIAN_WORKERS": "1",
        "VTASKS_CONCURRENCY": "4",
        "DATABASE_POOL_MAX_SIZE": "8",
    }
    logging = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    common = {"image": GLITCHTIP_IMAGE, "environment": environment,
              "networks": ["private", "egress"], "logging": logging,
              "security_opt": ["no-new-privileges:true"], "mem_limit": "768m", "pids_limit": 128}
    bootstrap = (
        "import os; from django.contrib.auth import get_user_model; "
        "from django.core.management import call_command; "
        "User = get_user_model(); email = os.environ['DJANGO_SUPERUSER_EMAIL']; "
        "exists = User.objects.filter(**{User.USERNAME_FIELD: email}).exists(); "
        "call_command('createsuperuser', interactive=False, **{User.USERNAME_FIELD: email}) if not exists else None"
    )
    services = {
        "glitchtip-db": {
            "image": POSTGRES_IMAGE, "restart": "unless-stopped", "networks": ["private"],
            "environment": {"POSTGRES_USER": "glitchtip", "POSTGRES_DB": "glitchtip",
                            "POSTGRES_PASSWORD": "${GLITCHTIP_DB_PASSWORD:?Missing GlitchTip DB password}"},
            "volumes": ["glitchtip-db:/var/lib/postgresql"],
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U glitchtip -d glitchtip"],
                            "interval": "10s", "timeout": "5s", "retries": 10},
            "logging": logging, "security_opt": ["no-new-privileges:true"],
            "mem_limit": "256m", "pids_limit": 128,
        },
        "glitchtip": {
            **common, "restart": "unless-stopped",
            "depends_on": {"glitchtip-db": {"condition": "service_healthy"}},
            "ports": [f"127.0.0.1:{config['error_port']}:8000"],
            "volumes": ["glitchtip-uploads:/code/uploads"],
            "healthcheck": {"test": ["CMD", "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/_health/', timeout=5)"],
                "interval": "10s", "timeout": "10s", "retries": 30, "start_period": "60s"},
        },
        "glitchtip-init": {
            **common, "profiles": ["setup"], "restart": "no",
            "depends_on": {"glitchtip": {"condition": "service_healthy"}},
            "environment": {**environment,
                "DJANGO_SUPERUSER_EMAIL": config["error_admin_email"],
                "DJANGO_SUPERUSER_PASSWORD": "${GLITCHTIP_ADMIN_PASSWORD:?Missing GlitchTip admin password}"},
            "command": ["./manage.py", "shell", "-c", bootstrap],
        },
    }
    app = config["app"]
    return {
        "services": services,
        "volumes": {"glitchtip-db": {}, "glitchtip-uploads": {}},
        "files": {}, "secrets": defaults,
        "caddy_sites": {f"{app}-errors.caddy": f"""# Managed by pb-template for app: {app}
{config['error_domain']} {{
    encode zstd gzip
    request_body {{
        max_size 20MB
    }}
    reverse_proxy 127.0.0.1:{config['error_port']}
}}
"""},
    }
