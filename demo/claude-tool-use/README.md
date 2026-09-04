# Natural-language layer for the scanner (Anthropic Tool Use)

A small demo that puts a plain-English front end on the CS2 trade-up scanner.
You ask a question; Claude decides which scanner function to call and with which
arguments (Anthropic **Tool Use** / function calling); the script runs it; Claude
turns the rows into a short answer. Every number in the answer comes from a tool
result -- the model is told never to invent prices or ROI figures.

## Run it

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...        # or: ant auth login
python nl_scanner.py --demo               # three canned questions
python nl_scanner.py                      # interactive
```

Example:

```
> Any profitable trade-ups in Dreams & Nightmares? Give me the numbers.
  -> scan_tradeups({"collection": "Dreams & Nightmares"})
The PP-Bizon | Space Cat contract costs $6.20 for the 10 inputs and returns about
$8.80, so +$2.60 profit at 41.9% ROI (break-even 70.5%). Two of the three
outcomes are wins: MP7 | Guerrilla (35%, $9.60) and Sawed-Off | Spirit Board
(25%, $12.30); the 40% MP9 | Starlight Protector drop is the loss.
```

## What it demonstrates

- The Anthropic Python SDK against `claude-opus-5`.
- **Tool Use**: four JSON-schema tools (`list_collections`, `scan_tradeups`,
  `find_anomalies`, `get_skin_price`), model-selected per question, with typed
  arguments and optional filter parameters.
- The request -> `tool_use` -> `tool_result` -> repeat loop **written out by hand**
  in `nl_scanner.py` (not the SDK's `tool_runner` helper) so the cycle is legible,
  including parallel tool calls returned in a single `tool_result` message and
  `is_error` results fed back to the model.
- A system prompt that constrains the model to tool-sourced facts only.

## Files

| File | Role |
|---|---|
| `nl_scanner.py` | SDK client, tool schemas, the manual tool-use loop, CLI |
| `scanner_api.py` | The four callable functions; each docstring names the real `src/` module it mirrors |
| `snapshot.py` | A frozen slice of scanner output so the demo runs offline (illustrative prices, not live) |
| `selfcheck.py` | Mocks the API to exercise the loop end to end; no key needed |
| `requirements.txt` | `anthropic` |

## How it maps to the full project

`scanner_api.py` reads `snapshot.py` so the demo needs nothing but an API key. In
the full pipeline the same four functions would delegate to `src/tradeup.py`
(trade-up EV and ranking), `src/anomalies.py` (underpriced listings), and
`src/steam_prices.py` + `src/thirdparty_prices.py` (cross-market pricing). The
tool contract -- names, arguments, return shape -- is the same either way.

Not wired to a paid workload: costs a few cents per question on `claude-opus-5`;
switch `MODEL` to `claude-sonnet-5` in `nl_scanner.py` to cut that.
