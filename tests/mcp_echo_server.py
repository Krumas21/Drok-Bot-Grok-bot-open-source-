"""Tiny stdio MCP server used by test_integrations.py."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def shout(text: str) -> str:
    """Return the text in upper case."""
    return text.upper()


if __name__ == "__main__":
    server.run("stdio")
