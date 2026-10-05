"""Tests for the ``view_id`` tool parameter.

A Kubecost installation that uses Cost Allocation Context views scopes every
query to a view: the caller passes ``view_id`` and the tools forward it to the
upstream API as the ``viewId`` query parameter. A caller that omits it must get
an unscoped request — no ``viewId`` is invented on their behalf.

The upstream request is observed through the injectable HTTP backend rather than
a mocked transport, so these tests assert on exactly the params the tool built.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mcp_kubecost.client import set_http_backend
from mcp_kubecost.tools import kubecost_tools as ktools
from mcp_kubecost.tools.kubecost_tools import register_kubecost_tools

# Every tool that issues an HTTP call: its name, arguments that reach that call,
# and an empty upstream payload its parser accepts.
# `kubecost_list_windows` is deliberately absent: it computes windows locally.
_EMPTY_LIST = {"data": []}
_EMPTY_WRAPPED = {"code": 200, "data": {"recommendations": []}}

HTTP_TOOLS: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("kubecost_get_workload_costs", {"window": "7d"}, _EMPTY_LIST),
    (
        # The tool rejects bare relative windows for a diff, so both sides are explicit ranges.
        "kubecost_get_cost_comparison",
        {
            "current_window": "2026-06-08T00:00:00Z,2026-06-15T00:00:00Z",
            "baseline_window": "2026-06-01T00:00:00Z,2026-06-08T00:00:00Z",
        },
        _EMPTY_LIST,
    ),
    ("kubecost_get_container_sizing", {}, _EMPTY_LIST),
    ("kubecost_get_abandoned_workloads", {}, _EMPTY_LIST),
    ("kubecost_get_savings_overview", {}, _EMPTY_LIST),
    ("kubecost_get_pv_sizing", {}, _EMPTY_LIST),
    ("kubecost_get_local_disk_savings", {}, _EMPTY_LIST),
    ("kubecost_get_cluster_rightsizing", {"cluster": "cluster-one"}, _EMPTY_WRAPPED),
    ("kubecost_get_unclaimed_volumes", {}, _EMPTY_LIST),
    ("kubecost_get_quota_sizing", {}, _EMPTY_WRAPPED),
]
_TOOL_IDS = [name for name, _args, _payload in HTTP_TOOLS]


def _sc(result) -> dict:  # noqa: ANN001 — untyped so the optional structured_content narrows
    """Return structured_content from a ToolResult."""
    return result.structured_content


class _ParamRecorder:
    """Swallow every upstream call and remember the params it carried."""

    def __init__(self) -> None:
        self.params: list[dict[str, Any] | None] = []
        self.payload: dict[str, Any] = dict(_EMPTY_LIST)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.params.append(params)
        return self.payload

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.params.append(params)
        return self.payload

    def view_ids(self) -> list[Any]:
        return [(p or {}).get("viewId") for p in self.params]


@pytest.fixture
def recorder() -> _ParamRecorder:
    """Install the recorder as the HTTP backend; conftest removes it after each test."""
    recorder = _ParamRecorder()
    set_http_backend(recorder.get, recorder.post)
    return recorder


def _app() -> FastMCP:
    app = FastMCP("view-id-check")
    register_kubecost_tools(app)
    return app


class TestViewIdReachesUpstream:
    @pytest.mark.parametrize(("tool", "arguments", "payload"), HTTP_TOOLS, ids=_TOOL_IDS)
    @pytest.mark.asyncio
    async def test_set_view_id_is_forwarded_as_view_id_param(
        self, recorder: _ParamRecorder, tool: str, arguments: dict[str, Any], payload: dict[str, Any]
    ):
        recorder.payload = payload
        async with Client(_app()) as client:
            await client.call_tool(tool, {**arguments, "view_id": "42"})

        assert recorder.params, f"{tool} made no upstream call"
        assert recorder.view_ids() == ["42"] * len(recorder.params)

    @pytest.mark.parametrize(("tool", "arguments", "payload"), HTTP_TOOLS, ids=_TOOL_IDS)
    @pytest.mark.asyncio
    async def test_omitted_view_id_leaves_the_request_unscoped(
        self, recorder: _ParamRecorder, tool: str, arguments: dict[str, Any], payload: dict[str, Any]
    ):
        recorder.payload = payload
        async with Client(_app()) as client:
            await client.call_tool(tool, arguments)

        assert recorder.params, f"{tool} made no upstream call"
        for params in recorder.params:
            assert params is None or "viewId" not in params

    @pytest.mark.asyncio
    async def test_view_id_zero_is_sent_rather_than_treated_as_absent(self, recorder: _ParamRecorder):
        """ "0" is the unrestricted view, not a missing value — it has to reach the API."""
        async with Client(_app()) as client:
            await client.call_tool("kubecost_get_savings_overview", {"view_id": "0"})

        assert recorder.view_ids() == ["0"]

    @pytest.mark.asyncio
    async def test_paged_tool_scopes_every_page(self, monkeypatch: pytest.MonkeyPatch):
        """`kubecost_get_abandoned_workloads` pages internally; each page must stay in the view.

        A page size of 2 and a full first page force a second fetch, so this covers the
        paging loop rather than a single call.
        """
        monkeypatch.setattr(ktools, "_ABANDONED_API_PAGE_SIZE", 2)
        pages = [[{"pod": "a"}, {"pod": "b"}], [{"pod": "c"}]]
        params_seen: list[dict[str, Any] | None] = []

        async def paged_get(path: str, params: dict[str, Any] | None = None) -> Any:
            params_seen.append(params)
            return pages[len(params_seen) - 1]

        async def unused_post(path: str, json: Any = None, params: Any = None) -> Any:
            raise AssertionError("no tool should POST")

        set_http_backend(paged_get, unused_post)
        async with Client(_app()) as client:
            await client.call_tool("kubecost_get_abandoned_workloads", {"view_id": "7"})

        assert len(params_seen) == 2, "expected the tool to fetch a second page"
        assert [(p or {}).get("viewId") for p in params_seen] == ["7", "7"]


class TestViewIdSchemaValidation:
    @pytest.mark.parametrize("value", ["-1", "abc", "1.5", "0; DROP", "", "1" * 51])
    @pytest.mark.asyncio
    async def test_invalid_value_is_rejected_before_any_upstream_call(self, recorder: _ParamRecorder, value: str):
        async with Client(_app()) as client:
            with pytest.raises(ToolError):
                await client.call_tool("kubecost_get_savings_overview", {"view_id": value})

        assert recorder.params == []

    @pytest.mark.asyncio
    async def test_null_is_accepted_as_no_view_scope(self, recorder: _ParamRecorder):
        async with Client(_app()) as client:
            result = await client.call_tool("kubecost_get_savings_overview", {"view_id": None})

        assert _sc(result)["status"] in {"ok", "empty"}
        assert recorder.view_ids() == [None]
