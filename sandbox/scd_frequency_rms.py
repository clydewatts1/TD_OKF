import os
import sys
from datetime import datetime
import teradatasql
from dotenv import load_dotenv

load_dotenv()

TD_HOST = os.getenv("TERADATA_HOST")
TD_USER = os.getenv("TERADATA_USER")
TD_PASSWORD = os.getenv("TERADATA_PASSWORD")
TD_LOGMECH = os.getenv("TERADATA_LOGMECH", "TD2")
TARGET_DB = os.getenv("DATABASE_METADATA", "DWB02T_SANDBOX")
TARGET_TABLE = os.getenv("TABLE_SCD_COUNT", "scd_frequency_metrics")

SOURCE_DB = "DWP01T_SRCI_RMS"
SCD_COLUMNS = ['EFF_FROM_DTTM', 'START_DT']

IS_BROWSER_AUTH = TD_LOGMECH.upper() in ["BROWSER"]

required_params = [TD_HOST, TARGET_DB]
if not IS_BROWSER_AUTH:
    required_params.extend([TD_USER, TD_PASSWORD])

if not all(required_params):
    print("Error: Missing database connection details in the .env file.")
    sys.exit(1)

def get_teradata_connection():
    try:
        print(f"Connecting to Teradata host '{TD_HOST}' using login mechanism '{TD_LOGMECH}'...")
        if IS_BROWSER_AUTH:
            conn = teradatasql.connect(host=TD_HOST, logmech=TD_LOGMECH)
        else:
            conn = teradatasql.connect(host=TD_HOST, user=TD_USER, password=TD_PASSWORD, logmech=TD_LOGMECH)
        print("Successfully connected to Teradata!")
        return conn
    except Exception as e:
        print(f"Failed to connect to Teradata database: {e}")
        sys.exit(1)

def initialize_scd_tracking_table(cursor):
    full_target_name = f'"{TARGET_DB}"."{TARGET_TABLE}"'
    create_table_ddl = f"""
    CREATE SET TABLE {full_target_name} , NO FALLBACK ,
         NO BEFORE JOURNAL,
         NO AFTER JOURNAL,
         CHECKSUM = DEFAULT,
         DEFAULT MERGEBLOCKRATIO
         (
          DatabaseName VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC,
          TableName VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC,
          ColumnName VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC,
          SCD_ColumnValue TIMESTAMP(6),
          RowCount BIGINT,
          ExtractionTimestamp TIMESTAMP(6)
         )
    PRIMARY INDEX ( DatabaseName , TableName );
    """

    try:
        check_query = """
        SELECT 1
        FROM DBC.TablesV
        WHERE UPPER(DatabaseName) = UPPER(?)
          AND UPPER(TableName) = UPPER(?)
        """
        cursor.execute(check_query, [TARGET_DB, TARGET_TABLE])

        if cursor.fetchone():
            print(f"Target table {full_target_name} already exists.")
        else:
            print(f"Creating target table {full_target_name}...")
            cursor.execute(create_table_ddl)
            print("Target table created successfully.")
    except Exception as e:
        if "3803" in str(e):
            print(f"Target table {full_target_name} already exists.")
        else:
            raise

def fetch_scd_columns(cursor):
    """Find tables with SCD columns (EFF_FROM_DTTM, START_DT) in DWP01T_SRCI_RMS"""
    query = """
    SELECT DISTINCT
        DatabaseName,
        TableName,
        ColumnName,
        ROW_NUMBER() OVER (PARTITION BY DatabaseName, TableName ORDER BY ColumnName) AS RN
    FROM DBC.ColumnsV
    WHERE DatabaseName = ?
      AND ColumnName IN (?, ?)
    QUALIFY RN = 1
    ORDER BY TableName, ColumnName
    """

    try:
        cursor.execute(query, [SOURCE_DB, SCD_COLUMNS[0], SCD_COLUMNS[1]])
        return cursor.fetchall()
    except Exception as e:
        print(f"Error fetching SCD columns: {e}")
        return []

def fetch_scd_value_counts(cursor, db_name, tbl_name, col_name):
    """Fetch distinct SCD column values and their counts"""
    count_query = f"""
    LOCKING ROW FOR ACCESS
    SELECT
        "{col_name}" AS ColumnValue,
        COUNT(*)(BIGINT) AS ValueCount
    FROM "{db_name}"."{tbl_name}"
    WHERE "{col_name}" IS NOT NULL
    GROUP BY 1
    ORDER BY ColumnValue DESC
    """

    try:
        cursor.execute(count_query)
        return cursor.fetchall()
    except Exception as e:
        print(f"Warning: Could not fetch SCD values for {db_name}.{tbl_name}.{col_name}: {e}")
        return []

def delete_existing_entries(cursor, db_name, tbl_name, col_name):
    """Delete all existing tracking entries for this column to prevent duplicates"""
    delete_sql = f"""
    DELETE FROM "{TARGET_DB}"."{TARGET_TABLE}"
    WHERE UPPER(DatabaseName) = UPPER(?)
      AND UPPER(TableName) = UPPER(?)
      AND UPPER(ColumnName) = UPPER(?)
    """
    try:
        cursor.execute(delete_sql, [db_name, tbl_name, col_name])
    except Exception as e:
        print(f"Warning: Could not clear previous entries for {db_name}.{tbl_name}.{col_name}: {e}")

def insert_scd_metric(cursor, db_name, tbl_name, col_name, col_value, row_count):
    """Insert SCD frequency metric for a specific value into tracking table"""
    insert_sql = f"""
    INSERT INTO "{TARGET_DB}"."{TARGET_TABLE}"
    (DatabaseName, TableName, ColumnName, SCD_ColumnValue, RowCount, ExtractionTimestamp)
    VALUES (?, ?, ?, ?, ?, ?)
    """
    timestamp = datetime.now()

    try:
        cursor.execute(insert_sql, [db_name, tbl_name, col_name, col_value, row_count, timestamp])
    except Exception as e:
        print(f"Error writing SCD metric for {db_name}.{tbl_name}.{col_name} = {col_value}: {e}")

def main():
    conn = get_teradata_connection()
    cursor = conn.cursor()

    try:
        initialize_scd_tracking_table(cursor)

        scd_columns = fetch_scd_columns(cursor)
        if not scd_columns:
            print(f"No tables with SCD columns ({', '.join(SCD_COLUMNS)}) found in {SOURCE_DB}.")
            return

        print(f"\nFound {len(scd_columns)} column(s) to track for SCD frequency.\n")

        total_inserts = 0

        for db_name, tbl_name, col_name, rn in scd_columns:
            print(f"Processing: {db_name}.{tbl_name}.{col_name}")

            value_counts = fetch_scd_value_counts(cursor, db_name, tbl_name, col_name)

            if not value_counts:
                print(f"  -> No distinct values found or column is empty.\n")
                continue

            delete_existing_entries(cursor, db_name, tbl_name, col_name)

            for col_value, count in value_counts:
                insert_scd_metric(cursor, db_name, tbl_name, col_name, col_value, count)
                total_inserts += 1

            print(f"  -> Inserted {len(value_counts)} distinct value(s) with their counts.\n")

        conn.commit()
        print(f"SCD frequency metrics successfully updated! Total entries inserted: {total_inserts}")

    except Exception as e:
        print(f"Execution error: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
        print("Database connection closed.")

if __name__ == "__main__":
    main()
