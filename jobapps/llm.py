"""The one place that talks to Claude.

Backends (drafts.backend in config.yaml):
  claude-cli - the `claude` CLI in headless mode, signed in with your Claude account (OAuth)
  api        - the Anthropic API (needs ANTHROPIC_API_KEY)
  none       - no model calls; callers fall back to deterministic behaviour
  auto       - api if a key is set, else claude-cli if installed, else none

Every call returns JSON validated against a schema. Callers are responsible for checking that
what comes back is grounded in their source text (see grounding.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class LLMError(Exception):
    pass


def claude_exe() -> str | None:
    """The claude CLI, also when a background task runs with a thinner PATH than your terminal."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if candidate.exists():
            return str(candidate)
    return None


def backend(cfg: dict) -> str:
    choice = (cfg.get("backend") or "auto").lower()
    if choice != "auto":
        return choice
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "api"
    if claude_exe():
        return "claude-cli"
    return "none"


# Health of model calls in this process, shown on the dashboard (e.g. an expired Claude login).
STATUS: dict = {"last_ok": None, "last_error": None, "last_error_at": None, "calls": 0, "failures": 0}


def structured(system: str, user: str, schema: dict, cfg: dict, *, effort: str | None = None,
               web: bool = False, budget_usd: float | None = None) -> dict:
    """One schema-constrained call. `web=True` lets the model search/fetch the web (claude-cli only)."""
    from datetime import datetime
    kind = backend(cfg)
    effort = effort or cfg.get("effort", "medium")
    STATUS["calls"] += 1
    try:
        if kind == "api":
            if web:
                raise LLMError("web search is only wired up for the claude-cli backend")
            result = _api(system, user, schema, cfg["model"], effort)
        elif kind == "claude-cli":
            result = _cli(system, user, schema, cfg["model"], effort, web, budget_usd, cfg.get("timeout_seconds", 600))
        else:
            raise LLMError("no LLM backend available")
    except Exception as exc:
        # Whatever went wrong (the CLI couldn't be started, the reply wasn't the JSON asked for), callers get the one
        # kind of failure they handle: this call didn't work, the next job's may.
        error = exc if isinstance(exc, LLMError) else LLMError(f"{type(exc).__name__}: {exc}")
        STATUS.update(last_error=str(error)[:300], last_error_at=datetime.now().isoformat(timespec="seconds"),
                      failures=STATUS["failures"] + 1)
        raise error from None
    STATUS["last_ok"] = datetime.now().isoformat(timespec="seconds")
    return result


def _api(system: str, user: str, schema: dict, model: str, effort: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        raise LLMError("ANTHROPIC_API_KEY was rejected") from None
    except anthropic.RateLimitError:
        raise LLMError("rate limited by the Anthropic API; try again later") from None
    except anthropic.APIStatusError as exc:
        raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from None
    except anthropic.APIConnectionError:
        raise LLMError("could not reach the Anthropic API") from None
    if resp.stop_reason == "refusal":
        raise LLMError("the model declined this prompt")
    if resp.stop_reason == "max_tokens":
        raise LLMError("response was cut off (max_tokens)")
    return json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))


def _cli(system: str, user: str, schema: dict, model: str, effort: str, web: bool,
         budget_usd: float | None, timeout: int) -> dict:
    exe = claude_exe()
    if not exe:
        raise LLMError("claude CLI not found on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        sys_file = Path(tmp) / "system.md"
        sys_file.write_text(system, encoding="utf-8")
        cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence",
               # Lean, sandboxed call: no MCP servers, plugins, skills, settings files or local tools.
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
               "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
               "--system-prompt-file", str(sys_file), "--json-schema", json.dumps(schema),
               "--model", model, "--effort", effort]
        if web:
            cmd += ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch"]
        else:
            cmd += ["--tools", ""]
        if budget_usd:
            cmd += ["--max-budget-usd", f"{budget_usd:.2f}"]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            proc = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout, cwd=tmp, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise LLMError("claude CLI timed out") from None
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"claude CLI failed: {(proc.stderr or proc.stdout).strip()[:300]}") from None
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise LLMError(f"claude CLI: {str(out.get('result') or out.get('subtype'))[:300]}")
    return out["structured_output"]
