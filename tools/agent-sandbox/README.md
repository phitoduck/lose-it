# agent-sandbox

Single-file pydantic-ai agent (`agent.py`) that drives the `loseit` CLI via a homelab-hosted `qwen3:8b` Ollama model. Runs in Docker with only the user's `~/.config/loseit/` mounted read-only.

## Quickstart

```bash
docker build -t loseit-agent tools/agent-sandbox

mkdir -p tools/agent-sandbox/runs
docker run --rm \
  -v ~/.config/loseit:/home/agent/.config/loseit:ro \
  -v "$PWD/tools/agent-sandbox/runs:/home/agent/runs" \
  loseit-agent "100g of guacamole as a snack"
```

Requires Tailscale up (model is at `https://ollama.priv.mlops-club.org/v1`). Per-run logs land at `runs/run-{UTC}.log`.

## Sample run

Prompt: `255g of a mix that was evenly split between asparagus, lentils, and red potatoes and I also had 100g of homemade guacamole`

Final output:

```
Logged 85g asparagus (19 cal), 85g lentils (99 cal), 85g potatoes (76 cal),
and 100g guacamole (203 cal). Total: 397 cal (snacks).
```

<details>
<summary>What the agent did under the hood (15 tool calls, 5 model turns, ~90s)</summary>

```
[search]        query="guacamole"
[search]        query="red potatoes"
[search]        query="lentils"
[search]        query="asparagus"

[describe_food] [5aa5…, bcc2…, 8dfd…]                # lentils — top 3 candidates
[describe_food] [fc32…, 977f…, 0e71…]                # asparagus
[describe_food] [ca6f…, 86d9…, affe…]                # red potatoes
[describe_food] [0479…, 5a06…, cc46…]                # guacamole

[diary]         on_date="2026-06-12"                 # snapshot before logging

[log_food]      0479… (Guacamole, brand=Homemade) → 100g
                ❌ "doesn't have a tablespoon→g conversion … pick a different food entry"
[log_food]      fc32… (Asparagus, Spears, Cooked) → 85g
                ✅ 19 cal
[log_food]      5aa5… (Beans, Lentils, Cooked) → 85g
                ✅ 99 cal
[log_food]      ca6f… (Potatoes, Red, Baby, Cooked) → 85g
                ✅ 76 cal
[log_food]      cc46… (Guacamole, brand=Chipotle) → 100g
                ✅ 203 cal     # re-picked an entry that supports grams

[diary]         on_date="2026-06-12"                 # final verification
```

**Usage:** `RunUsage(input_tokens=64871, output_tokens=4606, requests=5, tool_calls=15)`

The interesting moment is the guacamole retry. The first candidate (`0479…`, brand `Homemade`) is stored only in tablespoons with no gram-conversion factor. The tool's error response includes an explicit `guidance` field telling the model to re-search rather than do unit math. The model picks a different `food_id` (`cc46…`, brand `Chipotle`) which does support grams and logs cleanly.

</details>

## Tools exposed to the model

One per non-destructive `loseit` subcommand. `delete` and `login` are deliberately omitted.

| Tool            | Subcommand               |
|-----------------|--------------------------|
| `search`        | `loseit search`          |
| `describe_food` | `loseit describe-food`   |
| `log_food`      | `loseit log`             |
| `diary`         | `loseit diary`           |
| `whoami`        | `loseit whoami`          |

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `OLLAMA_BASE_URL` | `https://ollama.priv.mlops-club.org/v1` | OpenAI-compatible endpoint |
| `OLLAMA_MODEL`    | `qwen3:8b` | Model on Ollama |
| `AGENT_PROMPT`    | — | Prompt if not passed as argv |

## Sandbox guarantees

Non-root user, read-only credential mount, no host bind beyond `~/.config/loseit`, no Docker socket, no kubeconfig. Outbound network needs are limited to the Ollama endpoint and `loseit.com` / its CloudFront origin.
