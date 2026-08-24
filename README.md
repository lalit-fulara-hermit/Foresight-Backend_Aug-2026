# Foresight Backend, IEC Edition

Thin, real, on-demand. Nothing runs until you press Fetch.

## What it does

1. **Fetch** (on your command): pulls fresh items from 8 sources.
2. **Form**: new items become draft signals.
3. **Reason**: each draft goes to the Claude API. Label, suggested IEC action,
   and reasoning come back and are stored verbatim.
4. **Review**: you approve, edit, or reject. Every decision logged with name
   and time.
5. **Publish**: approved signals appear on the reader page with full trail.

## Sources (all immediate access)

arXiv, Crossref, OpenAlex, Federal Register, Regulations.gov (needs the
instant api.data.gov key), GDELT, CORDIS, standards-body RSS (IEC, ISO, IEEE).
Any source that fails is skipped and reported; a fetch never dies on one
bad source.

## Keys (environment variables)

| Variable | Required | Where from |
|---|---|---|
| ANTHROPIC_API_KEY | Yes | console.anthropic.com, API Keys, Create Key |
| REGULATIONS_GOV_API_KEY | Optional | api.data.gov/signup, instant email |
| FORESIGHT_KEY | Recommended | Any string you invent. Clients must send it in the X-Foresight-Key header |
| FORESIGHT_QUERY | Optional | Default search topic. Default: "solid state battery" |

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export FORESIGHT_KEY=choose-a-secret
uvicorn main:app --host 0.0.0.0 --port 8080
```

Test it:

```bash
curl http://localhost:8080/health
curl -X POST http://localhost:8080/fetch \
  -H "X-Foresight-Key: choose-a-secret" \
  -H "Content-Type: application/json" \
  -d '{"query": "solid state battery", "per_source": 5}'
curl http://localhost:8080/signals?status=draft
```

## Deploy (when host is chosen)

Works unchanged on GCP Cloud Run, Render, or Railway. The store is a single
SQLite file, so no database service is needed at demo scale.

Cloud Run, when ready:

```bash
gcloud run deploy foresight-backend --source . --region europe-west1 \
  --set-env-vars FORESIGHT_KEY=...,FORESIGHT_QUERY="solid state battery" \
  --set-secrets ANTHROPIC_API_KEY=anthropic-key:latest
```

Render or Railway: connect the repo, set the same environment variables in
the dashboard, done.

Note on Cloud Run: SQLite lives on the instance disk, which resets on
redeploy and does not share across instances. Set max instances to 1 for the
demo. For the paid build this becomes Firestore or Postgres; the store module
is the only file that changes.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | /health | Liveness plus counts |
| POST | /fetch | Pull sources now, form signals, run reasoning |
| GET | /signals?status=draft | Review queue |
| GET | /signals?status=published | Reader page |
| POST | /review | Approve or reject, with optional edits |
| GET | /signals/{id}/trail | Source items and decision log for one signal |
| GET | /config | The live prompt and rules, for on-screen display |
| PUT | /config | The live-edit moment: change a rule |
| POST | /signals/{id}/rerun | Re-run reasoning after a rule change |
| GET | /snapshot | Full export for the frontend cache layer |

## The honesty mechanism

The classification prompt and rules live in config.json and are served by
GET /config. The frontend shows them verbatim. What the audience reads on
screen is exactly what runs. Editing a rule and re-running one signal is the
live-modification moment IEC asked for.

## Tested

test_flow.py runs the full spine with mock data: ingest, dedupe, form,
reason, review with edits, publish, trail, config edit, snapshot.
10 checks, all passing. Live source APIs and the Claude call could not be
tested from the build sandbox; first live test happens at deployment.
