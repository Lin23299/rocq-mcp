"""Read-only goal/hyp selectors stay bound to a live registered state."""

from __future__ import annotations

from collections import deque
import json
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import rocq_mcp.interactive as interactive
import rocq_mcp.server as server
from rocq_mcp.output_store import OutputStore, OutputStoreError


def _dict_goal(ty, *, hyp_type="nat", definition=None):
    hyp = {"names": ["x", "x_alias"], "ty": hyp_type}
    if definition is not None:
        hyp["def"] = definition
    return {"info": {}, "hyps": [hyp], "ty": ty}


@pytest.fixture
def goals(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    state = SimpleNamespace(proof_finished=False)
    sid = interactive._state_add(state, "Goals.v", "t", str(ws), None, None, 0)
    focused = SimpleNamespace(
        hyps=[SimpleNamespace(names=["x", "x_alias"], ty="nat", def_="x + 1")],
        ty="P " + "🙂" * 2_500 + "GOAL_TAIL",
    )
    complete = SimpleNamespace(
        goals=[focused],
        stack=[([_dict_goal("LEFT_SIDE")], [_dict_goal("RIGHT_SIDE")])],
        shelf=[_dict_goal("SHELVED")], given_up=[_dict_goal("GIVEN_UP")],
    )
    pet = SimpleNamespace(complete_goals=lambda _state: complete)

    async def run_with_pet(fn, _state, _tool, **_kwargs):
        return fn(pet)

    monkeypatch.setattr(server, "_run_with_pet", run_with_pet)
    monkeypatch.setattr(server, "_set_workspace_if_needed", lambda *args: None)
    monkeypatch.setattr(interactive, "_check_staleness", lambda *args: None)
    ctx = SimpleNamespace(lifespan_context={
        "pet_timeout": 30.0, "recent_errors": deque(maxlen=20),
        "output_stdio": True, "output_principal": "one-stdio-client",
    })
    return sid, focused, ctx, pet


def _assert_invalid_goal_view(result, sid, location):
    assert result["success"] is True
    assert result["state_id"] == sid and result["proof_finished"] is False
    assert result["service_generation"] == "one-stdio-client"
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "invalid_text"
    assert "reason" not in result
    assert not {"text", "goals", "goal_ref", "digest", "status", "start", "end"}.intersection(result)
    assert result["views"][location] == {
        "location": location, "kind": "unavailable", "complete": False,
        "total_bytes": None, "shown_bytes": 0, "reason": "invalid_text",
    }
    # A receipt containing any surrogate would fail the real UTF-8 boundary.
    json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert server._output_view_fits(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("part,field", [("conclusion", "ty"), ("names", "names"),
                                         ("type", "ty"), ("definition", "def_")])
@pytest.mark.parametrize("bad", ["\ud800", "\udfff"])
async def test_selected_goal_part_invalid_text_preserves_live_state(goals, part, field, bad):
    sid, focused, ctx, _pet = goals
    before = dict(interactive._state_table)
    target = focused if part == "conclusion" else focused.hyps[0]
    setattr(target, field, ["BAD" + bad] if part == "names" else "BAD" + bad)
    response = await server.rocq_get_goal_part(
        sid, "focused", 1, part, service_generation="one-stdio-client", ctx=ctx,
        hyp_index=None if part == "conclusion" else 1,
    )
    _assert_invalid_goal_view(response, sid, "text")
    assert interactive._state_table == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["part", "roster"])
@pytest.mark.parametrize("group", ["focused", "stack_left", "stack_right", "shelved", "given_up"])
@pytest.mark.parametrize("corrupt", ["conclusion", "name", "type", "definition", "hidden_name"])
async def test_full_goal_digest_invalid_text_is_caught_across_all_groups(goals, tool, group, corrupt):
    sid, focused, ctx, pet = goals
    complete = pet.complete_goals(None)
    depth = 1 if group.startswith("stack_") else None
    goal = interactive._goal_group(complete, group, depth)[0]
    # Focused is an object; the other four groups exercise real JSON decoding.
    if isinstance(goal, dict):
        goal["ty"] = "SAFE_CONCLUSION"
        hyp = goal["hyps"][0]
        if corrupt == "conclusion":
            goal["ty"] = "BAD\ud800"
        elif corrupt == "hidden_name":
            goal["hyps"] = [{"names": ["safe"], "ty": "nat"} for _ in range(8)] + [
                {"names": ["BAD\ud800"], "ty": "nat"}]
        else:
            hyp[{"name": "names", "type": "ty", "definition": "def"}[corrupt]] = (
                ["BAD\ud800"] if corrupt == "name" else "BAD\ud800")
    else:
        focused.ty = "SAFE_CONCLUSION"
        if corrupt == "conclusion":
            focused.ty = "BAD\ud800"
        elif corrupt == "hidden_name":
            focused.hyps = [SimpleNamespace(names=["safe"], ty="nat", def_=None) for _ in range(8)] + [
                SimpleNamespace(names=["BAD\ud800"], ty="nat", def_=None)]
        else:
            setattr(focused.hyps[0], {"name": "names", "type": "ty", "definition": "def_"}[corrupt],
                    ["BAD\ud800"] if corrupt == "name" else "BAD\ud800")
    before = dict(interactive._state_table)
    if tool == "part":
        response = await server.rocq_get_goal_part(
            sid, group, 1, "conclusion", depth=depth,
            service_generation="one-stdio-client", ctx=ctx,
        )
    else:
        response = await server.rocq_get_goal_roster(
            sid, "one-stdio-client", group=group, depth=depth, ctx=ctx,
        )
    _assert_invalid_goal_view(response, sid, "text" if tool == "part" else "roster")
    assert interactive._state_table == before


@pytest.mark.asyncio
async def test_roster_invalid_later_goal_does_not_leak_partial_page_or_fake_empty_goals(goals):
    sid, focused, ctx, pet = goals
    complete = pet.complete_goals(None)
    complete.goals = [focused, _dict_goal("BAD\ud800")]
    response = await server.rocq_get_goal_roster(sid, "one-stdio-client", ctx=ctx)
    _assert_invalid_goal_view(response, sid, "roster")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["part", "roster"])
async def test_invalid_goal_view_retains_clamped_timeout_metadata(goals, tool):
    sid, focused, ctx, _pet = goals
    focused.ty = "BAD\ud800"
    timeout = server.ROCQ_QUERY_TIMEOUT_CAP + 1
    if tool == "part":
        result = await server.rocq_get_goal_part(
            sid, "focused", 1, "conclusion", service_generation="one-stdio-client", timeout=timeout, ctx=ctx,
        )
    else:
        result = await server.rocq_get_goal_roster(sid, "one-stdio-client", timeout=timeout, ctx=ctx)
    _assert_invalid_goal_view(result, sid, "text" if tool == "part" else "roster")
    assert result["clamped_timeout"] == server.ROCQ_QUERY_TIMEOUT_CAP


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["selector", "stale", "no_goal_data"])
@pytest.mark.parametrize("tool", ["part", "roster"])
async def test_invalid_text_does_not_mask_selector_state_or_pet_read_failure(goals, monkeypatch, scenario, tool):
    sid, focused, ctx, pet = goals
    focused.ty = "BAD\ud800"
    if scenario == "stale":
        monkeypatch.setattr(interactive, "_check_staleness", lambda *_: "dependency rebuilt")
    elif scenario == "no_goal_data":
        pet.complete_goals = lambda _: None
    if tool == "part":
        result = await server.rocq_get_goal_part(
            sid, "focused", 999 if scenario == "selector" else 1, "conclusion",
            service_generation="one-stdio-client", ctx=ctx,
        )
    else:
        result = await server.rocq_get_goal_roster(
            sid, "one-stdio-client", cursor=999 if scenario == "selector" else 0, ctx=ctx,
        )
    assert result["success"] is False and result.get("view_error_code") != "invalid_text"
    assert not {"text", "goals", "goal_ref"}.intersection(result)
    if scenario == "stale":
        assert result["reason"] == "state_invalidated"
    elif scenario == "no_goal_data":
        assert result["reason"] == "unavailable"


@pytest.mark.asyncio
async def test_large_live_conclusion_chunk_roundtrip_does_not_register_states(goals):
    sid, focused, ctx, _pet = goals
    before = len(interactive._state_table)
    cursor, data = 0, []
    goal_ref = None
    while True:
        result = await server.rocq_get_goal_part(
            sid, "focused", 1, "conclusion", service_generation="one-stdio-client",
            offset_bytes=cursor, ctx=ctx,
        )
        assert result["success"] and result["state_id"] == sid
        assert result["selector"]["goal_index"] == 1
        assert result["goal_ref"]["document_identity"]
        assert result["goal_ref"]["owner_session"] == "one-stdio-client"
        if goal_ref is None:
            goal_ref = result["goal_ref"]
        else:
            assert result["goal_ref"] == goal_ref
        assert result["proof_finished"] is False
        assert server._output_view_fits(result)
        if result["has_more"] or cursor:
            assert result["view_status"] == "partial_recoverable"
            assert result["views"]["source"]["state_id"] == sid
        data.append(result["text"].encode("utf-8"))
        if not result["has_more"]:
            break
        assert result["end"] > cursor
        cursor = result["end"]
    assert b"".join(data) == focused.ty.encode("utf-8")
    assert len(interactive._state_table) == before


@pytest.mark.asyncio
async def test_hyp_names_type_local_definition_and_all_goal_groups(goals):
    sid, _focused, ctx, _pet = goals
    for part, expected in (("names", "x, x_alias"), ("type", "nat"),
                           ("definition", "x + 1")):
        result = await server.rocq_get_goal_part(
            sid, "focused", 1, part, service_generation="one-stdio-client",
            hyp_index=1, ctx=ctx,
        )
        assert result["success"] and result["text"] == expected
    for group, depth, expected in (("stack_left", 1, "LEFT_SIDE"),
                                   ("stack_right", 1, "RIGHT_SIDE"),
                                   ("shelved", None, "SHELVED"),
                                   ("given_up", None, "GIVEN_UP")):
        selected = await server.rocq_get_goal_part(
            sid, group, 1, "conclusion", service_generation="one-stdio-client",
            depth=depth, ctx=ctx,
        )
        assert selected["success"] and selected["text"] == expected


@pytest.mark.asyncio
async def test_invalid_selector_expiry_and_stale_dependencies_are_not_empty_goals(goals, monkeypatch):
    sid, _focused, ctx, _pet = goals
    for kwargs in (
        {"group": "stack_left", "depth": 0},
        {"group": "focused", "goal_index": 999},
        {"group": "focused", "part": "type", "hyp_index": 999},
        {"group": "focused", "part": "definition", "hyp_index": 999},
        {"group": "focused", "part": "conclusion", "hyp_index": 1},
        {"group": "focused", "part": "type"},
    ):
        arguments = {"group": "focused", "goal_index": 1, "part": "conclusion", **kwargs}
        response = await server.rocq_get_goal_part(
            sid, service_generation="one-stdio-client", ctx=ctx, **arguments,
        )
        assert response["success"] is False
        assert "text" not in response
    interactive._state_remove(sid)
    expired = await server.rocq_get_goal_part(
        sid, "focused", 1, "conclusion", service_generation="one-stdio-client", ctx=ctx,
    )
    assert expired["success"] is False and "text" not in expired


@pytest.mark.asyncio
async def test_dependency_warning_fails_closed(goals, monkeypatch):
    sid, _focused, ctx, _pet = goals
    monkeypatch.setattr(interactive, "_check_staleness", lambda *args: "dependency rebuilt")
    response = await server.rocq_get_goal_part(
        sid, "focused", 1, "conclusion", service_generation="one-stdio-client", ctx=ctx,
    )
    assert response["success"] is False and response["reason"] == "state_invalidated"
    assert "dependency rebuilt" in response["stale_warning"]
    assert "text" not in response


@pytest.mark.asyncio
async def test_goal_part_rejects_internal_utf8_offsets_and_too_small_next_codepoint(goals):
    sid, focused, ctx, pet = goals
    focused.ty = "🙂next"
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[focused], stack=[], shelf=[], given_up=[],
    )
    for offset, width in ((1, 8), (0, 1)):
        rejected = await server.rocq_get_goal_part(
            sid, "focused", 1, "conclusion", service_generation="one-stdio-client",
            offset_bytes=offset, max_bytes=width, ctx=ctx,
        )
        assert rejected["success"] is False and "text" not in rejected
    aligned = await server.rocq_get_goal_part(
        sid, "focused", 1, "conclusion", service_generation="one-stdio-client",
        offset_bytes=4, ctx=ctx,
    )
    assert aligned["success"] and aligned["text"] == "next"


@pytest.mark.asyncio
async def test_reused_numeric_state_id_does_not_authorize_previous_generation(goals):
    sid, _focused, ctx, _pet = goals
    for tool, options in (
        (server.rocq_get_goal_part,
         {"group": "focused", "goal_index": 1, "part": "conclusion"}),
        (server.rocq_get_goal_roster, {}),
    ):
        wrong = await tool(sid, service_generation="previous-stdio-process", ctx=ctx, **options)
        assert wrong["success"] is False
        assert wrong["view_error_code"] == "expired"
        assert "text" not in wrong and "goals" not in wrong


@pytest.mark.asyncio
async def test_roster_pages_large_goal_and_focus_stack_without_renumbering(goals):
    sid, focused, ctx, pet = goals
    focused.hyps[0].names = ["a" * 2_000]
    complete = SimpleNamespace(
        goals=[focused] * 13,
        stack=[([_dict_goal(f"LEFT_{i}")], []) for i in range(23)],
        shelf=[_dict_goal("SHELF")], given_up=[],
    )
    pet.complete_goals = lambda _state: complete
    before = len(interactive._state_table)
    first = await server.rocq_get_goal_roster(sid, "one-stdio-client", ctx=ctx)
    assert first["success"] and first["total_goals"] == 13
    assert first["focus_depth"] == 23 and first["stack_has_more"]
    assert first["view_status"] == "partial_recoverable"
    assert first["views"]["roster"]["total_bytes"] is None
    assert first["next_cursor"] == 5
    assert first["goals"][0]["goal_ref"]["goal_index"] == 1
    assert len(first["goals"][0]["hypotheses_preview"][0]["names_preview"]) <= 64
    assert server._output_view_fits(first)
    remaining = await server.rocq_get_goal_roster(
        sid, "one-stdio-client", cursor=first["next_cursor"], ctx=ctx,
    )
    assert remaining["goals"][0]["goal_index"] == 6
    last = await server.rocq_get_goal_roster(sid, "one-stdio-client", cursor=10, ctx=ctx)
    assert last["goals"][-1]["goal_index"] == 13 and not last["has_more"]
    assert last["roster_truncated"] and last["view_status"] == "partial_recoverable"
    stack_goal = await server.rocq_get_goal_part(
        sid, "stack_left", 1, "conclusion", service_generation="one-stdio-client",
        depth=21, ctx=ctx,
    )
    assert stack_goal["success"] and stack_goal["text"] == "LEFT_20"
    stack_roster = await server.rocq_get_goal_roster(
        sid, "one-stdio-client", group="stack_left", depth=21, ctx=ctx,
    )
    assert stack_roster["success"] and stack_roster["total_goals"] == 1
    assert stack_roster["goals"][0]["conclusion_preview"] == "LEFT_20"
    assert len(interactive._state_table) == before


@pytest.mark.asyncio
async def test_twelve_hypotheses_beyond_roster_preview_are_enumerable_by_index(goals):
    sid, focused, ctx, pet = goals
    focused.hyps = [
        SimpleNamespace(names=["same_name" if index % 2 else f"h_{index}"],
                        ty=f"T_{index}", def_=None)
        for index in range(12)
    ]
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[focused], stack=[], shelf=[], given_up=[],
    )
    roster = await server.rocq_get_goal_roster(sid, "one-stdio-client", ctx=ctx)
    assert roster["success"] and roster["goals"][0]["hypotheses_total"] == 12
    assert len(roster["goals"][0]["hypotheses_preview"]) == 8
    assert not roster["has_more"] and not roster["stack_has_more"]
    assert roster["view_status"] == "partial_recoverable"
    assert roster["views"]["roster"]["kind"] == "live_state"
    reference = roster["goals"][0]["goal_ref"]
    for number in range(9, 13):
        selected = await server.rocq_get_goal_part(
            sid, "focused", 1, "names", hyp_index=number,
            service_generation="one-stdio-client", ctx=ctx,
        )
        assert selected["success"] and selected["goal_ref"] == reference
        assert selected["text"] == focused.hyps[number - 1].names[0]


@pytest.mark.asyncio
async def test_single_long_name_or_conclusion_cannot_make_roster_look_complete(goals):
    sid, focused, ctx, pet = goals
    focused.ty = "True"
    focused.hyps = [SimpleNamespace(names=["N" * 2_000], ty="nat", def_=None)]
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[focused], stack=[], shelf=[], given_up=[],
    )
    names = await server.rocq_get_goal_roster(sid, "one-stdio-client", ctx=ctx)
    assert names["success"] and not names["has_more"] and not names["stack_has_more"]
    assert names["roster_truncated"] and names["view_status"] == "partial_recoverable"
    assert names["views"]["roster"]["kind"] == "live_state"
    assert names["goals"][0]["hypotheses_preview_incomplete"] is True
    assert names["goals"][0]["conclusion_preview_incomplete"] is False
    exact_name = await server.rocq_get_goal_part(
        sid, "focused", 1, "names", hyp_index=1,
        service_generation="one-stdio-client", ctx=ctx,
    )
    assert exact_name["success"] and exact_name["text"] == "N" * 2_000

    focused.hyps[0].names = ["n"]
    focused.ty = "C" * 500
    conclusion = await server.rocq_get_goal_roster(sid, "one-stdio-client", ctx=ctx)
    assert conclusion["roster_truncated"] and conclusion["view_status"] == "partial_recoverable"
    assert conclusion["goals"][0]["hypotheses_preview_incomplete"] is False
    assert conclusion["goals"][0]["conclusion_preview_incomplete"] is True
    exact_conclusion = await server.rocq_get_goal_part(
        sid, "focused", 1, "conclusion", service_generation="one-stdio-client", ctx=ctx,
    )
    assert exact_conclusion["success"] and exact_conclusion["text"] == "C" * 500


@pytest.mark.asyncio
async def test_roster_rejects_bad_group_depth_and_expired_state(goals):
    sid, _focused, ctx, _pet = goals
    for group, depth, cursor in (("stack_right", None, 0), ("focused", 2, 0),
                                 ("focused", None, 999)):
        response = await server.rocq_get_goal_roster(
            sid, "one-stdio-client", group=group, depth=depth, cursor=cursor, ctx=ctx,
        )
        assert response["success"] is False
        assert "goals" not in response
    interactive._state_remove(sid)
    expired = await server.rocq_get_goal_roster(sid, "one-stdio-client", ctx=ctx)
    assert expired["success"] is False


@pytest.mark.asyncio
async def test_start_check_default_large_goal_gets_live_selector(goals, monkeypatch):
    sid, focused, ctx, pet = goals
    focused.ty = "BEG" + "x" * 80_000 + "GOAL_END"
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[focused], stack=[], shelf=[], given_up=[],
    )
    original_text = interactive._format_complete_goals(pet.complete_goals(None))
    assert "[clipped " in original_text
    projected = await server._live_goal_view(
        {"success": True, "state_id": sid, "goals": original_text},
        ctx=ctx, state_id=sid,
    )
    assert projected["view_status"] == "partial_recoverable"
    assert projected["views"]["goals"]["kind"] == "live_state"
    assert projected["views"]["goals"]["total_bytes"] is None
    roster = await server.rocq_get_goal_roster(sid, "one-stdio-client", ctx=ctx)
    assert roster["focused_goals"] == 1
    assert server._output_view_fits(projected)
    conclusion = await server.rocq_get_goal_part(
        sid, "focused", 1, "conclusion", service_generation="one-stdio-client", ctx=ctx,
    )
    assert conclusion["success"] and conclusion["total_bytes"] == len(focused.ty)


@pytest.mark.asyncio
async def test_original_820kb_single_hypothesis_survives_default_clipping(goals):
    import hashlib

    sid, focused, ctx, pet = goals
    focused.hyps[0].ty = "λ" * 410_000 + "HYP_TAIL"
    focused.ty = "True"
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[focused], stack=[], shelf=[], given_up=[],
    )
    display = interactive._format_complete_goals(pet.complete_goals(None))
    assert "[clipped " in display
    view = await server._live_goal_view(
        {"success": True, "state_id": sid, "goals": display}, ctx=ctx, state_id=sid,
    )
    assert view["views"]["goals"]["kind"] == "live_state"
    assert view["views"]["goals"]["total_bytes"] is None
    raw = focused.hyps[0].ty.encode("utf-8")
    position = len(raw) - len("HYP_TAIL")
    chunk = await server.rocq_get_goal_part(
        sid, "focused", 1, "type", hyp_index=1,
        service_generation="one-stdio-client", offset_bytes=position, ctx=ctx,
    )
    assert chunk["success"] and chunk["text"] == "HYP_TAIL"
    assert chunk["total_bytes"] == len(raw) and chunk["digest"] == hashlib.sha256(raw).hexdigest()
    assert server._output_view_fits(chunk)


@pytest.mark.asyncio
async def test_step_multi_candidate_is_snapshot_not_a_registered_child(goals, tmp_path):
    sid, focused, ctx, pet = goals
    focused.ty = "BIG_GOAL_" + "q" * 70_000 + "CANDIDATE_END"
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[focused], stack=[], shelf=[], given_up=[],
    )
    pet.run = lambda _state, _tactic, **_kw: SimpleNamespace(
        proof_finished=False, feedback=[],
    )
    before = len(interactive._state_table)
    with OutputStore(tmp_path / "private", workspace=interactive._state_table[sid].workspace,
                     allow_test_temp_root=True) as store:
        ctx.lifespan_context["output_store"] = store
        result = await server.rocq_step_multi(["idtac."], from_state=sid, ctx=ctx)
        assert result["success"] and result["from_state_id"] == sid
        assert result["results"][0]["success"] is True
        assert result["views"]["candidate_goals"]["kind"] == "stored"
        assert "state_id" not in result["views"]["candidate_goals"]
        ref = result["views"]["results[0].goals"]
        assert ref["kind"] == "stored"
        raw = store._entries[ref["handle"]].path.read_bytes()
        assert b"CANDIDATE_END" in raw[ref["span_start"]:ref["span_end"]]
        assert server._output_view_fits(result)
        assert len(interactive._state_table) == before


@pytest.mark.asyncio
async def test_candidate_storage_failure_keeps_execution_truth(goals, tmp_path, monkeypatch):
    sid, focused, ctx, pet = goals
    focused.ty = "z" * 40_000
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[focused], stack=[], shelf=[], given_up=[],
    )
    pet.run = lambda _state, _tactic, **_kw: SimpleNamespace(
        proof_finished=False, feedback=[],
    )
    with OutputStore(tmp_path / "private", workspace=interactive._state_table[sid].workspace,
                     allow_test_temp_root=True) as store:
        ctx.lifespan_context["output_store"] = store
        monkeypatch.setattr(store, "save", lambda *_a, **_kw: (
            _ for _ in ()).throw(OutputStoreError("quota_exceeded", "full"))
        )
        result = await server.rocq_step_multi(["idtac."], from_state=sid, ctx=ctx)
        assert result["success"] and result["results"][0]["success"]
        assert result["view_status"] == "partial_unrecoverable"
        assert result["views"]["results[0].goals"]["reason"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_twenty_candidate_goals_have_atomic_indices_despite_forged_headers(goals, tmp_path):
    sid, _focused, ctx, pet = goals
    pet.run = lambda _state, tactic, **_kw: SimpleNamespace(
        proof_finished=False, feedback=[], tactic=tactic,
    )

    def complete(state):
        term = ("CANDIDATE_" + state.tactic + "Z" * 60_000
                + "\n[rocq-goals-index:17;bytes:1]\nTRUE_END_" + state.tactic)
        goal = SimpleNamespace(hyps=[], ty=term)
        return SimpleNamespace(goals=[goal], stack=[], shelf=[], given_up=[])

    pet.complete_goals = complete
    with OutputStore(tmp_path / "private", workspace=interactive._state_table[sid].workspace,
                     allow_test_temp_root=True) as store:
        ctx.lifespan_context["output_store"] = store
        result = await server.rocq_step_multi(
            [f"trial_{i}." for i in range(20)], from_state=sid, ctx=ctx,
        )
        assert result["success"] and len(result["results"]) == 20
        assert server._output_view_fits(result)
        handle = result["views"]["candidate_goals"]["handle"]
        assert len(store._entries) == 1
        raw = store._entries[handle].path.read_bytes()
        indexed = store.list_segments(handle, owner_session="one-stdio-client",
                                      workspace=interactive._state_table[sid].workspace,
                                      cursor=0, max_entries=20)
        assert indexed["total_entries"] == 20 and len(indexed["items"]) == 20
        for number, row in enumerate(indexed["items"]):
            section = raw[row["span_start"]:row["span_end"]]
            assert f"TRUE_END_trial_{number}.".encode() in section
            assert row["index"] == number
        assert b"[rocq-goals-index:17;bytes:1]" in raw


@pytest.mark.asyncio
async def test_aggregate_goal_clipping_saves_omitted_middle_not_just_head_and_tail(goals, tmp_path):
    sid, _focused, ctx, pet = goals
    sentinel = "MIDDLE_ONLY_IN_FULL_GOALS_SNAPSHOT"
    many = [SimpleNamespace(hyps=[], ty="Q" * 3_000 + (sentinel if i == 7 else ""))
            for i in range(17)]
    complete = SimpleNamespace(goals=many, stack=[], shelf=[], given_up=[])
    pet.complete_goals = lambda _state: complete
    pet.run = lambda _state, _tactic, **_kw: SimpleNamespace(
        proof_finished=False, feedback=[],
    )
    preview = interactive._format_complete_goals(complete)
    assert "[clipped " in preview and sentinel not in preview
    assert "[clipped " not in interactive._format_goals(many)
    with OutputStore(tmp_path / "private", workspace=interactive._state_table[sid].workspace,
                     allow_test_temp_root=True) as store:
        ctx.lifespan_context["output_store"] = store
        result = await server.rocq_step_multi(["idtac."], from_state=sid, ctx=ctx)
        assert result["success"] and result["results"][0]["success"]
        ref = result["views"]["results[0].goals"]
        assert ref["kind"] == "stored"
        raw = store._entries[ref["handle"]].path.read_bytes()
        assert sentinel.encode() in raw[ref["span_start"]:ref["span_end"]]
        assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_goal_selector_reads_a_real_pet_state(workspace):
    from tests.conftest import PET_AVAILABLE, make_lifespan_state

    if not PET_AVAILABLE:
        pytest.skip("pet not installed")
    path = workspace / "real_goal_selector.v"
    path.write_text(
        "Theorem goal_sample : forall n : nat, n = n.\n"
        "Proof.\nintros n.\nreflexivity.\nQed.\n",
    )
    ls = make_lifespan_state(pet_timeout=30, full=True)
    ls.update(output_stdio=True, output_principal="real-pet-stdio")
    ctx = SimpleNamespace(lifespan_context=ls)
    try:
        start = await server.rocq_start(
            file=path.name, theorem="goal_sample", workspace=str(workspace), ctx=ctx,
        )
        assert start["success"], start
        assert start["service_generation"] == "real-pet-stdio"
        stepped = await server.rocq_check(
            body="intros n.", from_state=start["state_id"], ctx=ctx,
        )
        assert stepped["success"], stepped
        assert stepped["service_generation"] == "real-pet-stdio"
        roster = await server.rocq_get_goal_roster(
            stepped["state_id"], "real-pet-stdio", ctx=ctx,
        )
        assert roster["success"] and roster["focused_goals"] == 1
        assert roster["goals"][0]["hypotheses_total"] >= 1
        conclusion = await server.rocq_get_goal_part(
            stepped["state_id"], "focused", 1, "conclusion",
            service_generation="real-pet-stdio", ctx=ctx,
        )
        assert conclusion["success"] and "n" in conclusion["text"]
        assert conclusion["goal_ref"] == roster["goals"][0]["goal_ref"]
        hypothesis = await server.rocq_get_goal_part(
            stepped["state_id"], "focused", 1, "type", hyp_index=1,
            service_generation="real-pet-stdio", ctx=ctx,
        )
        assert hypothesis["success"] and "nat" in hypothesis["text"]
        assert server._output_view_fits(stepped)
    finally:
        server._invalidate_pet(ls)


@settings(max_examples=45, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(group=st.sampled_from(["focused", "stack_left", "stack_right", "shelved", "given_up"]),
       term=st.text(alphabet="xyλ🙂", min_size=1, max_size=50),
       transition=st.sampled_from(["same", "wrong_generation", "evicted", "stale"]))
@pytest.mark.asyncio
async def test_property_goal_state_group_and_generation_never_cross_bind(
    goals, monkeypatch, group, term, transition,
):
    _old, _focused, ctx, pet = goals
    ws = interactive._state_table[_old].workspace
    sid = interactive._state_add(
        SimpleNamespace(proof_finished=False), "Property.v", "goal", ws, None, None, 0,
    )
    pet.complete_goals = lambda _state: SimpleNamespace(
        goals=[SimpleNamespace(hyps=[], ty="focused:" + term)],
        stack=[([_dict_goal("left:" + term)], [_dict_goal("right:" + term)])],
        shelf=[_dict_goal("shelf:" + term)], given_up=[_dict_goal("given:" + term)],
    )
    depth = 1 if group in {"stack_left", "stack_right"} else None
    expected = {"focused": "focused:", "stack_left": "left:",
                "stack_right": "right:", "shelved": "shelf:", "given_up": "given:"}[group] + term
    try:
        initial = await server.rocq_get_goal_part(
            sid, group, 1, "conclusion", depth=depth,
            service_generation="one-stdio-client", ctx=ctx,
        )
        assert initial["success"] and initial["text"] == expected
        assert initial["goal_ref"]["state_id"] == sid
        assert initial["goal_ref"]["group"] == group
        if transition == "wrong_generation":
            generation = "previous-process"
        else:
            generation = "one-stdio-client"
        if transition == "evicted":
            interactive._state_remove(sid)
        elif transition == "stale":
            monkeypatch.setattr(interactive, "_check_staleness", lambda *args: "dependency changed")
        repeated = await server.rocq_get_goal_part(
            sid, group, 1, "conclusion", depth=depth,
            service_generation=generation, ctx=ctx,
        )
        if transition == "same":
            assert repeated["success"] and repeated["text"] == expected
            assert repeated["goal_ref"] == initial["goal_ref"]
        else:
            assert repeated["success"] is False and "text" not in repeated
    finally:
        interactive._state_remove(sid)
        monkeypatch.setattr(interactive, "_check_staleness", lambda *args: None)
