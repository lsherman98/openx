.PHONY: help install dev pb build build-deploy check types types-check migrate sync setup-vps provision deploy deploy-check setup-observability observability observability-check observability-status
ARGS ?=
PNPM ?= pnpm

help:
	@echo "install      Install locked frontend and Go dependencies"
	@echo "dev / pb     Start frontend / backend in separate terminals"
	@echo "build        Build the frontend and local backend binary"
	@echo "build-deploy Build and deploy a release to the configured VPS"
	@echo "types        Generate Go constants and TypeScript types from local schema"
	@echo "check        Lint, syntax checks, frontend/backend build, generated schema check"
	@echo "setup-vps    Generate systemd, Caddy, and env configuration locally"
	@echo "provision    Apply reviewed VPS configuration (ARGS=--apply)"
	@echo "deploy       Build/release to the configured VPS (ARGS=--apply)"
	@echo "setup-observability Prompt for optional Grafana/Loki/Alloy and error reporting"
	@echo "observability       Preview service deployment (ARGS=--apply to apply)"
	@echo "observability-check Validate generated optional service configuration locally"
	@echo "observability-status Read optional service status on the VPS"

install:
	$(PNPM) install --frozen-lockfile
	cd pocketbase && go mod download

dev:
	$(PNPM) dev

pb:
	cd pocketbase && go run . serve --http=127.0.0.1:8090

build:
	$(PNPM) build
	cd pocketbase && go build -o server .

check:
	$(PNPM) lint
	python3 -c "import ast; from pathlib import Path; [ast.parse(p.read_text(), filename=str(p)) for p in Path('scripts').glob('*.py')]"
	node scripts/check-syntax.mjs
	cd pocketbase && go vet ./...
	$(MAKE) build
	$(MAKE) types-check
	@if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git diff --check; fi

types:
	python3 scripts/generate_types.py $(ARGS)

types-check:
	python3 scripts/generate_types.py --schema schema.json --check

migrate:
	cd pocketbase && go run . migrate collections

sync:
	cd pocketbase && go run . migrate history-sync

setup-vps:
	python3 scripts/deploy.py configure $(ARGS)

provision:
	python3 scripts/deploy.py provision $(ARGS)

deploy:
	python3 scripts/deploy.py release $(ARGS)

build-deploy:
	python3 scripts/deploy.py release --apply $(ARGS)

deploy-check:
	python3 scripts/deploy.py check $(ARGS)

setup-observability:
	python3 scripts/observability.py configure $(ARGS)

observability:
	python3 scripts/observability.py apply $(ARGS)

observability-check:
	python3 scripts/observability.py check $(ARGS)

observability-status:
	python3 scripts/observability.py status $(ARGS)
