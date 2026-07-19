# Route Calculator Push and Render Runbook

This file is the terminal checklist for making the project git-ready, cleaning local junk, and preparing it for Render.

Run these commands from:

```bash
cd "/Users/abdulsemiuamisu/Desktop/Lemtik Security/Internalservices/routecalculator"
```

## 1. Remove Local Python Artifacts

If you created a local virtualenv inside this folder, remove it first:

```bash
rm -rf .venv
```

Remove Python cache files:

```bash
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type f -name "*.pyc" -delete
```

If you want a clean local database state before pushing:

```bash
rm -f routecalculator.db
```

Leave `nigeria-latest.osm.pbf` in place if you want to keep the routing data in the folder.
If you do not want to commit it, add it to `.gitignore` and remove it from git tracking if needed.

## 2. Create or Update `.gitignore`

If `.gitignore` does not exist, create it with these entries:

```gitignore
.venv/
__pycache__/
*.pyc
*.pyo
*.pyd
routecalculator.db
*.db-journal
.env
.env.*
.DS_Store
```

If you also do not want to commit the OSM extract:

```gitignore
nigeria-latest.osm.pbf
```

If any of these files were already tracked by git, untrack them once:

```bash
git rm --cached routecalculator.db
git rm --cached nigeria-latest.osm.pbf
```

Only run the `git rm --cached` commands if the files are already tracked and you want them removed from the repository index.

## 3. Install Dependencies

Use a fresh virtual environment outside the repo if you want a clean setup:

```bash
python3 -m venv ../routecalculator-venv
source ../routecalculator-venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

If you are staying inside the repo, use:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 4. Local Smoke Test

Run a quick compile check:

```bash
python3 -m py_compile app.py config.py integrations.py service.py storage.py
```

Run the service locally:

```bash
python app.py
```

Then test the health endpoint in another terminal:

```bash
curl http://127.0.0.1:8000/health
```

Test route calculation:

```bash
curl -X POST http://127.0.0.1:8000/route/calculate \
  -H "Content-Type: application/json" \
  -d '{
    "request_type":"route_calculate",
    "request_id":"req_abc123",
    "org_id":"org_xyz",
    "incident":{
      "id":"INC-2024-001",
      "location":{"lat":6.4281,"lng":3.4219,"description":"North West Wing, Floor 3"},
      "type":"stabbing",
      "indoor":true,
      "building_id":"BLDG-HOTEL-001"
    },
    "responders":{
      "officers":["OFF-001","OFF-003","OFF-007"],
      "vehicles":["V001","V003"]
    },
    "routing_preferences":{"type":"hybrid","prioritise":"speed"}
  }'
```

## 5. Prepare Git

Check what will be committed:

```bash
git status
```

Review the exact diff:

```bash
git diff --stat
```

If you are happy with the files, stage them:

```bash
git add app.py config.py integrations.py service.py storage.py requirements.txt README.md API.md push.md migrations
```

If you also want the spec file and other local assets committed, add them explicitly.

Commit:

```bash
git commit -m "Build route calculator service"
```

Push:

```bash
git push origin main
```

If your branch is not `main`, replace it with the current branch name:

```bash
git branch --show-current
```

## 6. Render Setup

On Render, create a new Web Service and point it to this repository.

Use these settings:

- Build command: `pip install -r requirements.txt`
- Start command: `python app.py`

If you prefer the ASGI entrypoint and have `uvicorn` available:

- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`

Environment variables to set on Render:

```env
VALHALLA_URL=
VALHALLA_ROUTE_PATH=/route
RADAR_BASE_URL=
RADAR_SECRET_KEY=
RADAR_PUBLISHABLE_KEY=
MAPBOX_ACCESS_TOKEN=
DATABASE_URL=
INTERNAL_API_KEY=
RELATIONSHIP_API_URL=
RELATIONSHIP_API_KEY=
RELATIONSHIP_PUSH_PATH=/route/push
INFRASTRUCTURE_CORRIDOR_METRES=100
MAX_OFFICERS_PER_ROUTE_QUERY=20
VALHALLA_TIMEOUT_SECONDS=5
RADAR_TIMEOUT_SECONDS=5
RELATIONSHIP_TIMEOUT_SECONDS=5
ENVIRONMENT=production
PORT=8000
HOST=0.0.0.0
```

If you are using Supabase/Postgres, set `DATABASE_URL` to the Supabase connection string.

## 7. Optional Cleanup Before Push

If you want a strict repo without local runtime junk:

```bash
rm -rf __pycache__
rm -f routecalculator.db
rm -rf .venv
```

Then confirm only the source files remain:

```bash
git status
```

## 8. Recommended Order

1. Clean local artifacts
2. Add or update `.gitignore`
3. Run compile and smoke tests
4. Stage and commit files
5. Push to GitHub
6. Deploy on Render

