"""Exercise the tool-use loop with a mocked Anthropic client. No API key needed.

    python selfcheck.py
"""
import json
import types

import nl_scanner


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_client(script):
    """script: list of responses; each is (stop_reason, [blocks])."""
    calls = {"n": 0}

    def create(**kwargs):
        stop_reason, blocks = script[calls["n"]]
        calls["n"] += 1
        return _Block(stop_reason=stop_reason, content=blocks, stop_details=None)

    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create)), calls


def test_single_tool_then_answer():
    tool_call = _Block(type="tool_use", id="t1", name="scan_tradeups",
                       input={"collection": "Dreams & Nightmares"})
    script = [
        ("tool_use", [tool_call]),
        ("end_turn", [_Block(type="text", text="41.9% ROI on the Space Cat contract.")]),
    ]
    client, calls = _fake_client(script)
    out = nl_scanner.ask(client, "profitable trade-ups?")
    assert "41.9%" in out, out
    assert calls["n"] == 2, "should loop exactly twice (tool call, then answer)"


def test_parallel_calls_and_tool_error():
    blocks = [
        _Block(type="tool_use", id="a", name="list_collections", input={}),
        _Block(type="tool_use", id="b", name="get_skin_price", input={"name": "no such skin"}),
    ]
    script = [
        ("tool_use", blocks),
        ("end_turn", [_Block(type="text", text="done")]),
    ]
    client, _ = _fake_client(script)
    # run_tool_calls should return one result per call, with is_error on the miss
    results = nl_scanner.run_tool_calls(blocks)
    assert len(results) == 2
    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True
    assert "Kilowatt" in json.loads(results[0]["content"])
    assert nl_scanner.ask(client, "x") == "done"


def test_refusal_path():
    client, _ = _fake_client([("refusal", [])])
    client.messages.create = lambda **kw: types.SimpleNamespace(
        stop_reason="refusal", content=[],
        stop_details=types.SimpleNamespace(explanation="nope"))
    assert nl_scanner.ask(client, "x").startswith("[refused]")


if __name__ == "__main__":
    test_single_tool_then_answer()
    test_parallel_calls_and_tool_error()
    test_refusal_path()
    print("selfcheck OK - tool-use loop, parallel calls, tool errors, refusal path")
