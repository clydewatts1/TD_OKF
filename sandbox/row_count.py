import os
import sys
from datetime import datetime
import teradatasql
from dotenv import load_dotenv

# Load parameters from .env file
load_dotenv()

# Retrieve values with safety fallbacks
TD_HOST = os.getenv("TERADATA_HOST")
TD_USER = os.getenv("TERADATA_USER")
TD_PASSWORD = os.getenv("TERADATA_PASSWORD")
TD_LOGMECH = os.getenv("TERADATA_LOGMECH", "TD2")
SOURCE_DB_PATTERN = os.getenv("SOURCE_DATABASE_PATTERN", "%")
SOURCE_TABLE_PATTERN = os.getenv("SOURCE_TABLE_PATTERN", "%")
TARGET_DB = os.getenv("DATABASE_METADATA", "DWB02T_SANDBOX")
TARGET_TABLE = os.getenv("TABLE_ROW_COUNT", "table_size_metrics")

# Identify browser-based authentication modes (handles standard 'BROWSER' and 'BROWER' typo variations)
IS_BROWSER_AUTH = TD_LOGMECH.upper() in ["BROWSER", "BROWER"]

# Validate that crucial parameters are present (conditionally bypass username/password requirements for browser auth)
required_params = [TD_HOST, SOURCE_DB_PATTERN, SOURCE_TABLE_PATTERN, TARGET_DB]
if not IS_BROWSER_AUTH:
    required_params.extend([TD_USER, TD_PASSWORD])

if not all(required_params):
    print("Error: Missing database connection details or configuration patterns in the .env file.")
    sys.exit(1)

def get_teradata_connection():
    """
    Establishes and returns a connection to Teradata database using teradatasql.
    Optionally skips username/password credentials when using Single Sign-On browser authentication.
    """
    try:
        print(f"Connecting to Teradata host '{TD_HOST}' using login mechanism '{TD_LOGMECH}'...")
        
        # Build connection arguments dynamically
        conn_args = {
            "host": TD_HOST,
            "logmech": TD_LOGMECH
        }
        
        # Only inject credentials if they are populated (and not bypassed by BROWSER SSO)
        if TD_USER:
            conn_args["user"] = TD_USER
        if TD_PASSWORD:
            conn_args["password"] = TD_PASSWORD
        

        if IS_BROWSER_AUTH:
            print("Browser-based authentication detected. Skipping username/password credentials.")
            conn = teradatasql.connect(host=TD_HOST, logmech=TD_LOGMECH)        
        else:
            conn = teradatasql.connect(host=TD_HOST, user=TD_USER, password=TD_PASSWORD, logmech=TD_LOGMECH)
        print("Successfully connected to Teradata!")
        return conn
    except Exception as e:
        print(f"Failed to connect to Teradata database: {e}")
        sys.exit(1)

def initialize_target_table(cursor):
    """
    Ensures that the tracking target table exists. 
    Creates it dynamically if it doesn't exist.
    """
    full_target_name = f'"{TARGET_DB}"."{TARGET_TABLE}"'
    
    # DDL for our tracking table
    create_table_ddl = f"""
    CREATE SET TABLE {full_target_name} , NO FALLBACK ,
         NO BEFORE JOURNAL,
         NO AFTER JOURNAL,
         CHECKSUM = DEFAULT,
         DEFAULT MERGEBLOCKRATIO
         (
          DatabaseName VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC,
          TableName VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC,
          TableType CHAR(1) CHARACTER SET LATIN UPPERCASE,
          TableSizeBytes BIGINT,
          RowCount BIGINT,
          ExtractionTimestamp TIMESTAMP(6)
         )
    PRIMARY INDEX ( DatabaseName , TableName );
    """
    
    try:
        # Use parameterized, case-insensitive lookup against Teradata catalog
        check_query = """
        SELECT 1 
        FROM DBC.TablesV 
        WHERE UPPER(DatabaseName) = UPPER(?) 
          AND UPPER(TableName) = UPPER(?)
        """
        cursor.execute(check_query, [TARGET_DB, TARGET_TABLE])
        
        if cursor.fetchone():
            print(f"Target table {full_target_name} already exists. Ready to write.")
        else:
            print(f"Target table {full_target_name} not found. Creating it now...")
            cursor.execute(create_table_ddl)
            print("Target table created successfully.")
    except Exception as e:
        # Fallback handling in case of race conditions or existing table issues
        if "3803" in str(e):  # Teradata error code: Table already exists
            print(f"Target table {full_target_name} is already present (verified via fallback check).")
        else:
            print(f"Error checking/creating target table: {e}")
            raise

def fetch_source_tables(cursor):
    """
    Queries DBC views to gather O (NoPI/Queue) and T (Standard) tables,
    along with their current physical storage sizes in bytes.
    Uses pattern matching for both database names and table names.
    """
    print(f"Fetching tables of type 'O' and 'T' matching Database: '{SOURCE_DB_PATTERN}' and Table: '{SOURCE_TABLE_PATTERN}'...")
    
    query = """
    SELECT 
        t.DatabaseName, 
        t.TableName, 
        t.TableKind,
        COALESCE(SUM(s.CurrentPerm), 0) AS TableSizeBytes
    FROM DBC.TablesV t
    LEFT JOIN DBC.TableSizeV s 
      ON t.DatabaseName = s.DatabaseName 
      AND t.TableName = s.TableName
    WHERE t.TableKind IN ('T', 'O')
      AND t.DatabaseName LIKE ?
      AND t.TableName LIKE ?
    GROUP BY t.DatabaseName, t.TableName, t.TableKind
    ORDER BY TableSizeBytes DESC
    """
    
    try:
        cursor.execute(query, [SOURCE_DB_PATTERN, SOURCE_TABLE_PATTERN])
        return cursor.fetchall()
    except Exception as e:
        print(f"Error querying DBC catalog views: {e}")
        return []

def get_exact_row_count(cursor, db_name, tbl_name):
    """
    Runs a direct dynamic SELECT COUNT(*)(BIGINT) query to get exact real-time row counts.
    Using locking modifiers to prevent blocking concurrent workloads.
    """
    # LOCKING ROW FOR ACCESS avoids reading locks and doesn't block reporting queries
    count_query = f'LOCKING ROW FOR ACCESS SELECT COUNT(*)(BIGINT) FROM "{db_name}"."{tbl_name}"'
    try:
        cursor.execute(count_query)
        result = cursor.fetchone()
        return result[0] if result else 0
    except Exception as e:
        print(f"Warning: Could not count rows for {db_name}.{tbl_name} (skipping row count): {e}")
        return -1

def delete_existing_metric(cursor, db_name, tbl_name):
    """
    Deletes any existing metric record for the specific table to prevent duplicate
    entries and allow the script to be run multiple times safely.
    """
    delete_sql = f'DELETE FROM "{TARGET_DB}"."{TARGET_TABLE}" WHERE UPPER(DatabaseName) = UPPER(?) AND UPPER(TableName) = UPPER(?)'
    try:
        cursor.execute(delete_sql, [db_name, tbl_name])
    except Exception as e:
        print(f"Warning: Could not clear previous metric for {db_name}.{tbl_name}: {e}")

def insert_metric_record(cursor, db_name, tbl_name, tbl_type, size_bytes, row_count):
    """
    Inserts a collected metadata snapshot record into the metrics tracker table.
    """
    insert_sql = f"""
    INSERT INTO "{TARGET_DB}"."{TARGET_TABLE}" 
    (DatabaseName, TableName, TableType, TableSizeBytes, RowCount, ExtractionTimestamp)
    VALUES (?, ?, ?, ?, ?, ?)
    """
    timestamp = datetime.now()
    try:
        cursor.execute(insert_sql, [db_name, tbl_name, tbl_type, size_bytes, row_count, timestamp])
    except Exception as e:
        print(f"Error writing metric entry for {db_name}.{tbl_name}: {e}")

def main():
    conn = get_teradata_connection()
    cursor = conn.cursor()
    
    try:
        # Initialize target table
        initialize_target_table(cursor)
        
        # Pull metadata for types 'O' and 'T'
        tables = fetch_source_tables(cursor)
        if not tables:
            print("No matching source tables found.")
            return
        
        print(f"Found {len(tables)} tables to process. Commencing metric calculation...")
        
        # Gather metric for each table and write to database
        for row in tables:
            db_name, tbl_name, tbl_type, size_bytes = row
            
            print(f"Processing: {db_name}.{tbl_name} ({'Queue/NoPI' if tbl_type == 'O' else 'Standard Table'})")
            
            # Fetch dynamic row count
            row_count = get_exact_row_count(cursor, db_name, tbl_name)
            
            # Delete any existing record for this table to allow multiple runs without duplication
            delete_existing_metric(cursor, db_name, tbl_name)
            
            # Record the new snapshot
            insert_metric_record(cursor, db_name, tbl_name, tbl_type, size_bytes, row_count)
            print(f" -> Size: {size_bytes} Bytes | Rows: {row_count if row_count != -1 else 'N/A'} [Cleared & Saved]")
        # Commit transactions
        conn.commit()
        print("\nAll database metrics successfully updated and synchronized!")
        
    except Exception as e:
        print(f"An unexpected execution error occurred: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
        print("Database connection closed.")

if __name__ == "__main__":
    main()