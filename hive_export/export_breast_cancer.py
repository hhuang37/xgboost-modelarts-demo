# -*- coding: utf-8 -*-
"""把 sklearn 乳腺癌数据集导出为逗号分隔的文本文件，并生成配套的 Hive 建表 SQL。

运行一次会生成两个文件（与本脚本同目录）：
  breast_cancer.csv       569 行 x 31 列（30 个特征 + target），带表头，逗号分隔
  breast_cancer_hive.sql  CREATE EXTERNAL TABLE，指向 HDFS 上的原始 CSV

Hive 列名不能带空格，因此特征名 "mean radius" 会映射为 mean_radius。

把 CSV 上传到 HDFS 后（例如 `hdfs dfs -put breast_cancer.csv /test/breast_cancer/`），
直接执行生成的 breast_cancer_hive.sql 即可建表，无需再 LOAD DATA。
"""
from pathlib import Path

import pandas as pd
from sklearn.datasets import load_breast_cancer

OUT_DIR = Path(__file__).resolve().parent
CSV_PATH = OUT_DIR / "breast_cancer.csv"
SQL_PATH = OUT_DIR / "breast_cancer_hive.sql"

# --- 1. 导出 CSV（lineterminator 固定为 \n，避免 Windows 下产生 \r 混入字段值）---
data = load_breast_cancer()
df = pd.DataFrame(data.data, columns=data.feature_names)
df["target"] = data.target
df.to_csv(CSV_PATH, index=False, lineterminator="\n")

# --- 2. 生成 Hive 建表 SQL，列名与 CSV 表头一一对应 ---
hive_cols = [c.lower().replace(" ", "_") for c in df.columns]
columns_ddl = ",\n  ".join(
    f"`{c}` {'INT' if c == 'target' else 'DOUBLE'}" for c in hive_cols
)

sql = f"""-- 由 export_breast_cancer.py 自动生成，共 {len(df)} 行数据、{df.shape[1]} 列
-- CSV 表头与列名的对应关系：空格换成下划线，如 "mean radius" -> mean_radius
--
-- 本脚本只负责建表：原始 CSV 已直接放在 HDFS 的 /test/breast_cancer 目录下，
-- 所以这里用 EXTERNAL TABLE + LOCATION 直接读取，不再拷贝数据，也避免
-- DROP TABLE 时连带删除 HDFS 上的源文件。
DROP TABLE IF EXISTS breast_cancer;
CREATE EXTERNAL TABLE breast_cancer (
  {columns_ddl}
)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY ','
STORED AS TEXTFILE
LOCATION '/test/breast_cancer'
TBLPROPERTIES ('skip.header.line.count'='1');

-- 说明：
-- 1) 数据源路径：hdfs:///test/breast_cancer/breast_cancer.csv
--    （如文件名不是 breast_cancer.csv，请把 LOCATION 指向它所在的目录即可，
--     Hive 会读取该目录下全部文件）
-- 2) 不要执行 LOAD DATA — 数据已经在 HDFS 上，重复加载会失败或产生重复行
-- 3) DROP TABLE 只会删除元数据，HDFS 上的 CSV 文件不会被删除

-- 验证
SELECT COUNT(*) AS cnt FROM breast_cancer;
SELECT * FROM breast_cancer LIMIT 3;
"""
SQL_PATH.write_text(sql, encoding="utf-8", newline="\n")

print(f"CSV: {CSV_PATH} ({len(df)} 行, {df.shape[1]} 列)")
print(f"SQL: {SQL_PATH}")
