# TD_OKF

Teradata Open Knowledge Format (OKF) Extractor.

This project runs a small pipeline of Python scripts that:

1. Collect table row counts and size metrics from Teradata.
2. Collect column type metadata.
3. Generate an OKF Markdown bundle in the `okf_bundle` folder.

## Project Structure

- `sandbox/row_count.py`: Builds or refreshes table-level metrics in Teradata.
- `sandbox/data_type.py`: Builds or refreshes column type metadata.
- `sandbox/otk_generator.py`: Generates Markdown output in `okf_bundle/`.
- `env_sample`: Template for required environment variables.

## Prerequisites

- Python 3.10+ recommended.
- Network access to your Teradata environment.
- Teradata permissions to read metadata views and write to the configured sandbox database.

## Setup

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows Git Bash:

```bash
python -m venv .venv
source .venv/Scripts/activate
```

### 2. Install dependencies

```bash
pip install teradatasql python-dotenv
```

## Configure .env

### 1. Create a local .env file from the template

```bash
cp env_sample .env
```

If `cp` is not available, copy `env_sample` manually and name it `.env`.

### 2. Set the values in .env

Required keys:

- `TERADATA_HOST`: Teradata host name.
- `TERADATA_LOGMECH`: Login mechanism, for example `TD2` or `BROWSER`.
- `TERADATA_USER`: Username (not required for `BROWSER` SSO).
- `TERADATA_PASSWORD`: Password (not required for `BROWSER` SSO).
- `SOURCE_DATABASE_PATTERN`: Source database filter. Supports single or comma-delimited patterns, for example `DWP01%_ACC_ORR%` or `DWP01%ACC_ORR,DWP01%IDW`.
- `SOURCE_TABLE_PATTERN`: Source table filter. Supports single or comma-delimited patterns, for example `%` or `DW_%,BM_%`.
- `DATABASE_METADATA`: Target metadata database where helper tables are stored.
- `TABLE_ROW_COUNT`: Target table name for row-count and size metrics.
- `TABLE_COLUMN_TYPE`: Target table name for column type metadata.
- `OKF_DIRECTORY`: Output root directory for generated OKF files.

## Run Order

Run scripts in this order from the repository root:

### 1. Row counts and table sizes

```bash
python sandbox/row_count.py
```

What it does:

- Connects to Teradata.
- Ensures the row-count metrics table exists in `DATABASE_METADATA`.
- Populates or refreshes metrics in `TABLE_ROW_COUNT`.

### 2. Column types metadata

```bash
python sandbox/data_type.py
```

What it does:

- Populates or refreshes column metadata used by the final OKF generation step.
- Writes data to the table configured in `TABLE_COLUMN_TYPE`.

### 3. Generate OKF bundle

```bash
python sandbox/otk_generator.py
```

You can override any supported setting at runtime using uppercase CLI flags.
Flag names match `.env` keys.

Examples:

```bash
python sandbox/otk_generator.py --OKF_DIRECTORY okf_primark
```

```bash
python sandbox/otk_generator.py --SOURCE_DATABASE_PATTERN DWP01A_ACC_ORR% --SOURCE_TABLE_PATTERN BACK_FEED% --OKF_DIRECTORY okf_primark
```

Configuration precedence is:

1. CLI flag value
2. `.env` value
3. Script default

Supported CLI override flags:

- `--TERADATA_HOST`
- `--TERADATA_LOGMECH`
- `--TERADATA_USER`
- `--TERADATA_PASSWORD`
- `--SOURCE_DATABASE_PATTERN`
- `--SOURCE_TABLE_PATTERN`
- `--DATABASE_METADATA`
- `--TABLE_ROW_COUNT`
- `--TABLE_COLUMN_TYPE`
- `--OKF_DIRECTORY`

Pattern matching notes:

- Comma-delimited values are supported for both `SOURCE_DATABASE_PATTERN` and `SOURCE_TABLE_PATTERN`.
- Multi-pattern filters are translated to SQL `LIKE ANY (...)` conditions.
- Spaces around commas are ignored.

Example `.env` values:

```dotenv
SOURCE_DATABASE_PATTERN=DWP01%ACC_ORR,DWP01%IDW
SOURCE_TABLE_PATTERN=DW_%,BM_%
```

What it does:

- Reads source metadata plus the two helper tables.
- Produces Markdown files in `OKF_DIRECTORY` (default `okf_bundle/`).
- Generates index files including `OKF_DIRECTORY/index.md`.
- Builds a master index with quick database links and per-database summary counts.
- Adds `Indexes` and `Statistics` sections to each table file between `Schema` and `Teradata DDL`.

Indexes and Statistics section sources:

- `Indexes` is sourced from `DBC.IndicesV`, grouped by `IndexNumber` with ordered `ColumnPosition`.
- `Statistics` is sourced from `DBC.StatsTbl`, grouped by `StatsId` with ordered `ColumnPosition`.
- If no rows are returned for a section, the generator writes an empty-state message (`No indexes defined.` or `No statistics collected.`).

## Output

Expected generated folder:

- `okf_bundle/index.md`
- `okf_bundle/tables/<database>/index.md`
- `okf_bundle/tables/<database>/<table>.md`

## General Usage Notes

- Use narrow filters in `SOURCE_DATABASE_PATTERN` and `SOURCE_TABLE_PATTERN` first, then widen scope.
- For browser SSO, set `TERADATA_LOGMECH=BROWSER`.
- Keep `.env` out of version control.
- Re-run the scripts whenever metadata needs refreshing.

## Troubleshooting

- Connection failed:
	- Check `TERADATA_HOST`, `TERADATA_LOGMECH`, credentials, VPN, and firewall access.
- Missing table or permission errors:
	- Verify write access to `DATABASE_METADATA` and read access to Teradata system views.
- Empty output bundle:
	- Confirm patterns in `SOURCE_DATABASE_PATTERN` and `SOURCE_TABLE_PATTERN` match real objects.
- Python package import errors:
	- Activate `.venv` and reinstall dependencies.
