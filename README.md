# StaffDeck Demo (Secondary Development)

> A secondary-development demo based on [OpenBMB/StaffDeck](https://github.com/OpenBMB/StaffDeck), with the persistence layer migrated from SQLite to PostgreSQL.

[简体中文](./README.zh.md) | **English**

## What is this

This is a forked demo of StaffDeck — an enterprise platform for building and managing digital employees. The goal of this repo is **not** to re-distribute StaffDeck, but to demonstrate a targeted refactor: **replacing the embedded SQLite database with PostgreSQL** for production-grade deployments.

## What we changed

The only meaningful change vs. upstream is the **database layer**:

| Area | Upstream (StaffDeck) | This Demo |
|------|----------------------|-----------|
| Database | SQLite (file-based) | PostgreSQL (via Docker) |
| Driver | built-in | `psycopg[binary]` (psycopg3) |
| Schema init | `create_all` + 2000 lines of SQLite-specific migrations | `create_all` only (Alembic planned) |
| Connection | `check_same_thread=False` | `QueuePool` + `pool_pre_ping` + `pool_recycle` |
| Lock handling | retry on "database is locked" | removed (PG uses MVCC) |

Business code (API, agent runtime, services) is **untouched** — the refactor is isolated to the data and config layers.

## Quick start

### Prerequisites

- Python 3.11+
- Node.js 20+
- Docker (for PostgreSQL)

### 1. Start PostgreSQL

```bash
docker run -d --name staffdeck-postgres \
  -e POSTGRES_USER=staffdeck \
  -e POSTGRES_PASSWORD=staffdeck123 \
  -e POSTGRES_DB=staffdeck \
  -p 5432:5432 \
  -v staffdeck-pgdata:/var/lib/postgresql/data \
  postgres:16-alpine
```

### 2. Install backend

```bash
python -m venv backend/.venv
# Windows
backend\.venv\Scripts\python -m pip install -e "backend[dev]"
# macOS/Linux
backend/.venv/bin/python -m pip install -e "backend[dev]"
```

### 3. Install frontend

```bash
npm --prefix frontend-enterprise ci
```

### 4. Configure

```bash
cp backend/.env.example backend/.env
# Edit backend/.env to set your model API key and APP_SECRET
```

### 5. Run

```bash
# Windows
.\scripts\dev_up.ps1 --detach
# macOS/Linux/WSL
scripts/dev_up.sh --detach
```

Visit http://127.0.0.1:5173/workspace/gallery — default admin login: `admin` / `admin`.

## Tech stack

- **Frontend**: React 18 + TypeScript + Vite 6 + TailwindCSS + shadcn/ui
- **Backend**: FastAPI + SQLModel + OpenAI/Anthropic SDK
- **Database**: PostgreSQL 16 (this demo) / SQLite (upstream)

## Project structure

```
├── backend/                  # FastAPI backend (API, agent runtime, storage)
├── frontend-enterprise/      # React/TypeScript workspace UI
├── scripts/                  # Lifecycle scripts + DB migration tools
├── Dockerfile                # Multi-stage build (single-port deployment)
└── k8s/                      # Kubernetes deployment template
```

## Credits

- Original project: [OpenBMB/StaffDeck](https://github.com/OpenBMB/StaffDeck) (AGPL-3.0)
- This demo is for learning/research purposes only.
