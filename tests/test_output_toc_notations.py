"""toc / notations retain their full printed text before the legacy 8k cut."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

import rocq_mcp.interactive as interactive
import rocq_mcp.server as server
from rocq_mcp.output_store import OutputStore


@pytest.fixture
def tools(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    (ws / "Sample.v").write_text("Definition x := 1.\n")
    pet = SimpleNamespace(
        toc=lambda _file: [("main", [object()])],
        start=lambda *_args: object(),
        list_notations_in_statement=lambda *_args: [],
    )

    async def run_with_pet(fn, _state, _tool, **_kw):
        return fn(pet)

    monkeypatch.setattr(server, "_run_with_pet", run_with_pet)
    monkeypatch.setattr(server, "_set_workspace_if_needed", lambda *args: None)
    with OutputStore(tmp_path / "private", workspace=ws, allow_test_temp_root=True) as store:
        ctx = SimpleNamespace(lifespan_context={
            "pet_timeout": 30, "recent_errors": deque(maxlen=20),
            "output_stdio": True, "output_principal": "one-stdio-client",
            "output_store": store,
        })
        yield ws, pet, ctx, store


@pytest.mark.asyncio
async def test_toc_small_unchanged_and_large_toc_tail_recoverable(tools, monkeypatch):
    ws, _pet, ctx, store = tools
    monkeypatch.setattr(interactive, "_format_toc_elements", lambda _elems: ["  Lemma small (line 1)"])
    short = await server.rocq_toc("Sample.v", workspace=str(ws), ctx=ctx)
    assert short["success"] and short["output"] == "File: Sample.v\n  Lemma small (line 1)"
    assert short["view_status"] == "complete" and not store._entries

    monkeypatch.setattr(interactive, "_format_toc_elements",
                        lambda _elems: ["  Lemma " + "x" * 11_000 + "TOC_UNIQUE_TAIL"])
    large = await server.rocq_toc("Sample.v", workspace=str(ws), ctx=ctx)
    assert large["success"] and large["view_status"] == "partial_recoverable"
    assert "TOC_UNIQUE_TAIL" not in large["output"]
    ref = large["views"]["output"]
    assert ref["kind"] == "stored" and len(store._entries) == 1
    hit = await server.rocq_find_output(ref["handle"], "TOC_UNIQUE_TAIL",
                                        workspace=str(ws), ctx=ctx)
    assert hit["success"] and hit["hits"]
    assert server._output_view_fits(large)


@pytest.mark.asyncio
async def test_notations_full_text_saved_before_old_8k_prefix(tools):
    ws, pet, ctx, store = tools
    pet.list_notations_in_statement = lambda *_args: [
        SimpleNamespace(notation="_ + _" + "x" * 10_000 + "NOTATION_UNIQUE_TAIL",
                        path="Coq.Init.Nat", secpath=None, scope="nat_scope")
    ]
    result = await server.rocq_notations("n + 0 = n", workspace=str(ws), ctx=ctx)
    assert result["success"] and result["view_status"] == "partial_recoverable"
    assert "NOTATION_UNIQUE_TAIL" not in result["output"]
    handle = result["views"]["output"]["handle"]
    assert len(store._entries) == 1
    found = await server.rocq_find_output(handle, "NOTATION_UNIQUE_TAIL",
                                          workspace=str(ws), ctx=ctx)
    assert found["success"] and found["hits"]
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_no_notations_keeps_existing_short_message(tools):
    ws, _pet, ctx, store = tools
    response = await server.rocq_notations("True", workspace=str(ws), ctx=ctx)
    assert response["success"] and response["output"] == "No notations found in statement."
    assert response["view_status"] == "complete"
    assert not store._entries


@pytest.mark.asyncio
async def test_empty_toc_field_view_counts_the_displayed_file_header(tools):
    ws, pet, ctx, store = tools
    pet.toc = lambda _file: []
    result = await server.rocq_toc("Sample.v", workspace=str(ws), ctx=ctx)
    assert result["success"] and result["output"] == "File: Sample.v"
    ref = result["views"]["output"]
    assert ref["kind"] == "inline" and ref["complete"]
    assert ref["total_bytes"] == ref["shown_bytes"] == len(result["output"].encode("utf-8"))
    assert not store._entries
