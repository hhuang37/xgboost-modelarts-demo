# hive_export — Reading training data from Huawei MRS Hive

[![简体中文](https://img.shields.io/badge/README-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-blue)](README.md)

This directory is a **data-source extension** for the xgboost demo: the demo
originally loads the breast_cancer dataset straight from `sklearn.datasets`;
here we add a second path — put the same table into Hive on Huawei Cloud MRS,
then read it back from remote Python. Three parts:

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

> ⚠️ Seeing `sasl_decode ... Unable to find a callback: 32775` when querying a
> large result set? That is the libsasl2 large-frame decode bug — fixes in
> `MRS_RUN.md` §5 (small-batch fetch / downgrade to 2.1.27).

The single source of truth for cluster facts (IPs/SPN/ports) and the
troubleshooting quick reference: `MRS_RUN.md` §0 and §5 (Chinese); the notebook
also carries a condensed troubleshooting table (§8).

## C. Train straight from Hive (the train_upload_hive series)

The Hive edition of `train_upload.ipynb`: the dataset no longer comes from
`sklearn.datasets` but from the `breast_cancer` table built in section A
(Hive's `mean_radius` columns are restored to sklearn's `mean radius` so
feature names stay consistent with `sample_request.json` / `app.py`);
everything else (OLD/NEW model training, OBS upload, hot-swap verification)
is unchanged.

| File | Purpose | Status |
|---|---|---|
| `train_upload_hive.ipynb` (Chinese original) / `train_upload_hive_EN.ipynb` | Full edition: the 6 connection cells from section B + fetch/validation/training/upload, step-by-step troubleshootable | ✅ verified 2026-08-20 |
| `train_upload_hive_simple.ipynb` / `train_upload_hive_simple_EN.ipynb` + `train_upload_hive_lib.py` (Chinese log messages) / `train_upload_hive_lib_EN.py` (English log messages, used by the EN notebook) | Slim edition: connection/fetch/dependency install (with heartbeat) moved into the lib; the notebook keeps only config and the training flow; upload the lib next to the notebook — the two libs share identical logic, only the log language differs | ⏳ not yet verified |

Choosing: full edition for troubleshooting or to see every step; slim edition
for daily use. Both share the same connection recipe (ADR-0002).

## Relation to the xgboost demo

The original training (`train_upload.ipynb`) and deployment verification
(`verify_hotswap.ipynb`) are unaffected; the Hive edition in section C is a
drop-in alternative training entry — it produces the same model file and
uploads to the same OBS path as the original, so the downstream hot-swap
verification flow stays as is.
