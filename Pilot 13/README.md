# GASUM Pilot 13

Clean delivery copy of the decision-support artefact for the master thesis. This version keeps only the code and minimal runtime assets needed to run the backend and frontend locally.

## What is included

- `backend/`: FastAPI API, optimisation logic and JSON runtime data
- `frontend/`: React + Vite dashboard
- `scripts/app_up.sh`, `scripts/app_down.sh`, `scripts/app_status.sh`: helper scripts to start and stop the app
- `database/`: empty runtime folder for generated analytics data

## Dataset note

The repository includes a **small synthetic/anonymised scenario** so the application can be demonstrated without shipping the full working dataset.

## Run locally

### Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Backend URL: [http://localhost:8000](http://localhost:8000)
Docs: [http://localhost:8000/docs](http://localhost:8000/docs)

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend URL: [http://localhost:5173](http://localhost:5173)

### Detached start scripts

After installing dependencies, you can also use:

```bash
./scripts/app_up.sh
./scripts/app_status.sh
./scripts/app_down.sh
```

## Repository scope

This delivery copy intentionally excludes thesis drafts, figures, reports, raw documents, caches, local virtual environments, installed node modules, and validation artefacts that are not required to run the application.
