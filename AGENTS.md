# AGENTS.md — uiautomator2-mcp

## Layout
- `src/uiautomator2_mcp/server.py` — all MCP tools (`@mcp.tool()`, ~50) + entrypoint `main()` (`mcp.run(transport="stdio")`). Single ~2000-line module; tools are plain functions with mocked-device-friendly signatures.
- `src/uiautomator2_mcp/{device_manager,adb_tools,logcat}.py` — device session cache, ADB/emulator helpers, logcat queries.
- `tests/` — `conftest.py` inserts `src/` into `sys.path`; no install needed to run pytest.

## Commands
- `python -m pytest tests/ -q` — full suite (no Android device needed; everything is mocked).
- `python -m pytest tests/<file>.py -q` — single file; `... -k <name>` for one test.
- `python -m py_compile src/uiautomator2_mcp/server.py` — fast syntax check.
- `pip install -e .` or `uv pip install -e .` — editable install.
- `uv build` — build wheel/sdist (hatchling, packages under `src/`).
- No lint/typecheck/formatter config exists — don't add tooling unprompted.

## Testing conventions
- Tests import `uiautomator2_mcp.server` directly and call tool functions via `getattr(server, name)` with `Mock`/`SimpleNamespace` fakes — they never touch the MCP protocol layer or a real device.
- New tools must accept `device_id` (multi-device routing); `test_server_multi_device.py` asserts the tool list exposes it — update that list when adding tools.

## mcp v1/v2 compat (do not regress)
- `server.py` imports via try/except shim: `mcp.server.mcpserver.MCPServer` (v2) first, `mcp.server.fastmcp.FastMCP` (v1) fallback. Never revert to a direct v1 import — uvx resolves latest mcp (2.x) and the server crashes on startup.
- Always pass `instructions=` as keyword: in v2 the 2nd positional ctor arg is `title`, not `instructions`.
- Dependency floor is `mcp>=1.2.0` (FastMCP appeared in 1.2.0); keep the spec unbounded above so uvx gets 2.x.
- Only MCP surface used: `@mcp.tool()`, `mcp.run(transport="stdio")`, `mcp.types` — all stable across v1/v2.

## Release
- Version lives only in `pyproject.toml` (`version = "x.y.z"`).
- Pushing a `v*` tag triggers `.github/workflows/publish-pypi.yml` (`uv build` + `uv publish`, needs `PYPI_API_TOKEN` secret).
- PyPI rejects re-uploading an existing version — always bump the version before tagging/retrying a publish.

## Commits
- Conventional commits (`fix: …`, `docs: …`), present tense, one logical change per commit. Don't stage `.slim/` scratch state.
