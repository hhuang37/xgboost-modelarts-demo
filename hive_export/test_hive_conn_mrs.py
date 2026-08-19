#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_hive_conn_mrs.py —— 在 MRS 集群内网节点(Linux)上测试/验证 Hive(Kerberos) 连接

由 test_hive_conn.ipynb 改写。原理与 notebook 完全一致:
  华为 MRS 的 Hive SPN 是固定串 hive/hadoop.<域名全小写>@<REALM>
  （中间段不是 _HOST、不是 IP/主机名），而 pyhive 在 auth=KERBEROS 时把 TCP 连接的
  host 直接当 SPN 的 host 用，导致对不上。
  解决: pyhive 官方逃生口 hive.connect(thrift_transport=...) 自定义 transport ——
        TCP 层连 HiveServer2 内网 IP:21066，SASL 层的 host 传 SPN 中间段，两者解耦。

相对 notebook 的改动（针对内网 MRS 节点）:
  1. 默认地址全部用内网 IP（不是 EIP），可用命令行参数覆盖;
  2. 不再自动 pip install（内网无公网）—— 依赖缺失时打印离线安装指引
     （--install-deps 可强行尝试一次）;
  3. 票据获取三选一: 已有票据(klist 自动探测) / keytab kinit / --ask-password 交互输入;
  4. 自动从节点本机的 hive-site.xml 里发现真实 principal（找得到就优先用，免得猜 SPN）。

用法（详见同目录 MRS_RUN.md）:
  python3 test_hive_conn_mrs.py                              # 用文件头默认配置跑全流程
  python3 test_hive_conn_mrs.py --host 10.0.0.51             # 覆盖 HiveServer2 地址
  python3 test_hive_conn_mrs.py --kdc 10.0.0.51 --kdc 10.0.0.52
  python3 test_hive_conn_mrs.py --keytab /root/hhx.keytab    # 用 MRS Manager 下载的 keytab
  python3 test_hive_conn_mrs.py --ask-password               # 交互输入密码做 kinit
  python3 test_hive_conn_mrs.py --query "SELECT COUNT(*) FROM breast_cancer"
  python3 test_hive_conn_mrs.py --check                      # 只做依赖+网络连通性检查

阶段: 依赖检查 -> 端口连通 -> 生成 krb5.conf -> 获取票据 -> 连接(SPN 依次尝试) -> 验证查询
退出码: 0=全部通过  1=连接/查询失败  2=依赖缺失
"""
import argparse
import importlib
import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# ================== 默认配置（按 MRS 控制台「节点管理」里的内网 IP 修改） ==================
HIVE_HOST = "10.0.0.51"        # HiveServer2 所在节点的内网 IP（TCP 层连这里）
HIVE_PORT = 21066              # HiveServer2 Thrift 端口
DATABASE = "default"
USERNAME = "hhx"               # MRS 业务用户

# ---- Kerberos ----
REALM = "252A63EC_2C90_4B5A_B4D7_17A3077B1CB8.COM"   # MRS 系统域名(Realm)
SERVICE = "hive"
# 华为 MRS 的 Hive SPN 中间段是固定串。本集群(10.0.0.15)已实测验证:
#   正确值 = hadoop. + 域名全小写  (即下方候选 ①，且已从客户端 hive-site.xml 自动发现)
#   曾出现的 "hadoop.haddop_xxx" 变体在 KDC 里不存在(候选 ② 仅作兼容保留)。
# 优先级: hive-site.xml 自动发现 > ① > ②
SPN_HOST_CANDIDATES = [
    "hadoop." + REALM.lower(),
    "hadoop.haddop_" + REALM.lower(),
]

KDC_HOSTS = ["10.0.0.51", "10.0.0.15"]   # Master 节点内网 IP（跑 KDC 的节点）
KDC_PORT = 21732              # MRS 专用 KDC 端口（开源环境是 88）
USER_KEYTAB = None             # MRS Manager 下载的 keytab 路径，如 /root/hhx.keytab

# MRS 客户端路径（存在则优先用它的 krb5.conf 和 kinit）
MRS_CLIENT = "/opt/hadoopclient"

# hive-site.xml 可能的位置（用于自动发现真实 principal；找不到就跳过，不影响运行）
HIVE_SITE_GLOBS = [
    "/opt/hadoopclient/Hive/config/hive-site.xml",   # MRS 客户端（华为默认路径）
    "/usr/hdp/current/hive-server2/conf/hive-site.xml",
    "/usr/hdp/current/hive-client/conf/hive-site.xml",
    "/etc/hive/conf/hive-site.xml",
    "/opt/client/Hive/config/hive-site.xml",
    "/opt/Bigdata/*/*/Hive/*/conf*/hive-site.xml",
]

SCRIPT_DIR = Path(__file__).resolve().parent

# ====================================================================================


def ok(msg):
    print("[PASS] " + msg)


def fail(msg):
    print("[FAIL] " + msg)


def info(msg):
    print("[INFO] " + msg)


def warn(msg):
    print("[WARN] " + msg)


def stage(n, title):
    print("\n" + "=" * 20 + " {} {}".format(n, title) + " " + "=" * 20)


# ---------------------- 1. 依赖检查 ----------------------
REQUIRED = ["pyhive", "thrift", "thrift_sasl", "puresasl"]  # GSSAPI 绑定另查 kerberos/sasl

PIP_HINT = """在 MRS 节点安装依赖（已在 10.0.0.15 实测通过的完整步骤）:
  1) 配置华为云 HCE 公共 yum 源(节点能出公网时):
     cat > /etc/yum.repos.d/hce-os.repo <<EOF
     [hce-os]
     name=HCE 2.0 OS
     baseurl=https://repo.huaweicloud.com/hce/2.0/os/x86_64/
     enabled=1
     gpgcheck=0
     EOF
  2) 装编译工具链和头文件(python 的 sasl 包是 C++ 扩展):
     yum install -y gcc gcc-c++ krb5-devel cyrus-sasl-devel cyrus-sasl-gssapi
  3) 装 python 包:
     pip3 install pyhive thrift thrift-sasl pure-sasl kerberos sasl
  ★ MRS 的 HiveServer2 通常配置 qop=auth-conf(加密包装层):
    pure-sasl+kerberos(pykerberos) 能完成 SASL 协商，但在加密包装首个请求时报
    "Invalid token was supplied" —— 必须安装 cyrus 的 sasl 包(pip3 install sasl)，
    pyhive 的 get_installed_sasl 会自动优先使用它。
"""


def check_deps(auto_install):
    stage(1, "依赖检查")
    missing = []
    for name in REQUIRED:
        try:
            importlib.import_module(name)
        except Exception as e:  # 装了但坏的也算缺
            missing.append((name, "{}: {}".format(type(e).__name__, e)))
    # GSSAPI 绑定: cyrus 的 sasl 或 pykerberos 至少一个；auth-conf 集群强推 cyrus
    have_cyrus = have_pykrb = False
    for name, flag in (("sasl", "have_cyrus"), ("kerberos", "have_pykrb")):
        try:
            importlib.import_module(name)
            if flag == "have_cyrus":
                have_cyrus = True
            else:
                have_pykrb = True
        except Exception:
            continue
    if have_cyrus:
        info("GSSAPI 绑定使用: sasl (cyrus, 支持 qop=auth-conf, 推荐)")
    elif have_pykrb:
        info("GSSAPI 绑定使用: kerberos (pure-sasl)")
        warn("仅 pykerberos 时, 若集群 qop=auth-conf 会在加密包装阶段报 "
             "'Invalid token was supplied' —— 建议 pip3 install sasl (cyrus)")
    else:
        missing.append(("sasl 或 kerberos", ImportError("GSSAPI 需要二者之一")))

    for name, err in missing:
        fail("缺依赖 {}: {}".format(name, err))
    if not missing:
        ok("python 依赖齐全")
        return True

    if auto_install:
        info("尝试 --install-deps 自动安装（内网大概率失败）...")
        pkgs = ["pyhive", "thrift", "thrift-sasl", "pure-sasl", "kerberos"]
        r = subprocess.call([sys.executable, "-m", "pip", "install"] + pkgs)
        if r == 0:
            ok("自动安装完成，请重新运行本脚本")
        else:
            print(PIP_HINT)
        return False

    print(PIP_HINT)
    fail("请先补齐上述依赖后重跑")
    return False


# ---------------------- 2. 网络连通性 ----------------------
def tcp_probe(host, port, name, timeout=5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        ok("{} {}:{} 可达".format(name, host, port))
        return True
    except Exception as e:
        fail("{} {}:{} 不可达: {}".format(name, host, port, e))
        return False
    finally:
        s.close()


def check_network(host, port, kdcs, kdc_port):
    stage(2, "端口连通性")
    net_ok = tcp_probe(host, port, "HiveServer2")
    for k in kdcs:
        if not tcp_probe(k, kdc_port, "KDC"):
            net_ok = False
    if not net_ok:
        info("排查: 安全组/网络 ACL 是否放行本节点 -> HiveServer2:{}、Master:{}(UDP+TCP)"
             .format(port, kdc_port))
    return net_ok


# ---------------------- 3. krb5 配置：优先复用，缺了才生成 ----------------------
def setup_krb5(realm, kdcs, kdc_port, spn_hosts):
    """已有可用的 KRB5_CONFIG（如 source 过 bigdata_env）或 MRS 客户端自带 krb5.conf 时
    直接复用（里面的 KDC 端口/备用 KDC 是权威的），否则才自己生成一份。"""
    stage(3, "krb5 配置")
    existing = os.environ.get("KRB5_CONFIG")
    if existing and Path(existing).exists():
        ok("复用已有 KRB5_CONFIG={}".format(existing))
        return existing
    client_conf = Path(MRS_CLIENT) / "KrbClient/kerberos/var/krb5kdc/krb5.conf"
    if client_conf.exists():
        os.environ["KRB5_CONFIG"] = str(client_conf)
        ok("复用 MRS 客户端自带 {}".format(client_conf))
        return str(client_conf)

    # 自己生成（非 MRS 客户端环境）
    krb5_file = SCRIPT_DIR / ("krb5.ini" if sys.platform == "win32" else "krb5.conf")
    domain_lines = ["    .{} = {}".format(realm.lower(), realm)]
    for h in dict.fromkeys(spn_hosts):          # 去重保序
        domain_lines.append("    {} = {}".format(h, realm))
        domain_lines.append("    .{} = {}".format(h, realm))
    lines = [
        "[libdefaults]",
        "    default_realm = {}".format(realm),
        "    dns_canonicalize_hostname = false",  # 关键: SPN 中间段是 DNS 里不存在的假域名
        "    rdns = false",
        "    udp_preference_limit = 1",
        "",
        "[realms]",
        "    {} = {{".format(realm),
    ]
    lines += ["        kdc = {}:{}".format(h, kdc_port) for h in kdcs]
    lines += [
        "        admin_server = {}:{}".format(kdcs[0], kdc_port),
        "    }",
        "",
        "[domain_realm]",
    ]
    lines += domain_lines + [""]
    krb5_file.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    os.environ["KRB5_CONFIG"] = str(krb5_file)   # kinit/klist/pykerberos/cyrus-sasl 都会读它
    ok("已生成 {} 并设置 KRB5_CONFIG".format(krb5_file))
    return str(krb5_file)


# ---------------------- 4. 票据(TGT) ----------------------
def _run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kw)


def find_kinit():
    """找 kinit: 先 PATH，再 MRS 客户端目录（节点上 kinit 常不在 PATH）"""
    p = shutil.which("kinit")
    if p:
        return p
    cands = [
        Path(MRS_CLIENT) / "KrbClient/kerberos/bin/kinit",
        Path("/usr/bin/kinit"),
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def ensure_ticket(principal, keytab, ask_password):
    stage(4, "获取用户票据 (kinit)")
    kinit = find_kinit()
    klist = shutil.which("klist") or str(Path(MRS_CLIENT) / "KrbClient/kerberos/bin/klist")
    if not kinit:
        fail("未找到 kinit —— MRS 节点在 {}, 或: yum install krb5-workstation".format(
            MRS_CLIENT + "/KrbClient/kerberos/bin"))
        return False

    if klist:
        r = _run([klist])
        if r.returncode == 0 and "krbtgt" in r.stdout:
            ok("已有有效票据，跳过 kinit:")
            print("    " + "\n    ".join(r.stdout.strip().splitlines()[:4]))
            return True

    if keytab and Path(keytab).exists():
        r = _run([kinit, "-kt", str(keytab), principal])
        if r.returncode == 0:
            ok("kinit(keytab) 成功: {}".format(principal))
            return True
        fail("kinit -kt 失败: {}".format(r.stderr.strip()))
        return False

    if ask_password:
        pw = getpass.getpass("输入 {} 的 MRS 密码: ".format(principal))
        r = _run([kinit, principal], input=pw + "\n")
        if r.returncode == 0:
            ok("kinit(密码) 成功: {}".format(principal))
            return True
        fail("kinit 失败(密码错误或 KDC 不可达): {}".format(r.stderr.strip()))
        return False

    fail("没有有效票据。三选一后重跑:")
    print("  a) 交互输入密码:   python3 {} --ask-password".format(Path(sys.argv[0]).name))
    print("  b) 用 keytab:      python3 {} --keytab /root/hhx.keytab".format(Path(sys.argv[0]).name))
    print("     (keytab 在 MRS Manager -> 系统 -> 用户管理 -> hhx -> 更多 -> 下载认证凭据)")
    print("  c) 手动 kinit 后重跑: kinit {}".format(principal))
    return False


# ---------------------- principal 自动发现 ----------------------
def find_principal_from_hive_site():
    """在节点本机搜 hive-site.xml，读 hive.server2.authentication.kerberos.principal"""
    import glob
    import xml.etree.ElementTree as ET
    for pattern in HIVE_SITE_GLOBS:
        for path in glob.glob(pattern):
            try:
                root = ET.parse(path).getroot()
                for prop in root.iter("property"):
                    if prop.findtext("name", "").strip() == \
                            "hive.server2.authentication.kerberos.principal":
                        value = (prop.findtext("value") or "").strip()
                        m = re.match(r"^([^/]+)/([^@]+)@(.+)$", value)
                        if m:
                            return path, m.group(1), m.group(2), m.group(3)
            except Exception:
                continue
    return None


# ---------------------- 5. 连接 ----------------------
def connect_hive(host, port, database, username, spn_hosts, realm):
    from thrift.transport import TSocket
    import thrift_sasl
    from pyhive import hive
    from pyhive.hive import get_installed_sasl
    from pyhive.sasl_compat import PureSASLClient

    def make_transport(spn_host):
        tcp = TSocket.TSocket(host, port)
        tcp.setTimeout(30000)

        def sasl_factory():
            # 已 kinit 场景: 优先 pyhive 自带工厂（cyrus-sasl，缺失自动退回 pure-sasl）
            return get_installed_sasl(host=spn_host, sasl_auth="GSSAPI", service=SERVICE)

        return thrift_sasl.TSaslClientTransport(sasl_factory, "GSSAPI", tcp)

    for spn_host in spn_hosts:
        principal = "{}/{}@{}".format(SERVICE, spn_host, realm)
        info("尝试 SPN: {}".format(principal))
        t = make_transport(spn_host)
        try:
            conn = hive.connect(thrift_transport=t, database=database, username=username)
            ok("连接成功! 生效 SPN = {}".format(principal))
            return conn, principal
        except Exception as e:
            fail("{} 失败: {}: {}".format(principal, type(e).__name__, e))
            try:
                t.close()
            except Exception:
                pass
    return None, None


# ---------------------- 6. 验证查询 ----------------------
def run_verify(conn, database, extra_query):
    cur = conn.cursor()
    cur.execute("SHOW DATABASES")
    dbs = [r[0] for r in cur.fetchall()]
    ok("SHOW DATABASES ({} 个): {}".format(len(dbs), ", ".join(dbs)))

    cur.execute("SHOW TABLES")
    tables = [r[0] for r in cur.fetchall()]
    ok("SHOW TABLES in {} ({} 个): {}".format(database, len(tables), ", ".join(tables) or "<空>"))

    if "breast_cancer" in tables:
        cur.execute("SELECT COUNT(*) FROM breast_cancer")
        ok("breast_cancer 行数: {}".format(cur.fetchall()[0][0]))
    else:
        info("default 库里没有 breast_cancer 表（如还没建表见 breast_cancer_hive.sql）")

    if extra_query:
        info("执行 --query: {}".format(extra_query))
        cur.execute(extra_query)
        rows = cur.fetchall()
        for row in rows[:100]:
            print("    " + str(row))
        if len(rows) > 100:
            info("... 共 {} 行，仅显示前 100 行".format(len(rows)))
        ok("自定义查询成功，返回 {} 行".format(len(rows)))
    cur.close()


def main():
    ap = argparse.ArgumentParser(
        description="MRS 内网节点 Hive(Kerberos) 连接测试 —— 由 test_hive_conn.ipynb 改写",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--host", default=HIVE_HOST, help="HiveServer2 节点内网 IP")
    ap.add_argument("--port", type=int, default=HIVE_PORT, help="HiveServer2 Thrift 端口")
    ap.add_argument("--database", default=DATABASE)
    ap.add_argument("--username", default=USERNAME, help="MRS 业务用户")
    ap.add_argument("--realm", default=REALM, help="Kerberos Realm(MRS 系统域名)")
    ap.add_argument("--kdc", action="append", default=None,
                    help="Master/KDC 节点内网 IP，可重复传多个")
    ap.add_argument("--kdc-port", type=int, default=KDC_PORT,
                    help="KDC 端口(华为 MRS 专用端口 21732, 开源环境 88)")
    ap.add_argument("--keytab", default=USER_KEYTAB, help="用户 keytab 路径")
    ap.add_argument("--ask-password", action="store_true", help="交互输入密码执行 kinit")
    ap.add_argument("--spn-host", action="append", default=None,
                    help="手动指定 SPN 中间段(可重复)；默认自动发现+内置两候选")
    ap.add_argument("--query", default=None, help="连接成功后额外执行的 SQL")
    ap.add_argument("--check", action="store_true", help="只做依赖+网络连通性检查")
    ap.add_argument("--install-deps", action="store_true", help="依赖缺失时尝试 pip 安装(内网多半失败)")
    args = ap.parse_args()

    try:  # 防节点 locale 为 POSIX 时中文输出报错
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    kdcs = args.kdc if args.kdc else KDC_HOSTS
    realm = args.realm

    # --- 依赖 ---
    if not check_deps(args.install_deps):
        sys.exit(2)

    # --- SPN 候选: 自动发现优先 ---
    spn_hosts = []
    if not args.spn_host:
        found = find_principal_from_hive_site()
        if found:
            path, svc, spn_host, found_realm = found
            ok("从 {} 自动发现 principal: {}/{}@{}".format(path, svc, spn_host, found_realm))
            if found_realm != realm:
                info("发现的真实 Realm({})与配置({})不同，以发现值为准".format(found_realm, realm))
                realm = found_realm
            spn_hosts.append(spn_host)
        else:
            info("未在本机找到 hive-site.xml，使用内置 SPN 候选（连接阶段会依次尝试）")
        base = ["hadoop.haddop_" + realm.lower(), "hadoop." + realm.lower()]
        spn_hosts += [h for h in base if h not in spn_hosts]
    else:
        spn_hosts = args.spn_host

    # --- 网络 ---
    net_ok = check_network(args.host, args.port, kdcs, args.kdc_port)
    if args.check:
        info("--check 模式：检查到此结束")
        sys.exit(0 if net_ok else 1)
    if not net_ok:
        sys.exit(1)   # 内网不通，kinit/连接必败，直接退出

    # --- krb5 配置 ---
    setup_krb5(realm, kdcs, args.kdc_port, spn_hosts)

    # --- 票据 ---
    principal = "{}@{}".format(args.username, realm)
    if not ensure_ticket(principal, args.keytab, args.ask_password):
        sys.exit(1)

    # --- 连接 ---
    stage(5, "连接 HiveServer2 (SPN 依次尝试)")
    conn, spn_in_use = connect_hive(args.host, args.port, args.database, args.username,
                                    spn_hosts, realm)
    if conn is None:
        fail("所有 SPN 候选均失败。排查顺序:")
        print("  1) 票据: klist 确认有 {} 的 krbtgt".format(realm))
        print("  2) SPN : MRS Manager -> Hive -> 配置 -> 搜 principal，"
              "把准确中间段用 --spn-host 传入")
        print("  3) 网络: 本节点 -> {}:{} 与 KDC:{}".format(args.host, args.port, args.kdc_port))
        print("  4) 端口: HiveServer2 thrift 端口是否为 {}".format(args.port))
        print("  5) 若错误含 Invalid token/Token header: 集群 qop=auth-conf, "
              "必须用 cyrus sasl(pip3 install sasl), 详见脚本头部 PIP_HINT")
        sys.exit(1)

    # --- 验证查询 ---
    stage(6, "验证查询")
    try:
        run_verify(conn, args.database, args.query)
    except Exception as e:
        fail("查询失败: {}: {}".format(type(e).__name__, e))
        sys.exit(1)
    finally:
        conn.close()

    print("\n" + "=" * 60)
    print("全部通过: 依赖/网络/krb5/kinit/连接/查询 均 OK")
    print("=" * 60)
    sys.exit(0)


if __name__ == "__main__":
    main()
