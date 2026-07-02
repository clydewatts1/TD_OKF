---
type: Teradata Table
title: "DWP01A_ACC_ORR.BACK_FEED_CNTL"
tags:
  - DWP01A_ACC_ORR
  - teradata
  - view
timestamp: 2026-07-02T06:36:17Z
---

# DWP01A_ACC_ORR.BACK_FEED_CNTL

**Database:** `DWP01A_ACC_ORR`  
**Object Type:** `View (V)`  
**Description:** No description provided.  
**Rows:** `Unknown`  
**Size (Bytes):** `Unknown`  

**Primary Key:** None  
**Primary Index:** No Primary Index  
**Partition Columns:** None

## Schema

| Column Name | Teradata Type | OKF Type | Nullable | Description | Order |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `Location_Id` | `VARCHAR(30)` | `string` | `False` | Id no for a specific Location e.g. Store or Depot | 1 |
| `Location_Name` | `VARCHAR(255)` | `string` | `False` | Name of the STORE. Its the lowest level of the Organisational Hierarchy. Its the location where customers can view and purchase goods and services. | 2 |
| `Location_Type` | `VARCHAR(30)` | `string` | `False` | Stores location traits - some common factors that group locations together - which could have an influence on trade | 3 |
| `BACK_FEED_LOC_FLG` | `VARCHAR(1) CHARACTER SET UNICODE` | `string` | `False` | This Column is used identify the Flag to identify the location for Backfeed sales ( TO ER system ) | 4 |

## Teradata DDL

```sql
REPLACE VIEW DWP01A_ACC_ORR.BACK_FEED_CNTL AS
LOCKING ROW FOR ACCESS
SELECT
 Location_Id
,Location_Name
,Location_Type
,BACK_FEED_LOC_FLG
FROM DWP01T_ACC_ORR.BACK_FEED_CNTL;
```
