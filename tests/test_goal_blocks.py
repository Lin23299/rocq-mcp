"""Goal text blocks: structural clipping + content-block transport.

Deep proof states expand one goal to hundreds of KB.  Two behaviours are
pinned here:

1. ``interactive._format_goal`` leaves in-budget goals byte-identical to
   the legacy format, and clips oversized ones structurally — every
   hypothesis keeps its name, hypothesis/conclusion bodies are sliced
   head+tail with an explicit marker.
2. ``server._as_tool_result`` moves the large text fields (``goals``,
   ``goals_at_failure``, ``output``) into their own content blocks so
   real newlines survive the transport; the first block stays a
   single-line JSON envelope so tooling can still parse it.
"""

from __future__ import annotations

import json

import pytest

# Import order matters: interactive imports server back (pre-existing
# circular layout), so server must be imported first.
from rocq_mcp import interactive, server


class _Hyp:
    def __init__(self, names, ty, def_=None):
        self.names = names
        self.ty = ty
        self.def_ = def_


class _Goal:
    def __init__(self, hyps, ty):
        self.hyps = hyps
        self.ty = ty


class _Complete:
    def __init__(self, goals=(), stack=(), shelf=(), given_up=()):
        self.goals = list(goals)
        self.stack = list(stack)
        self.shelf = list(shelf)
        self.given_up = list(given_up)


def _big_goal(n_hyps, hyp_chars, concl_chars):
    return _Goal(
        [_Hyp(["H%d" % i], "x" * hyp_chars) for i in range(n_hyps)],
        "y" * concl_chars,
    )


def test_small_goal_is_byte_identical():
    g = _Goal([_Hyp(["n"], "nat"), _Hyp(["m"], "nat")], "n = m")
    assert interactive._format_goal(g, 12000) == "n : nat\nm : nat\n|-n = m"


def test_hyp_def_is_preserved_when_clipping():
    g = _Goal([_Hyp(["D"], "x" * 5000, def_="abbreviate"), _Hyp(["H"], "y" * 5000)], "z" * 20000)
    out = interactive._format_goal(g, 12000)
    assert "D := abbreviate :" in out
    assert "[clipped" in out


@pytest.mark.parametrize(
    "n_hyps,hyp_chars",
    [(10, 5000), (2, 30000), (50, 5000), (22, 2000)],
)
def test_oversized_goal_keeps_every_name(n_hyps, hyp_chars):
    g = _big_goal(n_hyps, hyp_chars, 30000)
    out = interactive._format_goal(g, 12000)
    assert "[clipped" in out
    assert len(out) <= 12600
    for i in range(n_hyps):
        assert "H%d" % i in out


def test_conclusion_tail_survives():
    g = _Goal([_Hyp(["H"], "x" * 5000)], "head" + "y" * 30000 + "POSTCONDITION")
    out = interactive._format_goal(g, 12000)
    assert out.rstrip().endswith("POSTCONDITION")


def test_clipping_disabled_returns_full_text():
    g = _big_goal(2, 5000, 30000)
    out = interactive._format_goal(g, -1)
    assert "clipped" not in out
    assert len(out) > 39000


def test_format_goals_multi_prefix_and_dict_input():
    g1 = _Goal([_Hyp(["H"], "x" * 5000)], "y" * 20000)
    g2 = _Goal([_Hyp(["H"], "x" * 5000)], "y" * 20000)
    out = interactive._format_goals([g1, g2], max_chars=8000)
    assert out.startswith("Goal 1:")
    assert "Goal 2:" in out
    assert len(out) <= 2 * 8400


def test_complete_goals_total_cap_and_override():
    gs = [_big_goal(1, 20000, 20000) for _ in range(4)]
    text = interactive._format_complete_goals(_Complete(goals=gs))
    assert len(text) <= 44000
    assert "[clipped" in text
    full = interactive._format_complete_goals(_Complete(goals=gs[:1]), max_chars=-1)
    assert "clipped" not in full


def test_complete_goals_none_is_empty():
    assert interactive._format_complete_goals(None) == ""


def test_split_text_blocks_splits_envelope_and_payload():
    blocks = server._split_text_blocks(
        json.dumps(
            {
                "success": True,
                "state_id": 5,
                "goals": "Goal 1:\nA : X\n|-P\n",
                "focus_depth": 1,
            }
        )
    )
    assert blocks is not None
    assert [b.type for b in blocks] == ["text", "text"]
    envelope = json.loads(blocks[0].text)
    assert envelope["state_id"] == 5
    assert envelope["goals_chars"] == 18
    assert "goals" not in envelope
    # First block: single-line JSON.  Second block: real newlines.
    assert blocks[0].text.count("\n") == 0
    assert blocks[1].text.count("\n") == 3


def test_split_text_blocks_output_field():
    blocks = server._split_text_blocks(json.dumps({"success": True, "output": "a\nb"}))
    assert blocks is not None
    assert [b.type for b in blocks] == ["text", "text"]
    assert blocks[1].text == "a\nb"
    assert json.loads(blocks[0].text)["output_chars"] == 3


def test_split_text_blocks_passthrough():
    assert server._split_text_blocks("not json") is None
    assert server._split_text_blocks(json.dumps({"success": True})) is None
    assert server._split_text_blocks(json.dumps(["a", "b"])) is None


@pytest.mark.skipif(server.ToolResult is None, reason="fastmcp without ToolResult")
async def test_middleware_transport_keeps_real_newlines():
    """The MCP transport must deliver the goal text as its own block."""
    from fastmcp import Client, FastMCP

    mcp = FastMCP("goal-blocks-test")
    mcp.add_middleware(server.TextBlockOutputMiddleware())

    @mcp.tool
    def demo() -> dict:
        return {"success": True, "state_id": 1, "goals": "Goal 1:\nA : X\n|-P"}

    @mcp.tool
    def plain() -> str:
        return "just text, not JSON"

    async with Client(mcp) as client:
        result = await client.call_tool("demo", {})
        plain_result = await client.call_tool("plain", {})
    texts = [b.text for b in result.content]
    assert len(texts) == 2
    assert texts[0].count("\n") == 0
    assert texts[1].count("\n") == 2
    assert json.loads(texts[0])["goals_chars"] == len(texts[1])
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    assert structured and structured["goals"] == "Goal 1:\nA : X\n|-P"
    # Non-JSON payloads pass through untouched.
    assert [b.text for b in plain_result.content] == ["just text, not JSON"]
