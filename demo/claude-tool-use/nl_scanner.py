#!/usr/bin/env python3
"""Ask the CS2 trade-up scanner questions in plain English.

Claude reads your question, decides which scanner function(s) to call and with
which arguments (Anthropic Tool Use / function calling), the script runs them,
and Claude turns the rows into a short answer. It never invents a price or an ROI
number -- every figure comes from a tool result.

    export ANTHROPIC_API_KEY=sk-ant-...
    pip install anthropic
    python nl_scanner.py --demo        # three canned questions
    python nl_scanner.py               # interactive; blank line or Ctrl-D to quit

The tool-call loop below is written out by hand (rather than using the SDK's
tool_runner helper) so the request -> tool_use -> tool_result -> repeat cycle is
visible. See scanner_api.py for the four tools; snapshot.py for the demo data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import anthropic

from scanner_api import DISPATCH

# claude-opus-5 by default; override if your key targets a different model
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")

SYSTEM = (
    "You are the query assistant for a Counter-Strike 2 trade-up and market scanner. "
    "Answer only from the tools provided. Never invent prices, ROI figures, probabilities, "
    "or skin names -- if a tool returns nothing useful, say so plainly. "
    "Prefer one tool call unless the question clearly needs more. "
    "Keep answers to a few sentences and lead with the numbers that matter "
    "(cost, revenue, ROI %, break-even %, discount %)."
)

TOOLS = [
    {
        "name": "list_collections",
        "description": "List the CS2 weapon collections available to scan.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scan_tradeups",
        "description": (
            "Ranked trade-up contracts for one collection: cost to buy the 10 inputs, "
            "expected revenue, profit, ROI %, break-even %, and every possible output "
            "skin with its drop probability and whether that outcome is a win or a loss."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string", "description": "Collection name, e.g. 'Kilowatt'."},
                "max_cost": {"type": "number", "description": "Only contracts costing this many USD or less."},
                "min_roi_pct": {"type": "number", "description": "Only contracts with ROI at or above this percent."},
            },
            "required": ["collection"],
        },
    },
    {
        "name": "find_anomalies",
        "description": (
            "Underpriced market listings: skins whose lowest ask sits well below the "
            "rolling median, with the discount %, 24h sales volume, and which market."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "min_volume_24h": {"type": "integer", "description": "Minimum 24h sales volume to include."},
                "min_discount_pct": {"type": "number", "description": "Minimum discount below median, in percent."},
            },
        },
    },
    {
        "name": "get_skin_price",
        "description": "Cross-market price (Steam, Skinport, CSFloat) for one skin by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skin name or a distinctive part of it."}},
            "required": ["name"],
        },
    },
]


def run_tool_calls(blocks) -> list[dict]:
    """Execute every tool_use block Claude returned; one tool_result per call."""
    results = []
    for block in blocks:
        if block.type != "tool_use":
            continue
        fn = DISPATCH.get(block.name)
        try:
            out = fn(**block.input) if fn else {"error": f"unknown tool {block.name!r}"}
            is_error = isinstance(out, dict) and "error" in out
        except Exception as exc:  # surface the failure to Claude, don't crash the loop
            out, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
        print(f"  -> {block.name}({json.dumps(block.input)})")
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(out),
            "is_error": is_error,
        })
    return results


def ask(client: anthropic.Anthropic, question: str) -> str:
    messages = [{"role": "user", "content": question}]
    while True:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "tool_use":
            messages.append({"role": "user", "content": run_tool_calls(resp.content)})
            continue
        if resp.stop_reason == "refusal":
            return "[refused] " + (resp.stop_details.explanation if resp.stop_details else "no detail")
        return "".join(b.text for b in resp.content if b.type == "text").strip()


def make_client() -> anthropic.Anthropic:
    try:
        return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY / ant auth profile
    except anthropic.AnthropicError as exc:
        sys.exit(f"Could not create the Anthropic client: {exc}\n"
                 "Set ANTHROPIC_API_KEY or run `ant auth login`.")


DEMO_QUESTIONS = [
    "Which collections can I scan?",
    "Any profitable trade-ups in Dreams & Nightmares? Give me the numbers.",
    "Show me the biggest underpriced listings with at least 100 sales in the last day.",
]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--demo", action="store_true", help="run three canned questions and exit")
    args = p.parse_args()
    client = make_client()

    if args.demo:
        for q in DEMO_QUESTIONS:
            print(f"\n> {q}")
            print(ask(client, q))
        return

    print("Ask the scanner a question (blank line or Ctrl-D to quit).")
    while True:
        try:
            q = input("\n> ").strip()
        except EOFError:
            break
        if not q:
            break
        try:
            print(ask(client, q))
        except anthropic.RateLimitError:
            print("[rate limited] wait a moment and try again")
        except anthropic.APIStatusError as exc:
            print(f"[api error {exc.status_code}] {exc.message}")


if __name__ == "__main__":
    main()
