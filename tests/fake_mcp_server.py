"""A tiny MCP server for the tests (stdio): `python tests/fake_mcp_server.py`. Needs the `mcp` SDK."""

from mcp.server.mcpserver import MCPServer

app = MCPServer("fake")


@app.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@app.tool(name="echo-text")
def echo(text: str) -> str:
    """Say the text back."""
    return text


@app.tool()
def boom() -> str:
    """Always fails."""
    raise ValueError("kaboom")


if __name__ == "__main__":
    app.run()
