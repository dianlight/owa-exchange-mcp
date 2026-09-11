"""Exchange MCP Server - access OWA email, calendar, directory via MCP."""

# Single source of truth for the version: pyproject.toml reads this attribute
# (`[tool.setuptools.dynamic] version = {attr = "exchange_mcp.__version__"}`)
# rather than carrying its own copy. An editable install doesn't refresh its
# metadata when the tree changes, so importlib.metadata.version() can report a
# stale number - and a startup banner that misreports its own version is worse
# than no banner. Keep server.json's two `version` fields in sync by hand; that
# manifest is published to the MCP registry and can't read Python attributes.
__version__ = "2.0.0b4"
