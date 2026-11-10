from pathlib import Path
from typing import TypedDict

import pytest
from langchain_core.runnables import RunnableConfig

from quality_platform.storage.checkpoints import create_checkpoint_saver


@pytest.mark.asyncio
async def test_saver_reopens_persisted_checkpoint(tmp_path: Path) -> None:
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        value: int

    builder = StateGraph(State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    config: RunnableConfig = {"configurable": {"thread_id": "T1:S1"}}
    path = tmp_path / "checkpoints.sqlite"

    async with create_checkpoint_saver(path) as saver:
        graph = builder.compile(checkpointer=saver)
        assert (await graph.ainvoke({"value": 1}, config))["value"] == 2
    async with create_checkpoint_saver(path) as saver:
        graph = builder.compile(checkpointer=saver)
        assert (await graph.aget_state(config)).values["value"] == 2


@pytest.mark.asyncio
async def test_checkpoint_namespaces_isolate_same_thread(tmp_path: Path) -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    path = tmp_path / "checkpoints.sqlite"
    async with create_checkpoint_saver(path) as saver:
        first_checkpoint = empty_checkpoint()
        first_checkpoint["channel_values"] = {"value": "scope-one"}
        second_checkpoint = empty_checkpoint()
        second_checkpoint["channel_values"] = {"value": "scope-two"}
        first_config: RunnableConfig = {"configurable": {"thread_id": "T1", "checkpoint_ns": "S1"}}
        second_config: RunnableConfig = {"configurable": {"thread_id": "T1", "checkpoint_ns": "S2"}}
        await saver.aput(first_config, first_checkpoint, {"source": "input", "step": 0}, {})
        await saver.aput(second_config, second_checkpoint, {"source": "input", "step": 0}, {})
        first = await saver.aget(first_config)
        second = await saver.aget(second_config)
        assert first is not None and first["channel_values"]["value"] == "scope-one"
        assert second is not None and second["channel_values"]["value"] == "scope-two"


@pytest.mark.asyncio
async def test_completed_parallel_branch_is_not_replayed_after_other_branch_fails(
    tmp_path: Path,
) -> None:
    from langgraph.graph import START, StateGraph

    class State(TypedDict, total=False):
        fast: int
        slow: int

    fast_runs = 0
    slow_runs = 0

    def fast_node(_state: State) -> State:
        nonlocal fast_runs
        fast_runs += 1
        return {"fast": fast_runs}

    def slow_node(_state: State) -> State:
        nonlocal slow_runs
        slow_runs += 1
        if slow_runs == 1:
            raise RuntimeError("transient failure")
        return {"slow": slow_runs}

    builder = StateGraph(State)
    builder.add_node("fast", fast_node)  # type: ignore[arg-type]
    builder.add_node("slow", slow_node)  # type: ignore[arg-type]
    builder.add_edge(START, "fast")
    builder.add_edge(START, "slow")
    config: RunnableConfig = {"configurable": {"thread_id": "parallel-T1"}}
    path = tmp_path / "checkpoints.sqlite"
    async with create_checkpoint_saver(path) as saver:
        graph = builder.compile(checkpointer=saver)
        with pytest.raises(RuntimeError, match="transient"):
            await graph.ainvoke({}, config, durability="sync")
    async with create_checkpoint_saver(path) as saver:
        graph = builder.compile(checkpointer=saver)
        result = await graph.ainvoke(None, config, durability="sync")
    assert result == {"fast": 1, "slow": 2}
    assert fast_runs == 1
