# agent-sandbox

Single-file pydantic-ai agent (`agent.py`) that drives the `loseit` CLI via a homelab-hosted Ollama model (`qwen3:8b` by default). Runs in Docker with no host filesystem access except a read-only mount of `~/.config/loseit/` for the CLI's credentials.

## Quick start

```bash
# Build (uses git+main for lose-it; pin with --build-arg LOSEIT_REF=v0.3.0)
docker build -t loseit-agent tools/agent-sandbox

# Run (mount a runs dir to persist per-run logs)
mkdir -p ./runs
docker run --rm -it \
  -v ~/.config/loseit:/home/agent/.config/loseit:ro \
  -v "$PWD/runs:/home/agent/runs" \
  loseit-agent "log 100g of guacamole as a snack"
```

Each run writes one log file at `./runs/run-YYYYMMDD-HHMMSSZ.log` containing the system + user prompt, every tool invocation (args + full return), the complete final message history (every model request/response, tool calls with parsed arguments), and token usage.

The agent's only outbound network needs are:
- `https://ollama.priv.mlops-club.org/v1` — model endpoint (Tailscale-only, so the host must be on the tailnet)
- `https://www.loseit.com` + `https://d3hsih69yn4d89.cloudfront.net` — Lose It! API
- `https://github.com/phitoduck/lose-it` — only at build time

## Tools exposed to the model

One tool per non-destructive loseit subcommand:

| Tool            | Subcommand        |
|-----------------|-------------------|
| `search`        | `loseit search`        |
| `describe_food` | `loseit describe-food` |
| `log`           | `loseit log` (with `dry_run` param) |
| `diary`         | `loseit diary`         |
| `whoami`        | `loseit whoami`        |

Deliberately **not** exposed: `delete` (destructive), `login` (out-of-band auth), `version`.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `OLLAMA_BASE_URL` | `https://ollama.priv.mlops-club.org/v1` | OpenAI-compatible endpoint |
| `OLLAMA_MODEL`    | `qwen3:8b` | Model name on Ollama |
| `AGENT_PROMPT`    | — | Prompt if not passed as argv |

## Sandbox guarantees

- Non-root user (`agent`, uid 1000)
- Read-only credential mount (`:ro`)
- No `--network host`, no Docker socket, no kubeconfig
- No host home dir mount beyond `~/.config/loseit`
