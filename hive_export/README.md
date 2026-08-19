# hive_export — 从华为 MRS Hive 读取训练数据

[![English](https://img.shields.io/badge/README-English-blue)](README_EN.md)

本目录是 xgboost demo 的**数据入口扩展**：原 demo 的 breast_cancer 数据直接来自
`sklearn.datasets`，这里新增一条数据通路 —— 把同一张表放进华为云 MRS 的 Hive，
再从远端 Python 读回来。两部分工作：

## A. 准备 Hive 表（一次性）

| 文件 | 作用 |
|---|---|
| `export_breast_cancer.py` | 导出 sklearn 乳腺癌数据集为 `breast_cancer.csv`（569×31），并生成建表 SQL |
| `breast_cancer.csv` | 导出产物（带表头，逗号分隔，`\n` 行尾） |
| `breast_cancer_hive.sql` | `CREATE EXTERNAL TABLE ... LOCATION '/test/breast_cancer'`，列名 = 特征名空格换下划线（`mean radius` → `mean_radius`） |
| `test_breast_cancer_hive_sql.py` | 建表 SQL 的结构测试（EXTERNAL / 无 LOAD DATA / 31 列对齐） |

步骤：跑一次 `export_breast_cancer.py`（幂等）→ 把 CSV 上传 HDFS
（`hdfs dfs -put breast_cancer.csv /test/breast_cancer/`）→ 在 Hive 执行生成的 SQL。
EXTERNAL + LOCATION 直读 HDFS 原文件，DROP TABLE 不会删源数据。

## B. 连接 Kerberos 安全集群读表（三种运行环境）

集群是 Kerberos 安全模式（固定 SPN、KDC 21732、`qop=auth-conf`），连接配方与
决策记录见 `../docs/adr/0002-mrs-hive-connection-recipe.md`。

| 环境 | 入口 | 状态 |
|---|---|---|
| **ModelArts Notebook**（推荐） | `modelarts_hive_conn.ipynb`（英文版 `_EN.ipynb`） | ✅ 2026-08-19 实测通过 |
| MRS 集群节点 | `test_hive_conn_mrs.py` + 手册 `MRS_RUN.md` | ✅ 2026-08-19 实测通过 |
| Windows 本机 | `test_hive_conn.ipynb` + `krb5.ini` | 早期版本，参考用 |

**ModelArts 快速开始**：notebook 实例需与 MRS 同 VPC、可出公网（pip 装依赖）、
安全组放行 `21066`（HiveServer2）与 `21732`（KDC，TCP+UDP）。打开
`modelarts_hive_conn.ipynb` 按格顺序跑即可 —— 第 3 格自动适配
root / 免密 sudo / 无 root 三种环境，第 5 格交互输 MRS 业务用户密码。

集群事实表（IP/SPN/端口）与排障速查的唯一权威来源：`MRS_RUN.md` §0 与 §5；
notebook 内也有精简版排障表（§8）。

## 与 xgboost demo 的关系

独立可选功能：不影响原训练（`train_upload.ipynb`）与部署验证
（`verify_hotswap.ipynb`）流程。要让训练改从 Hive 取数，用 notebook §8 的
`run_query()` 拿到 DataFrame 后接原训练代码即可。
