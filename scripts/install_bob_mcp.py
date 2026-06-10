#!/usr/bin/env python3
"""Script to Install or update a FastMCP server in Bob's .bob/mcp.json config.

This script will create a .bob/mcp.json config file in the project directory.

Runs `fastmcp install mcp-json` to generate the server command/args, wraps the
result in Bob's `mcpServers` structure, adds `transport: "stdio"`, and upserts
into the output file (creates it if missing).

Usage (run from a project that has fastmcp.json):

uv run scripts/install_bob_mcp.py ./fastmcp.json \
    --project "$PWD" \
    --env-file .env

Options:
    --project PATH    uv project directory (default: current directory)
    --env-file PATH   load env vars into the server config
    --env KEY=VALUE   add env vars (repeatable)
    --name NAME       override the server name in mcp.json
    --output PATH     target file (default: ./.bob/mcp.json)

Validate the config using Bob advanced mode or the fastMCP cli:

fastmcp call ./.bob/mcp.json <tool_name> --input-json '{...}'
Example:
fastmcp call ./.bob/mcp.json \
    kubecost_get_cluster_cost_by_workload \
    --input-json '{"aggregate": "cluster,namespace"}'
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install a FastMCP server into Bob's .bob/mcp.json config.",
    )
    parser.add_argument(
        "fastmcp_json",
        type=Path,
        help="Path to fastmcp.json or server.py",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path.cwd(),
        help="Project directory for uv run (default: current directory)",
    )
    parser.add_argument(
        "--name",
        help="Custom server name in MCP config",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bob/mcp.json"),
        help="Target Bob MCP config path (default: ./.bob/mcp.json)",
    )
    return parser.parse_args()


def run_fastmcp_mcp_json(args: argparse.Namespace) -> dict[str, Any]:
    cmd = [
        "uv",
        "run",
        "fastmcp",
        "install",
        "mcp-json",
        str(args.fastmcp_json.resolve()),
        "--project",
        str(args.project.resolve()),
    ]
    # Env vars are applied after parsing; fastmcp's Rich print wraps long env
    # values and produces invalid JSON when --env-file is forwarded.
    if args.name:
        cmd.extend(["--name", args.name])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        sys.exit(result.returncode)

    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse fastmcp output as JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(config, dict) or not config:
        print("fastmcp output must be a non-empty JSON object.", file=sys.stderr)
        sys.exit(1)

    for server_name, server_config in config.items():
        if not isinstance(server_config, dict):
            print(f"Invalid server config for {server_name!r}.", file=sys.stderr)
            sys.exit(1)
        if "command" not in server_config or "args" not in server_config:
            print(
                f"Server config for {server_name!r} must include command and args.",
                file=sys.stderr,
            )
            sys.exit(1)

    return config


def load_bob_config(output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        try:
            content = output_path.read_text().strip()
        except (OSError, UnicodeDecodeError) as e:
            print(f"\nError reading {output_path}: {e}", file=sys.stderr)
            sys.exit(1)

        if content:
            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                print(f"\nError parsing JSON in {output_path}: {e}", file=sys.stderr)
                sys.exit(1)

            if not isinstance(data, dict):
                print(
                    f"Invalid config in {output_path}: expected JSON object.",
                    file=sys.stderr,
                )
                sys.exit(1)
            mcp_servers = data.get("mcpServers")
            if mcp_servers is None:
                mcp_servers = {}
            elif not isinstance(mcp_servers, dict):
                print(
                    f"Invalid mcpServers in {output_path}: expected object.",
                    file=sys.stderr,
                )
                sys.exit(1)
            return {"mcpServers": mcp_servers}

    return {"mcpServers": {}}


def upsert_servers(
    bob_config: dict[str, Any],
    flat_config: dict[str, Any],
) -> list[str]:
    updated_names: list[str] = []
    mcp_servers = bob_config.setdefault("mcpServers", {})

    for server_name, server_config in flat_config.items():
        server_config = dict(server_config)
        server_config["transport"] = "stdio"

        if server_name in mcp_servers:
            mcp_servers[server_name] = {**mcp_servers[server_name], **server_config}
        else:
            mcp_servers[server_name] = server_config

        updated_names.append(server_name)

    return updated_names


def write_bob_config(output_path: Path, bob_config: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(bob_config, indent=4) + "\n")


def main() -> None:
    args = parse_args()
    flat_config = run_fastmcp_mcp_json(args)
    bob_config = load_bob_config(args.output)
    updated_names = upsert_servers(bob_config, flat_config)
    write_bob_config(args.output, bob_config)

    names = ", ".join(updated_names)
    print(f"Updated Bob MCP config for {names} in {args.output}")


if __name__ == "__main__":
    main()
