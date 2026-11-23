"""Minimal MCP server for end-to-end testing of mcli."""

from typing import Literal

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("mock")


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the input text."""
    return text


@mcp.tool()
def add(a: int, b: int = 0) -> str:
    """Add two integers and return the sum as text."""
    return str(a + b)


@mcp.tool()
def render(mode: Literal["png", "svg"] = "png", scale: float = 1.0, draft: bool = False) -> str:
    """Pretend to render an image."""
    return f"{mode}:{scale}:{draft}"


@mcp.tool()
def fail(msg: str = "boom") -> str:
    """Always raises an error."""
    raise ValueError(msg)


if __name__ == "__main__":
    mcp.run()
