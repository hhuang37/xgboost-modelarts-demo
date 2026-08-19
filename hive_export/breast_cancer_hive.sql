-- 由 export_breast_cancer.py 自动生成，共 569 行数据、31 列
-- CSV 表头与列名的对应关系：空格换成下划线，如 "mean radius" -> mean_radius
--
-- 本脚本只负责建表：原始 CSV 已直接放在 HDFS 的 /test/breast_cancer 目录下，
-- 所以这里用 EXTERNAL TABLE + LOCATION 直接读取，不再拷贝数据，也避免
-- DROP TABLE 时连带删除 HDFS 上的源文件。
DROP TABLE IF EXISTS breast_cancer;
CREATE EXTERNAL TABLE breast_cancer (
  `mean_radius` DOUBLE,
  `mean_texture` DOUBLE,
  `mean_perimeter` DOUBLE,
  `mean_area` DOUBLE,
  `mean_smoothness` DOUBLE,
  `mean_compactness` DOUBLE,
  `mean_concavity` DOUBLE,
  `mean_concave_points` DOUBLE,
  `mean_symmetry` DOUBLE,
  `mean_fractal_dimension` DOUBLE,
  `radius_error` DOUBLE,
  `texture_error` DOUBLE,
  `perimeter_error` DOUBLE,
  `area_error` DOUBLE,
  `smoothness_error` DOUBLE,
  `compactness_error` DOUBLE,
  `concavity_error` DOUBLE,
  `concave_points_error` DOUBLE,
  `symmetry_error` DOUBLE,
  `fractal_dimension_error` DOUBLE,
  `worst_radius` DOUBLE,
  `worst_texture` DOUBLE,
  `worst_perimeter` DOUBLE,
  `worst_area` DOUBLE,
  `worst_smoothness` DOUBLE,
  `worst_compactness` DOUBLE,
  `worst_concavity` DOUBLE,
  `worst_concave_points` DOUBLE,
  `worst_symmetry` DOUBLE,
  `worst_fractal_dimension` DOUBLE,
  `target` INT
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