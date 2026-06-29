import os
import sys
from datetime import datetime, timezone
from collections import defaultdict
import teradatasql
from dotenv import load_dotenv

# Load configuration properties from the environment file
load_dotenv()

TD_HOST = os.getenv("TERADATA_HOST")
TD_USER = os.getenv("TERADATA_USER")
TD_PASSWORD = os.getenv("TERADATA_PASSWORD")
TD_LOGMECH = os.getenv("TERADATA_LOGMECH", "TD2")
SOURCE_DB_PATTERN = os.getenv("SOURCE_DATABASE_PATTERN", "%")
SOURCE_TABLE_PATTERN = os.getenv("SOURCE_TABLE_PATTERN", "%")
TARGET_DB = os.getenv("DATABASE_METADATA", "DWB02T_SANDBOX")
TABLE_METRICS = os.getenv("TABLE_ROW_COUNT", "table_size_metrics")
COLUMN_METRICS =  os.getenv("TABLE_COLUMN_TYPE", "table_column_types")

OUTPUT_DIR = "okf_bundle"
TABLES_DIR = os.path.join(OUTPUT_DIR, "tables")
IS_BROWSER_AUTH = TD_LOGMECH.upper() in ["BROWSER", "BROWER"]

def get_teradata_connection():
    try:
        print(f"Connecting to Teradata host '{TD_HOST}'...")
        if IS_BROWSER_AUTH:
            return teradatasql.connect(host=TD_HOST, logmech=TD_LOGMECH)        
        else:
            return teradatasql.connect(host=TD_HOST, user=TD_USER, password=TD_PASSWORD, logmech=TD_LOGMECH)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

def fetch_master_metadata(cursor):
    """
    Executes the mega-join query to pull all table and column metadata, 
    including metrics and OKF data types from our sandbox tables.
    """
    print("Extracting master schema, descriptions, and metrics...")
    query = f"""
    SELECT 
        C.DatabaseName, C.TableName, C.ColumnName, T.TableKind,
        COALESCE(T.CommentString, '') AS TableDescription,
        COALESCE(C.CommentString, '') AS ColumnDescription,
        C.Nullable, COALESCE(SZ.TableSizeBytes, 0) AS TableSizeBytes,
        COALESCE(SZ.RowCount, 0) AS RowCount, TP.TeradataDataType,
        TP.OKFDataType,
        COALESCE(C.PartitioningColumn, 'N') AS PartitioningColumn,
        ROW_NUMBER() OVER (PARTITION BY C.DatabaseName, C.TableName ORDER BY C.ColumnId) AS ColumnOrder
    FROM DBC.TablesV AS T
    INNER JOIN DBC.ColumnsV AS C
        ON T.DatabaseName = C.DatabaseName AND T.TableName = C.TableName
    LEFT OUTER JOIN "{TARGET_DB}"."{TABLE_METRICS}" AS SZ
        ON T.DatabaseName = SZ.DatabaseName AND T.TableName = SZ.TableName
    LEFT OUTER JOIN "{TARGET_DB}"."{COLUMN_METRICS}" AS TP
        ON C.DatabaseName = TP.DatabaseName AND C.TableName = TP.TableName AND C.ColumnName = TP.ColumnName
    WHERE T.DatabaseName LIKE ? AND T.TableName LIKE ?
      AND T.TableKind IN ('T', 'O', 'V')
    ORDER BY C.DatabaseName, C.TableName, ColumnOrder;
    """
    try:
        cursor.execute(query, [SOURCE_DB_PATTERN, SOURCE_TABLE_PATTERN])
        return cursor.fetchall()
    except Exception as e:
        print(f"Error reading master metadata: {e}")
        return []

def fetch_indices(cursor):
    """Fetches Primary Key (K) and Primary Index (P, Q) details."""
    print("Extracting index definitions...")
    query = """
    SELECT DatabaseName, TableName, IndexType, UniqueFlag, ColumnName
    FROM DBC.IndicesV
    WHERE DatabaseName LIKE ? AND TableName LIKE ?
      AND IndexType IN ('P', 'Q', 'K')
    ORDER BY DatabaseName, TableName, IndexNumber, ColumnPosition;
    """
    try:
        cursor.execute(query, [SOURCE_DB_PATTERN, SOURCE_TABLE_PATTERN])
        return cursor.fetchall()
    except Exception as e:
        print(f"Error reading indices: {e}")
        return []

def fetch_table_ddl(cursor, db_name, tbl_name, table_kind):
    """Executes SHOW TABLE or SHOW VIEW to get the exact DDL."""
    clean_kind = table_kind.strip().upper()
    command = "SHOW VIEW" if clean_kind == 'V' else "SHOW TABLE"
    query = f'{command} "{db_name}"."{tbl_name}";'
    try:
        cursor.execute(query)
        result = cursor.fetchone()
        ddl_text = result[0].replace('\r', '\n') if result else ""
        
        return ddl_text
    except Exception as e:
        # Fails gracefully if user doesn't have SHOW privileges
        return ""

def generate_okf_markdown(info, columns, indices, ddl):
    """Constructs the OKF v0.1 compliant Markdown file string."""
    
    db_name, tbl_name = info['db'], info['table']
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    clean_type = info['type'].strip().upper()
    
    # Map Table Kind
    type_map = {'T': 'Standard Table', 'O': 'Queue Table', 'V': 'View'}
    obj_type = type_map.get(clean_type, 'Unknown')

    # Parse Indexes into printable strings
    pk_cols = [c for t, u, c in indices if t == 'K']
    pi_cols = [c for t, u, c in indices if t in ('P', 'Q')]
    is_unique_pi = any(u == 'Y' for t, u, c in indices if t == 'Q')
    part_cols = [col['name'] for col in columns if col['is_partition'] == 'Y']
    
    pk_str = f"`{', '.join(pk_cols)}`" if pk_cols else "None"
    pi_type = "Unique Primary Index" if is_unique_pi else "Non-Unique Primary Index" if pi_cols else "No Primary Index"
    pi_str = f"{pi_type} on `{', '.join(pi_cols)}`" if pi_cols else pi_type
    part_str = f"`{', '.join(part_cols)}`" if part_cols else "None"

    rows_str = "Unknown" if clean_type == 'V' else f"{info['rows']:,}"
    size_str = "Unknown" if clean_type == 'V' else f"{info['size']:,}"

    # Handle YAML safe description for frontmatter (omit entirely if empty)
    yaml_desc_line = ""
    if info['desc']:
        safe_desc = info['desc'].replace('"', '\\"')
        yaml_desc_line = f'description: "{safe_desc}"\n'

    # Handle Human Readable description for markdown body
    tbl_desc = info['desc'] if info['desc'] else "No description provided."

    # Build Frontmatter
    md = f"""---
type: Teradata Table
title: "{db_name}.{tbl_name}"
{yaml_desc_line}tags:
  - {db_name}
  - teradata
  - {obj_type.lower().replace(' ', '_')}
timestamp: {current_time}
---

# {db_name}.{tbl_name}

**Database:** `{db_name}`  
**Object Type:** `{obj_type} ({clean_type})`  
**Description:** {tbl_desc}  
**Rows:** `{rows_str}`  
**Size (Bytes):** `{size_str}`  

**Primary Key:** {pk_str}  
**Primary Index:** {pi_str}  
**Partition Columns:** {part_str}

## Schema

| Column Name | Teradata Type | OKF Type | Nullable | Description | Order |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""
    # Build Schema Table
    for col in columns:
        is_null = "True" if col['nullable'] == 'Y' else "False"
        td_type = col['td_type'] if col['td_type'] else 'UNKNOWN'
        okf_type = col['okf_type'] if col['okf_type'] else 'any'
        
        desc = col['desc'].replace('\n', ' ').replace('\r', '').replace('|', '\\|') if col['desc'] else ''
        
        md += f"| `{col['name']}` | `{td_type}` | `{okf_type}` | `{is_null}` | {desc} | {col['order']} |\n"

    # Append DDL
    if ddl:
        md += f"\n## Teradata DDL\n\n```sql\n{ddl}\n```\n"

    return md

def generate_bundle_indices(tables, output_dir, tables_dir):
    """Generates the root index.md and individual database index.md files."""
    print("Generating OKF index files...")
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Group tables by database for the index
    db_map = defaultdict(list)
    for (db, tbl), data in tables.items():
        db_map[db].append((tbl, data['info']['type'], data['info']['desc']))
        
    # 1. Generate the Master Index (Root)
    master_md = f"""---
type: Bundle
title: "Teradata Metadata Bundle"
description: "Master index of all extracted Teradata tables and views."
timestamp: {current_time}
---

# Teradata Metadata Bundle

This bundle contains metadata extracted from Teradata, organized by database.

"""
    for db in sorted(db_map.keys()):
        safe_db = db.replace(" ", "_").replace('"', '')
        # Link the database header to its specific sub-index
        master_md += f"## Database: [{db}](tables/{safe_db}/index.md)\n\n"
        master_md += "| Object Name | Type | Description |\n"
        master_md += "| :--- | :--- | :--- |\n"
        for tbl, tkind, desc in sorted(db_map[db]):
            safe_tbl = tbl.replace(" ", "_").replace('"', '')
            clean_type = tkind.strip().upper()
            type_map = {'T': 'Table', 'O': 'Queue Table', 'V': 'View'}
            obj_type = type_map.get(clean_type, 'Unknown')
            
            clean_desc = desc.replace('\n', ' ').replace('\r', '').replace('|', '\\|') if desc else 'No description provided.'
            
            master_md += f"| [{tbl}](tables/{safe_db}/{safe_tbl}.md) | `{obj_type}` | {clean_desc} |\n"
        master_md += "\n"
        
    master_index_path = os.path.join(output_dir, "index.md")
    with open(master_index_path, 'w', encoding='utf-8') as f:
        f.write(master_md)

    # 2. Generate the Database-Level Indexes
    for db in sorted(db_map.keys()):
        safe_db = db.replace(" ", "_").replace('"', '')
        db_dir = os.path.join(tables_dir, safe_db)
        os.makedirs(db_dir, exist_ok=True)
        
        db_md = f"""---
type: Collection
title: "Database {db}"
description: "Index of tables and views in the {db} database."
timestamp: {current_time}
---

# Database: {db}

| Object Name | Type | Description |
| :--- | :--- | :--- |
"""
        for tbl, tkind, desc in sorted(db_map[db]):
            safe_tbl = tbl.replace(" ", "_").replace('"', '')
            clean_type = tkind.strip().upper()
            type_map = {'T': 'Table', 'O': 'Queue Table', 'V': 'View'}
            obj_type = type_map.get(clean_type, 'Unknown')
            clean_desc = desc.replace('\n', ' ').replace('\r', '').replace('|', '\\|') if desc else 'No description provided.'
            
            # Since this index is in the same folder as the tables, the link is just the filename
            db_md += f"| [{tbl}]({safe_tbl}.md) | `{obj_type}` | {clean_desc} |\n"
            
        db_index_path = os.path.join(db_dir, "index.md")
        with open(db_index_path, 'w', encoding='utf-8') as f:
            f.write(db_md)

def main():
    os.makedirs(TABLES_DIR, exist_ok=True)
    conn = get_teradata_connection()
    cursor = conn.cursor()
    
    try:
        master_rows = fetch_master_metadata(cursor)
        index_rows = fetch_indices(cursor)
        
        if not master_rows:
            print("No matching metadata found. Ensure your tracker tables are populated and wildcards match.")
            return
            
        # Group Data
        tables = defaultdict(lambda: {'info': {}, 'columns': [], 'indices': []})
        
        for r in master_rows:
            db, tbl, col, tkind, tdesc, cdesc, cnull, size, rows, td_type, okf, is_part, order = r
            key = (db, tbl)
            if not tables[key]['info']:
                tables[key]['info'] = {'db': db, 'table': tbl, 'type': tkind, 'desc': tdesc, 'size': size, 'rows': rows}
            tables[key]['columns'].append({'name': col, 'td_type': td_type, 'okf_type': okf, 'nullable': cnull, 'desc': cdesc, 'is_partition': is_part, 'order': order})
            
        for db, tbl, itype, uniq, col in index_rows:
            tables[(db, tbl)]['indices'].append((itype, uniq, col))
            
        print(f"Generating OKF files for {len(tables)} tables...")
        
        for (db, tbl), data in tables.items():
            ddl_text = fetch_table_ddl(cursor, db, tbl, data['info']['type'])
            md_content = generate_okf_markdown(data['info'], data['columns'], data['indices'], ddl_text)
            
            db_dir = os.path.join(TABLES_DIR, db.replace(" ", "_").replace('"', ''))
            os.makedirs(db_dir, exist_ok=True)
            
            safe_filename = f"{tbl}".replace(" ", "_").replace('"', '') + ".md"
            filepath = os.path.join(db_dir, safe_filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(md_content)
                
        # Call the new indices generator
        generate_bundle_indices(tables, OUTPUT_DIR, TABLES_DIR)
                
        print(f"Success! OKF bundle created in '{OUTPUT_DIR}'.")

    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    main()