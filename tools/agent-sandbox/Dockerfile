# Sandboxed pydantic-ai agent that drives the loseit CLI via a homelab Ollama model.
# Build:  docker build -t loseit-agent tools/agent-sandbox
# Run:    see README.md
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# git: needed at build time to install lose-it from its GitHub repo.
# ca-certificates: needed at runtime for HTTPS to Ollama + loseit.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 agent
USER agent
WORKDIR /home/agent

# Pre-install deps into a uv-managed venv so cold runs are fast.
# Pin to a known-good pydantic-ai release line — agent tool-calling APIs are stabilizing.
ENV UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    PATH="/home/agent/.local/bin:/home/agent/.venv/bin:${PATH}" \
    OLLAMA_BASE_URL="https://ollama.priv.mlops-club.org/v1" \
    OLLAMA_MODEL="qwen3:8b"

# `lose-it` is unpublished — install from git. Override with --build-arg LOSEIT_REF=...
ARG LOSEIT_REF=main
RUN uv venv /home/agent/.venv \
 && uv pip install --python /home/agent/.venv/bin/python \
        "pydantic-ai>=1.0,<2" \
        "git+https://github.com/phitoduck/lose-it.git@${LOSEIT_REF}" \
 && /home/agent/.venv/bin/loseit --help >/dev/null

COPY --chown=agent:agent agent.py /home/agent/agent.py

ENTRYPOINT ["/home/agent/.venv/bin/python", "/home/agent/agent.py"]
