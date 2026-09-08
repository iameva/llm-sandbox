#!/usr/bin/env python3
"""Translate backend profiles into launcher arguments without changing session paths."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

DEFAULTS = {
    "claude": {"provider": "anthropic", "auth": "login", "model": "opus", "fast_model": "haiku"},
    "chatgpt": {"provider": "openai", "auth": "login"},
    "openai": {"provider": "openai", "auth": "api_key",
               "key_env": "OPENAI_API_KEY", "key_file": "~/.config/openai.api"},
    "deepseek": {"provider": "deepseek", "auth": "api_key",
                 "model": "deepseek-v4-pro", "fast_model": "deepseek-v4-flash",
                 "key_env": "DEEPSEEK_API_KEY", "key_file": "~/.config/deepseek.api"},
}
FIELDS = {"provider", "auth", "model", "fast_model", "key_env", "key_file",
          "base_url", "anthropic_base_url", "harnesses"}
HARNESSES = {"claude", "codex", "pi", "omp", "opencode", "aider", "llm"}


def string(value, label):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be a non-empty string without control characters")
    return value


def endpoint(value):
    string(value, "endpoint")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("backend endpoints must be HTTP(S) URLs without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("backend endpoints must not include a query or fragment")
    return value.rstrip("/")


def load_profile(path, harness, selected, model):
    config = {}
    if path.exists():
        config = json.loads(path.read_text())
        if not isinstance(config, dict) or set(config) - {"profiles", "defaults"}:
            raise ValueError("backends.json accepts only profiles and defaults objects")
    profiles = config.get("profiles", {})
    defaults = config.get("defaults", {})
    if not isinstance(profiles, dict) or not isinstance(defaults, dict):
        raise ValueError("profiles and defaults must be objects")
    if set(defaults) - HARNESSES:
        raise ValueError("defaults contains an unknown harness")
    name = selected or defaults.get(harness, "")
    if not name:
        return "", {"model": model} if model else {}
    string(name, "backend name")
    if name not in DEFAULTS and name not in profiles:
        raise ValueError(f"unknown backend {name!r}; choose claude, chatgpt, openai, deepseek, or define a profile")
    custom = profiles.get(name, {})
    if not isinstance(custom, dict) or set(custom) - FIELDS:
        raise ValueError(f"invalid fields in profile {name!r}")
    profile = {**DEFAULTS.get(name, {}), **custom}
    overrides = profile.pop("harnesses", {})
    if not isinstance(overrides, dict) or set(overrides) - HARNESSES:
        raise ValueError("harnesses must map supported harness names to overrides")
    override = overrides.get(harness, {})
    if not isinstance(override, dict) or set(override) - (FIELDS - {"harnesses"}):
        raise ValueError(f"invalid overrides for {harness}")
    profile.update(override)
    if model:
        profile["model"] = model
    for key, value in profile.items():
        string(value, key)
    if profile.get("provider") not in {"anthropic", "openai", "deepseek"}:
        raise ValueError("provider must be anthropic, openai, or deepseek")
    if profile.get("auth") not in {"login", "api_key"}:
        raise ValueError("auth must be login or api_key")
    return name, profile


def credential(profile, dry_run):
    variable = profile.get("key_env", "")
    if variable and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise ValueError("key_env must be an environment variable name")
    if dry_run:
        return "DRY_RUN_KEY_NOT_LOADED"
    key = os.environ.get(variable, "") if variable else ""
    filename = profile.get("key_file")
    if not key and filename:
        path = Path(filename).expanduser()
        if path.is_file():
            key = path.read_text().strip()
    if not key:
        raise ValueError(f"missing API key; set {variable or 'key_env in this profile'}"
                         f" or populate {filename or 'key_file in this profile'}")
    return string(key, "API key")


def plan(harness, name, profile, dry_run=False):
    records = []
    def arg(*values):
        records.extend(("arg", value) for value in values)
    def env(key, value):
        records.append(("env", f"{key}={value}"))
    def secret(key, value):
        records.append(("secret", f"{key}={value}"))
    def codex(key, value):
        arg("-c", f"{key}={json.dumps(value)}")

    model = profile.get("model", "")
    if not name:
        if model:
            if harness == "llm":
                raise ValueError("a shell has no model; select a coding harness")
            arg("--model", model)
        return records
    if harness == "llm":
        raise ValueError("--backend requires a coding harness, not llm")
    provider = profile["provider"]
    login = profile["auth"] == "login"
    fast = profile.get("fast_model", model)
    if provider == "anthropic" and harness != "claude":
        raise ValueError("Claude backends are restricted to the claude harness")
    if provider == "deepseek" and login:
        raise ValueError("DeepSeek requires auth=api_key")
    if login and harness in {"claude", "aider"} and provider == "openai":
        raise ValueError("ChatGPT login is not supported by this harness; use an API-key gateway profile for Claude Code")
    if provider == "openai" and harness == "claude" and not profile.get("anthropic_base_url"):
        raise ValueError("GPT in Claude Code needs anthropic_base_url pointing to an Anthropic-compatible gateway")
    if harness != "claude" and "anthropic_base_url" in profile and "base_url" not in profile:
        raise ValueError("this profile supplies only an Anthropic gateway endpoint; set base_url for this harness")
    # Resume can restore a model from the old backend. Always send a model
    # override with a named profile rather than trusting a saved default.
    if not model:
        raise ValueError(f"set a model for {name!r} in backends.json or pass --model MODEL")
    base = endpoint(profile.get("base_url", "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"))
    if login and ("base_url" in profile or "anthropic_base_url" in profile):
        raise ValueError("login profiles cannot redirect credentials to custom endpoints")
    key = "" if login else credential(profile, dry_run)

    if harness == "claude":
        if not login:
            url = profile.get("anthropic_base_url", "https://api.deepseek.com/anthropic" if provider == "deepseek" else "https://api.anthropic.com")
            env("ANTHROPIC_BASE_URL", endpoint(url))
            if provider == "anthropic":
                secret("ANTHROPIC_API_KEY", key)
                env("ANTHROPIC_AUTH_TOKEN", "")
            else:
                secret("ANTHROPIC_AUTH_TOKEN", key)
                env("ANTHROPIC_API_KEY", "")
        if model:
            arg("--model", model)
            for variable in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"):
                env(variable, model)
            env("ANTHROPIC_DEFAULT_HAIKU_MODEL", fast)
            env("CLAUDE_CODE_SUBAGENT_MODEL", fast)
        if provider == "deepseek":
            env("CLAUDE_CODE_EFFORT_LEVEL", "max")
    elif harness == "codex":
        if login:
            codex("model_provider", "openai")
            codex("forced_login_method", "chatgpt")
        else:
            # One provider ID across API backends keeps provider-based session
            # filtering stable. Login profiles retain the native OpenAI provider.
            codex("model_provider", "sandbox_backend")
            codex("model_providers.sandbox_backend.name", "Sandbox backend")
            codex("model_providers.sandbox_backend.base_url", base)
            codex("model_providers.sandbox_backend.env_key", "SANDBOX_PROVIDER_API_KEY")
            codex("model_providers.sandbox_backend.wire_api", "responses")
            codex("model_providers.sandbox_backend.requires_openai_auth", False)
            secret("SANDBOX_PROVIDER_API_KEY", key)
        if model:
            arg("--model", model)
    elif harness in {"pi", "omp"}:
        if login:
            if harness == "omp":
                arg("--model", f"openai-codex/{model}")
            else:
                arg("--provider", "openai-codex")
                if model:
                    arg("--model", model)
        elif provider == "openai" and "base_url" not in profile:
            if harness == "omp":
                arg("--model", f"openai/{model}")
            else:
                arg("--provider", "openai")
                if model:
                    arg("--model", model)
            secret("OPENAI_API_KEY", key)
        else:
            if not model:
                raise ValueError("custom API providers require a model")
            config = {"baseUrl": base, "api": "openai-completions" if provider == "deepseek" else "openai-responses",
                      "apiKey": "$SANDBOX_PROVIDER_API_KEY" if harness == "pi" else "SANDBOX_PROVIDER_API_KEY", "authHeader": True,
                      "models": []}
            for ident in dict.fromkeys([model, fast]):
                entry = {"id": ident, "name": ident, "reasoning": True, "input": ["text"],
                         "contextWindow": 1000000 if provider == "deepseek" else 128000,
                         "maxTokens": 384000 if provider == "deepseek" else 8192,
                         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
                if provider == "deepseek":
                    entry["compat"] = {
                        "supportsDeveloperRole": False, "supportsReasoningEffort": True,
                        "maxTokensField": "max_tokens", "reasoningEffortMap": {"high": "high", "xhigh": "max"},
                        "supportsToolChoice": False, "requiresReasoningContentForToolCalls": True,
                        "requiresAssistantContentForToolCalls": True,
                        "extraBody": {"thinking": {"type": "enabled"}},
                    }
                    if harness == "omp":
                        entry["thinking"] = {"minLevel": "high", "maxLevel": "xhigh", "mode": "effort"}
                config["models"].append(entry)
            env("SANDBOX_PROVIDER_CONFIG", json.dumps(config, separators=(",", ":")))
            secret("SANDBOX_PROVIDER_API_KEY", key)
            records.append(("asset", "backend-provider.mjs"))
            arg("--extension", "/opt/sandbox/backend-provider.mjs")
            if harness == "omp":
                arg("--model", f"sandbox_backend/{model}")
            else:
                arg("--provider", "sandbox_backend", "--model", model)
            if provider == "deepseek":
                arg("--thinking", "high")
        if harness == "omp" and model:
            selected_provider = "openai-codex" if login else ("sandbox_backend" if any(k == "asset" for k, _ in records) else "openai")
            arg("--smol", f"{selected_provider}/{fast}", "--slow", f"{selected_provider}/{model}",
                "--plan", f"{selected_provider}/{model}")
    elif harness == "opencode":
        selected_provider = "openai" if login else "sandbox_backend"
        config = {"model": f"{selected_provider}/{model}", "small_model": f"{selected_provider}/{fast}"}
        if not login:
            config["provider"] = {selected_provider: {
                "npm": "@ai-sdk/openai-compatible" if provider == "deepseek" else "@ai-sdk/openai",
                "name": "Sandbox backend", "options": {"baseURL": base, "apiKey": "{env:SANDBOX_PROVIDER_API_KEY}"},
                "models": {ident: {"name": ident} for ident in {model, fast}},
            }}
            secret("SANDBOX_PROVIDER_API_KEY", key)
        env("OPENCODE_CONFIG_CONTENT", json.dumps(config, separators=(",", ":")))
        arg("--model", config["model"])
    elif harness == "aider":
        prefix = "deepseek" if provider == "deepseek" else "openai"
        secret("DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY", key)
        arg("--model", f"{prefix}/{model}")
        if fast:
            arg("--weak-model", f"{prefix}/{fast}")
        if "base_url" in profile:
            raise ValueError("custom base_url is not supported by the aider adapter")
    return records


def migrate_claude_sessions(root):
    """Copy legacy DeepSeek projects once, keeping both sides on collisions."""
    source = root / "deepseek-claude/projects"
    if not source.is_dir():
        return
    destination = root / "claude/projects"
    if any(path.is_symlink() for path in (root / "claude", root / "deepseek-claude", source, destination)):
        raise ValueError("session migration requires real directories, not symlinks")
    destination.mkdir(parents=True, exist_ok=True)
    marker = root / ".deepseek-sessions-imported"
    with (root / ".session-migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists():
            return
        copied = conflicts = 0
        for src in source.rglob("*"):
            if src.is_symlink():
                raise ValueError(f"legacy session migration does not follow symlinks: {src}")
            target = destination / src.relative_to(source)
            if target.is_symlink():
                raise ValueError(f"session migration does not replace symlinks: {target}")
            if src.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    conflicts += 1
                    continue
                # Link a complete temporary copy into place without replacing a
                # session another process may have created in the meantime.
                fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".import-")
                os.close(fd)
                try:
                    shutil.copy2(src, temporary)
                    try:
                        os.link(temporary, target)
                        copied += 1
                    except FileExistsError:
                        conflicts += 1
                finally:
                    os.unlink(temporary)
        marker.touch()
        print(f"sandbox-run: imported {copied} legacy DeepSeek session files; "
              f"kept {conflicts} existing shared files. Originals remain in {source}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    parser.add_argument("--backend", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/llm-sandbox/backends.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        name, profile = load_profile(args.config, args.harness, args.backend, args.model)
        records = plan(args.harness, name, profile, args.dry_run)
        if args.harness == "claude" and not args.dry_run:
            migrate_claude_sessions(Path.home() / ".config/llm-sandbox")
        if name:
            print(f"sandbox-run: {args.harness} backend={name} auth={profile['auth']} "
                  f"model={profile.get('model', 'harness default')}; shared {args.harness} sessions", file=sys.stderr)
        for kind, value in records:
            sys.stdout.buffer.write(kind.encode() + b"\0" + value.encode() + b"\0")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"sandbox-run: {exc}\n")


if __name__ == "__main__":
    main()
