# Local setup in Visual Studio Code

This guide starts from the downloaded ZIP and takes you through running the application, editing it, and checking your changes on Windows with PowerShell. The quickest route uses Docker Desktop and PostgreSQL. A Python/Node setup is also included for backend or frontend-only work.

## 1. Extract and open the project

1. Download `self-healing-workflow-engine-aws-ready.zip` and extract it to a development folder, for example `C:\dev\Self-Healing-Workflow-Engine`.
2. In VS Code, choose **File > Open Folder...** and select the extracted `Self-Healing-Workflow-Engine` directory (the one containing `README.md`, `backend`, and `frontend`). Trust the folder when VS Code asks.
3. Open **Terminal > New Terminal**. Select PowerShell if it is not already selected.

Do not open the ZIP itself or only the `frontend` subfolder as the workspace: the Docker Compose file and AWS deployment script are at the project root.

## 2. Install the local tools

For the recommended Docker setup, install:

- [Visual Studio Code](https://code.visualstudio.com/)
- [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) with the WSL 2 backend enabled
- [Git for Windows](https://git-scm.com/download/win) (recommended for saving and comparing improvements)

Useful VS Code extensions are **Python** (Microsoft), **Pylance** (Microsoft), **Docker** (Microsoft), and **ESLint** (Microsoft). They are optional for running the project.

After installing Docker Desktop, start it and wait until it reports that the engine is running. In the VS Code terminal, confirm:

```powershell
docker --version
docker compose version
```

## 3. Start the complete app with Docker

Run these commands from the project root (check with `Get-Location`):

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

The first build downloads dependencies and may take several minutes. Compose starts PostgreSQL, applies database migrations, then starts the API, scheduler, worker, and production-built frontend. When the containers are healthy, open:

- App: [http://localhost:5173](http://localhost:5173)
- API docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- API readiness: [http://localhost:8000/health/ready](http://localhost:8000/health/ready)

Try the **Workflow Builder**: validate the starter JSON, save its definition, then run it. The Demo & Reliability Lab provides additional persisted scenarios.

To follow startup or application logs:

```powershell
docker compose logs -f api scheduler worker frontend
```

Press Ctrl+C to stop following logs. To stop containers while retaining the PostgreSQL volume, run `docker compose down`. To also erase the local database and all its workflow data, run `docker compose down -v`.

## 4. Make and review local improvements

Edit files in VS Code and save them. For backend changes, rebuild/restart the affected container:

```powershell
docker compose up --build -d api scheduler worker
```

For frontend changes, rebuild the frontend image:

```powershell
docker compose up --build -d frontend
```

For a clean start after changing dependencies or Docker configuration, use `docker compose down` followed by `docker compose up --build -d`. The named PostgreSQL volume remains unless you explicitly remove it with `down -v`.

The project checks can be run from the root if Python 3.12+ and the development dependencies are installed:

```powershell
python -m pytest
python -m ruff check backend migrations scripts tests
```

For frontend checks, Node.js 22+ and pnpm are needed:

```powershell
Set-Location frontend
pnpm install --frozen-lockfile
pnpm test
pnpm build
Set-Location ..
```

## 5. Optional: run without Docker

This is useful when iterating directly on Python or React code. Install Python 3.12+, Node.js 22+, and pnpm. From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
$env:DATABASE_URL = 'sqlite+aiosqlite:///./workflow-local.db'
$env:ENVIRONMENT = 'development'
python -m alembic upgrade head
```

If PowerShell blocks activation, for this terminal only run `Set-ExecutionPolicy -Scope Process Bypass`, then activate again. Open three additional VS Code terminals, activate `.venv` in each, and run one command in each:

```powershell
python -m backend.app
```

```powershell
python -m backend.app scheduler
```

```powershell
python -m backend.app worker
```

In another terminal, run the frontend:

```powershell
Set-Location frontend
pnpm install --frozen-lockfile
pnpm dev -- --port 5191
```

Open [http://localhost:5191](http://localhost:5191). Vite forwards API calls to the local API on port 8000. SQLite is intended for single-process development; use the Docker Compose PostgreSQL setup for multiple workers and database concurrency/recovery behavior.

## Troubleshooting

- **Docker daemon not available:** start Docker Desktop, wait for it to finish starting, then retry `docker compose up`.
- **Port already allocated:** stop the other service using 5173, 8000, or 5432, or change the corresponding published port in `docker-compose.yml` and restart Compose.
- **App opens but API is not ready:** inspect `docker compose ps` and `docker compose logs --tail 200 api migrate postgres`.
- **Frontend shows a stale version:** run `docker compose up --build -d frontend`, then hard-refresh the browser with Ctrl+Shift+R.
- **Containers appear out of sync after code changes:** run `docker compose up --build -d` from the project root.

For AWS deployment, continue with [the end-to-end AWS guide](../deploy/aws/README.md).
