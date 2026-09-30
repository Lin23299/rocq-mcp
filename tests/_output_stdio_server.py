"""Small real-stdio MCP server for output-tool integration tests."""

import os
from pathlib import Path
from types import SimpleNamespace

from fastmcp import Context, FastMCP

from rocq_mcp import server
from rocq_mcp import interactive
from rocq_mcp.output_store import OutputStore


mcp = FastMCP("output-tools-stdio-test", lifespan=server.app_lifespan)
mcp.add_middleware(server.TextBlockOutputMiddleware())
mcp.tool(server.rocq_find_output)
mcp.tool(server.rocq_read_output)
mcp.tool(server.rocq_query)
mcp.tool(server.rocq_check)
mcp.tool(server.rocq_step_multi)
mcp.tool(server.rocq_list_output_segments)
mcp.tool(server.rocq_toc)
mcp.tool(server.rocq_notations)
mcp.tool(server.rocq_get_goal_part)
mcp.tool(server.rocq_get_goal_roster)
mcp.tool(server.rocq_start)


async def _query_fixture(**_kwargs):
    raw = "Q" * 139_000 + "QUERY_TAIL_SYMBOL"
    return {"success": True, "output": raw, "_all_feedback": raw,
            "_shown_feedback": raw, "_feedback_messages": 1, "_shown_messages": 1}


server.run_query = _query_fixture


async def _start_fixture(*, workspace, lifespan_state, **kwargs):
    if kwargs.get("theorem") == "reject":
        return {"success": False, "reason": "validation", "error": "actual start failure"}
    sid = interactive._state_add(SimpleNamespace(proof_finished=False), "Goal.v", "goal",
                                 workspace, None, None, 0)
    return {"success": True, "state_id": sid, "proof_finished": False,
            "goals": interactive._format_complete_goals(_goal_fixture(None))}


server.run_start = _start_fixture


async def _check_fixture(*, from_state, **_kwargs):
    if _kwargs.get("body") == "goal-mode.":
        preview = interactive._format_complete_goals(_goal_fixture(None))
        return {"success": True, "state_id": from_state, "from_state_id": from_state,
                "proof_finished": False, "commands_run": 1, "goals": preview,
                "_raw_feedback": [],
                "_feedback_workspace": os.environ["TEST_OUTPUT_WORKSPACE"]}
    first = "C" * 75_000 + "CHECK_FIRST_TAIL_ONLY_IN_SNAPSHOT"
    second = "CHECK_TAIL_ONLY_IN_FEEDBACK"
    return {"success": True, "state_id": from_state + 1,
            "from_state_id": from_state, "proof_finished": False,
            "commands_run": 2, "goals": "|- True",
            "feedback": [["first.", first[:50_000] + "... (truncated)"]],
            "_raw_feedback": [(0, "first.", first), (1, "second.", second)],
            "_feedback_workspace": os.environ["TEST_OUTPUT_WORKSPACE"]}


async def _step_fixture(*, from_state, **_kwargs):
    if _kwargs.get("tactics") == ["goal-mode."]:
        preview = interactive._format_complete_goals(_goal_fixture(None))
        whole = interactive._format_complete_goals(_goal_fixture(None), max_chars=-1)
        return {"success": True, "from_state_id": from_state,
                "results": [{"tactic": "goal-mode.", "success": True,
                             "proof_finished": False, "goals": preview}],
                "_raw_feedback": [],
                "_raw_candidate_goals": [(0, "goal-mode.", whole)],
                "_feedback_workspace": os.environ["TEST_OUTPUT_WORKSPACE"]}
    raw = "M" * 80_000 + "STEP_TAIL_ONLY_IN_FEEDBACK"
    return {"success": True, "from_state_id": from_state,
            "results": [{"tactic": "first.", "success": True,
                         "proof_finished": False, "goals": "|- True",
                         "feedback": raw[:50_000] + "... (truncated)"}],
            "_raw_feedback": [(0, "first.", raw)],
            "_feedback_workspace": os.environ["TEST_OUTPUT_WORKSPACE"]}


server.run_check = _check_fixture
server.run_step_multi = _step_fixture


async def _toc_fixture(**_kwargs):
    raw = "File: Sample.v\n" + "L" * 11_000 + "TOC_TAIL_FOR_STDIO"
    return {"success": True, "output": raw[:8_000] + "\n... (truncated)",
            "_all_output": raw, "_shown_output": raw[:8_000]}


async def _notations_fixture(**_kwargs):
    raw = "Notations found:\n" + "N" * 11_000 + "NOTATION_TAIL_FOR_STDIO"
    return {"success": True, "output": raw[:8_000] + "\n... (truncated)",
            "_all_output": raw, "_shown_output": raw[:8_000]}


server.run_toc = _toc_fixture
server.run_notations = _notations_fixture


def _goal_fixture(_state):
    focused = SimpleNamespace(
        hyps=[SimpleNamespace(names=["example"], ty="nat", def_=None)],
        ty="GOAL_START " + "G" * 72_000 + " GOAL_END_FROM_LIVE_STATE",
    )
    # Fault injection stays in this disposable fake-Pet stdio fixture.
    invalid = os.environ.get("TEST_OUTPUT_INVALID_GOAL")
    if invalid == "conclusion":
        focused.ty = "BAD\ud800"
    elif invalid == "hyp_name":
        focused.hyps[0].names = ["BAD\ud800"]
    elif invalid == "hidden_definition":
        focused.hyps = [SimpleNamespace(names=["safe"], ty="nat", def_=None) for _ in range(8)] + [
            SimpleNamespace(names=["hidden"], ty="nat", def_="BAD\ud800")]
    return SimpleNamespace(goals=[focused], stack=[], shelf=[], given_up=[])


class _GoalPet:
    @staticmethod
    def set_workspace(**_kwargs):
        pass

    complete_goals = staticmethod(_goal_fixture)


async def _goal_pet_call(fn, _state, _tool, **_kwargs):
    return fn(_GoalPet())


server._run_with_pet = _goal_pet_call


_production_output_store = server._get_output_store


def _test_output_store(lifespan_state, workspace=""):
    # Only this disposable stdio fixture may use pytest's /tmp cache root.
    workspace = workspace or os.environ["TEST_OUTPUT_WORKSPACE"]
    if lifespan_state.get("output_store") is None and lifespan_state.get("output_stdio"):
        lifespan_state["output_store"] = OutputStore(
            Path(os.environ["ROCQ_MCP_OUTPUT_ROOT"]), workspace=workspace,
            allow_test_temp_root=True,
        )
    return _production_output_store(lifespan_state, workspace)


server._get_output_store = _test_output_store


@mcp.tool
def seed_goal(ctx: Context) -> dict:
    workspace = os.environ["TEST_OUTPUT_WORKSPACE"]
    sid = interactive._state_add(
        SimpleNamespace(proof_finished=False), "Goal.v", "goal", workspace,
        None, None, 0,
    )
    return {"state_id": sid, "service_generation": server._output_owner(ctx)}


@mcp.tool
def giant_unmodelled_diagnostic(state_id: int) -> dict:
    """Exercise the final transport guard on an unrelated result shape."""
    return {
        "success": False, "reason": "tactic_failed", "state_id": state_id,
        "proof_finished": False,
        "diagnostics": [{"error": "E" * 90_000 + "DIAGNOSTIC_TAIL"}],
    }


@mcp.tool
def seed(text: str, ctx: Context) -> dict:
    store = server._get_output_store(ctx.lifespan_context)
    return store.save(text, owner_session=server._output_owner(ctx),
                      workspace=os.environ["TEST_OUTPUT_WORKSPACE"], origin="test")


@mcp.tool
def whoami(ctx: Context) -> dict:
    connection = getattr(ctx.session, "_connection", None)
    return {
        "session_id": ctx.session_id,
        "client_id": ctx.client_id,
        "connection": id(connection) if connection is not None else None,
        "read_stream": id(getattr(ctx.session, "_read_stream", None)),
        "write_stream": id(getattr(ctx.session, "_write_stream", None)),
        "lifespan": id(ctx.lifespan_context),
    }


if __name__ == "__main__":
    server._output_stdio_mode = True
    mcp.run(transport="stdio")
