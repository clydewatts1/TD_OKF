
from azure.identity import InteractiveBrowserCredential
from databricks import sql
from dotenv import load_dotenv
import os

load_dotenv()

SERVER_HOSTNAME = os.getenv("DATABRICKS_SERVER_HOSTNAME")
HTTP_PATH = os.getenv("DATABRICKS_HTTP_PATH")
TARGET_CATALOG = os.getenv("TARGET_CATALOG")
TARGET_SCHEMA = os.getenv("TARGET_SCHEMA")

def test_databricks_connection():
    if not all([SERVER_HOSTNAME, HTTP_PATH]):
        print("Error: Missing required environment variables.")
        return

    print("\nOpening browser for SSO login — check your taskbar/browser...")
    try:
        credential = InteractiveBrowserCredential()
        token = credential.get_token("2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default")
        print("SSO login complete. Connecting to SQL warehouse...")

        connection = sql.connect(
            server_hostname=SERVER_HOSTNAME,
            http_path=HTTP_PATH,
            access_token=token.token
        )
        print("Connection established. Running test queries...")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_version(), current_catalog(), current_user()")
            result = cursor.fetchone()
            print("\n✅ Connection Successful!")
            print(f"  Databricks Runtime : {result[0]}")
            print(f"  Default Catalog    : {result[1]}")
            print(f"  Logged in as       : {result[2]}")

            if TARGET_CATALOG and TARGET_SCHEMA:
                cursor.execute(f"SHOW TABLES IN {TARGET_CATALOG}.{TARGET_SCHEMA}")
                tables = cursor.fetchall()
                print(f"\n  Tables in {TARGET_CATALOG}.{TARGET_SCHEMA}: {len(tables)} found")

        connection.close()
        print("\nConnection closed cleanly.")

    except Exception as e:
        print(f"\n❌ Connection Failed:\n{e}")

def list_all_catalogs_and_schemas():
    try:
        credential = InteractiveBrowserCredential()
        token = credential.get_token("2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default")
        connection = sql.connect(
            server_hostname=SERVER_HOSTNAME,
            http_path=HTTP_PATH,
            access_token=token.token
        )
        with connection.cursor() as cursor:
            cursor.execute("SHOW CATALOGS")
            catalogs = cursor.fetchall()
            print("\nAvailable Catalogs:")
            for catalog in catalogs:
                print(f"  - {catalog[0]}")
                cursor.execute(f"SHOW SCHEMAS IN {catalog[0]}")
                schemas = cursor.fetchall()
                for schema in schemas:
                    print(f"    - {schema[0]}")

        connection.close()
    except Exception as e:
        print(f"\n❌ Failed to list catalogs and schemas:\n{e}")

if __name__ == "__main__":
    test_databricks_connection()
    list_all_catalogs_and_schemas()