# prospect-agent

A proactive agent on a marketing studio's website (the demo studio is "Fieldwork Studio", `studio.json`). It watches
what each visitor does, decides on its own when something is worth saying, answers the chat, and offers a write-up
or a person by email. Built and tested with [Minutehand](https://minutehand.ai). The concept is in `USE_CASE.md`.

Live: https://prospect-agent-944666793499.us-central1.run.app

## How it works

One planner, one executor, one turn per visitor at a time (`agent/server.py`):

- **Planner** (`looks.plan`) reads the moment after anything worth a look (a signal, a linger, leaving, the first
  view of a visit) and scores how much it calls for the agent to speak, 0 to 1. It speaks when the score clears a bar
  that rises with each of its own messages still unopened (`speaking_bar`). It also chooses when to look again.
- **Executor** (`looks.execute`) writes: answers what they wrote, says what the planner briefed, researches
  (Perplexity, ScraperAPI, a headless browser for their site), emails a write-up (Resend). Every factual claim is
  checked against its sources (`_told_once`); a message scoring misleading ≥ 0.6 is sent back to be rewritten.
- **Widget** (`site/agent.js`) is the chat: a button, previews that pop up for 20 seconds, the conversation, and the
  agent's state (Thinking, Writing, Looking things up). The server's thread is the record; the tab keeps a copy so
  the next page draws at once.

Models: `openai-responses:gpt-6-luna`, low reasoning effort (`AGENT_*_MODEL` to change).

## Routes

| Route | Who | |
|---|---|---|
| `GET /`, `/services`, … | anyone | the site, from `site/` |
| `POST /api/events`, `/api/thread/send`, `/read`, `/dismiss`; `GET /api/thread` | the widget | public, rationed |
| `GET /api/unsubscribe` | email links | public |
| `GET /console`, `/api/visitors`; `POST /inbound/email` | the studio | `?key=OPERATOR_KEY` |
| `GET /api/shown`, `/report`; `POST /api/world`, `/wake` | a Minutehand run | only exist with `MINUTEHAND_ON` |

Anything that spends a model call is rationed: 6 turns a minute per visitor, 30 across everyone; messages over 2,000
characters are refused. Without `OPERATOR_KEY` set, the studio routes answer only to this machine.

## Run it locally

```
OPENAI_API_KEY=… PERPLEXITY_API_KEY=… SCRAPERAPI_KEY=… MAIL_OUTBOX=outbox.txt uv run python -m agent.server
```

Opens on http://127.0.0.1:8760 (`PORT` to change); the console is `/console`. `MAIL_OUTBOX` writes emails to a file
instead of sending them; `RESEND_API_KEY` sends them. `uv.lock` expects Minutehand's source at `../minutehand-input`.
`desk_stock.json` (the news desk's stock) is not in the repo; the server starts without it and writes one, and the
`Dockerfile` copies it, so deploy from a checkout that has run once.

## Test it with Minutehand

`simulation/agent.yaml` declares the site to Minutehand; `simulation/generated/agent.yaml` adds 20 generated visitor
sites. `eval/moments.py` reads a run's recording and the agent log: what each visitor did with each message, and how
long answers took.

## Deploy

Cloud Run service `prospect-agent`, project `minutehand-host`, region `us-central1`, one instance always on (the agent
wakes itself). Keys are in Secret Manager, never in the image:

| Secret | Env var |
|---|---|
| `prospect-agent-openai` | `OPENAI_API_KEY` |
| `prospect-agent-perplexity` | `PERPLEXITY_API_KEY` |
| `prospect-agent-scraperapi` | `SCRAPERAPI_KEY` |
| `prospect-agent-resend` | `RESEND_API_KEY` |
| `prospect-agent-operator` | `OPERATOR_KEY` (the console's `?key=`) |

The build context holds this directory as `app/` and Minutehand's source as `minutehand-input/` (see `Dockerfile`):

```
CTX=$(mktemp -d)
rsync -a --exclude .venv --exclude '*.db*' --exclude .env --exclude .git --exclude simulation --exclude eval ./ $CTX/app/
rsync -a --exclude .venv --exclude .git --exclude tests --exclude examples ../minutehand-input/ $CTX/minutehand-input/
cp Dockerfile $CTX/
gcloud run deploy prospect-agent --source $CTX --region us-central1 --project minutehand-host --quiet \
  --no-invoker-iam-check --min-instances 1 --max-instances 1 --no-cpu-throttling --memory 2Gi --timeout 300 \
  --set-env-vars "^|^PUBLIC_URL=https://fieldwork-concierge.web.app|MAIL_FROM=Fieldwork Studio <onboarding@resend.dev>" \
  --set-secrets "OPENAI_API_KEY=prospect-agent-openai:latest,PERPLEXITY_API_KEY=prospect-agent-perplexity:latest,SCRAPERAPI_KEY=prospect-agent-scraperapi:latest,RESEND_API_KEY=prospect-agent-resend:latest,OPERATOR_KEY=prospect-agent-operator:latest"
```

The console link: `gcloud secrets versions access latest --secret prospect-agent-operator --project minutehand-host`,
appended as `/console?key=…`.

The database is SQLite on the container's disk (`/tmp/studio_agent.db`): visitors and conversations reset on every
deploy or restart. `firebase.json` would put the site at `fieldwork-concierge.web.app` (`firebase deploy --only
hosting`); it is not published.
