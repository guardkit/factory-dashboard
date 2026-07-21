# factory-dashboard — one image, two services (the compose front door, G-02/L4).
#   - "board"     : uvicorn backend.app:app on 127.0.0.1:8090 (host network — loopback-only,
#                   byte-identical exposure to the bare processes this replaces).
#   - "projector" : python -m backend.projector (NATS consumers + mirrors + the F-5 stall monitor).
# No ENTRYPOINT is fixed — the compose services choose their command from the same image
# (the office-manager idiom). Dependencies come from uv.lock (frozen); the project itself runs
# from the copied source, so no packaging step is needed.

# syntax=docker/dockerfile:1

FROM docker.io/library/python:3.12-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

FROM docker.io/library/python:3.12-slim-bookworm AS runtime

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY backend ./backend
COPY frontend ./frontend
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
# The board binds loopback on the host network; nothing is EXPOSEd because nothing is published —
# the container shares the host's namespace and 127.0.0.1:8090 is the whole surface.
