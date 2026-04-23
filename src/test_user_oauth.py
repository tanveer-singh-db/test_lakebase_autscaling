"""Smoke-test `LakebaseAutoscalingClient` in user_oauth mode.

Matches the official doc's Python SDK pattern:

    w = WorkspaceClient()  # ambient auth — no host, no profile

The SDK picks up credentials from (in order): env vars, the active Databricks
notebook runtime, or `~/.databrickscfg`. Locally:

    databricks auth login --host <workspace-url> --profile <name>
    export DATABRICKS_CONFIG_PROFILE=<name>

In a Databricks notebook, ambient auth is automatic — no extra setup.

Required env vars:
    LAKEBASE_URL            postgresql://<email-url-encoded>@<host>/<db>?sslmode=require
    LAKEBASE_ENDPOINT_PATH  projects/<project>/branches/<branch>/endpoints/<endpoint>
"""

import os
import sys
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.lakebase_utils.lakebase_connect import LakebaseAutoscalingClient  # noqa: E402


def _parse(url: str) -> dict:
    u = urlparse(url)
    return {
        "host": u.hostname,
        "port": u.port or 5432,
        "database": u.path.lstrip("/") or "postgres",
        "user": unquote(u.username or ""),
    }


def main() -> None:
    url = os.environ["LAKEBASE_URL"]
    endpoint_path = os.environ["LAKEBASE_ENDPOINT_PATH"]

    cfg = _parse(url)
    print(f"host={cfg['host']} db={cfg['database']} user={cfg['user']}")

    with LakebaseAutoscalingClient(
        host=cfg["host"],
        database=cfg["database"],
        port=cfg["port"],
        auth_mode="user_oauth",
        oauth_user=cfg["user"],
        endpoint_path=endpoint_path,
    ) as client:
        print("fetch:", client.fetch("SELECT current_user, current_database(), now()"))


if __name__ == "__main__":
    main()
