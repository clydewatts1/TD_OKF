import os
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict
from databricks import sql
from azure.identity import InteractiveBrowserCredential
from dotenv import load_dotenv

# Load configuration properties from the environment file
load_dotenv()

# Configuration mapping
DBR_SERVER = os.getenv("DATABRICKS_SERVER_HOSTNAME")
DBR_HTTP = os.getenv("DATABRICKS_HTTP_PATH")
DBR_TOKEN = os.getenv("DATABRICKS_TOKEN")

# Allow comma-separated lists with % wildcards
CATALOG_PATTERN = os.getenv("DATABRICKS_CATALOG", "%")
SCHEMA_PATTERN = os.getenv("DATABRICKS_SCHEMA", "%")
OBJECT_PATTERN = os.getenv("DATABRICKS_OBJECTS", "%")  # Equivalent to SOURCE_TABLE_PATTERN
OKF_DIRECTORY = os.getenv("DATABRICKS_OKF_DIRECTORY", "okf_bundle_databricks")
TABLES_DIR = os.path.join(OKF_DIRECTORY, "tables")
INCLUDE_ROW_COUNTS = os.getenv("DATABRICKS_INCLUDE_ROW_COUNTS", "false").strip().lower() in {"1", "true", "yes", "y", "on"}


# --- Utility Functions ---

def normalize_text(value, default=""):
    if value is None: return default
    cleaned = str(value).strip()
    return cleaned if cleaned else default

def anchor_slug(value):
    slug = normalize_text(value).lower().replace(" ", "-")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    return "".join(ch for ch in slug if ch in allowed)

def md_cell(value):
    return normalize_text(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")

def sql_lit(value):
    """Escapes single quotes for SQL string literals."""
    return normalize_text(value).replace("'", "''")

def human_size(num_bytes):
    if num_bytes in [None, ""]: return ""
    try: value = float(num_bytes)
    except: return normalize_text(num_bytes)
    units = [(1024 ** 4, "TB"), (1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")]
    for base, unit in units:
        if value >= base: return f"{value / base:.2f} {unit}"
    return f"{value:.0f} bytes"

def map_databricks_type_to_okf(dbr_type):
    """Maps Databricks Spark SQL types to OKF types."""
    dt = normalize_text(dbr_type).upper()
    if 'INT' in dt or dt in ['LONG', 'SHORT', 'BYTE']: return 'integer'
    if 'DECIMAL' in dt or dt in ['FLOAT', 'DOUBLE']: return 'number'
    if 'TIMESTAMP' in dt: return 'datetime'
    if 'DATE' in dt: return 'date'
    if 'BOOLEAN' in dt: return 'boolean'
    return 'string'

def parse_like_patterns(raw_value):
    raw_text = normalize_text(raw_value, "%")
    return [item.strip() for item in raw_text.split(",") if item.strip()] or ["%"]

def build_sql_or(col, patterns):
    """Builds safe SQL OR conditions for the patterns to query Unity Catalog"""
    clauses = [f"{col} LIKE '{sql_lit(p)}'" for p in patterns]
    return "(" + " OR ".join(clauses) + ")"

def kv_rows_to_md(rows):
    if not rows:
        return ""
    md = "| Key | Value |\n| :--- | :--- |\n"
    for key, value in rows:
        md += f"| `{md_cell(key)}` | {md_cell(value)} |\n"
    return md


# --- Databricks Connection & Extraction ---

def get_databricks_connection():
    if not all([DBR_SERVER, DBR_HTTP]):
        print("Error: DATABRICKS_SERVER_HOSTNAME and DATABRICKS_HTTP_PATH must be set.")
        sys.exit(1)

    token = DBR_TOKEN
    if not token:
        print("No DATABRICKS_TOKEN found. Initiating Azure SSO interactive login...")
        try:
            credential = InteractiveBrowserCredential()
            token_obj = credential.get_token("2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default")
            token = token_obj.token
            print("SSO login complete.")
        except Exception as e:
            print(f"Failed to authenticate via SSO: {e}")
            sys.exit(1)

    print(f"Connecting to Databricks SQL Warehouse at '{DBR_SERVER}'...")
    try:
        return sql.connect(
            server_hostname=DBR_SERVER,
            http_path=DBR_HTTP,
            access_token=token
        )
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def fetch_master_metadata(cursor):
    """Fetches logical table and column definitions from system.information_schema."""
    print(f"Extracting master schema from Unity Catalog...")
    
    catalogs = parse_like_patterns(CATALOG_PATTERN)
    schemas = parse_like_patterns(SCHEMA_PATTERN)
    tables = parse_like_patterns(OBJECT_PATTERN)

    cat_clause = build_sql_or("t.table_catalog", catalogs)
    sch_clause = build_sql_or("t.table_schema", schemas)
    tbl_clause = build_sql_or("t.table_name", tables)

    extended_query = f"""
    SELECT 
        t.table_catalog,
        t.table_schema,
        t.table_name,
        t.table_type,
        t.comment AS table_description,
        t.created,
        t.created_by,
        t.last_altered,
        t.last_altered_by,
        t.data_source_format,
        t.storage_path,
        c.column_name,
        c.data_type,
        c.is_nullable,
        c.comment AS column_description,
        c.ordinal_position
    FROM system.information_schema.tables t
    JOIN system.information_schema.columns c
      ON t.table_catalog = c.table_catalog
      AND t.table_schema = c.table_schema
      AND t.table_name = c.table_name
    WHERE {cat_clause} AND {sch_clause} AND {tbl_clause}
    ORDER BY t.table_catalog, t.table_schema, t.table_name, c.ordinal_position
    """

    basic_query = f"""
    SELECT 
        t.table_catalog,
        t.table_schema,
        t.table_name,
        t.table_type,
        t.comment AS table_description,
        NULL AS created,
        NULL AS created_by,
        NULL AS last_altered,
        NULL AS last_altered_by,
        NULL AS data_source_format,
        NULL AS storage_path,
        c.column_name,
        c.data_type,
        c.is_nullable,
        c.comment AS column_description,
        c.ordinal_position
    FROM system.information_schema.tables t
    JOIN system.information_schema.columns c
      ON t.table_catalog = c.table_catalog
      AND t.table_schema = c.table_schema
      AND t.table_name = c.table_name
    WHERE {cat_clause} AND {sch_clause} AND {tbl_clause}
    ORDER BY t.table_catalog, t.table_schema, t.table_name, c.ordinal_position
    """

    for label, query in [("extended", extended_query), ("fallback", basic_query)]:
        try:
            cursor.execute(query)
            rows = cursor.fetchall()
            return [row.asDict() for row in rows]
        except Exception as e:
            print(f"Warning: {label} metadata query failed: {e}")

    print("Error reading master metadata.")
    return []


def fetch_constraints(cursor):
    """Fetches Primary and Foreign Key constraints from Unity Catalog."""
    print(f"Extracting Primary/Foreign Key relationships...")
    catalogs = parse_like_patterns(CATALOG_PATTERN)
    schemas = parse_like_patterns(SCHEMA_PATTERN)
    tables = parse_like_patterns(OBJECT_PATTERN)

    cat_clause = build_sql_or("tc.table_catalog", catalogs)
    sch_clause = build_sql_or("tc.table_schema", schemas)
    tbl_clause = build_sql_or("tc.table_name", tables)

    query = f"""
    SELECT
        tc.table_catalog,
        tc.table_schema,
        tc.table_name,
        tc.constraint_type,
        tc.constraint_name,
        kcu.column_name,
        ccu.table_catalog AS ref_table_catalog,
        ccu.table_schema AS ref_table_schema,
        ccu.table_name AS ref_table_name,
        ccu.column_name AS ref_column_name
    FROM system.information_schema.table_constraints tc
    JOIN system.information_schema.key_column_usage kcu
      ON tc.constraint_catalog = kcu.constraint_catalog
      AND tc.constraint_schema = kcu.constraint_schema
      AND tc.constraint_name = kcu.constraint_name
    LEFT JOIN system.information_schema.referential_constraints rc
      ON tc.constraint_catalog = rc.constraint_catalog
      AND tc.constraint_schema = rc.constraint_schema
      AND tc.constraint_name = rc.constraint_name
    LEFT JOIN system.information_schema.constraint_column_usage ccu
      ON rc.unique_constraint_catalog = ccu.constraint_catalog
      AND rc.unique_constraint_schema = ccu.constraint_schema
      AND rc.unique_constraint_name = ccu.constraint_name
    WHERE {cat_clause} AND {sch_clause} AND {tbl_clause}
      AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
    ORDER BY tc.table_catalog, tc.table_schema, tc.table_name, tc.constraint_type, tc.constraint_name, kcu.ordinal_position
    """
    try:
        cursor.execute(query)
        return [row.asDict() for row in cursor.fetchall()]
    except Exception as e:
        print(f"Warning: Could not fetch constraints (Feature might require higher Unity Catalog privileges): {e}")
        return []


def fetch_tags(cursor):
    """Fetches Unity Catalog table and column tags where available."""
    print("Extracting Unity Catalog tags (if permitted)...")
    catalogs = parse_like_patterns(CATALOG_PATTERN)
    schemas = parse_like_patterns(SCHEMA_PATTERN)
    tables = parse_like_patterns(OBJECT_PATTERN)

    cat_clause = build_sql_or("table_catalog", catalogs)
    sch_clause = build_sql_or("table_schema", schemas)
    tbl_clause = build_sql_or("table_name", tables)

    table_tags_query = f"""
    SELECT table_catalog, table_schema, table_name, tag_name, tag_value
    FROM system.information_schema.table_tags
    WHERE {cat_clause} AND {sch_clause} AND {tbl_clause}
    """

    column_tags_query = f"""
    SELECT table_catalog, table_schema, table_name, column_name, tag_name, tag_value
    FROM system.information_schema.column_tags
    WHERE {cat_clause} AND {sch_clause} AND {tbl_clause}
    """

    table_tags = defaultdict(list)
    column_tags = defaultdict(lambda: defaultdict(list))

    try:
        cursor.execute(table_tags_query)
        for row in cursor.fetchall():
            d = row.asDict()
            key = (d["table_catalog"], d["table_schema"], d["table_name"])
            table_tags[key].append((d["tag_name"], d["tag_value"]))
    except Exception as e:
        print(f"  Warning: Could not read table tags - {e}")

    try:
        cursor.execute(column_tags_query)
        for row in cursor.fetchall():
            d = row.asDict()
            key = (d["table_catalog"], d["table_schema"], d["table_name"])
            column_tags[key][d["column_name"]].append((d["tag_name"], d["tag_value"]))
    except Exception as e:
        print(f"  Warning: Could not read column tags - {e}")

    return table_tags, column_tags


def fetch_table_details(cursor, catalog, schema, table, obj_type):
    """Runs DESCRIBE DETAIL and COUNT(1) to get exact physical metrics."""
    fqn = f"`{catalog}`.`{schema}`.`{table}`"
    metrics = {
        "size_bytes": None, "format": "Unknown", "created_at": None, "last_modified": None, 
        "row_count": None, "num_files": None, "partition_columns": [], "clustering_columns": [],
        "location": None, "table_id": None, "properties": {}
    }
    ddl = ""

    # Fetch DDL
    try:
        cursor.execute(f"SHOW CREATE TABLE {fqn}")
        result = cursor.fetchone()
        if result:
            ddl = result[0].replace('\r', '\n')
    except Exception as e:
        print(f"  Warning: Could not fetch DDL for {fqn} - {e}")

    # Fetch physical metrics & exact row count (Skip for Views)
    if "VIEW" not in normalize_text(obj_type).upper():
        
        # 1. Physical metadata (clustering, partitioning, size)
        try:
            cursor.execute(f"DESCRIBE DETAIL {fqn}")
            result = cursor.fetchone()
            if result:
                row_dict = result.asDict()
                metrics["size_bytes"] = row_dict.get("sizeInBytes")
                metrics["format"] = row_dict.get("format", "Unknown")
                metrics["created_at"] = row_dict.get("createdAt")
                metrics["last_modified"] = row_dict.get("lastModified")
                metrics["num_files"] = row_dict.get("numFiles")
                metrics["partition_columns"] = row_dict.get("partitionColumns", [])
                metrics["clustering_columns"] = row_dict.get("clusteringColumns", [])
                metrics["location"] = row_dict.get("location")
                metrics["table_id"] = row_dict.get("id")
                metrics["properties"] = row_dict.get("properties") or {}
        except Exception:
            pass # Suppress detail errors for unmanaged/external tables

        # 2. Optional exact row count (can be expensive on non-Delta tables)
        if INCLUDE_ROW_COUNTS:
            try:
                cursor.execute(f"SELECT COUNT(1) FROM {fqn}")
                row_count = cursor.fetchone()
                if row_count:
                    metrics["row_count"] = row_count[0]
            except Exception as e:
                print(f"  Warning: Could not count rows for {fqn} - {e}")

    return metrics, ddl


# --- Markdown Generation ---

def generate_okf_markdown(info, columns, metrics, constraints, ddl, table_tags=None, column_tags=None):
    """Constructs the OKF v0.1 compliant Markdown file string."""
    catalog, schema, table = info['catalog'], info['schema'], info['table']
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    obj_type = normalize_text(info['type'], 'TABLE')
    tbl_desc = normalize_text(info['desc'], "No description provided.")
    ds_format = normalize_text(info.get('data_source_format'))
    storage_path = normalize_text(info.get('storage_path'))
    created = normalize_text(info.get('created'))
    created_by = normalize_text(info.get('created_by'))
    last_altered = normalize_text(info.get('last_altered'))
    last_altered_by = normalize_text(info.get('last_altered_by'))
    
    size_str = f"{metrics['size_bytes']:,} bytes ({human_size(metrics['size_bytes'])})" if metrics['size_bytes'] else "Unknown"
    rows_str = f"{metrics['row_count']:,}" if metrics['row_count'] is not None else "Unknown"
    files_str = f"{metrics['num_files']:,}" if metrics['num_files'] is not None else "Unknown"

    part_cols = f"`{', '.join(metrics['partition_columns'])}`" if metrics.get('partition_columns') else "None"
    clust_cols = f"`{', '.join(metrics['clustering_columns'])}`" if metrics.get('clustering_columns') else "None"

    # Separate PK and FK constraints
    pk_cols = [c['column_name'] for c in constraints if c['constraint_type'] == 'PRIMARY KEY']
    fk_rows = [c for c in constraints if c['constraint_type'] == 'FOREIGN KEY']
    fk_cols = [c['column_name'] for c in fk_rows]
    
    pk_str = f"`{', '.join(pk_cols)}`" if pk_cols else "None"
    fk_str = f"`{', '.join(fk_cols)}`" if fk_cols else "None"

    md = f"""---
type: Databricks Table
title: "{catalog}.{schema}.{table}"
tags:
  - {catalog}
  - {schema}
  - databricks
  - {obj_type.lower().replace(' ', '_')}
timestamp: {current_time}
---

# {catalog}.{schema}.{table}

**Catalog:** `{catalog}`  
**Schema:** `{schema}`  
**Object Type:** `{obj_type}`  
**Description:** {tbl_desc}  
**Created:** `{created or 'Unknown'}` by `{created_by or 'Unknown'}`  
**Last Altered:** `{last_altered or 'Unknown'}` by `{last_altered_by or 'Unknown'}`  

**Format:** `{metrics['format']}`  
**Data Source Format (I_S):** `{ds_format or 'Unknown'}`  
**Rows:** `{rows_str}`  
**Physical Size:** `{size_str}` (across {files_str} files)  
**Storage Path:** `{storage_path or metrics.get('location') or 'Unknown'}`  

**Primary Key:** {pk_str}  
**Foreign Keys:** {fk_str}  

## Databricks Optimization
*(Equivalent to Teradata Primary Indexes & Partitioning)*

**Partition Columns:** {part_cols}  
**Liquid Clustering Columns:** {clust_cols}  

## Databricks Details

**Table ID:** `{normalize_text(metrics.get('table_id'), 'Unknown')}`  
**DESCRIBE DETAIL Location:** `{normalize_text(metrics.get('location'), 'Unknown')}`  

## Schema

| Column Name | Databricks Type | OKF Type | Nullable | Description | Order |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""
    for col in columns:
        is_null = "True" if normalize_text(col['nullable']).upper() == 'YES' else "False"
        dbr_type = normalize_text(col['type'], 'UNKNOWN')
        okf_type = map_databricks_type_to_okf(dbr_type)
        desc = md_cell(col['desc'])
        col_name = col['name']
        if column_tags and column_tags.get(col_name):
            tag_str = ", ".join([f"{k}:{v}" for k, v in column_tags[col_name]])
            desc = f"{desc} (tags: {md_cell(tag_str)})"
        md += f"| `{col_name}` | `{dbr_type}` | `{okf_type}` | `{is_null}` | {desc} | {col['order']} |\n"

    if table_tags:
        md += "\n## Unity Catalog Table Tags\n\n"
        md += kv_rows_to_md(table_tags)

    if fk_rows:
        md += "\n## Foreign Key References\n\n"
        md += "| Column | References | Constraint |\n| :--- | :--- | :--- |\n"
        for fk in fk_rows:
            ref = f"{normalize_text(fk.get('ref_table_catalog'))}.{normalize_text(fk.get('ref_table_schema'))}.{normalize_text(fk.get('ref_table_name'))}.{normalize_text(fk.get('ref_column_name'))}"
            md += f"| `{normalize_text(fk.get('column_name'))}` | `{md_cell(ref)}` | `{normalize_text(fk.get('constraint_name'))}` |\n"

    properties = metrics.get('properties') or {}
    if properties:
        md += "\n## Table Properties\n\n"
        rows = sorted([(str(k), str(v)) for k, v in properties.items()], key=lambda x: x[0].lower())
        md += kv_rows_to_md(rows)

    if ddl:
        md += "\n## Databricks DDL\n\n```sql\n"
        md += ddl.strip() + "\n```\n"

    return md


def generate_bundle_indices(tables):
    """Generates the root index.md and individual catalog/schema index.md files."""
    print("Generating OKF index files...")
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    hierarchy = defaultdict(lambda: defaultdict(list))
    for (cat, sch, tbl), data in tables.items():
        hierarchy[cat][sch].append((tbl, data['info']['type'], data['info']['desc']))

    # 1. Master Index
    master_md = f"""---
type: Bundle
title: "Databricks Metadata Bundle"
description: "Master index of all extracted Databricks tables and views."
timestamp: {current_time}
---

# Databricks Metadata Bundle (Unity Catalog)

## Catalogs & Schemas

"""
    for cat in sorted(hierarchy.keys()):
        master_md += f"### Catalog: `{cat}`\n\n"
        master_md += "| Schema | Objects | Link |\n| :--- | ---: | :--- |\n"
        for sch in sorted(hierarchy[cat].keys()):
            obj_count = len(hierarchy[cat][sch])
            safe_cat = anchor_slug(cat)
            safe_sch = anchor_slug(sch)
            master_md += f"| `{sch}` | {obj_count} | [View Index](tables/{safe_cat}/{safe_sch}/index.md) |\n"
        master_md += "\n"

    os.makedirs(OKF_DIRECTORY, exist_ok=True)
    with open(os.path.join(OKF_DIRECTORY, "index.md"), 'w', encoding='utf-8') as f:
        f.write(master_md)

    # 2. Schema-Level Indexes
    for cat in hierarchy:
        safe_cat = anchor_slug(cat)
        for sch in hierarchy[cat]:
            safe_sch = anchor_slug(sch)
            sch_dir = os.path.join(TABLES_DIR, safe_cat, safe_sch)
            os.makedirs(sch_dir, exist_ok=True)
            
            sch_md = f"""---
type: Collection
title: "{cat}.{sch}"
description: "Index of tables and views in {cat}.{sch}."
timestamp: {current_time}
---

# Schema: {cat}.{sch}

[Back to master index](../../../index.md)

| Object Name | Type | Description |
| :--- | :--- | :--- |
"""
            sorted_objs = sorted(hierarchy[cat][sch], key=lambda x: x[0].upper())
            for tbl, ttype, desc in sorted_objs:
                safe_tbl = anchor_slug(tbl)
                clean_desc = md_cell(desc) if desc else "No description provided."
                sch_md += f"| [{tbl}]({safe_tbl}.md) | `{ttype}` | {clean_desc} |\n"

            with open(os.path.join(sch_dir, "index.md"), 'w', encoding='utf-8') as f:
                f.write(sch_md)


def main():
    os.makedirs(TABLES_DIR, exist_ok=True)
    conn = get_databricks_connection()
    cursor = conn.cursor()
    
    try:
        master_rows = fetch_master_metadata(cursor)
        constraint_rows = fetch_constraints(cursor)
        table_tags_map, column_tags_map = fetch_tags(cursor)

        if not master_rows:
            print("No matching metadata found in Unity Catalog. Check your patterns or permissions.")
            return
            
        print(f"Discovered {len(master_rows)} columns. Structuring data...")
        
        # Group Data
        tables = defaultdict(lambda: {'info': {}, 'columns': [], 'constraints': []})
        
        for row in master_rows:
            cat = row['table_catalog']
            sch = row['table_schema']
            tbl = row['table_name']
            ttype = row['table_type']
            tdesc = row['table_description']
            col = row['column_name']
            dtype = row['data_type']
            is_null = row['is_nullable']
            cdesc = row['column_description']
            order = row['ordinal_position']
            key = (cat, sch, tbl)
            if not tables[key]['info']:
                tables[key]['info'] = {
                    'catalog': cat,
                    'schema': sch,
                    'table': tbl,
                    'type': ttype,
                    'desc': tdesc,
                    'created': row.get('created'),
                    'created_by': row.get('created_by'),
                    'last_altered': row.get('last_altered'),
                    'last_altered_by': row.get('last_altered_by'),
                    'data_source_format': row.get('data_source_format'),
                    'storage_path': row.get('storage_path')
                }
            tables[key]['columns'].append({
                'name': col, 'type': dtype, 'nullable': is_null, 'desc': cdesc, 'order': order
            })

        for row in constraint_rows:
            cat = row['table_catalog']
            sch = row['table_schema']
            tbl = row['table_name']
            key = (cat, sch, tbl)
            if key in tables:
                tables[key]['constraints'].append({
                    'constraint_type': row['constraint_type'],
                    'constraint_name': row['constraint_name'],
                    'column_name': row['column_name'],
                    'ref_table_catalog': row.get('ref_table_catalog'),
                    'ref_table_schema': row.get('ref_table_schema'),
                    'ref_table_name': row.get('ref_table_name'),
                    'ref_column_name': row.get('ref_column_name')
                })

        print(f"Generating OKF files and fetching physical metrics for {len(tables)} objects...")
        
        for (cat, sch, tbl), data in tables.items():
            print(f" -> Processing {cat}.{sch}.{tbl}")
            metrics, ddl = fetch_table_details(cursor, cat, sch, tbl, data['info']['type'])
            table_tags = table_tags_map.get((cat, sch, tbl), [])
            column_tags = column_tags_map.get((cat, sch, tbl), {})
            
            md_content = generate_okf_markdown(
                data['info'],
                data['columns'],
                metrics,
                data['constraints'],
                ddl,
                table_tags=table_tags,
                column_tags=column_tags
            )
            
            safe_cat = anchor_slug(cat)
            safe_sch = anchor_slug(sch)
            safe_tbl = anchor_slug(tbl)
            
            sch_dir = os.path.join(TABLES_DIR, safe_cat, safe_sch)
            os.makedirs(sch_dir, exist_ok=True)
            
            with open(os.path.join(sch_dir, f"{safe_tbl}.md"), 'w', encoding='utf-8') as f:
                f.write(md_content)
                
        # Call the indices generator
        generate_bundle_indices(tables)
                
        print(f"Success! Databricks OKF bundle created in '{OKF_DIRECTORY}'.")

    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    main()