#!/usr/bin/env python3
"""
Generate a fully-commented Hasura GraphQL Engine configuration template for the
exact graphql-engine image used by this repo, and embed it into a Kubernetes
ConfigMap that the Hasura Deployment sources at runtime.

Outputs:
  - config/hasura/hasura.env
  - manifests/hasura-configmap.yaml

Design goals:
  - Cover *all* environment variables discovered in the graphql-engine binary
    (HASURA_GRAPHQL_* and LUX_*), for the pinned image tag.
  - Prefer "safe" defaults: do not write secrets into the ConfigMap.
  - Preserve your existing non-secret settings when regenerating.

Usage:
  python3 scripts/hasura/generate_hasura_config.py

Optional:
  HASURA_IMAGE=hasura/graphql-engine:vX.Y.Z python3 scripts/hasura/generate_hasura_config.py
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
HASURA_MANIFEST = REPO_ROOT / "manifests" / "hasura.yaml"

OUT_ENV = REPO_ROOT / "config" / "hasura" / "hasura.env"
OUT_CONFIGMAP = REPO_ROOT / "manifests" / "hasura-configmap.yaml"


SENSITIVE_VARS: Set[str] = {
    # Secrets / connection strings. Keep out of ConfigMaps by default.
    "HASURA_GRAPHQL_ACCESS_KEY",
    "HASURA_GRAPHQL_ADMIN_SECRET",
    "HASURA_GRAPHQL_ADMIN_SECRETS",
    "HASURA_GRAPHQL_DATABASE_URL",
    "HASURA_GRAPHQL_METADATA_DATABASE_URL",
    "HASURA_GRAPHQL_JWT_SECRET",
    "HASURA_GRAPHQL_JWT_SECRETS",
    "HASURA_GRAPHQL_PRO_KEY",
    "HASURA_GRAPHQL_EE_LICENSE_KEY",
    # These are often URLs that can contain credentials.
    "HASURA_GRAPHQL_REDIS_URL",
    "HASURA_GRAPHQL_RATE_LIMIT_REDIS_URL",
    # LUX_* often points at internal infra / auth endpoints.
    "LUX_LOG_SINK_URL",
    "LUX_METRICS_URL",
    "LUX_OAUTH_AUTHORIZATION_URL",
    "LUX_OAUTH_TOKEN_URL",
    "LUX_OAUTH_JWK_URL",
    "LUX_VALIDATE_PAT_URL",
}


# Some supported env vars are not documented in `graphql-engine --help` output
# (v2.38.0). Provide short explanations so the template is still useful.
MANUAL_DOCS: Dict[str, str] = {
    "HASURA_GRAPHQL_AUTH_HOOK_TYPE": (
        "Authorization webhook type/transport. "
        "This is an internal/legacy knob in some builds; prefer configuring "
        "`HASURA_GRAPHQL_AUTH_HOOK` + `HASURA_GRAPHQL_AUTH_HOOK_MODE`."
    ),
    "HASURA_GRAPHQL_BACKWARDS_COMPAT_NULL_IN_NONNULLABLE_VARIABLES": (
        "Backwards-compatibility switch for older behavior regarding nulls in "
        "non-nullable variables (legacy/compat mode)."
    ),
    "HASURA_GRAPHQL_DYNAMIC_SECRETS_ALLOWED_PATH_PREFIX": (
        "Allowed path prefix for dynamic secret references. "
        "Used to restrict which filesystem paths may be read when using "
        "`dynamic-from-file://...` style configuration values."
    ),
    "HASURA_GRAPHQL_EE_LICENSE_KEY": (
        "Hasura Enterprise Edition license key (secret). Prefer mounting via a "
        "Kubernetes Secret or Vault, not a ConfigMap."
    ),
    "HASURA_GRAPHQL_EE_LICENSE_KEY_PATH": (
        "Path to a file containing the Hasura Enterprise Edition license key."
    ),
    "HASURA_GRAPHQL_MAX_CACHE_SIZE": (
        "Deprecated alias for query response cache entry size. Prefer "
        "`HASURA_GRAPHQL_CACHE_MAX_ENTRY_SIZE`."
    ),
    "HASURA_GRAPHQL_METADATA_DEFAULTS": (
        "Default metadata behavior/settings applied by the server. "
        "This setting is version/feature dependent; consult Hasura docs for the "
        "expected JSON/YAML structure for your version."
    ),
    "HASURA_GRAPHQL_METRICS_SECRET": (
        "Secret/token used to protect metrics endpoints where supported. "
        "Treat this as sensitive and provide via Secret/Vault."
    ),
    "HASURA_GRAPHQL_QUERY_PLAN_CACHE_SIZE": (
        "Deprecated/legacy query plan cache size setting. Newer versions may "
        "ignore this value; consult the server help/docs for your version."
    ),
    "HASURA_GRAPHQL_REMOTE_SCHEMA_PRIORITIZE_DATA": (
        "Prefer data from the remote schema when there are field conflicts. "
        "This is a niche compatibility knob; leave unset unless needed."
    ),
    "HASURA_GRAPHQL_SSO_PROVIDERS": (
        "Single sign-on provider configuration (JSON array). "
        "Treat as sensitive if it contains client secrets."
    ),
    "LUX_AUTH_ISSUER": "Issuer used for Lux authentication (Pro/EE).",
    "LUX_LOG_SEND_INTERVAL": "Interval (seconds) at which logs are sent to Lux (Pro/EE).",
    "LUX_LOG_SINK_URL": "Lux log sink URL (Pro/EE).",
    "LUX_METRICS_URL": "Lux metrics server URL (Pro/EE).",
    "LUX_OAUTH_AUTHORIZATION_URL": "OAuth authorization URL used for Lux auth (Pro/EE).",
    "LUX_OAUTH_TOKEN_URL": "OAuth token URL used for Lux auth (Pro/EE).",
    "LUX_OAUTH_JWK_URL": "OAuth JWK URL used for Lux auth (Pro/EE).",
    "LUX_SCHEME": "Lux scheme/identifier used by the Hasura Pro/EE telemetry client.",
    "LUX_VALIDATE_PAT_URL": "Endpoint used to validate personal access tokens for Lux auth (Pro/EE).",
}


# Defaults are not always spelled out in the help text. For booleans that are
# implemented as flags (e.g. --dev-mode), Hasura typically defaults to false.
MANUAL_DEFAULTS: Dict[str, str] = {
    "HASURA_GRAPHQL_DEV_MODE": "false",
    "HASURA_GRAPHQL_DISABLE_CORS": "false",
    "HASURA_GRAPHQL_ENABLE_ALLOWLIST": "false",
    "HASURA_GRAPHQL_ENABLE_CONSOLE": "false",
    "HASURA_GRAPHQL_ENABLE_MAINTENANCE_MODE": "false",
    "HASURA_GRAPHQL_ENABLE_METADATA_QUERY_LOGGING": "false",
    "HASURA_GRAPHQL_ENABLE_PERSISTED_QUERIES": "false",
    "HASURA_GRAPHQL_ENABLE_REMOTE_SCHEMA_PERMISSIONS": "false",
    "HASURA_GRAPHQL_ENABLE_TRIGGERS_ERROR_LOG_LEVEL": "false",
    "HASURA_GRAPHQL_REMOTE_SCHEMA_SKIP_NULLS": "false",
    "HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES": "false",
    "HASURA_GRAPHQL_V1_BOOLEAN_NULL_COLLAPSE": "false",
    "HASURA_GRAPHQL_WS_READ_COOKIE": "false",
    # Help text in some versions contains a typo ("read-commited"). Prefer the
    # canonical value.
    "HASURA_GRAPHQL_TX_ISOLATION": "read-committed",
    "LUX_DISABLE_LOG_SENDER": "false",
}


def _run(cmd: Sequence[str], *, text: bool = True) -> str:
    return subprocess.check_output(list(cmd), text=text, stderr=subprocess.STDOUT)


def _get_hasura_image() -> str:
    if os.environ.get("HASURA_IMAGE"):
        return os.environ["HASURA_IMAGE"]

    if not HASURA_MANIFEST.exists():
        raise FileNotFoundError(f"Missing {HASURA_MANIFEST}")

    image_re = re.compile(r"^\s*image:\s*(\S+)\s*$")
    for line in HASURA_MANIFEST.read_text(encoding="utf-8").splitlines():
        m = image_re.match(line)
        if m:
            return m.group(1)

    raise RuntimeError(f"Unable to find an image in {HASURA_MANIFEST}")


def _extract_env_vars_from_binary(image: str) -> List[str]:
    """
    Extract env var names from the graphql-engine binary using `strings`.

    This is the closest thing to a version-accurate inventory of supported env
    vars without depending on external docs.
    """

    cid = _run(["docker", "create", image]).strip()
    try:
        with tempfile.TemporaryDirectory(prefix="hasura-config-") as td:
            td_path = Path(td)
            bin_path = td_path / "graphql-engine"
            _run(["docker", "cp", f"{cid}:/usr/bin/graphql-engine", str(bin_path)])

            envs: Set[str] = set()
            env_re = re.compile(r"\b(HASURA_GRAPHQL_[A-Z0-9_]+|LUX_[A-Z0-9_]+)\b")
            proc = subprocess.Popen(
                ["strings", str(bin_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="ignore",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                for m in env_re.findall(line):
                    envs.add(m)
            proc.wait()
            return sorted(envs)
    finally:
        # Best-effort cleanup.
        subprocess.run(["docker", "rm", "-f", cid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _help_texts(image: str) -> Tuple[str, str]:
    root_help = _run(["docker", "run", "--rm", "--entrypoint", "graphql-engine", image, "--help"])
    serve_help = _run(["docker", "run", "--rm", "--entrypoint", "graphql-engine", image, "serve", "--help"])
    return root_help, serve_help


def _parse_env_docs(text: str, *, only: Optional[Set[str]] = None) -> Dict[str, str]:
    """
    Parse help output sections that list environment variables, returning:
      env_var -> doc string (single paragraph, wrapped later)
    """

    docs: Dict[str, str] = {}

    # Example:
    #   HASURA_GRAPHQL_SERVER_PORT            Port on which ...
    #                                        (default: 8080)
    # Some help output lines have the env var name alone on the line, with the
    # description on subsequent indented lines.
    start_re = re.compile(r"^\s{2}([A-Z][A-Z0-9_]+)(?:\s{2,}(.*\S))?\s*$")

    current: Optional[str] = None
    buf: List[str] = []

    def sanitize(s: str) -> str:
        # Keep generated files ASCII-only (friendlier diffs / tooling).
        return (
            s.replace("‘", "'")
            .replace("’", "'")
            .replace("“", '"')
            .replace("”", '"')
            .replace("\u2013", "-")
            .replace("\u2014", "-")
        )

    def flush() -> None:
        nonlocal current, buf
        if current is None:
            return
        # Join wrapped help lines into a single doc paragraph.
        doc = sanitize(" ".join(part.strip() for part in buf if part.strip()))
        if doc:
            docs[current] = doc
        current = None
        buf = []

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        m = start_re.match(line)
        if m:
            env = m.group(1)
            if only is not None and env not in only:
                flush()
                current = None
                buf = []
                continue
            flush()
            current = env
            first = (m.group(2) or "").strip()
            buf = [first] if first else []
            continue

        if current is None:
            continue

        if not line.strip():
            flush()
            continue

        # Continuation lines are indented.
        if line.startswith(" ") or line.startswith("\t"):
            buf.append(line.strip())
            continue

        # Any other non-empty non-indented line terminates current env var entry.
        flush()

    flush()
    return docs


def _parse_existing_env_file(path: Path) -> Dict[str, str]:
    """
    Parse existing config env file to preserve current settings on regeneration.

    We only keep straightforward `NAME=value` assignments that are not commented.
    Values are kept "as-is" (including quotes) so shell semantics are preserved.
    """

    if not path.exists():
        return {}

    existing: Dict[str, str] = {}
    assign_re = re.compile(r"^([A-Z][A-Z0-9_]+)=(.*)$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = assign_re.match(line)
        if not m:
            continue
        existing[m.group(1)] = m.group(2)
    return existing


def _parse_manifest_env_overrides(path: Path) -> Dict[str, str]:
    """
    Parse any hard-coded env values from manifests/hasura.yaml (one-time import).

    This is intentionally lightweight (no YAML parser dependency). It supports
    the common `- name: FOO` / `value: "bar"` pattern used in this repo.
    """

    if not path.exists():
        return {}

    overrides: Dict[str, str] = {}
    name_re = re.compile(r"^\s*-\s*name:\s*([A-Z][A-Z0-9_]+)\s*$")
    value_re = re.compile(r"^\s*value:\s*(.*\S)\s*$")

    current: Optional[str] = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = name_re.match(raw)
        if m:
            current = m.group(1)
            continue
        if current is None:
            continue
        mv = value_re.match(raw)
        if not mv:
            continue

        # Keep the YAML scalar as a string and re-quote safely for sh.
        val = mv.group(1).strip()
        if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
            val = val[1:-1]
        overrides[current] = _sh_quote(val)
        current = None

    return overrides


def _sh_quote(value: str) -> str:
    """
    Quote a value for POSIX sh as a single-quoted string.
    """

    # Empty string needs explicit quotes.
    if value == "":
        return "''"
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _sh_unquote_simple(value_rhs: str) -> str:
    """
    Best-effort unquoting for values produced by `_sh_quote`.

    This is intentionally minimal and only needs to handle our generated
    defaults (no embedded newlines). If the value isn't single-quoted, it's
    returned unchanged.
    """

    v = value_rhs.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        inner = v[1:-1]
        # Reverse the common `'\"'\"'` escape used to embed single quotes in a
        # single-quoted POSIX shell string.
        return inner.replace("'\"'\"'", "'")
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def _wrap_comment(text: str, *, width: int = 92, indent: str = "# ") -> List[str]:
    words = text.split()
    if not words:
        return []
    lines: List[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
            continue
        if len(cur) + 1 + len(w) <= (width - len(indent)):
            cur += " " + w
        else:
            lines.append(indent + cur)
            cur = w
    if cur:
        lines.append(indent + cur)
    return lines


def _section_for(var: str) -> str:
    if var.startswith("HASURA_GRAPHQL_PG_") or var in {
        "HASURA_GRAPHQL_DATABASE_URL",
        "HASURA_GRAPHQL_METADATA_DATABASE_URL",
        "HASURA_GRAPHQL_NO_OF_RETRIES",
        "HASURA_GRAPHQL_READ_REPLICA_URLS",
        "HASURA_GRAPHQL_TX_ISOLATION",
        "HASURA_GRAPHQL_USE_PREPARED_STATEMENTS",
        "HASURA_GRAPHQL_PG_SSL_CERTIFICATE_PATH",
        "HASURA_GRAPHQL_STRIPES_PER_READ_REPLICA",
        "HASURA_GRAPHQL_CONNECTIONS_PER_READ_REPLICA",
    }:
        return "Database"
    if var in {"HASURA_GRAPHQL_SERVER_HOST", "HASURA_GRAPHQL_SERVER_PORT", "HASURA_GRAPHQL_MAX_TOTAL_HEADER_LENGTH"}:
        return "Server"
    if var.startswith("HASURA_GRAPHQL_AUTH_") or var.startswith("HASURA_GRAPHQL_JWT") or var.startswith(
        "HASURA_GRAPHQL_ADMIN_"
    ) or var in {"HASURA_GRAPHQL_UNAUTHORIZED_ROLE"}:
        return "Auth"
    if var in {"HASURA_GRAPHQL_DISABLE_CORS", "HASURA_GRAPHQL_CORS_DOMAIN"}:
        return "CORS"
    if var.startswith("HASURA_GRAPHQL_CONSOLE") or var == "HASURA_GRAPHQL_ENABLE_CONSOLE":
        return "Console"
    if var.startswith("HASURA_GRAPHQL_ENABLED_LOG_TYPES") or var.startswith("HASURA_GRAPHQL_LOG_") or var in {
        "HASURA_GRAPHQL_DEV_MODE",
        "HASURA_GRAPHQL_ADMIN_INTERNAL_ERRORS",
        "HASURA_GRAPHQL_ENABLE_METADATA_QUERY_LOGGING",
        "HASURA_GRAPHQL_ENABLE_TRIGGERS_ERROR_LOG_LEVEL",
        "HASURA_GRAPHQL_PRO_ENABLE_LOG_COMPRESSION",
        "LUX_DISABLE_LOG_SENDER",
    }:
        return "Logging"
    if var.startswith("HASURA_GRAPHQL_EVENTS_") or var.startswith("HASURA_GRAPHQL_ASYNC_ACTIONS_"):
        return "Events"
    if var.startswith("HASURA_GRAPHQL_LIVE_QUERIES_") or var.startswith("HASURA_GRAPHQL_STREAMING_QUERIES_"):
        return "Subscriptions"
    if var.startswith("HASURA_GRAPHQL_WEBSOCKET_") or var in {"HASURA_GRAPHQL_CONNECTION_COMPRESSION", "HASURA_GRAPHQL_WS_READ_COOKIE"}:
        return "WebSocket"
    if var.startswith("HASURA_GRAPHQL_ENABLE_") or var.startswith("HASURA_GRAPHQL_EXPERIMENTAL_") or var in {
        "HASURA_GRAPHQL_INFER_FUNCTION_PERMISSIONS",
        "HASURA_GRAPHQL_SCHEMA_SYNC_POLL_INTERVAL",
        "HASURA_GRAPHQL_DEFAULT_NAMING_CONVENTION",
        "HASURA_GRAPHQL_METADATA_DATABASE_EXTENSIONS_SCHEMA",
        "HASURA_GRAPHQL_REMOTE_SCHEMA_SKIP_NULLS",
        "HASURA_GRAPHQL_REMOTE_SCHEMA_PRIORITIZE_DATA",
        "HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES",
        "HASURA_GRAPHQL_V1_BOOLEAN_NULL_COLLAPSE",
        "HASURA_GRAPHQL_BACKWARDS_COMPAT_NULL_IN_NONNULLABLE_VARIABLES",
        "HASURA_GRAPHQL_CONFIGURED_HEADER_PRECEDENCE",
        "HASURA_GRAPHQL_GRACEFUL_SHUTDOWN_TIMEOUT",
    }:
        return "Behavior"
    if var.startswith("HASURA_GRAPHQL_REDIS_") or var.startswith("HASURA_GRAPHQL_RATE_LIMIT_REDIS_") or var.startswith(
        "HASURA_GRAPHQL_CACHE_"
    ) or var in {"HASURA_GRAPHQL_MAX_CACHE_SIZE"}:
        return "Redis/Cache"
    if var.startswith("HASURA_GRAPHQL_PRO_") or var.startswith("HASURA_GRAPHQL_EE_") or var in {
        "HASURA_GRAPHQL_PROJECT_CONFIG",
        "HASURA_GRAPHQL_SSO_PROVIDERS",
    }:
        return "Pro/EE"
    if var.startswith("LUX_"):
        return "Lux"
    return "Other"


def _default_for(var: str, doc: str) -> Optional[str]:
    """
    Best-effort extraction of a machine-usable default from the doc string.

    Returns a raw unquoted string (we quote later) or None to mean "leave unset".
    """

    if var in MANUAL_DEFAULTS:
        return MANUAL_DEFAULTS[var]

    # Common pattern: "(default: 8080)" or "(Default: 1MB)"
    m = re.search(r"\((?:default|Default):\s*([^\)]+)\)", doc)
    if m:
        return m.group(1).strip()

    # Other patterns: "Default: public" or "Default 1000 (1s)"
    m = re.search(r"\bDefault:\s*([^\.\n]+?)(?:\.|$)", doc)
    if m:
        return m.group(1).strip()

    m = re.search(r"\bDefault\s+([0-9]+)\b", doc)
    if m:
        return m.group(1).strip()

    return None


def _normalize_default(var: str, default: str, doc: str) -> Optional[str]:
    """
    Convert the doc-extracted default into something safe to assign.

    Returns a raw value string or None to mean "don't assign explicitly".
    """

    d = default.strip()
    if not d:
        return None

    # Avoid writing "forever" to numeric env vars; leaving unset preserves default.
    if d.lower() == "forever":
        return None

    doc_l = doc.lower()

    # Hasura shows this default as "1MB" in help output, but the env var expects
    # an Int (bytes).
    if var == "HASURA_GRAPHQL_MAX_TOTAL_HEADER_LENGTH":
        m_size = re.match(r"^([0-9]+)\s*([KMG]B)$", d, flags=re.IGNORECASE)
        if m_size:
            n = int(m_size.group(1))
            unit = m_size.group(2).upper()
            mult = {"KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024}[unit]
            return str(n * mult)

    # If the default starts with a number and then has units/explanation, prefer
    # the numeric token. This avoids writing defaults like "1000 (1sec)" that
    # the server cannot parse as an Int.
    #
    # We *intentionally* do NOT match values like "1MB" (no word boundary).
    m = re.match(r"^([0-9][0-9,]*)\b(.*)$", d)
    if m:
        num_raw = m.group(1)
        rest = (m.group(2) or "").strip().lower()
        num = num_raw.replace(",", "")

        # If the doc says "milliseconds" but the default is expressed as seconds
        # (e.g. "1 second"), convert seconds -> milliseconds.
        if "millisecond" in doc_l and ("sec" in rest or "second" in rest) and "(" not in d:
            try:
                return str(int(num) * 1000)
            except ValueError:
                pass

        return num

    # Keep as-is for:
    # - booleans: true/false
    # - lists: "metadata,graphql,pgdump,config"
    # - enums: info/debug, graphql-default/hasura-default, etc.
    return d


def _render_env(
    *,
    image: str,
    vars_all: List[str],
    docs: Dict[str, str],
    existing_values: Dict[str, str],
    manifest_overrides: Dict[str, str],
    env_overrides: Dict[str, str],
) -> str:
    now = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    header: List[str] = [
        "# Generated by scripts/hasura/generate_hasura_config.py",
        f"# Image: {image}",
        f"# Generated (UTC): {now}",
        "#",
        "# This file is sourced by the Hasura container in manifests/hasura.yaml.",
        "# Env vars set directly on the Deployment container take precedence; this file provides defaults.",
        "# It is stored in a Kubernetes ConfigMap (NOT a Secret). Do not put secrets here.",
        "#",
        "# Regenerate after upgrading the Hasura image:",
        "#   python3 scripts/hasura/generate_hasura_config.py",
        "#",
    ]

    # Preserve your current settings.
    overrides: Dict[str, str] = {}
    overrides.update(manifest_overrides)
    overrides.update(existing_values)
    overrides.update(env_overrides)

    # Sort into stable sections.
    sections: Dict[str, List[str]] = {}
    for v in vars_all:
        sections.setdefault(_section_for(v), []).append(v)
    for k in sections:
        sections[k].sort()

    section_order = [
        "Database",
        "Server",
        "Auth",
        "CORS",
        "Console",
        "Logging",
        "Events",
        "Subscriptions",
        "WebSocket",
        "Behavior",
        "Redis/Cache",
        "Pro/EE",
        "Lux",
        "Other",
    ]

    lines: List[str] = []
    lines.extend(header)

    for section in section_order:
        vars_in_section = sections.get(section, [])
        if not vars_in_section:
            continue
        lines.append(f"# --- {section} ---")
        for var in vars_in_section:
            doc = docs.get(var) or MANUAL_DOCS.get(var) or "No help text available for this setting."

            # Comment block.
            lines.append(f"# {var}")
            lines.extend(_wrap_comment(doc))

            # Value selection:
            # - Keep secrets unset in the ConfigMap (unless user already committed them).
            # - Preserve existing values; otherwise use defaults when safe.
            value_rhs: Optional[str] = None

            if var in overrides:
                value_rhs = overrides[var]
                if var == "HASURA_GRAPHQL_TX_ISOLATION":
                    # Some versions emit a typo ("read-commited") in help output.
                    # If the existing config contains that typo, auto-correct it.
                    unquoted = value_rhs.strip().strip("\"'").strip()
                    if unquoted == "read-commited":
                        value_rhs = _sh_quote("read-committed")
            else:
                default_raw = _default_for(var, doc)
                if default_raw is not None:
                    normalized = _normalize_default(var, default_raw, doc)
                    if normalized is not None:
                        value_rhs = _sh_quote(normalized)

            if var in SENSITIVE_VARS:
                # Always keep the assignment commented unless the repo already
                # contains an explicit value for it.
                if var in existing_values:
                    lines.append(f"{var}={existing_values[var]}")
                else:
                    # If this setting is required for boot (e.g. DB URL), show
                    # it as commented-out with a placeholder.
                    placeholder = ""
                    if var in {"HASURA_GRAPHQL_DATABASE_URL", "HASURA_GRAPHQL_ADMIN_SECRET"}:
                        placeholder = "<set-via-vault-or-secret>"
                    if placeholder:
                        lines.append(f"# {var}={_sh_quote(placeholder)}")
                    else:
                        lines.append(f"# {var}=")
            else:
                if value_rhs is None:
                    # Default is "unset" or unknown. Keep commented to preserve
                    # server defaults.
                    lines.append(f"# {var}=")
                else:
                    lines.append(f"{var}={value_rhs}")

            lines.append("")  # spacer
        lines.append("")  # spacer between sections

    # If the manifest currently sets env vars that are not supported by the image,
    # call it out at the end for cleanup.
    unknown_manifest_vars = sorted(
        v for v in manifest_overrides.keys() if (v.startswith("HASURA_") or v.startswith("LUX_")) and v not in vars_all
    )
    if unknown_manifest_vars:
        lines.append("# --- Notes ---")
        lines.append("# The following env vars were found in manifests/hasura.yaml but were not")
        lines.append("# detected in the graphql-engine binary for this image and may be ignored:")
        for v in unknown_manifest_vars:
            lines.append(f"# - {v}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_configmap(*, env_text: str) -> str:
    # Store env file verbatim as a single key.
    indented = "\n".join("    " + line if line else "" for line in env_text.splitlines())
    if not indented.endswith("\n"):
        indented += "\n"
    return (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: hasura-config\n"
        "  namespace: graphql\n"
        "data:\n"
        "  hasura.env: |\n"
        f"{indented}"
    )


def main() -> int:
    image = _get_hasura_image()

    # Discover supported env vars from the binary (version-accurate inventory).
    vars_all = _extract_env_vars_from_binary(image)

    # Parse help output to attach docs to as many env vars as possible.
    root_help, serve_help = _help_texts(image)
    docs = {}
    docs.update(_parse_env_docs(root_help, only=set(vars_all)))
    docs.update(_parse_env_docs(serve_help, only=set(vars_all)))

    # Preserve existing settings.
    existing_values = _parse_existing_env_file(OUT_ENV)

    # One-time migration: pull any env values currently set in the deployment manifest.
    manifest_overrides = _parse_manifest_env_overrides(HASURA_MANIFEST)

    # Fix up previously-generated defaults that are not machine-parseable.
    # Example: help text may say "Default: 1000 (1sec)" but the server expects
    # just "1000".
    fixed_existing = dict(existing_values)
    for var, rhs in existing_values.items():
        doc = docs.get(var) or MANUAL_DOCS.get(var) or ""
        if not doc:
            continue
        default_raw = _default_for(var, doc)
        if default_raw is None:
            continue
        normalized = _normalize_default(var, default_raw, doc)
        if normalized is None:
            continue

        raw = _sh_unquote_simple(rhs)

        # If the existing value is exactly the doc default, but normalization
        # would change it, update to the normalized form.
        if raw == default_raw and normalized != default_raw:
            fixed_existing[var] = _sh_quote(normalized)
            continue

        # If the doc says milliseconds, but the default is written in seconds,
        # older generator versions may have stored the seconds value.
        doc_l = doc.lower()
        if "millisecond" in doc_l and "second" in default_raw.lower():
            m = re.match(r"^([0-9][0-9,]*)\b", default_raw)
            if m:
                seconds = m.group(1).replace(",", "")
                if raw == seconds and normalized != seconds:
                    fixed_existing[var] = _sh_quote(normalized)

    existing_values = fixed_existing

    # Optional: allow your shell environment to override non-sensitive settings
    # when generating (useful for "my settings" without editing the file).
    env_overrides: Dict[str, str] = {}
    for v in vars_all:
        if v in SENSITIVE_VARS:
            continue
        if v in os.environ:
            env_overrides[v] = _sh_quote(os.environ[v])

    # Generate outputs.
    OUT_ENV.parent.mkdir(parents=True, exist_ok=True)
    env_text = _render_env(
        image=image,
        vars_all=vars_all,
        docs=docs,
        existing_values=existing_values,
        manifest_overrides=manifest_overrides,
        env_overrides=env_overrides,
    )
    OUT_ENV.write_text(env_text, encoding="utf-8")

    configmap_text = _render_configmap(env_text=env_text)
    OUT_CONFIGMAP.write_text(configmap_text, encoding="utf-8")

    # Minimal summary to stderr so CI logs are readable.
    sys.stderr.write(f"Wrote {OUT_ENV.relative_to(REPO_ROOT)}\n")
    sys.stderr.write(f"Wrote {OUT_CONFIGMAP.relative_to(REPO_ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
