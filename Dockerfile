# The site and its agent as one container: Firebase Hosting (fieldwork-concierge) sends every request here.
# Built by deploy.sh from a context holding this directory as app/ and Minutehand's source as minutehand-input/, the
# layout uv.lock's editable path expects.
FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY minutehand-input /src/minutehand-input
WORKDIR /src/app
COPY app/pyproject.toml app/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN .venv/bin/playwright install --with-deps chromium
COPY app/agent agent
COPY app/site site
COPY app/console console
COPY app/studio.json app/desk_stock.json ./
ENV HOST=0.0.0.0 AGENT_DB=/tmp/studio_agent.db DESK_STOCK=/src/app/desk_stock.json
CMD [".venv/bin/python", "-m", "agent.server"]
