# hive_export — 从华为 MRS Hive 读取训练数据

[![English](https://img.shields.io/badge/README-English-blue)](README_EN.md)

本目录是 xgboost demo 的**数据入口扩展**：原 demo 的 breast_cancer 数据直接来自
`sklearn.datasets`，这里新增一条数据通路 —— 把同一张表放进华为云 MRS 的 Hive，
再从远端 Python 读回来。三部分内容：

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

> ⚠️ 查询大结果集时报 `sasl_decode ... Unable to find a callback: 32775`？
> 是 libsasl2 的大帧解密 bug，对策见 `MRS_RUN.md` §5（小块取 / 降级 2.1.27）。

集群事实表（IP/SPN/端口）与排障速查的唯一权威来源：`MRS_RUN.md` §0 与 §5；
notebook 内也有精简版排障表（§8）。

## C. 训练直接从 Hive 取数（train_upload_hive 系列）

`train_upload.ipynb` 的 Hive 版：数据不再来自 `sklearn.datasets`，而是读 A 节建好的
`breast_cancer` 表（列名 `mean_radius` 自动还原成 sklearn 的 `mean radius`，保证与
`sample_request.json` / `app.py` 的特征名一致），其余逻辑（训练 OLD/NEW 模型、上传
OBS、热切换验证）不变。

| 文件 | 作用 | 状态 |
|---|---|---|
| `train_upload_hive.ipynb`（英文版 `_EN.ipynb`） | 完整版：B 节连接 6 格 + 取数/校验/训练/上传，逐步可排障 | ✅ 2026-08-20 实测通过 |
| `train_upload_hive_simple.ipynb`（英文版 `_EN.ipynb`）+ `train_upload_hive_lib.py`（中文日志）/ `train_upload_hive_lib_EN.py`（英文日志，配 EN 简版） | 简版：连接/取数/依赖安装（带心跳）收进 lib，notebook 只留配置与训练流程；lib 与 notebook 同目录上传，两份 lib 逻辑相同仅日志语言不同 | ✅ 2026-08-20 实测通过 |

选型：要排障或想看清每一步 → 完整版；日常使用 → 简版。两者共用同一连接配方（ADR-0002）。

## 与 xgboost demo 的关系

原训练（`train_upload.ipynb`）与部署验证（`verify_hotswap.ipynb`）不受影响；
C 节的 Hive 版训练是开箱即用的替代入口 —— 产出的模型文件、OBS 上传路径与原版
完全一致，后续热切换验证流程照旧。
