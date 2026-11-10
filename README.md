# TracePilot

TracePilot is a local incident investigator with a React and TypeScript dashboard. A FastAPI investigator uses Qwen through Ollama to query structured logs, Prometheus metrics, Jaeger traces, and service health with read-only tools. It returns observations with evidence citations, a suspected cause when supported, and a tool timeline. PostgreSQL stores completed and partial investigations.

The demonstration stack simulates gaming infrastructure. The Game Server creates player sessions and forwards gameplay events to the Event Processor, which validates and stores them. Python 3.12 and FastAPI run the services, OpenTelemetry sends traces to Jaeger, and Docker Compose runs the services and their dependencies.

## Run locally

Install Python 3.12, Docker with Compose, and Ollama. Start Ollama and run these commands from the repository root.

```sh
docker compose up --build -d --wait
ollama pull qwen3:4b
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

The Game Server uses port 8000 and the Event Processor uses port 8001 on localhost. View traces in [Jaeger](http://127.0.0.1:16686) and metrics in [Prometheus](http://127.0.0.1:9090/query). Compose provides local defaults. To customize them, copy [`.env.example`](.env.example) to `.env` and export matching settings before running the investigator.

In a separate terminal at the repository root, start the investigator.

```sh
source .venv/bin/activate
python -m uvicorn services.investigator.main:app --host 127.0.0.1 --port "${INVESTIGATOR_PORT:-8002}"
```

The investigator uses the host Docker CLI to read recent application logs. It handles one investigation at a time, with defaults of eight tool calls and 180 seconds. If Ollama's first tool-selection reply is incomplete or malformed, the investigator makes one retry within the same deadline and tool budget. Both attempts disable thinking. The initial request allows 1,024 output tokens, and the retry allows 256 and constrains the response to a tool name and arguments. Run `docker compose down` to stop the Compose services.

## Dashboard

With the Compose stack, Ollama, and the investigator running, use Node.js 22.12 or later to start the dashboard from the repository root.

```sh
cd frontend
npm ci
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173) to check service telemetry, browse saved investigations, and inspect findings and their evidence. Vite proxies API requests to the local investigator. If you changed its port, set `TRACEPILOT_INVESTIGATOR_URL` before starting Vite. Set `VITE_PROMETHEUS_URL` or `VITE_JAEGER_URL` if you changed the monitoring UI ports.

Run `npm test` for mocked frontend tests and `npm run build` for the type check and production build.

## Generate gameplay traffic

With Compose running, generate synthetic player sessions and match events against the Game Server on `127.0.0.1`. Refresh Overview after a few Prometheus scrapes (10 seconds each).

```sh
python3 scripts/generate_traffic.py --duration 60 --rate 5 --players 6
python3 scripts/generate_traffic.py --duration 60 --rate 5 --players 6 --failure-burst 25
```

The rate limits Game Server requests per second, including session creation. The optional burst is measured in seconds and must be shorter than the run. It temporarily enables the Event Processor's development `processor_error` mode, which returns HTTP 502 at the Game Server. The generator attempts to restore the previous setting on exit. Run it separately from other failure simulations because the control applies to all events. Pass custom Compose ports with `--game-port` and `--processor-port`.

## Reproduce and investigate an incident

In the first terminal, create a session and submit an event while the processor error is enabled. The Game Server returns HTTP 502, and both services log the supplied request ID.

```sh
SESSION_ID=$(curl -fsS http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' -d '{"player_id":"player-42"}' \
  | python -c 'import json, sys; print(json.load(sys.stdin)["id"])')
REQUEST_ID=$(python -c 'import uuid; print(uuid.uuid4())')

curl -fsS -X PUT http://127.0.0.1:8001/dev/failure \
  -H 'Content-Type: application/json' -d '{"mode":"processor_error"}'

curl -si "http://127.0.0.1:8000/sessions/$SESSION_ID/events" \
  -H 'Content-Type: application/json' -H "X-Request-ID: $REQUEST_ID" \
  -d '{"event_type":"score_updated","payload":{"score":42}}'

curl -fsS -X PUT http://127.0.0.1:8001/dev/failure \
  -H 'Content-Type: application/json' -d '{"mode":"none"}'
```

Other modes are `database_error`, which yields HTTP 503 and PostgreSQL SQLSTATE `22012`, and `delay`, which yields HTTP 504 with the default settings while the write can still commit. The development endpoints are enabled by default in Compose. Set `ENABLE_FAILURE_SIMULATION=false` to disable them.

Submit the request ID to the investigator. The API also accepts a trace ID or a description. Fetch a saved result with `GET /investigations/{investigation_id}` using the returned ID.

```sh
curl -fsS http://127.0.0.1:8002/investigations \
  -H 'Content-Type: application/json' \
  -d "{\"request_id\":\"$REQUEST_ID\",\"description\":\"Investigate this failed event submission.\"}"
```

Investigations can still finish with partial results or an undetermined cause. In a live run, recovery retrieved metrics after a token-limited first reply, but a later model call used the remaining deadline before findings were available. In another run, the log tool rejected a model-generated time window longer than 60 minutes. A request ID or trace ID helps correlate evidence. Aggregate metrics alone cannot establish the cause of a specific failure.

## Tests and evaluation

```sh
python -m pytest -q
TRACEPILOT_COMPOSE_SMOKE=1 python -m pytest -q tests/test_compose.py
TRACEPILOT_LIVE_INVESTIGATOR=1 python -m pytest -q -s tests/test_investigator_live.py
```

The default suite uses mocked model responses. Run the Compose smoke test with the stack running. The live investigator tests also require the investigator and Ollama.

```sh
python -m evaluation.run --repetitions 2 --output evaluation/results.json
```

The evaluation checks Qwen's findings against HTTP responses, logs, traces, and database writes for three simulated failures. The runner saves results even when a rubric check fails.

| Recorded run | Correct cause | Valid citations | Complete outcome | Mean time |
| --- | ---: | ---: | ---: | ---: |
| [Baseline](evaluation/baseline.json) | 6/6 | 6/6 | 3/6 | 29.3 s |
| [Follow-up](evaluation/results.json) | 5/6 | 6/6 | 4/6 | 86.9 s |
| [Timeout retest](evaluation/timeout-followup.json) | 2/2 | 2/2 | 2/2 | 81.2 s |

The follow-up included a generic processor diagnosis and two timeout findings that omitted the successful write. Both targeted timeout retests reported the 504 and completed write after verified write evidence was added to the model input. These runs cover a small set of controlled incidents. Findings depend on the available evidence, and the investigator can return partial results or an undetermined cause.
