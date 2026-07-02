import os
import sys
import argparse
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
OKF_DIRECTORY = os.getenv("OKF_DIRECTORY", "okf_bundle")

CONFIG_DEFAULTS = {
    "TERADATA_LOGMECH": "TD2",
    "SOURCE_DATABASE_PATTERN": "%",
    "SOURCE_TABLE_PATTERN": "%",
    "DATABASE_METADATA": "DWB02T_SANDBOX",
    "TABLE_ROW_COUNT": "table_size_metrics",
    "TABLE_COLUMN_TYPE": "table_column_types",
    "OKF_DIRECTORY": "okf_bundle",
}

CONFIG_KEYS = [
    "TERADATA_HOST",
    "TERADATA_LOGMECH",
    "TERADATA_USER",
    "TERADATA_PASSWORD",
    "SOURCE_DATABASE_PATTERN",
    "SOURCE_TABLE_PATTERN",
    "DATABASE_METADATA",
    "TABLE_ROW_COUNT",
    "TABLE_COLUMN_TYPE",
    "OKF_DIRECTORY",
]

OUTPUT_DIR = OKF_DIRECTORY
TABLES_DIR = os.path.join(OUTPUT_DIR, "tables")


def normalize_text(value, default=""):
    if value is None:
        return default
    cleaned = str(value).strip()
    return cleaned if cleaned else default


def anchor_slug(value):
    slug = normalize_text(value).lower().replace(" ", "-")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    return "".join(ch for ch in slug if ch in allowed)


def object_type_label(table_kind):
    clean_type = normalize_text(table_kind).upper()
    type_map = {'T': 'Table', 'O': 'Queue Table', 'V': 'View'}
    return type_map.get(clean_type, 'Unknown')


def object_type_order(table_kind):
    clean_type = normalize_text(table_kind).upper()
    order = {'T': 0, 'V': 1, 'O': 2}
    return order.get(clean_type, 99)


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Generate an OKF markdown bundle from Teradata metadata."
    )
    for key in CONFIG_KEYS:
        parser.add_argument(
            f"--{key}",
            dest=key,
            default=None,
            help=f"Override {key} (.env and defaults are used when omitted).",
        )
    return vars(parser.parse_args())


def resolve_setting(cli_args, key):
    cli_value = cli_args.get(key)
    if cli_value is not None:
        return cli_value
    env_value = os.getenv(key)
    if env_value not in [None, ""]:
        return env_value
    return CONFIG_DEFAULTS.get(key)


def apply_runtime_config(cli_args):
    global TD_HOST
    global TD_USER
    global TD_PASSWORD
    global TD_LOGMECH
    global SOURCE_DB_PATTERN
    global SOURCE_TABLE_PATTERN
    global TARGET_DB
    global TABLE_METRICS
    global COLUMN_METRICS
    global OKF_DIRECTORY
    global OUTPUT_DIR
    global TABLES_DIR

    TD_HOST = resolve_setting(cli_args, "TERADATA_HOST")
    TD_USER = resolve_setting(cli_args, "TERADATA_USER")
    TD_PASSWORD = resolve_setting(cli_args, "TERADATA_PASSWORD")
    TD_LOGMECH = resolve_setting(cli_args, "TERADATA_LOGMECH")
    SOURCE_DB_PATTERN = resolve_setting(cli_args, "SOURCE_DATABASE_PATTERN")
    SOURCE_TABLE_PATTERN = resolve_setting(cli_args, "SOURCE_TABLE_PATTERN")
    TARGET_DB = resolve_setting(cli_args, "DATABASE_METADATA")
    TABLE_METRICS = resolve_setting(cli_args, "TABLE_ROW_COUNT")
    COLUMN_METRICS = resolve_setting(cli_args, "TABLE_COLUMN_TYPE")
    OKF_DIRECTORY = resolve_setting(cli_args, "OKF_DIRECTORY")

    OUTPUT_DIR = OKF_DIRECTORY
    TABLES_DIR = os.path.join(OUTPUT_DIR, "tables")


def print_effective_config():
    print("Using runtime configuration:")
    print(f"- TERADATA_HOST: {normalize_text(TD_HOST, '<not set>')}")
    print(f"- TERADATA_LOGMECH: {normalize_text(TD_LOGMECH, '<not set>')}")
    print(f"- SOURCE_DATABASE_PATTERN: {normalize_text(SOURCE_DB_PATTERN, '<not set>')}")
    print(f"- SOURCE_TABLE_PATTERN: {normalize_text(SOURCE_TABLE_PATTERN, '<not set>')}")
    print(f"- DATABASE_METADATA: {normalize_text(TARGET_DB, '<not set>')}")
    print(f"- TABLE_ROW_COUNT: {normalize_text(TABLE_METRICS, '<not set>')}")
    print(f"- TABLE_COLUMN_TYPE: {normalize_text(COLUMN_METRICS, '<not set>')}")
    print(f"- OKF_DIRECTORY: {normalize_text(OUTPUT_DIR, '<not set>')}")

def get_teradata_connection():
    try:
        print(f"Connecting to Teradata host '{TD_HOST}'...")
        is_browser_auth = normalize_text(TD_LOGMECH, "").upper() in ["BROWSER", "BROWER"]
        if is_browser_auth:
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
        return result[0].replace('\r', '\n') if result else ""
    except Exception as e:
        err_text = str(e)
        # Object may disappear between discovery and SHOW; keep log concise.
        if "Error 3807" in err_text:
            print(f"Warning: Could not fetch DDL for {db_name}.{tbl_name}: object does not exist (3807).")
        else:
            first_line = err_text.splitlines()[0] if err_text else "unknown error"
            print(f"Warning: Could not fetch DDL for {db_name}.{tbl_name}: {first_line}")
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
    tbl_desc = normalize_text(info['desc'], "No description provided.")

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
        is_null = "True" if normalize_text(col['nullable']).upper() == 'Y' else "False"
        td_type = normalize_text(col['td_type'], 'UNKNOWN')
        okf_type = normalize_text(col['okf_type'], 'any')
        
        desc = col['desc'].replace('\n', ' ').replace('\r', '').replace('|', '\\|') if col['desc'] else ''
        
        md += f"| `{col['name']}` | `{td_type}` | `{okf_type}` | `{is_null}` | {desc} | {col['order']} |\n"

    if ddl:
        md += "\n## Teradata DDL\n\n```sql\n"
        md += ddl.strip() + "\n```\n"

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
    total_objects = len(tables)
    total_databases = len(db_map)
    total_tables = 0
    total_views = 0
    total_queue_tables = 0

    db_summary_rows = []
    for db in sorted(db_map.keys()):
        counts = {'T': 0, 'O': 0, 'V': 0}
        for _, tkind, _ in db_map[db]:
            clean_type = normalize_text(tkind).upper()
            if clean_type in counts:
                counts[clean_type] += 1
        total_tables += counts['T']
        total_views += counts['V']
        total_queue_tables += counts['O']
        db_summary_rows.append((db, counts))

    master_md += (
        f"- Databases: **{total_databases}**\n"
        f"- Objects: **{total_objects}**\n"
        f"- Tables: **{total_tables}**\n"
        f"- Views: **{total_views}**\n"
        f"- Queue Tables: **{total_queue_tables}**\n\n"
    )

    if db_map:
        master_md += "## Quick Links\n\n"
        for db in sorted(db_map.keys()):
            master_md += f"- [{db}](#database-{anchor_slug(db)})\n"
        master_md += "\n"

    if db_summary_rows:
        master_md += "## Database Summary\n\n"
        master_md += "| Database | Objects | Tables | Views | Queue Tables |\n"
        master_md += "| :--- | ---: | ---: | ---: | ---: |\n"
        for db, counts in db_summary_rows:
            safe_db = db.replace(" ", "_").replace('"', '')
            total = counts['T'] + counts['V'] + counts['O']
            master_md += (
                f"| [{db}](tables/{safe_db}/index.md) | {total} | "
                f"{counts['T']} | {counts['V']} | {counts['O']} |\n"
            )
        master_md += "\n"

    for db in sorted(db_map.keys()):
        safe_db = db.replace(" ", "_").replace('"', '')
        master_md += f"## Database {db}\n\n"
        master_md += f"[Open database index](tables/{safe_db}/index.md)\n\n"
        master_md += "| Object Name | Type | Description |\n"
        master_md += "| :--- | :--- | :--- |\n"
        sorted_rows = sorted(
            db_map[db],
            key=lambda item: (object_type_order(item[1]), normalize_text(item[0]).upper())
        )
        for tbl, tkind, desc in sorted_rows:
            safe_tbl = tbl.replace(" ", "_").replace('"', '')
            obj_type = object_type_label(tkind)
            
            clean_desc = normalize_text(desc, 'No description provided.').replace('\n', ' ').replace('\r', '').replace('|', '\\|')
            
            # Explicitly include the database name in the link text for the master index
            master_md += f"| [{db}.{tbl}](tables/{safe_db}/{safe_tbl}.md) | `{obj_type}` | {clean_desc} |\n"
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

[Back to master index](../../index.md)

Object Summary: **{len(db_map[db])}** objects total.

"""
        type_counts = {'T': 0, 'O': 0, 'V': 0}
        for _, tkind, _ in db_map[db]:
            clean_type = normalize_text(tkind).upper()
            if clean_type in type_counts:
                type_counts[clean_type] += 1

        db_md += (
            f"- Tables: **{type_counts['T']}**\n"
            f"- Views: **{type_counts['V']}**\n"
            f"- Queue Tables: **{type_counts['O']}**\n\n"
        )

        db_md += (
            "## Quick Links\n\n"
            "- [Tables](#tables)\n"
            "- [Views](#views)\n"
            "- [Queue Tables](#queue-tables)\n\n"
        )

        grouped = {'T': [], 'V': [], 'O': [], 'UNKNOWN': []}
        for tbl, tkind, desc in db_map[db]:
            clean_type = normalize_text(tkind).upper()
            if clean_type not in grouped:
                grouped['UNKNOWN'].append((tbl, tkind, desc))
            else:
                grouped[clean_type].append((tbl, tkind, desc))

        sections = [
            ('T', 'Tables'),
            ('V', 'Views'),
            ('O', 'Queue Tables'),
            ('UNKNOWN', 'Other Objects'),
        ]

        for key, title in sections:
            if not grouped[key]:
                continue
            db_md += f"## {title}\n\n"
            db_md += "| Object Name | Type | Description |\n"
            db_md += "| :--- | :--- | :--- |\n"

            for tbl, tkind, desc in sorted(grouped[key], key=lambda item: normalize_text(item[0]).upper()):
                safe_tbl = tbl.replace(" ", "_").replace('"', '')
                obj_type = object_type_label(tkind)
                clean_desc = normalize_text(desc, 'No description provided.').replace('\n', ' ').replace('\r', '').replace('|', '\\|')
                db_md += f"| [{tbl}]({safe_tbl}.md) | `{obj_type}` | {clean_desc} |\n"

            db_md += "\n"

        for tbl, tkind, desc in sorted(db_map[db]):
            safe_tbl = tbl.replace(" ", "_").replace('"', '')
            _ = safe_tbl

        db_index_path = os.path.join(db_dir, "index.md")
        with open(db_index_path, 'w', encoding='utf-8') as f:
            f.write(db_md)

def main():
    cli_args = parse_cli_args()
    apply_runtime_config(cli_args)
    print_effective_config()

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
            
        unmatched_index_tables = set()
        for db, tbl, itype, uniq, col in index_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['indices'].append((itype, uniq, col))
            else:
                unmatched_index_tables.add(key)

        valid_tables = {
            key: data for key, data in tables.items()
            if data.get('info') and data.get('columns')
        }

        if unmatched_index_tables:
            print(
                f"Warning: Skipping index metadata for {len(unmatched_index_tables)} table(s) "
                "that were not present in master metadata."
            )

        print(f"Generating OKF files for {len(valid_tables)} tables...")
        
        for (db, tbl), data in valid_tables.items():
            ddl_content = fetch_table_ddl(cursor, db, tbl, data['info']['type'])
            md_content = generate_okf_markdown(data['info'], data['columns'], data['indices'], ddl_content)
            
            db_dir = os.path.join(TABLES_DIR, db.replace(" ", "_").replace('"', ''))
            os.makedirs(db_dir, exist_ok=True)
            
            safe_filename = f"{tbl}".replace(" ", "_").replace('"', '') + ".md"
            filepath = os.path.join(db_dir, safe_filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(md_content)
                
        # Call the new indices generator
        generate_bundle_indices(valid_tables, OUTPUT_DIR, TABLES_DIR)
                
        print(f"Success! OKF bundle created in '{OUTPUT_DIR}'.")

    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    main()