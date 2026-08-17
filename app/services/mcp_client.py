from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.core.config import Settings
from app.core.enums import RiskLevel


class McpToolError(RuntimeError):
    pass


class MindBridgeMcpToolClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def handle_report(self, report_id: int, risk_level: str | None, allowed_tools: tuple[str, ...] = ()) -> list[str]:
        required_tools = ["EXCEL_REPORT"]
        if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
            required_tools.append("CASE_CREATE")
        if risk_level == RiskLevel.HIGH.value:
            required_tools.append("ALERT_SEND")
        blocked = [tool for tool in required_tools if tool not in allowed_tools]
        if blocked:
            raise McpToolError(f"当前 Skill Policy 未授权工具：{', '.join(blocked)}")
        try:
            async with self._session() as session:
                results = [
                    await self._call_tool(session, "mindbridge_excel_report", {"report_id": report_id}),
                ]
                case_id = None
                if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
                    case_result = await self._call_tool(session, "mindbridge_case_create", {"report_id": report_id})
                    results.append(case_result)
                    case_id = self._extract_case_id(case_result)
                if risk_level == RiskLevel.HIGH.value:
                    if case_id is None:
                        raise McpToolError("高风险报告已创建，但无法解析 caseId，预警未发送")
                    results.append(await self._call_tool(session, "mindbridge_alert_send", {"case_id": case_id}))
                return results
        except McpToolError:
            raise
        except Exception as exc:
            raise McpToolError(f"MCP 工具调用异常：{type(exc).__name__}: {exc}") from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise McpToolError("缺少 mcp 依赖，无法通过 MCP 调用 MindBridge 工具") from exc

        project_root = self.settings.project_root
        env = os.environ.copy()
        python_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(project_root) if not python_path else f"{project_root}{os.pathsep}{python_path}"

        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_tools.server"],
            env=env,
            cwd=str(project_root),
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    async def _call_tool(self, session: Any, name: str, arguments: dict[str, Any]) -> str:
        result = await session.call_tool(name, arguments=arguments)
        message = self._result_message(result)
        if getattr(result, "isError", False):
            raise McpToolError(f"{name} 调用失败：{message}")
        return message

    def _result_message(self, result: Any) -> str:
        parts = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        if parts:
            return "\n".join(parts)
        structured = getattr(result, "structuredContent", None)
        return str(structured if structured is not None else result)

    def _extract_case_id(self, message: str) -> int | None:
        match = re.search(r"caseId=(\d+)", message)
        return int(match.group(1)) if match else None
