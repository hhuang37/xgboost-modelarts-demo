# test_hive_conn_mrs.py 启动手册（MRS 内网节点 · 已实测通过）

> 2026-08-19 已在华为 MRS 8.6.0.1 集群 master1（内网 10.0.0.15 / EIP 110.239.86.94）上
> 全流程验证通过：依赖 → 网络 → krb5 → kinit → Kerberos 连接 → 查询（breast_cancer 569 行）。
> 同日，同配方已在 ModelArts Notebook（无 root 的 ma-user 环境）实测通过
> （`modelarts_hive_conn.ipynb`，第 7 格输出两库 + 569 行）。

## 0. 本集群已确认的事实（排障结论，直接引用）

| 项 | 值 |
|---|---|
| HiveServer2 | `10.0.0.15:21066`（binary transport） |
| 正确 SPN | `hive/hadoop.252a63ec_2c90_4b5a_b4d7_17a3077b1cb8.com@252A63EC_2C90_4B5A_B4D7_17A3077B1CB8.COM`（即 `hadoop.` + 域名全小写；**`haddop_` 变体是错的**，KDC 里不存在） |
| KDC 端口 | **21732**（不是开源默认的 88！master1=10.0.0.15、master2=10.0.0.51） |
| KDC 辅助端口 | admin_server（kadmind）= **21730**，kpasswd = **21731**（来源：MRS Manager 下载的用户凭据包内官方 krb5.conf，2026-08-19 核对） |
| SASL QOP | **auth-conf**（加密包装层）→ **必须用 cyrus 的 `sasl` python 包**，pure-sasl+pykerberos 会在加密包装时报 `Invalid token was supplied` |
| krb5.conf | 节点 `/etc/krb5.conf` 是无用的 EXAMPLE.COM 样例；**正确的是 MRS 客户端自带的** `/opt/hadoopclient/KrbClient/kerberos/var/krb5kdc/krb5.conf`（脚本自动复用） |
| kinit | 系统本来没有；装 `krb5-workstation` 后用 `/usr/bin/kinit`（客户端自带的那套缺 LD_LIBRARY_PATH） |
| MRS 客户端 | `/opt/hadoopclient`（脚本会从其 `Hive/config/hive-site.xml` 自动发现真实 SPN） |

## 1. 部署（一次性）

```bash
# 本地 -> 节点（节点 sshd 禁了 sftp 子系统时用 base64 管道，或直接 vi 粘贴）
scp test_hive_conn_mrs.py root@110.239.86.94:/opt/     # 或在节点上粘贴
```

## 2. 装依赖（实测通过的完整命令，节点能出公网）

```bash
# 1) 华为云 HCE 公共 yum 源（节点 OS 是 hce2 时）
cat > /etc/yum.repos.d/hce-os.repo <<'EOF'
[hce-os]
name=HCE 2.0 OS
baseurl=https://repo.huaweicloud.com/hce/2.0/os/x86_64/
enabled=1
gpgcheck=0
EOF

# 2) 工具链 + 头文件（python 的 sasl 是 C++ 扩展，需要 g++；pykerberos 需要 krb5-devel）
yum install -y gcc gcc-c++ krb5-devel cyrus-sasl-devel cyrus-sasl-gssapi krb5-workstation

# 3) python 包（cyrus 的 sasl 是 auth-conf 集群的必需品！）
pip3 install pyhive thrift thrift-sasl pure-sasl kerberos sasl
```

内网完全无公网时：在有公网的 x86_64 Linux 上 `pip3 wheel -w wheels ...` 同一套包
（kerberos/sasl 需 gcc/g++/krb5-devel），把 wheels 目录拷过去
`pip3 install --no-index --find-links=wheels ...`。

## 3. 启动（节点上）

```bash
cd /opt

# 先 kinit（脚本也会自动探测已有票据；hhx 密码即 MRS Manager 里该用户的密码）
export KRB5_CONFIG=/opt/hadoopclient/KrbClient/kerberos/var/krb5kdc/krb5.conf
kinit hhx@252A63EC_2C90_4B5A_B4D7_17A3077B1CB8.COM

# 全流程测试（默认参数即本集群配置，无需覆盖）
python3 test_hive_conn_mrs.py

# 常用变体
python3 test_hive_conn_mrs.py --check                          # 只查依赖+网络
python3 test_hive_conn_mrs.py --ask-password                   # 交互输密码 kinit
python3 test_hive_conn_mrs.py --query "SELECT * FROM breast_cancer LIMIT 5"
```

## 4. 怎么算验证通过

全部 `[PASS]` 且退出码 0：

```
[PASS] python 依赖齐全
[PASS] 从 /opt/hadoopclient/Hive/config/hive-site.xml 自动发现 principal: hive/hadoop.252a63ec_...
[PASS] HiveServer2 10.0.0.15:21066 可达
[PASS] KDC 10.0.0.15:21732 可达 / KDC 10.0.0.51:21732 可达
[PASS] 复用 MRS 客户端自带 .../krb5kdc/krb5.conf
[PASS] 已有有效票据，跳过 kinit（或 kinit(密码) 成功）
[PASS] 连接成功! 生效 SPN = hive/hadoop.252a63ec_...@252A63EC_...
[PASS] SHOW DATABASES (2 个): default, mrs_system
[PASS] SHOW TABLES in default (1 个): breast_cancer
[PASS] breast_cancer 行数: 569
============================================================
全部通过: 依赖/网络/krb5/kinit/连接/查询 均 OK
============================================================
```

退出码：`0` 全通过 / `1` 连接或查询失败 / `2` 依赖缺失（可脚本化判断 `echo $?`）。

## 5. 常见失败速查（本集群实测遇到的）

| 现象 | 根因/处理 |
|---|---|
| `Invalid token was supplied` / `Token header is malformed`，且 SASL 协商日志显示 5 步都成功 | 集群 qop=auth-conf，pure-sasl+pykerberos 加密包装缺陷 → **pip3 install sasl**（cyrus），pyhive 自动优先使用 |
| `Server krbtgt/HADDOP_... not found in Kerberos database` | 用了 `hadoop.haddop_` 变体 SPN → 用自动发现或 `--spn-host hadoop.252a63ec_...` |
| KDC 88 端口探测失败 | MRS 的 KDC 在 **21732**：`--kdc-port 21732`（脚本默认已是） |
| 客户端 kinit 报 `libcom_err.so.3: cannot open shared object` | 用的是 `/opt/hadoopclient/KrbClient/kerberos/bin/kinit` 且没设 LD_LIBRARY_PATH → 装 `krb5-workstation` 用 `/usr/bin/kinit` |
| pip 装 `kerberos` 失败 `gcc not found` | `yum install -y gcc krb5-devel`（见第 2 节） |
| pip 装 `sasl` 失败 `cannot execute cc1plus` | 缺 g++：`yum install -y gcc-c++ cyrus-sasl-devel` |
| `AttributeError: 'sasl.saslwrapper.Client' object has no attribute 'available_mechs'`（ModelArts 无 root 路径） | 误把 pure-sasl 的 `available_mechs()` 用在 cyrus 的 `sasl` 包上（后者无列举机制 API）→ 改功能探测：`Client().setAttr(...)`+`init()`+`start("GSSAPI")`，只有报 `No worthy mechs` 才是缺插件（conda-forge 的 `cyrus-sasl` 自带 `lib/sasl2/libgssapiv2.so`） |
| beeline 报 `SPARK_JAVA_HOME is not set` | 先 `source /opt/hadoopclient/bigdata_env` |

## 6. 交叉验证（可选）

节点上用 MRS 自带 beeline（已实测通过，可查到 default/mrs_system 两库）：

```bash
source /opt/hadoopclient/bigdata_env
export KRB5_CONFIG=/opt/hadoopclient/KrbClient/kerberos/var/krb5kdc/krb5.conf
beeline -u "jdbc:hive2://10.0.0.15:21066/default;principal=hive/hadoop.252a63ec_2c90_4b5a_b4d7_17a3077b1cb8.com@252A63EC_2C90_4B5A_B4D7_17A3077B1CB8.COM;saslQop=auth-conf" -e "show databases"
```

## 7. 本次测试在节点上留下的环境变更（备忘）

- `/etc/yum.repos.d/hce-os.repo`：新增的华为云公共源（保留，后续装包可用）
- `yum` 新装：`gcc gcc-c++ krb5-devel cyrus-sasl-devel cyrus-sasl-gssapi krb5-workstation`
- `pip3` 新装：`pyhive thrift thrift-sasl pure-sasl kerberos sasl`
- `/opt/test_hive_conn_mrs.py`、`/opt/MRS_RUN.md`：本脚本与手册
- kinit 票据：`/tmp/krb5cc_0`（hhx@252A63EC...，24h 过期，过期后重新 kinit）
