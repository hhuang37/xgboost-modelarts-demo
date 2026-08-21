# -*- coding: utf-8 -*-
"""train_upload_hive 的逻辑库：把 notebook 里的"重活"收进函数，notebook 只留配置和调用。

中文日志版；英文日志孪生版是 `train_upload_hive_lib_EN.py`（代码逻辑逐字相同，仅文档/注释/打印消息为英文）。
改逻辑时两个文件必须同步改。

内容与来源（一一对应，改动时两边同步）：
  - pandas ABI 修复（sys.path 摘除 modelarts-sdk）  <- train_upload.ipynb §2
  - ensure_training_deps()  带心跳地装 pandas/sklearn/xgboost  <- train_upload.ipynb §2
  - run_stream()             流式执行器（关键行即时打印、静默期心跳） <- modelarts_hive_conn.ipynb 第 3 格
  - probe_network()          网络探测（安全组没放行在这里快速暴露）   <- modelarts_hive_conn.ipynb 第 2 格
  - setup_environment()      kinit/cyrus-sasl/pyhive 预装 + GSSAPI 自检 <- 第 3 格
  - write_krb5_conf()        生成 krb5.conf（dns_canonicalize_hostname=false 是关键）<- 第 4 格
  - kinit_user()             kinit（已有票据则跳过，否则 getpass 输密码）<- 第 5 格
  - connect_hive()           thrift_sasl 连接（TCP 地址与 SPN 解耦）   <- 第 6 格
  - connect_mrs_hive()       上面 5 步的一键编排（幂等，可整格重跑）
  - fetch_breast_cancer()    小块取数（绕开 libsasl2 大帧 bug）        <- train_upload_hive.ipynb §4

集群实测值（IP/SPN/端口）的单一事实来源：hive_export/MRS_RUN.md §0；决策记录：docs/adr/0002。
排障速查表：hive_export/modelarts_hive_conn.ipynb 第 8 节 / MRS_RUN.md §5。

用法（notebook，与本文件同目录）：
    from train_upload_hive_lib import *
    ensure_training_deps()
    conn = connect_mrs_hive(hive_host=..., username=..., realm=..., ...)
    df = fetch_breast_cancer(conn)      # 返回列名为下划线风格的 DataFrame（mean_radius）
"""
import collections
import importlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

__all__ = [
    "connect_mrs_hive", "fetch_breast_cancer", "ensure_training_deps",
    "probe_network", "setup_environment", "write_krb5_conf",
    "kinit_user", "connect_hive", "run_stream",
]

# ================= import 时立即生效：pandas ABI 修复（幂等） =================
# 根因：~/modelarts-dev/modelarts-sdk/ 里捆绑了一个 pandas 副本，被插到 sys.path
# 最前面，它的 C 扩展与环境的 numpy 版本不匹配，导致
#   ValueError: numpy.dtype size changed (Expected 96, got 88)
# 修复：把该目录从 sys.path 剔除，并清理已缓存的 pandas 模块。必须在 import
# pandas 之前执行 —— 所以放在模块顶层，notebook 里 `from ... import *` 即完成。
_BAD_FRAGMENTS = ("modelarts-dev/modelarts-sdk", "modelarts-dev\\modelarts-sdk")
_original_path = list(sys.path)
sys.path = [p for p in sys.path
            if not any(frag in p.replace("\\", "/") for frag in _BAD_FRAGMENTS)]
if len(sys.path) != len(_original_path):
    _removed = set(_original_path) - set(sys.path)
    print(f"[path-fix] 已从 sys.path 剔除: {_removed}")
for _mod_name in list(sys.modules):
    if _mod_name == "pandas" or _mod_name.startswith("pandas."):
        _mod_file = getattr(sys.modules[_mod_name], "__file__", "") or ""
        if any(frag in _mod_file.replace("\\", "/") for frag in _BAD_FRAGMENTS):
            del sys.modules[_mod_name]


def _have(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


# ---- 流式执行器: 关键行即时打印(带耗时), 静默期打心跳, 失败回放末尾输出 ----
_BAR = re.compile(r"^\W*\[\W*\d+%\W*\]\W*$")            # apt 的 [ 12%] 进度条(噪声)
_HOT = re.compile(r"solving|collecting|downloading|extracting|preparing|executing|"
                  r"transaction|unpacking|setting up|processing|fetched|^get|^hit|"
                  r"building wheel|successfully|installed|nothing to do|all requested|"
                  r"error|fail|conflict|warn", re.I)     # 值得展示的进度/结果行


def run_stream(cmd, note=None, heartbeat=20):
    """流式执行外部命令。返回码 0=成功; 失败时自动回放末尾 40 行输出。

    长时间无输出时每 `heartbeat` 秒打一行心跳，证明进程还活着
    （conda 求解/下载可能静默数分钟，属正常）。
    """
    if note:
        print(f"[run] {note}", flush=True)
    t0, tail = time.time(), collections.deque(maxlen=40)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    stop = threading.Event()

    def _beat():
        quiet = time.time()
        while not stop.wait(2):
            if time.time() - quiet >= heartbeat:
                print(f"   ... {int(time.time()-t0)}s 仍在运行（{cmd[0]} 无新输出，静默属正常）", flush=True)
                quiet = time.time()

    th = threading.Thread(target=_beat, daemon=True)
    th.start()
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        if line and not _BAR.match(line) and _HOT.search(line):
            print(f"[{int(time.time()-t0):>3}s] {line}", flush=True)
    rc = proc.wait()
    stop.set()
    th.join(timeout=1)
    if rc != 0:
        print("---- 命令末尾输出（最多 40 行）----")
        print("\n".join(t for t in tail if t.strip()) or "(无输出)")
    return rc


def ensure_training_deps():
    """训练三件套（pandas / scikit-learn / xgboost）缺啥装啥，带心跳进度。

    ModelArts 镜像通常已预装，此函数秒过；真要装时能从输出看到实时进展。
    """
    pkgs = [(p, m) for p, m in (("pandas", "pandas"),
                                ("scikit-learn", "sklearn"),
                                ("xgboost", "xgboost")) if not _have(m)]
    if not pkgs:
        print("[deps] pandas / scikit-learn / xgboost 已齐，跳过安装")
        return
    names = [p for p, _ in pkgs]
    rc = run_stream([sys.executable, "-m", "pip", "install"] + names,
                    f"pip 安装 {names}（心跳+进度）")
    if rc != 0:
        raise SystemExit("[FAIL] pip install 失败，看上方末尾输出排查")


def probe_network(hive_host, hive_port, kdc_hosts, kdc_port, timeout=5):
    """探测 HiveServer2 与 KDC 可达性。安全组没放行在这里快速暴露。

    只通 21066 不够 —— kinit 还要访问 KDC 的 21732（TCP+UDP）。
    """
    import socket

    def probe(host, port, name):
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            print(f"[OK]   {name} {host}:{port} 可达")
            return True
        except Exception as e:
            print(f"[FAIL] {name} {host}:{port} 不可达: {e}")
            return False
        finally:
            s.close()

    ok = probe(hive_host, hive_port, "HiveServer2")
    for k in kdc_hosts:
        ok &= probe(k, kdc_port, "KDC")
    assert ok, (
        "网络不通：请确认 notebook 与 MRS 同 VPC，且安全组放行 21066 与 21732(TCP+UDP)。"
    )
    return True


def setup_environment(spn_host):
    """预装 kinit + cyrus 的 sasl + pyhive（幂等；自动适配三种环境；实时进度）。

    目标产物: kinit 二进制 + cyrus 的 sasl(GSSAPI 插件在位) + 纯 python 的 pyhive 等
      ★ 集群 qop=auth-conf，必须用 cyrus 的 sasl；pure-sasl+pykerberos 实测在
        加密包装阶段报 "Invalid token was supplied"（见 ADR-0002）
    适配顺序（打印 [env] 说明命中哪支）：
      A. root            -> apt 装 gcc/g++/krb5-user/头文件 + GSSAPI 插件, pip 编译 sasl
      B. ma-user+免密sudo -> 同 A，apt 前加 sudo -n
      C. 无 root(常见)   -> conda-forge 预编译: krb5(自带 kinit) + sasl，无需编译器
    """
    need_kinit, need_sasl = shutil.which("kinit") is None, not _have("sasl")

    # conda 装的 kinit 在 sys.prefix/bin：内核重启后 PATH 可能不含它，先补上，
    # 否则依赖已齐也会误判 need_kinit，白白再跑一次 conda 求解（本实例实测约 5 分钟）
    if os.path.isfile(os.path.join(sys.prefix, "bin", "kinit")):
        os.environ["PATH"] = os.path.join(sys.prefix, "bin") + os.pathsep + os.environ["PATH"]
        need_kinit = need_kinit and shutil.which("kinit") is None

    # --- 系统层 ---
    if need_kinit or need_sasl:
        apt, env_name = None, "无 root（走 conda 分支）"
        if os.geteuid() == 0:
            apt, env_name = ["apt-get"], "root"
        else:
            sudo_ok = subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0
            if sudo_ok:
                apt, env_name = ["sudo", "-n", "apt-get"], "ma-user + 免密 sudo"
        print(f"[env] {env_name}", flush=True)

        if apt is not None:
            # libsasl2-modules-gssapi-mit / libsasl2-modules = cyrus 的 GSSAPI 插件(必须!)
            if run_stream(apt + ["update"], "apt-get update") != 0:
                raise SystemExit("[FAIL] apt-get update 失败")
            if run_stream(apt + ["install", "-y", "gcc", "g++", "krb5-user",
                                 "libkrb5-dev", "libsasl2-dev",
                                 "libsasl2-modules-gssapi-mit", "libsasl2-modules"],
                          "apt 安装编译链 + krb5 + cyrus sasl（首次约 1-2 分钟）") != 0:
                raise SystemExit("[FAIL] apt install 失败")
        else:
            # 无 root：ModelArts 自带 anaconda，conda-forge 有预编译的 krb5 和 sasl
            conda = shutil.which("conda")
            assert conda, "[FAIL] 没找到 conda —— 请把报错反馈给维护者"
            # --prefix sys.prefix: 显式装进当前内核环境，避免落到 base
            # --override-channels: 只用本命令指定的频道 —— 实例的 condarc 若配了已失效的
            #   镜像频道(如 TUNA 的 anaconda/pkgs/free, 已停同步 404), 不加这个会一起失败
            base = [conda, "install", "-y", "--override-channels", "--prefix", sys.prefix]
            attempts = [
                (base + ["-c", "conda-forge", "krb5", "sasl"],
                 "conda 安装 krb5 + sasl（conda-forge，绕开失效镜像频道；求解+下载 1-3 分钟）"),
                (base + ["-c", "https://conda.anaconda.org/conda-forge", "krb5", "sasl"],
                 "conda 重试（官方 conda-forge 源直连，可能较慢）"),
            ]
            rc = 1
            for cmd, note in attempts:
                rc = run_stream(cmd, note)
                if rc == 0:
                    break
            if rc != 0:
                raise SystemExit("[FAIL] conda install 两个源均失败（实例镜像源不可用？）；"
                                 "备选：改用 OBS 离线 wheel，或把上面的报错反馈给维护者")
            os.environ["PATH"] = os.path.join(sys.prefix, "bin") + os.pathsep + os.environ["PATH"]
            print("[提示] 若下方自检 import 报错（conda 刚装完包内核未感知），"
                  "重启内核后重跑连接格即可（已装的会自动跳过）")
    else:
        print("[env] 系统依赖已齐（kinit + sasl），跳过安装")

    # --- python 包（纯 python，pip 即可） ---
    pip_pkgs = [p for p, m in (("pyhive", "pyhive"), ("thrift", "thrift"),
                               ("thrift-sasl", "thrift_sasl"), ("sasl", "sasl"))
                if not _have(m)]
    if pip_pkgs:
        if run_stream([sys.executable, "-m", "pip", "install"] + pip_pkgs,
                      f"pip 安装 {pip_pkgs}") != 0:
            raise SystemExit("[FAIL] pip install 失败")

    # --- 自检：import + cyrus 的 GSSAPI 插件在位（auth-conf 的硬前提） ---
    # 注意: cyrus 的 sasl 包没有"列出机制"的 API。改用功能探测: 真的 init + start
    # 一次 GSSAPI，与连接时是同一条代码路径：
    #   start 成功                                  -> 插件在位（且已有票据）
    #   报 No worthy mechs / No mechanism available -> 插件缺失（依赖没装全，fatal）
    #   报 GSSAPI 凭据类错误（无票据等）           -> 插件在位，kinit 后即可用
    import glob
    from pyhive import hive  # noqa: F401  (自检 import)
    from pyhive.hive import get_installed_sasl  # noqa: F401
    import thrift_sasl  # noqa: F401
    import sasl as cyrus_sasl

    p = cyrus_sasl.Client()
    p.setAttr("host", spn_host)
    p.setAttr("service", "hive")
    assert p.init(), f"cyrus sasl 初始化失败: {p.getError()!r}"
    ok, _mech, _resp = p.start("GSSAPI")
    err = p.getError()
    err = err.decode("utf-8", "replace") if isinstance(err, bytes) else (err or "")
    if ok:
        print("[OK] python 依赖就绪；GSSAPI 插件可用；kinit =", shutil.which("kinit"))
    elif "worthy mechs" in err.lower() or "no mechanism available" in err.lower():
        for pat in (os.path.join(sys.prefix, "lib*", "sasl2", "*"),
                    "/usr/lib/*/sasl2/*", "/usr/lib64/sasl2/*"):
            for h in glob.glob(pat):
                if "gssapi" in os.path.basename(h).lower():
                    print("  gssapi 插件文件:", h)
        raise SystemExit(
            f"cyrus sasl 缺 GSSAPI 插件（{err}）\n"
            "root/sudo 环境: 检查 libsasl2-modules-gssapi-mit 是否装上；\n"
            "conda 环境: 把 !ls $CONDA_PREFIX/lib/sasl2/ 的输出发给维护者排查")
    else:
        print("[OK] python 依赖就绪；GSSAPI 插件在位（暂无票据，kinit 后生效；"
              f"探测信息: {err.splitlines()[0] if err else '-'}）")
        print("kinit =", shutil.which("kinit"))


def write_krb5_conf(realm, kdc_hosts, kdc_port, spn_host, path=None):
    """生成 krb5.conf 并设置 KRB5_CONFIG。返回 conf 文件 Path。

    dns_canonicalize_hostname=false 是关键：SPN 中间段 hadoop.xxx 是 DNS 里
    不存在的"假域名"，必须禁止 Kerberos 客户端解析它，原样当 SPN 用。
    udp_preference_limit=1 让 AS/TGS 请求走 TCP —— 与探测的 TCP 端口一致。
    """
    krb5_file = Path(path) if path else Path.cwd() / "krb5.conf"
    lines = [
        "[libdefaults]",
        f"    default_realm = {realm}",
        "    dns_canonicalize_hostname = false",   # <- 关键
        "    rdns = false",
        "    udp_preference_limit = 1",
        "",
        "[realms]",
        f"    {realm} = {{",
        *[f"        kdc = {h}:{kdc_port}" for h in kdc_hosts],
        f"        admin_server = {kdc_hosts[0]}:{kdc_port}",
        "    }",
        "",
        "[domain_realm]",
        f"    .{realm.lower()} = {realm}",
        f"    {spn_host} = {realm}",
        f"    .{spn_host} = {realm}",
        "",
    ]
    krb5_file.write_text("\n".join(lines), encoding="utf-8")
    os.environ["KRB5_CONFIG"] = str(krb5_file)   # kinit 与 cyrus-sasl 都读它
    print(f"[OK] 已生成 {krb5_file} 并设置 KRB5_CONFIG")
    return krb5_file


def kinit_user(username, realm, krb5_file):
    """kinit 获取用户票据（TGT，24h 有效）。已有票据则跳过；否则 getpass 输密码。

    kinit 可能来自 apt(krb5-user, /usr/bin) 或 conda(krb5, $CONDA_PREFIX/bin)，自适应。
    """
    import getpass

    kinit = shutil.which("kinit") or os.path.join(sys.prefix, "bin", "kinit")
    klist = shutil.which("klist") or os.path.join(sys.prefix, "bin", "klist")

    def _run(cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True,
                              env={**os.environ, "KRB5_CONFIG": str(krb5_file)}, **kw)

    r = _run([klist])
    if r.returncode == 0 and "krbtgt" in r.stdout:
        print("[OK] 已有有效票据，跳过 kinit：")
        print("\n".join(r.stdout.splitlines()[:4]))
        return
    principal = f"{username}@{realm}"
    pw = getpass.getpass(f"输入 {principal} 的密码: ")
    r = _run([kinit, principal], input=pw + "\n")
    assert r.returncode == 0, f"[FAIL] kinit 失败（密码错/KDC 不通？）：{r.stderr.strip()}"
    print(f"[OK] kinit 成功: {principal}")


def connect_hive(hive_host, hive_port, database, username, realm, spn_host, timeout_ms=30000):
    """连接 HiveServer2（核心：解耦 TCP 地址与 SPN）。

    pyhive 在 auth=KERBEROS 时把 TCP 连接的 host 直接当 SPN 的 host 用 -> 必然对不上
    （会去 KDC 请求 hive/<内网IP>@REALM 的票据，而集群注册的是固定串 SPN）。
    官方逃生口 thrift_transport=...：TCP 层连内网 IP，SASL 层 host 传 SPN 中间段。
    pyhive 的 get_installed_sasl 在装了 cyrus 的 sasl 包后自动优先用它（qop=auth-conf 必需）。
    """
    from thrift.transport import TSocket
    from pyhive import hive
    from pyhive.hive import get_installed_sasl
    import thrift_sasl

    principal = f"hive/{spn_host}@{realm}"
    print("尝试 SPN:", principal)

    def make_transport():
        tcp = TSocket.TSocket(hive_host, hive_port)
        tcp.setTimeout(timeout_ms)
        sasl_factory = lambda: get_installed_sasl(
            host=spn_host, sasl_auth="GSSAPI", service="hive")
        return thrift_sasl.TSaslClientTransport(sasl_factory, "GSSAPI", tcp)

    conn = hive.connect(thrift_transport=make_transport(),
                        database=database, username=username)
    print("[OK] 连接成功！生效 SPN =", principal)
    return conn


def connect_mrs_hive(hive_host="10.0.0.15", hive_port=21066, database="default",
                     username="hhx", realm="252A63EC_2C90_4B5A_B4D7_17A3077B1CB8.COM",
                     kdc_hosts=("10.0.0.15", "10.0.0.51"), kdc_port=21732):
    """一键连接 MRS Hive（Kerberos 安全集群）：探测 -> 预装 -> krb5 -> kinit -> 连接。

    默认值为 2026-08-19 实测值（单一事实来源 hive_export/MRS_RUN.md §0）。
    全程幂等：票据过期/内核重启后整格重跑即可，已完成的步骤自动跳过。
    """
    kdc_hosts = list(kdc_hosts)
    spn_host = "hadoop." + realm.lower()   # 实测正确的 SPN 中间段（haddop_ 变体是错的）
    print(f"principal = hive/{spn_host}@{realm}")
    probe_network(hive_host, hive_port, kdc_hosts, kdc_port)
    setup_environment(spn_host)
    krb5_file = write_krb5_conf(realm, kdc_hosts, kdc_port, spn_host)
    kinit_user(username, realm, krb5_file)
    return connect_hive(hive_host, hive_port, database, username, realm, spn_host)


def fetch_breast_cancer(conn, table="breast_cancer", batch=5):
    """整表读取 Hive 表，返回 DataFrame（列名保持 Hive 下划线风格，如 mean_radius）。

    坑（实测）：pyhive 默认 arraysize=10000，fetchall 会把 569 行打进一个超大 SASL
    加密帧；部分环境的 libsasl2（2.1.28 有已知回归 bug）解不开大帧，报
      TTransportException: sasl_decode ... Unable to find a callback: 32775
    对策：小块取（默认每次 5 行）—— 与 conn notebook 里 LIMIT 5 稳定通过是同一原理。
    """
    import pandas as pd

    cur = conn.cursor()
    cur.arraysize = batch
    cur.execute(f"SELECT * FROM {table}")
    cols = [d[0].split(".")[-1] for d in cur.description]   # 去掉可能带的 库名.表名. 前缀
    try:
        rows = cur.fetchall()
    except Exception as e:
        if "sasl_decode" in str(e) or "32775" in str(e):
            msg = f"""小块取仍触发 sasl 解包失败（libsasl2 2.1.28 已知回归 bug）。
修复：新格子里跑下面两行，装完点菜单 Kernel - Restart Kernel，从头重跑：
  import subprocess
  subprocess.run(['conda', 'install', '-y', '-c', 'conda-forge',
                  '--override-channels', 'libsasl2=2.1.27'], check=True)"""
            raise SystemExit(msg) from e
        raise
    finally:
        cur.close()
    print(f"取回 {len(rows)} 行")
    return pd.DataFrame(rows, columns=cols)
