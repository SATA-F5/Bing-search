"""AstrBot plugin: Bing web search.

The plugin exposes a ``web_search_bing`` LLM tool that mirrors AstrBot's
built-in web-search tools (same JSON payload and citation indexes), plus a
small ``/bing`` command for quick manual checks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

try:
    from .bing_search import (
        BingSearchClient,
        BingSearchError,
        SearchResult,
        build_search_payload,
    )
except ImportError:  # pragma: no cover - direct ``main.py`` import in tests
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bing_search import (  # type: ignore[no-redef]
        BingSearchClient,
        BingSearchError,
        SearchResult,
        build_search_payload,
    )

_COMMAND_NAMES = ("bing", "必应", "bsearch")
_COMMAND_PREFIX = re.compile(r"^(?:[/!！~～]|\s)*", re.IGNORECASE)


@dataclass
class BingWebSearchTool(FunctionTool[AstrAgentContext]):
    """Function tool registered into AstrBot's LLM tool manager."""

    name: str = "web_search_bing"
    description: str = (
        "A web search tool based on Bing. Use it to search the public web for "
        "up-to-date information. It returns JSON results containing title, url, "
        "snippet and citation index."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Required. Search query.",
                },
                "count": {
                    "type": "integer",
                    "description": (
                        "Optional. Number of results to return. Range: 1-10. "
                        "Defaults to the plugin configuration."
                    ),
                },
                "market": {
                    "type": "string",
                    "description": (
                        'Optional. Bing market, for example "zh-CN" or "en-US". '
                        "Defaults to the plugin configuration."
                    ),
                },
                "freshness": {
                    "type": "string",
                    "description": (
                        'Optional. Restrict results by age: "day", "week" or "month".'
                    ),
                },
            },
            "required": ["query"],
        }
    )
    client: Any = Field(default=None, repr=False)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        client = self.client
        if not isinstance(client, BingSearchClient):
            return "Error: Bing search client is not initialized."

        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "Error: query is required."

        raw_count = kwargs.get("count", client.default_count)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = client.default_count
        count = max(1, min(10, count))

        market = kwargs.get("market") or None
        freshness = kwargs.get("freshness") or None

        try:
            results = await client.search(
                query,
                count=count,
                market=market,
                freshness=freshness,
            )
        except BingSearchError as exc:
            logger.warning("Bing web search failed: %s", exc)
            return f"Error: Bing web search failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - tools must never crash the agent
            logger.exception("Unexpected Bing web search error: %s", exc)
            return "Error: Bing web search failed unexpectedly."

        if not results:
            return "Error: Bing web searcher does not return any results."
        return build_search_payload(results)


class BingSearchPlugin(Star):
    """Bing search tool + manual command."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self._client = BingSearchClient(self.config)

        if self._bool_config("register_llm_tool", True):
            self.context.add_llm_tools(BingWebSearchTool(client=self._client))
            logger.info("Registered LLM tool: web_search_bing")
        else:
            logger.info("LLM tool web_search_bing is disabled in plugin config")

    def _bool_config(self, key: str, default: bool) -> bool:
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive config read
            return default
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _int_config(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (AttributeError, TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    @filter.command("bing", alias={"必应", "bsearch"})
    async def bing_search(self, event: AstrMessageEvent):
        """使用 Bing 搜索网页。用法：/bing <关键词>"""
        if not self._bool_config("enable_command", True):
            yield event.plain_result("Bing 搜索指令已在插件配置中关闭。")
            return

        query = self._extract_query(event.message_str)
        if not query:
            yield event.plain_result(
                "用法：/bing <关键词>\n例如：/bing AstrBot 插件开发"
            )
            return

        count = self._int_config("command_count", 5, 1, 10)
        try:
            results = await self._client.search(query, count=count)
        except BingSearchError as exc:
            logger.warning("Bing command search failed: %s", exc)
            yield event.plain_result(f"Bing 搜索失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - command must never crash the bot
            logger.exception("Unexpected Bing command error: %s", exc)
            yield event.plain_result("Bing 搜索失败，请稍后再试。")
            return

        if not results:
            yield event.plain_result("没有找到相关结果，请换个关键词试试。")
            return

        yield event.plain_result(self._format_results(query, results))

    @staticmethod
    def _extract_query(message: str) -> str:
        """Drop the command name and keep the rest of the message."""
        text = (message or "").strip()
        if not text:
            return ""

        text = _COMMAND_PREFIX.sub("", text)
        lowered = text.lower()
        for name in _COMMAND_NAMES:
            if lowered == name:
                return ""
            if lowered.startswith(name + " "):
                return text[len(name) :].strip()
        # Fallback for platforms that already removed the command token.
        return text

    @staticmethod
    def _format_results(query: str, results: list[SearchResult]) -> str:
        lines = [f"🔎 Bing 搜索：{query}"]
        for index, result in enumerate(results, 1):
            snippet = result.snippet.strip()
            if len(snippet) > 180:
                snippet = snippet[:179].rstrip() + "…"
            block = f"{index}. {result.title}\n{result.url}"
            if snippet:
                block += f"\n{snippet}"
            lines.append(block)
        return "\n\n".join(lines)

    async def terminate(self):
        """Release the HTTP session when the plugin is disabled or reloaded."""
        await self._client.close()
