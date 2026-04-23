"""Smoke-test the Lakebase Data API client.

Required env vars:
    LAKEBASE_API_URL    https://<lakebase-host>/api/2.0/workspace/<workspace-id>/rest/<database>
    LAKEBASE_API_TOKEN  optional; falls back to databricks-sdk ambient auth

Example:
    export LAKEBASE_API_URL="https://ep-xxxx.database.<region>.azuredatabricks.net/api/2.0/workspace/<workspace-id>/rest/databricks_postgres"
    export LAKEBASE_API_TOKEN=$(databricks postgres generate-database-credential \
        projects/<project>/branches/<branch>/endpoints/<endpoint> \
        -p DEFAULT -o json | jq -r '.token')
    python src/test_lakebase_api.py
"""

import requests

from src.lakebase_utils.lakebase_api import LakebaseDataApiClient


def main() -> None:
    with LakebaseDataApiClient(
        base_url="https://ep-crimson-leaf-e1dz6s0k.database.eastus2.azuredatabricks.net/api/2.0/workspace/984752964297111/rest/databricks_postgres",
        auth_mode = "user_oauth",
        profile="LAKEBASE_READER"
    ) as client:
        print(f"base={client.base_url}")

        try:
            # # Single page
            rows = client.get("manual_tests", "synced_cdf_source_table", params={"limit": 5})
            print(f"single-page fetch: {len(rows)} row(s)")
            for row in rows:
                print(" ", row)

            # Paginated iteration (small page size to prove multi-page behavior)
            total = 0
            for row in client.paginate("manual_tests", "synced_cdf_source_table", page_size=1, max_rows=1):
                total += 1
                print(f"paginate(page_size=2, max_rows=1): {total} row(s) yielded")
        except requests.HTTPError as e:
            print(f"HTTP {e.response.status_code}: {e.response.text}")
            raise


if __name__ == "__main__":
    main()
