# hive_export — Reading training data from Huawei MRS Hive

[![简体中文](https://img.shields.io/badge/README-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-blue)](README.md)

This directory is a **data-source extension** for the xgboost demo: the demo
originally loads the breast_cancer dataset straight from `sklearn.datasets`;
here we add a second path — put the same table into Hive on Huawei Cloud MRS,
then read it back from remote Python. Two pieces of work:

## A. Prepare the Hive table (one-time)

| File | Purpose |
|---|---|
| `export_breast_cancer.py` | Exports the sklearn breast-cancer dataset to `breast_cancer.csv` (569×31) and generates the DDL |
| `breast_cancer.csv` | Exported artifact (header row, comma-separated, `\n` line endings) |
| `breast_cancer_hive.sql` | `CREATE EXTERNAL TABLE ... LOCATION '/test/breast_cancer'`; column names map spaces to underscores (`mean radius` → `mean_radius`) |
| `test_breast_cancer_hive_sql.py` | Structural tests for the DDL (EXTERNAL / no LOAD DATA / 31 aligned columns) |

Steps: run `export_breast_cancer.py` once (idempotent) → upload the CSV to HDFS
(`hdfs dfs -put breast_cancer.csv /test/breast_cancer/`) → execute the generated
SQL in Hive. EXTERNAL + LOCATION reads the HDFS file in place, so DROP TABLE
never deletes the source data.

## B. Connect to the Kerberos-secured cluster (three run environments)

The cluster runs in Kerberos secure mode (fixed SPN, KDC on 21732,
`qop=auth-conf`). The connection recipe and its decision record live in
`../docs/adr/0002-mrs-hive-connection-recipe.md`.

| Environment | Entry point | Status |
|---|---|---|
| **ModelArts Notebook** (recommended) | `modelarts_hive_conn.ipynb` (Chinese original) / `modelarts_hive_conn_EN.ipynb` | ✅ verified 2026-08-19 |
| MRS cluster node | `test_hive_conn_mrs.py` + run book `MRS_RUN.md` | ✅ verified 2026-08-19 |
| Windows workstation | `test_hive_conn.ipynb` + `krb5.ini` | early version, reference only |

**ModelArts quick start**: the notebook instance must be in the same VPC as
MRS, have public internet (pip installs dependencies), and the security group
must allow `21066` (HiveServer2) and `21732` (KDC, TCP+UDP). Open
`modelarts_hive_conn_EN.ipynb` and run the cells in order — cell 3 auto-adapts
to root / passwordless-sudo / no-root environments, and cell 5 prompts for the
MRS business-user password.

The single source of truth for cluster facts (IPs/SPN/ports) and the
troubleshooting quick reference: `MRS_RUN.md` §0 and §5 (Chinese); the notebook
also carries a condensed troubleshooting table (§8).

## Relation to the xgboost demo

An independent, optional feature: it does not affect the original training
(`train_upload.ipynb`) or deployment verification (`verify_hotswap.ipynb`)
flows. To feed training from Hive, grab a DataFrame via the notebook's §8
`run_query()` and hand it to the existing training code.
