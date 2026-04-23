
from lakebase_connect import LakebaseAutoscalingClient
import os

#
# with LakebaseAutoscalingClient(
#     auth_mode="oauth_token",
#     connection_string="postgresql://78e7e02f-9ad0-4e1b-a648-e9154cf020a0@ep-crimson-leaf-e1dz6s0k.database.eastus2.azuredatabricks.net/databricks_postgres?sslmode=require",
#     token=os.environ["LAKEBASE_TOKEN"],
# ) as client:
#     print(client.fetch("SELECT current_user, now()"))
#     print(client.fetch("select * from manual_tests.synced_cdf_source_table"))


client = LakebaseAutoscalingClient(
    auth_mode="user_oauth",
    connection_string="postgresql://me%40example.com@ep-xxxx.database.eastus2.azuredatabricks.net/databricks_postgres?sslmode=require",
    endpoint_path="projects/my-proj/branches/production/endpoints/primary",
)

df = client.select(
    """
    SELECT table_schema, table_name, table_type
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name
    """,
    spark=spark,
)
df.display()