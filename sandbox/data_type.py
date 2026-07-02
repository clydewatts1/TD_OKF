import os
import sys
from datetime import datetime
import re
import teradatasql
from dotenv import load_dotenv

# Load configuration properties from the environment file
load_dotenv()

# Retrieve values with safety fallbacks
TD_HOST = os.getenv("TERADATA_HOST")
TD_USER = os.getenv("TERADATA_USER")
TD_PASSWORD = os.getenv("TERADATA_PASSWORD")
TD_LOGMECH = os.getenv("TERADATA_LOGMECH", "TD2")
SOURCE_DB_PATTERN = os.getenv("SOURCE_DATABASE_PATTERN", "DWP01%")
SOURCE_TABLE_PATTERN = os.getenv("SOURCE_TABLE_PATTERN", "%")
TARGET_DB = os.getenv("DATABASE_METADATA", "DWB02T_SANDBOX")
TARGET_TABLE = os.getenv("TABLE_COLUMN_TYPE", "table_column_types")

# Check for browser-based Single Sign-On (SSO) bypass configurations
IS_BROWSER_AUTH = TD_LOGMECH.upper() in ["BROWSER", "BROWER"]

def is_connection_closed_error(err):
    return "connection is already closed" in str(err).lower()

def get_teradata_connection():
    """
    Establishes and returns a connection to Teradata database using teradatasql.
    Supports Single Sign-On browser authentication bypass.
    """
    try:
        print(f"Connecting to Teradata host '{TD_HOST}' using mechanism '{TD_LOGMECH}'...")
        if IS_BROWSER_AUTH:
            conn = teradatasql.connect(host=TD_HOST, logmech=TD_LOGMECH)        
        else:
            conn = teradatasql.connect(host=TD_HOST, user=TD_USER, password=TD_PASSWORD, logmech=TD_LOGMECH)
        print("Connected successfully to Teradata Database!")
        return conn
    except Exception as e:
        print(f"Failed to connect to Teradata database: {e}")
        sys.exit(1)

def initialize_target_table(cursor):
    """
    Ensures that the tracking target table exists.
    Created dynamically with proper column types including new Description and Nullable fields.
    """
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
          ColumnOrder INTEGER,
          TeradataDataType VARCHAR(256) CHARACTER SET UNICODE,
          OKFDataType VARCHAR(64) CHARACTER SET UNICODE,
          IsNullable VARCHAR(5) CHARACTER SET LATIN,
          ColumnDescription VARCHAR(500) CHARACTER SET UNICODE,
            ErrorCode INTEGER,
            ErrorDescription VARCHAR(1000) CHARACTER SET UNICODE,
          ExtractionTimestamp TIMESTAMP(6)
         )
    PRIMARY INDEX ( DatabaseName , TableName , ColumnName );
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
            print(f"Target catalog table {full_target_name} exists.")
        else:
            print(f"Target table {full_target_name} not found. Executing DDL now...")
            cursor.execute(create_table_ddl)
            print("Target catalog table created successfully.")
    except Exception as e:
        if "3803" in str(e):
            print(f"Target table {full_target_name} is already present.")
        else:
            print(f"Error checking/creating target catalog table: {e}")
            raise

    required_columns = {
        "DatabaseName": "VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC",
        "TableName": "VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC",
        "ColumnName": "VARCHAR(128) CHARACTER SET UNICODE NOT CASESPECIFIC",
        "ColumnOrder": "INTEGER",
        "TeradataDataType": "VARCHAR(256) CHARACTER SET UNICODE",
        "OKFDataType": "VARCHAR(64) CHARACTER SET UNICODE",
        "IsNullable": "VARCHAR(5) CHARACTER SET LATIN",
        "ColumnDescription": "VARCHAR(500) CHARACTER SET UNICODE",
        "ErrorCode": "INTEGER",
        "ErrorDescription": "VARCHAR(1000) CHARACTER SET UNICODE",
        "ExtractionTimestamp": "TIMESTAMP(6)",
    }

    try:
        cursor.execute(
            """
            SELECT ColumnName
            FROM DBC.ColumnsV
            WHERE UPPER(DatabaseName) = UPPER(?)
              AND UPPER(TableName) = UPPER(?)
            """,
            [TARGET_DB, TARGET_TABLE],
        )
        existing_columns = {row[0].upper() for row in cursor.fetchall()}

        for column_name, column_ddl in required_columns.items():
            if column_name.upper() not in existing_columns:
                print(f"Adding missing column {column_name} to {full_target_name}...")
                cursor.execute(f'ALTER TABLE {full_target_name} ADD {column_name} {column_ddl};')
    except Exception as e:
        print(f"Error validating or updating target catalog table schema: {e}")
        raise

def map_resolved_type_to_okf(resolved_type):
    """
    Translates a fully resolved Teradata SQL Type string to an OKF schema type.
    """
    type_str = resolved_type.strip().upper() if resolved_type else ""
    
    if 'CHAR' in type_str or 'CLOB' in type_str or 'JSON' in type_str:
        return 'string'
    elif 'INT' in type_str or 'BYTEINT' in type_str:
        return 'integer'
    elif 'DECIMAL' in type_str or 'NUMERIC' in type_str or 'FLOAT' in type_str or 'DOUBLE' in type_str:
        return 'number'
    elif 'TIMESTAMP' in type_str:
        return 'datetime'
    elif 'TIME' in type_str:
        return 'time'
    elif 'DATE' in type_str:
        return 'date'
    elif 'BYTE' in type_str or 'BLOB' in type_str:
        return 'string'
        
    return 'any'

def fetch_source_columns(cursor):
    """
    Extracts structural column order, descriptions, and nullable flags using Windowed ranking.
    """
    print(f"Extracting structural metadata matching database: '{SOURCE_DB_PATTERN}'...")
    full_target_name = f'"{TARGET_DB}"."{TARGET_TABLE}"'
    query = f"""
    SELECT 
        C.DatabaseName, 
        C.TableName, 
        C.ColumnName, 
        ROW_NUMBER() OVER (PARTITION BY C.DatabaseName, C.TableName ORDER BY C.ColumnId) as ColumnOrder,
        COALESCE(C.CommentString, 'No description provided') AS ColumnDescription,
        C.Nullable
    FROM DBC.ColumnsV AS C
    INNER JOIN DBC.TablesV AS T 
      ON C.DatabaseName = T.DatabaseName 
      AND C.TableName = T.TableName
    LEFT OUTER JOIN  {full_target_name} AS M
      ON C.DatabaseName = M.DatabaseName
        AND C.TableName = M.TableName
        AND C.ColumnName = M.ColumnName
    WHERE C.DatabaseName LIKE ?
      AND C.TableName LIKE ?
      AND T.TableKind IN ('T', 'O', 'V')
      /* Only include columns that are not already present in the target table */
    QUALIFY MAX(CASE WHEN M.ColumnName IS NULL THEN 1 ELSE 0 END) OVER (PARTITION BY C.DatabaseName, C.TableName) = 1
    ORDER BY C.DatabaseName, C.TableName, ColumnOrder
    """
    try:
        cursor.execute(query, [SOURCE_DB_PATTERN, SOURCE_TABLE_PATTERN])
        return cursor.fetchall()
    except Exception as e:
        print(f"Error querying column definitions from DBC: {e}")
        return []

def fetch_column_type(cursor, db_name, tbl_name, col_name):
    """
    Dynamic 0-row Type() extraction. Perfectly handles Views.
    """
    query = f"""
    SELECT TYPE(COL)
    FROM (SELECT DISTINCT 1 AS One FROM DBC.dbcinfo) AS B 
    LEFT OUTER JOIN
    (SELECT "{col_name}" AS COL FROM "{db_name}"."{tbl_name}" WHERE 1 <> 1) AS A
    ON 1=1
    """
    try:
        cursor.execute(query)
        result = cursor.fetchone()
        return (result[0] if result else "UNKNOWN", None, None)
    except Exception as e:
        if is_connection_closed_error(e):
            raise
        err_text = str(e)
        match = re.search(r"Error\s+(\d+)", err_text)
        err_code = int(match.group(1)) if match else None
        err_desc = err_text.splitlines()[0] if err_text else "Unknown error"
        print(f"Warning: Could not resolve type for {db_name}.{tbl_name}.{col_name}: {err_desc}")
        return "UNKNOWN", err_code, err_desc

def delete_existing_metrics(cursor, db_name, tbl_name):
    delete_sql = f'DELETE FROM "{TARGET_DB}"."{TARGET_TABLE}" WHERE UPPER(DatabaseName) = UPPER(?) AND UPPER(TableName) = UPPER(?)'
    try:
        cursor.execute(delete_sql, [db_name, tbl_name])
    except Exception as e:
        if is_connection_closed_error(e):
            raise
        print(f"Warning: Could not clear previous metadata for {db_name}.{tbl_name}: {e}")

def insert_column_record(cursor, db_name, tbl_name, col_name, col_order, resolved_type, okf_type, is_nullable, col_desc, err_code, err_desc):
    """
    Saves mapped column record into the metadata tracking database.
    """
    insert_sql = f"""
    INSERT INTO "{TARGET_DB}"."{TARGET_TABLE}" 
    (DatabaseName, TableName, ColumnName, ColumnOrder, TeradataDataType, OKFDataType, IsNullable, ColumnDescription, ErrorCode, ErrorDescription, ExtractionTimestamp)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    timestamp = datetime.now()
    try:
        cursor.execute(insert_sql, [
            db_name, tbl_name, col_name, col_order, resolved_type, okf_type, is_nullable, col_desc, err_code, err_desc, timestamp
        ])
    except Exception as e:
        if is_connection_closed_error(e):
            raise
        print(f"Error writing schema record for {db_name}.{tbl_name}.{col_name}: {e}")

def main():
    conn = get_teradata_connection()
    cursor = conn.cursor()
    
    try:
        initialize_target_table(cursor)
        
        columns = fetch_source_columns(cursor)
        if not columns:
            print("No matching tables or columns found.")
            return
        
        print(f"Discovered {len(columns)} columns. Initiating metadata extraction...")
        cleared_tables = set()
        
        for col in columns:
            db_name, tbl_name, col_name, col_order, col_desc, nullable_flag = col
            table_key = (db_name.upper(), tbl_name.upper())
            
            if table_key not in cleared_tables:
                delete_existing_metrics(cursor, db_name, tbl_name)
                cleared_tables.add(table_key)
                print(f" -> Processing schema for: {db_name}.{tbl_name}")
                
            resolved_type, err_code, err_desc = fetch_column_type(cursor, db_name, tbl_name, col_name)
            okf_type = map_resolved_type_to_okf(resolved_type)
            
            # Translate Teradata 'Y'/'N' to LLM friendly True/False string literals
            is_nullable_str = "True" if (nullable_flag or "").strip().upper() == 'Y' else "False"
            
            insert_column_record(
                cursor, db_name, tbl_name, col_name, col_order, 
                resolved_type, okf_type, is_nullable_str, col_desc, err_code, err_desc
            )
            
        conn.commit()
        print("\nAll database schema maps updated successfully!")
        
    except Exception as e:
        print(f"An unexpected execution error occurred: {e}")
        try:
            conn.rollback()
        except Exception as rollback_error:
            if is_connection_closed_error(rollback_error):
                print("Rollback skipped: Teradata connection is already closed.")
            else:
                print(f"Rollback failed: {rollback_error}")
    finally:
        try:
            cursor.close()
        except Exception as cursor_close_error:
            if not is_connection_closed_error(cursor_close_error):
                print(f"Warning: Could not close cursor cleanly: {cursor_close_error}")
        try:
            conn.close()
        except Exception as conn_close_error:
            if not is_connection_closed_error(conn_close_error):
                print(f"Warning: Could not close connection cleanly: {conn_close_error}")

if __name__ == "__main__":
    main()