import os
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict
import re
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


def md_cell(value):
    text = normalize_text(value)
    return text.replace("\n", " ").replace("\r", " ").replace("|", "\\|")


def format_number(value):
    if value in [None, ""]:
        return ""
    try:
        return f"{int(float(value)):,}"
    except Exception:
        return normalize_text(value)


def human_size(num_bytes):
    if num_bytes in [None, ""]:
        return ""
    try:
        value = float(num_bytes)
    except Exception:
        return normalize_text(num_bytes)

    units = [(1024 ** 4, "TB"), (1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")]
    for base, unit in units:
        if value >= base:
            return f"{value / base:.2f} {unit}"
    return f"{value:.0f} bytes"


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


def parse_like_patterns(raw_value):
    raw_text = normalize_text(raw_value, "%")
    patterns = [item.strip() for item in raw_text.split(",") if item.strip()]
    return patterns or ["%"]


def build_like_filter(column_name, raw_patterns):
    patterns = parse_like_patterns(raw_patterns)
    if len(patterns) == 1:
        return f"{column_name} LIKE ?", patterns

    placeholders = ", ".join(["?"] * len(patterns))
    return f"{column_name} LIKE ANY ({placeholders})", patterns


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
    db_filter, db_params = build_like_filter("T.DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("T.TableName", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT 
        C.DatabaseName, C.TableName, C.ColumnName, T.TableKind,
        T.CommentString AS TableDescription,
        C.CommentString AS ColumnDescription,
        C.ColumnTitle,
        C.DefaultValue,
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
    WHERE {db_filter} AND {table_filter}
      AND T.TableKind IN ('T', 'O', 'V')
    ORDER BY C.DatabaseName, C.TableName, ColumnOrder;
    """
    try:
        cursor.execute(query, db_params + table_params)
        return cursor.fetchall()
    except Exception as e:
        print(f"Error reading master metadata: {e}")
        return []

def fetch_indices(cursor):
    """Fetches index metadata used for table-level index rendering."""
    print("Extracting index definitions...")
    db_filter, db_params = build_like_filter("DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("TableName", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT
        DatabaseName,
        TableName,
        IndexNumber,
        IndexName,
        IndexType,
        UniqueFlag,
        ColumnPosition,
        ColumnName
    FROM DBC.IndicesV
    WHERE {db_filter} AND {table_filter}
      AND IndexType IN ('P', 'Q', 'S', 'U', 'J', 'N', 'K')
    ORDER BY DatabaseName, TableName, IndexNumber, ColumnPosition;
    """
    try:
        cursor.execute(query, db_params + table_params)
        return cursor.fetchall()
    except Exception as e:
        print(f"Error reading indices: {e}")
        return []


def fetch_statistics(cursor):
    """Fetches table statistics metadata used for stats section rendering."""
    print("Extracting statistics definitions...")
    db_filter, db_params = build_like_filter("DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("TableName", SOURCE_TABLE_PATTERN)

    full_query = f"""
    SELECT
        DatabaseName,
        TableName,
        StatsId,
        StatsName,
        StatsType,
        LastCollectTimeStamp,
        RowCount,
        UniqueValueCount,
        MinValue,
        MaxValue,
        NumOfNulls,
        CAST(NULL AS INTEGER) AS ColumnPosition,
        ColumnName
    FROM DBC.StatsV
    WHERE {db_filter} AND {table_filter}
    ORDER BY DatabaseName, TableName, StatsId, ColumnName;
    """
    reduced_query = f"""
    SELECT
        DatabaseName,
        TableName,
        StatsId,
        StatsName,
        StatsType,
        LastCollectTimeStamp,
        RowCount,
        CAST(NULL AS BIGINT) AS UniqueValueCount,
        CAST(NULL AS VARCHAR(1024)) AS MinValue,
        CAST(NULL AS VARCHAR(1024)) AS MaxValue,
        CAST(NULL AS BIGINT) AS NumOfNulls,
        CAST(NULL AS INTEGER) AS ColumnPosition,
        ColumnName
    FROM DBC.StatsV
    WHERE {db_filter} AND {table_filter}
    ORDER BY DatabaseName, TableName, StatsId, ColumnName;
    """
    try:
        cursor.execute(full_query, db_params + table_params)
        return cursor.fetchall()
    except Exception as e:
        err_text = str(e)
        if "Column" in err_text and "not found" in err_text:
            try:
                print("Warning: StatsV detail columns unavailable. Falling back to reduced statistics projection.")
                cursor.execute(reduced_query, db_params + table_params)
                return cursor.fetchall()
            except Exception as fallback_err:
                first_line = str(fallback_err).splitlines()[0] if str(fallback_err) else "unknown error"
                print(f"Warning: Could not read reduced statistics metadata: {first_line}")
                return []
        first_line = err_text.splitlines()[0] if err_text else "unknown error"
        print(f"Warning: Could not read statistics metadata: {first_line}")
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


def fetch_partitioning(cursor):
    print("Extracting partitioning metadata...")
    db_filter, db_params = build_like_filter("pc.DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("pc.TableName", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT
        pc.DatabaseName,
        pc.TableName,
        pc.ConstraintText,
        pc.ColumnPartitioningLevel,
        pc.PartitioningLevels
    FROM DBC.PartitioningConstraintsV pc
    WHERE {db_filter} AND {table_filter}
    ORDER BY pc.DatabaseName, pc.TableName;
    """
    try:
        cursor.execute(query, db_params + table_params)
        return cursor.fetchall()
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        print(f"Warning: Could not read partitioning metadata: {first_line}")
        return []


def fetch_relationships_outbound(cursor):
    print("Extracting outbound relationship metadata...")
    db_filter, db_params = build_like_filter("ri.ChildDB", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("ri.ChildTable", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT
        ri.ChildDB,
        ri.ChildTable,
        ri.IndexName,
        ri.ChildKeyColumn,
        ri.ParentDB,
        ri.ParentTable,
        ri.ParentKeyColumn
    FROM DBC.All_RI_ChildrenV ri
    WHERE {db_filter} AND {table_filter}
    ORDER BY ri.ChildDB, ri.ChildTable, ri.IndexName, ri.ChildKeyColumn;
    """
    try:
        cursor.execute(query, db_params + table_params)
        return cursor.fetchall()
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        print(f"Warning: Could not read outbound relationships: {first_line}")
        return []


def fetch_relationships_inbound(cursor):
    print("Extracting inbound relationship metadata...")
    db_filter, db_params = build_like_filter("ri.ParentDB", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("ri.ParentTable", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT
        ri.ParentDB,
        ri.ParentTable,
        ri.ChildDB,
        ri.ChildTable,
        ri.ChildKeyColumn,
        ri.ParentKeyColumn
    FROM DBC.All_RI_ParentsV ri
    WHERE {db_filter} AND {table_filter}
    ORDER BY ri.ParentDB, ri.ParentTable, ri.ChildTable, ri.ChildKeyColumn;
    """
    try:
        cursor.execute(query, db_params + table_params)
        return cursor.fetchall()
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        print(f"Warning: Could not read inbound relationships: {first_line}")
        return []


def fetch_column_domains(cursor):
    print("Extracting column domain metadata...")
    db_filter, db_params = build_like_filter("c.DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("c.TableName", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT
        c.DatabaseName,
        c.TableName,
        c.ColumnName,
        c.CompressValueList
    FROM DBC.ColumnsV c
    WHERE {db_filter} AND {table_filter}
      AND c.CompressValueList IS NOT NULL
    ORDER BY c.DatabaseName, c.TableName, c.ColumnId;
    """
    try:
        cursor.execute(query, db_params + table_params)
        return cursor.fetchall()
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        print(f"Warning: Could not read column domains: {first_line}")
        return []


def fetch_storage_usage(cursor):
    print("Extracting storage and usage metadata...")
    db_filter, db_params = build_like_filter("DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("TableName", SOURCE_TABLE_PATTERN)

    size_query = f"""
    SELECT
        DatabaseName,
        TableName,
        COUNT(*) AS amp_count,
        SUM(CurrentPerm) AS total_perm_bytes,
        MAX(CurrentPerm) AS max_amp_bytes,
        AVG(CurrentPerm) AS avg_amp_bytes
    FROM DBC.TableSizeV
    WHERE {db_filter} AND {table_filter}
    GROUP BY 1, 2;
    """

    timestamp_query = f"""
    SELECT
        DatabaseName,
        TableName,
        CreateTimeStamp,
        LastAlterTimeStamp,
        LastAccessTimeStamp
    FROM DBC.TablesV
    WHERE {db_filter} AND {table_filter};
    """

    size_rows = []
    ts_rows = []
    try:
        cursor.execute(size_query, db_params + table_params)
        size_rows = cursor.fetchall()
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        print(f"Warning: Could not read table size metrics: {first_line}")

    try:
        cursor.execute(timestamp_query, db_params + table_params)
        ts_rows = cursor.fetchall()
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        print(f"Warning: Could not read table timestamp metrics: {first_line}")

    return size_rows, ts_rows

def format_column_list(columns):
    return ", ".join(f"`{col}`" for col in columns) if columns else ""


def split_column_names(raw_value):
    text = normalize_text(raw_value)
    if not text:
        return []
    return [col.strip() for col in text.split(",") if col.strip()]


def format_iso_utc(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt_val = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt_val.strftime("%Y-%m-%dT%H:%M:%SZ")

    text = normalize_text(value)
    if not text:
        return ""

    normalized = text.replace(" ", "T")
    if normalized.endswith("+00:00"):
        normalized = normalized[:-6] + "Z"
    return normalized if normalized.endswith("Z") else normalized + "Z"


def index_type_display(index_type):
    mapping = {
        'P': 'PI',
        'Q': 'PI (with partitioning)',
        'S': 'SI',
        'U': 'USI',
        'J': 'Join Index',
        'N': 'NUSI',
        'K': 'Primary Key',
    }
    return mapping.get(normalize_text(index_type).upper(), normalize_text(index_type, 'Unknown'))


def statistics_type_display(stat_type):
    mapping = {
        'C': 'Column',
        'I': 'Index',
        'T': 'Expression',
    }
    return mapping.get(normalize_text(stat_type).upper(), normalize_text(stat_type, 'Unknown'))


def parse_compress_values(raw_value):
    text = normalize_text(raw_value)
    if not text:
        return []
    values = [v.strip() for v in re.findall(r"'([^']*)'", text)]
    if values:
        return [v.strip() for v in values if v.strip()]
    return [v.strip() for v in text.split(",") if v.strip()]


def render_partitioning_section(partitioning):
    if not partitioning:
        return "Not partitioned (NPPI / no partitioning defined).\n"

    first = partitioning[0]
    levels = normalize_text(first.get('partitioning_levels'), '0')
    col_level = first.get('column_partitioning_level')
    col_partitioned = "Yes" if col_level not in [None, ""] and float(col_level) > 0 else "No"
    constraint_text = normalize_text(first.get('constraint_text'))

    md = f"**Partitioning Levels:** `{levels}`\n"
    md += f"**Column Partitioned:** `{col_partitioned}`\n\n"
    if constraint_text:
        md += "```sql\n"
        md += constraint_text + "\n"
        md += "```\n"
    else:
        md += "Not partitioned (NPPI / no partitioning defined).\n"
    return md


def render_relationships_section(outbound_rows, inbound_rows):
    md = "**Foreign keys (this table references):**\n\n"
    if not outbound_rows:
        md += "None declared.\n\n"
    else:
        grouped = defaultdict(list)
        for row in outbound_rows:
            grouped[normalize_text(row['index_name'], '(unnamed)')].append(row)

        md += "| Local Column | References | On Column |\n"
        md += "| :--- | :--- | :--- |\n"
        for idx_name in sorted(grouped.keys()):
            rows = grouped[idx_name]
            local_cols = format_column_list([normalize_text(r['child_key_column']) for r in rows if normalize_text(r['child_key_column'])])
            parent_cols = format_column_list([normalize_text(r['parent_key_column']) for r in rows if normalize_text(r['parent_key_column'])])
            first = rows[0]
            references = f"`{normalize_text(first['parent_db'])}.{normalize_text(first['parent_table'])}`"
            md += f"| {local_cols} | {references} | {parent_cols} |\n"
        md += "\n"

    md += "**Referenced by (tables pointing here):**\n\n"
    if not inbound_rows:
        md += "None declared.\n"
    else:
        md += "| Child Table | Child Column | On Local Column |\n"
        md += "| :--- | :--- | :--- |\n"
        for row in inbound_rows:
            child_table = f"`{normalize_text(row['child_db'])}.{normalize_text(row['child_table'])}`"
            child_col = format_column_list(split_column_names(row['child_key_column']))
            parent_col = format_column_list(split_column_names(row['parent_key_column']))
            md += f"| {child_table} | {child_col} | {parent_col} |\n"
    return md


def render_domains_section(domain_rows):
    intro = (
        "Enumerated values derived from column compression definitions. These reflect the low-cardinality\n"
        "domain of each column (permitted / commonly-observed values), not a full distinct scan.\n\n"
    )
    if not domain_rows:
        return "No enumerated domains defined (no multi-value compression on this table).\n"

    md = intro
    md += "| Column | Distinct Domain Values (count) | Values |\n"
    md += "| :--- | :--- | :--- |\n"
    for row in domain_rows:
        values = [v.rstrip() for v in parse_compress_values(row['compress_value_list'])]
        full_count = len(values)
        shown = values[:30]
        rendered_values = ", ".join(f"`{md_cell(v)}`" for v in shown)
        if full_count > 30:
            rendered_values += f" … (+{full_count - 30} more)"
        md += f"| `{md_cell(row['column_name'])}` | {full_count} | {rendered_values} |\n"
    return md


def render_storage_usage_section(storage):
    total_bytes = storage.get('total_perm_bytes')
    amp_count = storage.get('amp_count')
    max_amp = storage.get('max_amp_bytes')
    avg_amp = storage.get('avg_amp_bytes')

    skew_factor = ""
    skew_num = None
    try:
        max_amp_val = float(max_amp)
        avg_amp_val = float(avg_amp)
        skew_num = (max_amp_val - avg_amp_val) / max_amp_val if max_amp_val > 0 else 0.0
        skew_factor = f"{skew_num:.2f}"
    except Exception:
        skew_factor = ""

    total_perm = ""
    if total_bytes not in [None, ""]:
        total_perm = f"`{format_number(total_bytes)} bytes ({human_size(total_bytes)})`"

    created = format_iso_utc(storage.get('create_timestamp'))
    altered = format_iso_utc(storage.get('last_alter_timestamp'))
    accessed = format_iso_utc(storage.get('last_access_timestamp')) if storage.get('last_access_timestamp') else "Not tracked"

    md = "| Metric | Value |\n"
    md += "| :--- | :--- |\n"
    md += f"| Total Perm Space | {total_perm} |\n"
    md += f"| AMP Count | `{format_number(amp_count)}` |\n"
    md += f"| Skew Factor | `{skew_factor}` |\n"
    md += f"| Created | `{created}` |\n"
    md += f"| Last Altered | `{altered}` |\n"
    md += f"| Last Accessed | `{accessed}` |\n"

    if skew_num is not None and skew_num > 0.20:
        md += "\nSkew interpretation: notable skew (> 0.20). Avoid reusing PI columns as Databricks distribution keys.\n"
    return md


def strip_compress_clauses(sql_text):
    upper_text = sql_text.upper()
    out = []
    i = 0
    n = len(sql_text)

    while i < n:
        if upper_text.startswith("COMPRESS", i):
            j = i + len("COMPRESS")
            while j < n and sql_text[j].isspace():
                j += 1
            if j < n and sql_text[j] == '(':
                depth = 0
                while j < n:
                    if sql_text[j] == '(':
                        depth += 1
                    elif sql_text[j] == ')':
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                i = j
                continue
            i = j
            continue

        out.append(sql_text[i])
        i += 1

    return "".join(out)


def build_logical_ddl(ddl, primary_index_comment, partitioning_comment):
    if not normalize_text(ddl):
        return ""

    logical = ddl.replace("\r", "\n")
    logical = strip_compress_clauses(logical)
    logical = re.sub(r"\bCOMPRESS\b", "", logical, flags=re.IGNORECASE)
    logical = re.sub(r"\s+CHARACTER\s+SET\s+(?:LATIN|UNICODE)", "", logical, flags=re.IGNORECASE)
    logical = re.sub(r"\s+(?:NOT\s+)?CASESPECIFIC\b", "", logical, flags=re.IGNORECASE)

    logical = re.sub(
        r"CREATE\s+(?:MULTISET\s+|SET\s+)?TABLE\s+([^\s,]+)\s*,.*?\(",
        r"CREATE TABLE \1 (",
        logical,
        flags=re.IGNORECASE | re.DOTALL,
    )

    logical = re.split(
        r"\n\s*(?:UNIQUE\s+)?PRIMARY\s+INDEX\b|\n\s*NO\s+PRIMARY\s+INDEX\b|\n\s*PARTITION\s+BY\b",
        logical,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    logical = re.sub(r"\n{3,}", "\n\n", logical)
    logical = re.sub(r"[ \t]+", " ", logical)
    logical = logical.strip()
    if logical.endswith(")"):
        logical += ";"
    elif not logical.endswith(";"):
        logical += "\n;"

    if normalize_text(primary_index_comment):
        logical += f"\n-- Original PI: {primary_index_comment}"
    if normalize_text(partitioning_comment):
        logical += f"\n-- Original partitioning: {partitioning_comment}"
    return logical


def render_collapsed_ddl(ddl):
    if not normalize_text(ddl):
        return ""
    md = "<details>\n"
    md += "<summary>Raw Teradata DDL (physical, includes MVC compression lists)</summary>\n\n"
    md += "```sql\n"
    md += ddl.strip() + "\n"
    md += "```\n\n"
    md += "</details>\n"
    return md


def render_indexes_section(index_details):
    if not index_details:
        return "No indexes defined.\n"

    grouped = defaultdict(list)
    for row in index_details:
        grouped[row['index_number']].append(row)

    md = "| Index Number | Index Name | Type | Columns | Unique |\n"
    md += "| :--- | :--- | :--- | :--- | :--- |\n"

    def sort_key(value):
        try:
            return int(value)
        except Exception:
            return str(value)

    for index_number in sorted(grouped.keys(), key=sort_key):
        rows = sorted(grouped[index_number], key=lambda item: (item['column_position'] if item['column_position'] is not None else 0))
        first = rows[0]
        idx_type = normalize_text(first['index_type']).upper()
        idx_name = normalize_text(first['index_name'])
        display_name = "Primary Index" if idx_type in ('P', 'Q') else (f"`{idx_name}`" if idx_name else "`(unnamed)`")
        col_names = [normalize_text(item['column_name']) for item in rows if normalize_text(item['column_name'])]
        col_list = format_column_list(col_names)
        unique_flag = "True" if normalize_text(first['unique_flag']).upper() == 'Y' else "False"
        md += f"| {index_number} | {display_name} | {index_type_display(idx_type)} | {col_list} | {unique_flag} |\n"

    return md


def render_statistics_section(stat_rows):
    if not stat_rows:
        return "No statistics collected.\n"

    grouped = defaultdict(list)
    for row in stat_rows:
        grouped[row['stats_id']].append(row)

    md = "| Column(s) | Type | Last Collected | Row Count | Distinct Values | Nulls | Min | Max | Cardinality Ratio |\n"
    md += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"

    def sort_key(value):
        try:
            return int(value)
        except Exception:
            return str(value)

    for stats_id in sorted(grouped.keys(), key=sort_key):
        rows = sorted(grouped[stats_id], key=lambda item: (item['column_position'] if item['column_position'] is not None else 0))
        first = rows[0]
        col_names = []
        for item in rows:
            col_names.extend(split_column_names(item['column_name']))
        col_list = format_column_list(col_names)
        last_collected = format_iso_utc(first['last_collect_timestamp'])
        row_count_value = first.get('row_count')
        distinct_value = first.get('unique_value_count')
        nulls_value = first.get('num_of_nulls')
        row_count = format_number(row_count_value)
        distinct_count = format_number(distinct_value)
        nulls_count = format_number(nulls_value)
        min_value = md_cell(first.get('min_value'))
        max_value = md_cell(first.get('max_value'))

        cardinality_ratio = ""
        try:
            row_num = float(row_count_value)
            uniq_num = float(distinct_value)
            if row_num > 0:
                cardinality_ratio = f"{(uniq_num / row_num):.4f}"
        except Exception:
            cardinality_ratio = ""

        md += (
            f"| {col_list} | {statistics_type_display(first['stat_type'])} | "
            f"{last_collected} | {row_count} | {distinct_count} | {nulls_count} | "
            f"{min_value} | {max_value} | {cardinality_ratio} |\n"
        )

    return md


def generate_okf_markdown(info, columns, indices, partitioning, statistics, rel_outbound, rel_inbound, domains, storage, ddl):
    """Constructs the OKF v0.1 compliant Markdown file string."""
    
    db_name, tbl_name = info['db'], info['table']
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    clean_type = info['type'].strip().upper()
    
    # Map Table Kind
    type_map = {'T': 'Standard Table', 'O': 'Queue Table', 'V': 'View'}
    obj_type = type_map.get(clean_type, 'Unknown')

    # Parse Indexes into printable strings
    pk_cols = [item['column_name'] for item in indices if normalize_text(item['index_type']).upper() == 'K' and normalize_text(item['column_name'])]
    pi_cols = [item['column_name'] for item in indices if normalize_text(item['index_type']).upper() in ('P', 'Q') and normalize_text(item['column_name'])]
    is_unique_pi = any(normalize_text(item['unique_flag']).upper() == 'Y' for item in indices if normalize_text(item['index_type']).upper() in ('P', 'Q'))
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

| Column Name | Teradata Type | OKF Type | Nullable | Description | Title | Default | Order |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
"""
    # Build Schema Table
    for col in columns:
        is_null = "True" if normalize_text(col['nullable']).upper() == 'Y' else "False"
        td_type = normalize_text(col['td_type'], 'UNKNOWN')
        okf_type = normalize_text(col['okf_type'], 'any')
        
        desc = md_cell(col['desc'])
        col_title = md_cell(col.get('title'))
        col_default = md_cell(col.get('default'))
        
        md += f"| `{col['name']}` | `{td_type}` | `{okf_type}` | `{is_null}` | {desc} | {col_title} | {col_default} | {col['order']} |\n"

    md += "\n## Indexes\n\n"
    md += render_indexes_section(indices)

    md += "\n## Partitioning\n\n"
    md += render_partitioning_section(partitioning)

    md += "\n## Statistics\n\n"
    md += render_statistics_section(statistics)

    md += "\n## Relationships\n\n"
    md += render_relationships_section(rel_outbound, rel_inbound)

    md += "\n## Column Domains\n\n"
    md += render_domains_section(domains)

    md += "\n## Storage & Usage\n\n"
    md += render_storage_usage_section(storage)

    primary_index_comment = pi_str
    partitioning_comment = normalize_text(partitioning[0]['constraint_text']) if partitioning and normalize_text(partitioning[0].get('constraint_text')) else part_str

    md += "\n## Logical DDL\n\n```sql\n"
    md += build_logical_ddl(ddl, primary_index_comment, partitioning_comment).strip() + "\n```\n"

    if ddl:
        md += "\n## Teradata DDL\n\n"
        md += render_collapsed_ddl(ddl)

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
        partition_rows = fetch_partitioning(cursor)
        stats_rows = fetch_statistics(cursor)
        outbound_rel_rows = fetch_relationships_outbound(cursor)
        inbound_rel_rows = fetch_relationships_inbound(cursor)
        domain_rows = fetch_column_domains(cursor)
        size_rows, timestamp_rows = fetch_storage_usage(cursor)
        
        if not master_rows:
            print("No matching metadata found. Ensure your tracker tables are populated and wildcards match.")
            return
            
        # Group Data
        tables = defaultdict(lambda: {
            'info': {},
            'columns': [],
            'indices': [],
            'partitioning': [],
            'statistics': [],
            'relationships_outbound': [],
            'relationships_inbound': [],
            'domains': [],
            'storage': {},
        })
        
        for r in master_rows:
            db, tbl, col, tkind, tdesc, cdesc, ctitle, cdefault, cnull, size, rows, td_type, okf, is_part, order = r
            key = (db, tbl)
            if not tables[key]['info']:
                tables[key]['info'] = {'db': db, 'table': tbl, 'type': tkind, 'desc': tdesc, 'size': size, 'rows': rows}
            tables[key]['columns'].append({
                'name': col,
                'td_type': td_type,
                'okf_type': okf,
                'nullable': cnull,
                'desc': cdesc,
                'title': ctitle,
                'default': cdefault,
                'is_partition': is_part,
                'order': order,
            })
            
        unmatched_index_tables = set()
        for db, tbl, index_num, index_name, itype, uniq, col_pos, col_name in index_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['indices'].append({
                    'index_number': index_num,
                    'index_name': index_name,
                    'index_type': itype,
                    'unique_flag': uniq,
                    'column_position': col_pos,
                    'column_name': col_name,
                })
            else:
                unmatched_index_tables.add(key)

        for db, tbl, constraint_text, col_part_level, part_levels in partition_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['partitioning'].append({
                    'constraint_text': constraint_text,
                    'column_partitioning_level': col_part_level,
                    'partitioning_levels': part_levels,
                })

        unmatched_stats_tables = set()
        for db, tbl, stats_id, stats_name, stat_type, last_collect_ts, row_count, uniq_count, min_val, max_val, null_count, col_pos, col_name in stats_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['statistics'].append({
                    'stats_id': stats_id,
                    'stats_name': stats_name,
                    'stat_type': stat_type,
                    'last_collect_timestamp': last_collect_ts,
                    'row_count': row_count,
                    'unique_value_count': uniq_count,
                    'min_value': min_val,
                    'max_value': max_val,
                    'num_of_nulls': null_count,
                    'column_position': col_pos,
                    'column_name': col_name,
                })
            else:
                unmatched_stats_tables.add(key)

        for child_db, child_tbl, index_name, child_col, parent_db, parent_tbl, parent_col in outbound_rel_rows:
            key = (child_db, child_tbl)
            if key in tables and tables[key]['info']:
                tables[key]['relationships_outbound'].append({
                    'index_name': index_name,
                    'child_key_column': child_col,
                    'parent_db': parent_db,
                    'parent_table': parent_tbl,
                    'parent_key_column': parent_col,
                })

        for parent_db, parent_tbl, child_db, child_tbl, child_col, parent_col in inbound_rel_rows:
            key = (parent_db, parent_tbl)
            if key in tables and tables[key]['info']:
                tables[key]['relationships_inbound'].append({
                    'child_db': child_db,
                    'child_table': child_tbl,
                    'child_key_column': child_col,
                    'parent_key_column': parent_col,
                })

        for db, tbl, col_name, compress_values in domain_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['domains'].append({
                    'column_name': col_name,
                    'compress_value_list': compress_values,
                })

        for db, tbl, amp_count, total_perm, max_amp, avg_amp in size_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['storage'].update({
                    'amp_count': amp_count,
                    'total_perm_bytes': total_perm,
                    'max_amp_bytes': max_amp,
                    'avg_amp_bytes': avg_amp,
                })

        for db, tbl, create_ts, alter_ts, access_ts in timestamp_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['storage'].update({
                    'create_timestamp': create_ts,
                    'last_alter_timestamp': alter_ts,
                    'last_access_timestamp': access_ts,
                })

        valid_tables = {
            key: data for key, data in tables.items()
            if data.get('info') and data.get('columns')
        }

        if unmatched_index_tables:
            print(
                f"Warning: Skipping index metadata for {len(unmatched_index_tables)} table(s) "
                "that were not present in master metadata."
            )

        if unmatched_stats_tables:
            print(
                f"Warning: Skipping statistics metadata for {len(unmatched_stats_tables)} table(s) "
                "that were not present in master metadata."
            )

        print(f"Generating OKF files for {len(valid_tables)} tables...")
        
        for (db, tbl), data in valid_tables.items():
            ddl_content = fetch_table_ddl(cursor, db, tbl, data['info']['type'])
            md_content = generate_okf_markdown(
                data['info'],
                data['columns'],
                data['indices'],
                data['partitioning'],
                data['statistics'],
                data['relationships_outbound'],
                data['relationships_inbound'],
                data['domains'],
                data['storage'],
                ddl_content,
            )
            
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