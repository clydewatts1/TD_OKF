import os
import sys
import argparse
import logging
from datetime import datetime, timezone
from collections import defaultdict
import re
import teradatasql
from dotenv import load_dotenv

# Basic logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
TABLE_SCD_COUNT = os.getenv("TABLE_SCD_COUNT", "scd_frequency_metrics")
OKF_DIRECTORY = os.getenv("OKF_DIRECTORY", "okf_bundle")

CONFIG_DEFAULTS = {
    "TERADATA_LOGMECH": "TD2",
    "SOURCE_DATABASE_PATTERN": "%",
    "SOURCE_TABLE_PATTERN": "%",
    "DATABASE_METADATA": "DWB02T_SANDBOX",
    "TABLE_ROW_COUNT": "table_size_metrics",
    "TABLE_COLUMN_TYPE": "table_column_types",
    "TABLE_SCD_COUNT": "scd_frequency_metrics",
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
    "TABLE_SCD_COUNT",
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
    global TABLE_SCD_COUNT
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
    TABLE_SCD_COUNT = resolve_setting(cli_args, "TABLE_SCD_COUNT")
    OKF_DIRECTORY = resolve_setting(cli_args, "OKF_DIRECTORY")

    OUTPUT_DIR = OKF_DIRECTORY
    TABLES_DIR = os.path.join(OUTPUT_DIR, "tables")


def print_effective_config():
    logging.info("Using runtime configuration:")
    logging.info(f"- TERADATA_HOST: {normalize_text(TD_HOST, '<not set>')}")
    logging.info(f"- TERADATA_LOGMECH: {normalize_text(TD_LOGMECH, '<not set>')}")
    logging.info(f"- SOURCE_DATABASE_PATTERN: {normalize_text(SOURCE_DB_PATTERN, '<not set>')}")
    logging.info(f"- SOURCE_TABLE_PATTERN: {normalize_text(SOURCE_TABLE_PATTERN, '<not set>')}")
    logging.info(f"- DATABASE_METADATA: {normalize_text(TARGET_DB, '<not set>')}")
    logging.info(f"- TABLE_ROW_COUNT: {normalize_text(TABLE_METRICS, '<not set>')}")
    logging.info(f"- TABLE_COLUMN_TYPE: {normalize_text(COLUMN_METRICS, '<not set>')}")
    logging.info(f"- TABLE_SCD_COUNT: {normalize_text(TABLE_SCD_COUNT, '<not set>')}")
    logging.info(f"- OKF_DIRECTORY: {normalize_text(OUTPUT_DIR, '<not set>')}")

def get_teradata_connection():
    try:
        logging.info(f"Connecting to Teradata host '{TD_HOST}'...")
        is_browser_auth = normalize_text(TD_LOGMECH, "").upper() in ["BROWSER"]
        if is_browser_auth:
            return teradatasql.connect(host=TD_HOST, logmech=TD_LOGMECH)        
        else:
            return teradatasql.connect(host=TD_HOST, user=TD_USER, password=TD_PASSWORD, logmech=TD_LOGMECH)
    except Exception as e:
        logging.error(f"Connection failed: {e}")
        sys.exit(1)

def fetch_master_metadata(cursor):
    """
    Executes the mega-join query to pull all table and column metadata, 
    including metrics and OKF data types from our sandbox tables.
    """
    logging.info("Extracting master schema, descriptions, and metrics...")
    db_filter, db_params = build_like_filter("T.DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("T.TableName", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT 
        C.DatabaseName, C.TableName, C.ColumnName, T.TableKind,
        T.CommentString AS TableDescription,
        C.CommentString AS ColumnDescription,
        C.ColumnTitle,
        C.DefaultValue,
        SZ.ExtractionTimestamp,
        C.Nullable, SZ.TableSizeBytes AS MetricsTableSizeBytes,
        SZ.RowCount AS MetricsRowCount, TP.TeradataDataType,
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
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} master metadata rows.")
        return rows
    except Exception as e:
        logging.error(f"Error reading master metadata: {e}")
        return []

def fetch_indices(cursor):
    """Fetches index metadata used for table-level index rendering."""
    logging.info("Extracting index definitions...")
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
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} index rows.")
        return rows
    except Exception as e:
        logging.error(f"Error reading indices: {e}")
        return []


def fetch_statistics(cursor):
    """Fetches table statistics metadata used for stats section rendering."""
    logging.info("Extracting statistics definitions...")
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
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} statistics rows.")
        return rows
    except Exception as e:
        err_text = str(e)
        if "Column" in err_text and "not found" in err_text:
            try:
                logging.warning("StatsV detail columns unavailable. Falling back to reduced statistics projection.")
                cursor.execute(reduced_query, db_params + table_params)
                rows = cursor.fetchall()
                logging.info(f"Fetched {len(rows)} reduced statistics rows.")
                return rows
            except Exception as fallback_err:
                first_line = str(fallback_err).splitlines()[0] if str(fallback_err) else "unknown error"
                logging.warning(f"Could not read reduced statistics metadata: {first_line}")
                return []
        first_line = err_text.splitlines()[0] if err_text else "unknown error"
        logging.warning(f"Could not read statistics metadata: {first_line}")
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
            logging.warning(f"Could not fetch DDL for {db_name}.{tbl_name}: object does not exist (3807).")
        else:
            first_line = err_text.splitlines()[0] if err_text else "unknown error"
            logging.warning(f"Could not fetch DDL for {db_name}.{tbl_name}: {first_line}")
        return ""


def fetch_scd_metadata(cursor):
    """Fetches SCD frequency metadata for all tables with tracked SCD columns."""
    logging.info("Extracting SCD frequency metadata...")
    db_filter, db_params = build_like_filter("DatabaseName", SOURCE_DB_PATTERN)
    table_filter, table_params = build_like_filter("TableName", SOURCE_TABLE_PATTERN)

    query = f"""
    SELECT
        DatabaseName,
        TableName,
        ColumnName,
        SCD_ColumnValue,
        RowCount,
        ExtractionTimestamp
    FROM "{TARGET_DB}"."{TABLE_SCD_COUNT}"
    WHERE {db_filter} AND {table_filter}
    ORDER BY DatabaseName, TableName, ColumnName, SCD_ColumnValue DESC
    """
    try:
        cursor.execute(query, db_params + table_params)
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} SCD metadata rows.")
        return rows
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read SCD metadata: {first_line}")
        return []

def fetch_partitioning(cursor):
    logging.info("Extracting partitioning metadata...")
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
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} partitioning rows.")
        return rows
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read partitioning metadata: {first_line}")
        return []


def fetch_relationships_outbound(cursor):
    logging.info("Extracting outbound relationship metadata...")
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
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} outbound relationship rows.")
        return rows
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read outbound relationships: {first_line}")
        return []


def fetch_relationships_inbound(cursor):
    logging.info("Extracting inbound relationship metadata...")
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
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} inbound relationship rows.")
        return rows
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read inbound relationships: {first_line}")
        return []


def fetch_column_domains(cursor):
    logging.info("Extracting column domain metadata...")
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
        rows = cursor.fetchall()
        logging.info(f"Fetched {len(rows)} column domain rows.")
        return rows
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read column domains: {first_line}")
        return []


def fetch_storage_usage(cursor):
    logging.info("Extracting storage and usage metadata...")
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
        logging.info(f"Fetched {len(size_rows)} table size rows.")
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read table size metrics: {first_line}")

    try:
        cursor.execute(timestamp_query, db_params + table_params)
        ts_rows = cursor.fetchall()
        logging.info(f"Fetched {len(ts_rows)} timestamp rows.")
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read table timestamp metrics: {first_line}")

    return size_rows, ts_rows


def fetch_historical_growth(cursor, db_name, tbl_name):
    """
    Fetches 90-day historical table size from PDCR, pivoted by week.
    Returns the raw query and the fetched data row.
    """
    query = """WITH WeeklySpace AS (
/*
   Extract Weekly Peak Table Size (in KB) over 90 Days
   Purpose: Provides a time-series view of table growth, bucketed by the Week-Ending Saturday date.
*/
SELECT
    DatabaseName,
    Tablename,
    LogDate AS Week_Ending_Saturday,
    (TD_SATURDAY(CURRENT_DATE)-LogDate)/7 AS WeeksAgo,
    CAST(MAX(CURRENTPERM) / 1024.0 AS DECIMAL(18,2)) AS Week_Size_KB
FROM pdcrinfo.TableSpace_Hst
WHERE DatabaseName = ? AND Tablename = ?
  AND LogDate >= TD_SATURDAY(CURRENT_DATE - 90) AND LogDate = TD_SATURDAY(Logdate) AND LOGDATE <= TD_SATURDAY(CURRENT_DATE)
GROUP BY 1, 2, 3, 4
),
WindowStats AS (
SELECT
    DatabaseName,
    Tablename,
    WeeksAgo,
    Week_Size_KB,
    AVG(Week_Size_KB) OVER (PARTITION BY DatabaseName, Tablename) AS Average_Size_KB,
    FIRST_VALUE(Week_Size_KB) OVER (PARTITION BY DatabaseName, Tablename ORDER BY WeeksAgo) AS First_Size_KB,
    FIRST_VALUE(Week_Size_KB) OVER (PARTITION BY DatabaseName, Tablename ORDER BY WeeksAgo DESC) AS Last_Size_KB
FROM WeeklySpace
)
SELECT
    CAST(MAX(CASE WHEN WeeksAgo = 0 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_0_Current_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 1 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_1_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 2 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_2_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 3 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_3_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 4 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_4_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 5 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_5_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 6 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_6_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 7 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_7_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 8 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_8_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 9 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_9_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 10 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_10_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 11 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_11_Ago_KB,
    CAST(MAX(CASE WHEN WeeksAgo = 12 THEN Week_Size_KB END) AS DECIMAL(18,2)) AS Wk_12_Ago_KB,
    MAX(Average_Size_KB) AS Average_Size_KB,
    MAX(First_Size_KB) AS First_Size_KB,
    MAX(Last_Size_KB) AS Last_Size_KB,
    CAST(MAX(First_Size_KB) - MAX(Last_Size_KB) AS DECIMAL(18,2)) AS Size_Diff_KB
FROM WindowStats;"""
    try:
        cursor.execute(query, [db_name, tbl_name])
        # Fetch column names from cursor description
        columns = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        if row:
            return query, dict(zip(columns, row))
        return query, None
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else "unknown error"
        logging.warning(f"Could not read historical growth for {db_name}.{tbl_name}: {first_line}")
        return query, None

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
    col_partitioned = "No"
    if col_level not in [None, ""]:
        try:
            col_partitioned = "Yes" if float(col_level) > 0 else "No"
        except (ValueError, TypeError):
            col_partitioned = "No"
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
    metrics_extracted = format_iso_utc(storage.get('metrics_extraction_timestamp'))

    md = "| Metric | Value |\n"
    md += "| :--- | :--- |\n"
    md += f"| Total Perm Space | {total_perm} |\n"
    md += f"| AMP Count | `{format_number(amp_count)}` |\n"
    md += f"| Skew Factor | `{skew_factor}` |\n"
    md += f"| Created | `{created}` |\n"
    md += f"| Last Altered | `{altered}` |\n"
    md += f"| Last Accessed | `{accessed}` |\n"
    md += f"| Metrics Extracted | `{metrics_extracted}` |\n"

    if skew_num is not None and skew_num > 0.20:
        md += "\nSkew interpretation: notable skew (> 0.20). Avoid reusing PI columns as Databricks distribution keys.\n"
    return md


def render_historical_growth_section(growth_data, source_query, db_name, tbl_name):
    """Renders the historical growth trend table and the source query."""
    if not growth_data:
        md = "No historical size data available in `pdcrinfo.TableSpace_Hst` for this object.\n"
    else:
        md = "Weekly peak table size (KB) over the last 90 days. Week 0 is the most recent week.\n\n"
        md += "| Metric | Value |\n"
        md += "| :--- | :--- |\n"
        md += f"| 90-Day Avg Size (KB) | `{format_number(growth_data.get('Average_Size_KB'))}` |\n"
        md += f"| 90-Day Growth (KB) | `{format_number(growth_data.get('Size_Diff_KB'))}` |\n"
        md += "\n"
        md += "| Wk 0 | Wk 1 | Wk 2 | Wk 3 | Wk 4 | Wk 5 | Wk 6 |\n"
        md += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        md += (
            f"| {format_number(growth_data.get('Wk_0_Current_KB'))} | {format_number(growth_data.get('Wk_1_Ago_KB'))} | "
            f"{format_number(growth_data.get('Wk_2_Ago_KB'))} | {format_number(growth_data.get('Wk_3_Ago_KB'))} | "
            f"{format_number(growth_data.get('Wk_4_Ago_KB'))} | {format_number(growth_data.get('Wk_5_Ago_KB'))} | "
            f"{format_number(growth_data.get('Wk_6_Ago_KB'))} |\n"
        )
        md += "\n"
        md += "| Wk 7 | Wk 8 | Wk 9 | Wk 10 | Wk 11 | Wk 12 |\n"
        md += "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        md += (
            f"| {format_number(growth_data.get('Wk_7_Ago_KB'))} | {format_number(growth_data.get('Wk_8_Ago_KB'))} | "
            f"{format_number(growth_data.get('Wk_9_Ago_KB'))} | {format_number(growth_data.get('Wk_10_Ago_KB'))} | "
            f"{format_number(growth_data.get('Wk_11_Ago_KB'))} | {format_number(growth_data.get('Wk_12_Ago_KB'))} |\n"
        )

    md += "\n<details><summary>Source Query</summary>\n\n```sql\n"
    safe_query = source_query.replace("= ?", f"= '{db_name}'", 1).replace("= ?", f"= '{tbl_name}'", 1)
    md += safe_query + "\n```\n</details>\n"
    return md


def render_scd_metadata_section(scd_rows):
    """Renders SCD frequency metadata for migration analysis."""
    if not scd_rows:
        return "No SCD metadata available for this table.\n"

    md = "SCD columns track slowly changing dimension changes. These timestamps and counts help assess change frequency for migration planning.\n\n"
    md += "| Column Name | Effective Date | Row Count | % of Total |\n"
    md += "| :--- | :--- | ---: | ---: |\n"

    total_rows = sum(row['row_count'] if row['row_count'] else 0 for row in scd_rows)

    for row in scd_rows:
        col_name = normalize_text(row['column_name'])
        col_value = format_iso_utc(row['scd_column_value']) if row['scd_column_value'] else "Unknown"
        row_count = format_number(row['row_count']) if row['row_count'] else "0"
        pct = ""
        if total_rows and row['row_count']:
            try:
                pct = f"{(float(row['row_count']) / float(total_rows) * 100):.2f}%"
            except (ValueError, TypeError):
                pct = "N/A"

        md += f"| `{col_name}` | `{col_value}` | {row_count} | {pct} |\n"

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


def build_logical_ddl_from_columns(db_name, tbl_name, columns, primary_index_comment, partitioning_comment):
    lines = [f"CREATE TABLE {db_name}.{tbl_name} ("]
    for idx, col in enumerate(columns):
        col_name = col['name']
        td_type = normalize_text(col['td_type'], 'UNKNOWN')
        nullable = normalize_text(col['nullable']).upper() == 'Y'

        clean_type = re.sub(r"\s+CHARACTER\s+SET\s+(?:LATIN|UNICODE)", "", td_type, flags=re.IGNORECASE)
        clean_type = re.sub(r"\s+(?:NOT\s+)?CASESPECIFIC\b", "", clean_type, flags=re.IGNORECASE)
        clean_type = re.sub(r"\s+", " ", clean_type).strip()

        nullability = "" if nullable else " NOT NULL"
        comma = "," if idx < len(columns) - 1 else ""
        lines.append(f" {col_name} {clean_type}{nullability}{comma}")

    lines.append(");")
    if normalize_text(primary_index_comment):
        lines.append(f"-- Original PI: {primary_index_comment}")
    if normalize_text(partitioning_comment):
        lines.append(f"-- Original partitioning: {partitioning_comment}")
    return "\n".join(lines)


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


def generate_okf_markdown(info, columns, indices, partitioning, statistics, rel_outbound, rel_inbound, domains, storage, growth, scd_metadata, ddl):
    """Constructs the OKF v0.2 compliant Markdown file string."""
    
    db_name, tbl_name = info['db'], info['table']
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    clean_type = info['type'].strip().upper()
    
    # Map Table Kind
    type_map = {'T': 'table', 'O': 'queue_table', 'V': 'view'}
    obj_type = type_map.get(clean_type, 'unknown')

    # Parse Indexes into printable strings
    pk_cols = [item['column_name'] for item in indices if normalize_text(item['index_type']).upper() == 'K' and normalize_text(item['column_name'])]
    pi_cols = [item['column_name'] for item in indices if normalize_text(item['index_type']).upper() in ('P', 'Q') and normalize_text(item['column_name'])]
    is_unique_pi = any(normalize_text(item['unique_flag']).upper() == 'Y' for item in indices if normalize_text(item['index_type']).upper() in ('P', 'Q'))
    part_cols = [col['name'] for col in columns if col['is_partition'] == 'Y']
    
    pk_str = f"`{', '.join(pk_cols)}`" if pk_cols else "None"
    pi_type = "Unique Primary Index" if is_unique_pi else "Non-Unique Primary Index" if pi_cols else "No Primary Index"
    pi_str = f"{pi_type} on `{', '.join(pi_cols)}`" if pi_cols else pi_type
    part_str = f"`{', '.join(part_cols)}`" if part_cols else "None"

    rows_str = "N/A" if clean_type == 'V' else (f"{info['rows']:,}" if info.get('rows') is not None else "Unknown")
    size_str = "N/A" if clean_type == 'V' else (f"{info['size']:,} bytes" if info.get('size') is not None else "Unknown")

    # Handle YAML safe description for frontmatter (omit entirely if empty)
    yaml_desc_line = ""
    if info['desc']:
        safe_desc = info['desc'].replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
        yaml_desc_line = f'description: "{safe_desc}"\n'

    # Handle Human Readable description for markdown body
    tbl_desc = normalize_text(info['desc'], "No description provided.")

    # Build Frontmatter
    md = f"""---
type: teradata {obj_type}
title: "{db_name}.{tbl_name}"
{yaml_desc_line}tags:
  - {db_name.lower()}
  - teradata
  - {obj_type.lower().replace(' ', '_')}
timestamp: {current_time}
---

# {db_name}.{tbl_name}

**Database:** `{db_name}`  
**Object Type:** `{obj_type} ({clean_type})`  
**Description:** {tbl_desc}  
**Rows:** `{rows_str}`  
**Size:** `{size_str}`  

**Primary Key:** {pk_str}  
**Primary Index:** {pi_str}  
**Partition Columns:** {part_str}

## Columns

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

    md += "\n## Historical Growth\n\n"
    md += render_historical_growth_section(growth['data'], growth['query'], db_name, tbl_name)

    md += "\n## SCD Frequency (Change Tracking)\n\n"
    md += render_scd_metadata_section(scd_metadata)

    md += "\n## Ownership\n\n"
    md += "Not yet defined.\n"

    md += "\n## Lineage\n\n"
    md += "Not yet defined.\n"

    primary_index_comment = pi_str
    partitioning_comment = normalize_text(partitioning[0]['constraint_text']) if partitioning and normalize_text(partitioning[0].get('constraint_text')) else part_str

    md += "\n## DDL\n\n"
    md += "### Logical DDL\n\n"
    md += "```sql\n"
    md += build_logical_ddl_from_columns(db_name, tbl_name, columns, primary_index_comment, partitioning_comment).strip() + "\n```\n"

    if ddl:
        md += "\n### Raw DDL\n\n"
        md += render_collapsed_ddl(ddl)

    return md

def generate_bundle_indices(tables, output_dir, tables_dir):
    """Generates the root index.md and individual database index.md files."""
    logging.info("Generating OKF index files...")
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
        scd_rows = fetch_scd_metadata(cursor)
        
        if not master_rows:
            logging.warning("No matching metadata found. Ensure your tracker tables are populated and wildcards match.")
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
            'growth': {},
            'scd_metadata': [],
        })
        
        for r in master_rows:
            db, tbl, col, tkind, tdesc, cdesc, ctitle, cdefault, metrics_ts, cnull, metrics_size, metrics_rows, td_type, okf, is_part, order = r
            key = (db, tbl)
            if not tables[key]['info']:
                tables[key]['info'] = {
                    'db': db, 'table': tbl, 'type': tkind, 'desc': tdesc, 
                    'size': metrics_size, 
                    'rows': metrics_rows, 
                    'metrics_ts': metrics_ts
                }

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

        for db, tbl, col_name, col_value, row_count, extract_ts in scd_rows:
            key = (db, tbl)
            if key in tables and tables[key]['info']:
                tables[key]['scd_metadata'].append({
                    'column_name': col_name,
                    'scd_column_value': col_value,
                    'row_count': row_count,
                    'extraction_timestamp': extract_ts,
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
                    'metrics_extraction_timestamp': tables[key]['info'].get('metrics_ts'),
                    'last_access_timestamp': access_ts,
                })

        valid_tables = {
            key: data for key, data in tables.items()
            if data.get('info') and data.get('columns')
        }

        if unmatched_index_tables:
            logging.warning(
                f"Skipping index metadata for {len(unmatched_index_tables)} table(s) "
                "that were not present in master metadata."
            )

        if unmatched_stats_tables:
            logging.warning(
                f"Skipping statistics metadata for {len(unmatched_stats_tables)} table(s) "
                "that were not present in master metadata."
            )

        logging.info(f"Generating OKF files for {len(valid_tables)} tables...")
        
        for (db, tbl), data in valid_tables.items():
            ddl_content = fetch_table_ddl(cursor, db, tbl, data['info']['type'])
            growth_query, growth_data = fetch_historical_growth(cursor, db, tbl)
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
                {'query': growth_query, 'data': growth_data},
                data['scd_metadata'],
                ddl_content,
            )
            
            db_dir = os.path.join(TABLES_DIR, db.replace(" ", "_").replace('"', ''))
            os.makedirs(db_dir, exist_ok=True)
            
            safe_filename = f"{tbl}".replace(" ", "_").replace('"', '') + ".md"
            filepath = os.path.join(db_dir, safe_filename)
            
            logging.info(f"Writing OKF file to {filepath}")
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(md_content)
                
        # Call the new indices generator
        generate_bundle_indices(valid_tables, OUTPUT_DIR, TABLES_DIR)
                
        logging.info(f"Success! OKF bundle created in '{OUTPUT_DIR}'.")

    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    main()
