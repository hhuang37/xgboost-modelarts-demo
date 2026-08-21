# -*- coding: utf-8 -*-
"""English-log edition of the train_upload_hive logic library.

Translated twin of `train_upload_hive_lib.py` (Chinese log messages): the code
logic is byte-identical — only docstrings, comments, and printed messages are
English. When changing logic, change BOTH files.

Contents and origins (one-to-one; keep both sides in sync):
  - pandas ABI fix (drop modelarts-sdk from sys.path)      <- train_upload.ipynb §2
  - ensure_training_deps()  install pandas/sklearn/xgboost with heartbeat  <- train_upload.ipynb §2
  - run_stream()             streaming runner (live lines, heartbeat on silence) <- modelarts_hive_conn.ipynb cell 3
  - probe_network()          network probing (missing security-group rules surface fast) <- cell 2
  - setup_environment()      kinit/cyrus-sasl/pyhive setup + GSSAPI self-check <- cell 3
  - write_krb5_conf()        generate krb5.conf (dns_canonicalize_hostname=false is the key) <- cell 4
  - kinit_user()             kinit (skip if a ticket exists, else getpass)  <- cell 5
  - connect_hive()           thrift_sasl connection (TCP address decoupled from SPN) <- cell 6
  - connect_mrs_hive()       one-call orchestration of the 5 steps above (idempotent)
  - fetch_breast_cancer()    small-batch fetch (dodges the libsasl2 large-frame bug) <- train_upload_hive.ipynb §4

Single source of truth for measured cluster values (IPs/SPN/ports):
hive_export/MRS_RUN.md §0; decision record: docs/adr/0002.
Troubleshooting quick reference: hive_export/modelarts_hive_conn_EN.ipynb §8 / MRS_RUN.md §5.

Usage (notebook, same directory as this file):
    from train_upload_hive_lib_EN import *
    ensure_training_deps()
    conn = connect_mrs_hive(hive_host=..., username=..., realm=..., ...)
    df = fetch_breast_cancer(conn)      # DataFrame with Hive-style underscored columns (mean_radius)
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

# ================= applied on import: pandas ABI fix (idempotent) =================
# Root cause: ~/modelarts-dev/modelarts-sdk/ bundles a pandas copy prepended to
# sys.path whose C extensions mismatch the environment's numpy, raising
#   ValueError: numpy.dtype size changed (Expected 96, got 88)
# Fix: drop those directories from sys.path and purge cached pandas modules.
# Must run BEFORE importing pandas — hence module top level; in the notebook the
# `from ... import *` cell does it.
_BAD_FRAGMENTS = ("modelarts-dev/modelarts-sdk", "modelarts-dev\\modelarts-sdk")
_original_path = list(sys.path)
sys.path = [p for p in sys.path
            if not any(frag in p.replace("\\", "/") for frag in _BAD_FRAGMENTS)]
if len(sys.path) != len(_original_path):
    _removed = set(_original_path) - set(sys.path)
    print(f"[path-fix] removed from sys.path: {_removed}")
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


# ---- streaming runner: print key lines live (with elapsed time), heartbeat on
# ---- silence, replay the tail on failure
_BAR = re.compile(r"^\W*\[\W*\d+%\W*\]\W*$")            # apt's [ 12%] progress bars (noise)
_HOT = re.compile(r"solving|collecting|downloading|extracting|preparing|executing|"
                  r"transaction|unpacking|setting up|processing|fetched|^get|^hit|"
                  r"building wheel|successfully|installed|nothing to do|all requested|"
                  r"error|fail|conflict|warn", re.I)     # progress/result lines worth showing


def run_stream(cmd, note=None, heartbeat=20):
    """Run an external command with streamed output. Return code 0 = success;
    on failure the last 40 lines of output are replayed automatically.

    Prints a heartbeat line every `heartbeat` seconds of silence to prove the
    process is alive (conda solve/download can stay quiet for minutes — normal).
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
                print(f"   ... {int(time.time()-t0)}s still running (no new output from {cmd[0]}; silence is normal)", flush=True)
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
        print("---- tail of command output (last 40 lines) ----")
        print("\n".join(t for t in tail if t.strip()) or "(no output)")
    return rc


def ensure_training_deps():
    """Install whatever is missing among pandas / scikit-learn / xgboost, with
    heartbeat progress.

    ModelArts images usually have them preinstalled, so this returns instantly;
    when an install really runs, live progress is visible in the output.
    """
    pkgs = [(p, m) for p, m in (("pandas", "pandas"),
                                ("scikit-learn", "sklearn"),
                                ("xgboost", "xgboost")) if not _have(m)]
    if not pkgs:
        print("[deps] pandas / scikit-learn / xgboost already present, skipping install")
        return
    names = [p for p, _ in pkgs]
    rc = run_stream([sys.executable, "-m", "pip", "install"] + names,
                    f"pip install {names} (heartbeat + progress)")
    if rc != 0:
        raise SystemExit("[FAIL] pip install failed — see the tail output above")


def probe_network(hive_host, hive_port, kdc_hosts, kdc_port, timeout=5):
    """Probe HiveServer2 and KDC reachability. A missing security-group rule
    surfaces here fast.

    Reaching 21066 alone is not enough — kinit also needs KDC port 21732 (TCP+UDP).
    """
    import socket

    def probe(host, port, name):
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            print(f"[OK]   {name} {host}:{port} reachable")
            return True
        except Exception as e:
            print(f"[FAIL] {name} {host}:{port} unreachable: {e}")
            return False
        finally:
            s.close()

    ok = probe(hive_host, hive_port, "HiveServer2")
    for k in kdc_hosts:
        ok &= probe(k, kdc_port, "KDC")
    assert ok, (
        "Network unreachable: make sure the notebook is in the same VPC as MRS "
        "and the security group allows 21066 and 21732 (TCP+UDP)."
    )
    return True


def setup_environment(spn_host):
    """Pre-install kinit + cyrus sasl + pyhive (idempotent; auto-adapts to three
    environments; live progress).

    Target artifacts: the kinit binary + cyrus sasl (GSSAPI plugin present) +
    pure-python pyhive etc.
      * The cluster runs qop=auth-conf, so cyrus's sasl is mandatory;
        pure-sasl+pykerberos measurably fails at the privacy-wrap stage with
        "Invalid token was supplied" (see ADR-0002)
    Adaptation order (the [env] line says which branch hit):
      A. root            -> apt install gcc/g++/krb5-user/headers + GSSAPI plugin, pip-build sasl
      B. ma-user+passless sudo -> same as A, with sudo -n before apt
      C. no root (common) -> conda-forge prebuilt: krb5(bundles kinit) + sasl, no compiler needed
    """
    need_kinit, need_sasl = shutil.which("kinit") is None, not _have("sasl")

    # conda's kinit lives in sys.prefix/bin: PATH may lack it after a kernel
    # restart, so add it first — otherwise deps are all present yet need_kinit
    # stays true and we pointlessly re-run a conda solve (~5 minutes, measured)
    if os.path.isfile(os.path.join(sys.prefix, "bin", "kinit")):
        os.environ["PATH"] = os.path.join(sys.prefix, "bin") + os.pathsep + os.environ["PATH"]
        need_kinit = need_kinit and shutil.which("kinit") is None

    # --- system layer ---
    if need_kinit or need_sasl:
        apt, env_name = None, "no root (conda branch)"
        if os.geteuid() == 0:
            apt, env_name = ["apt-get"], "root"
        else:
            sudo_ok = subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0
            if sudo_ok:
                apt, env_name = ["sudo", "-n", "apt-get"], "ma-user + passwordless sudo"
        print(f"[env] {env_name}", flush=True)

        if apt is not None:
            # libsasl2-modules-gssapi-mit / libsasl2-modules = cyrus's GSSAPI plugin (mandatory!)
            if run_stream(apt + ["update"], "apt-get update") != 0:
                raise SystemExit("[FAIL] apt-get update failed")
            if run_stream(apt + ["install", "-y", "gcc", "g++", "krb5-user",
                                 "libkrb5-dev", "libsasl2-dev",
                                 "libsasl2-modules-gssapi-mit", "libsasl2-modules"],
                          "apt install build chain + krb5 + cyrus sasl (first run ~1-2 min)") != 0:
                raise SystemExit("[FAIL] apt install failed")
        else:
            # No root: ModelArts ships anaconda, and conda-forge has prebuilt krb5 and sasl
            conda = shutil.which("conda")
            assert conda, "[FAIL] conda not found — please report this error to the maintainers"
            # --prefix sys.prefix: install into the current kernel env explicitly, not base
            # --override-channels: use only the channels given here — the instance's
            #   condarc may pin dead mirror channels (e.g. TUNA's anaconda/pkgs/free,
            #   no longer synced, 404) which would fail together without this flag
            base = [conda, "install", "-y", "--override-channels", "--prefix", sys.prefix]
            attempts = [
                (base + ["-c", "conda-forge", "krb5", "sasl"],
                 "conda install krb5 + sasl (conda-forge, bypassing dead mirror channels; solve+download 1-3 min)"),
                (base + ["-c", "https://conda.anaconda.org/conda-forge", "krb5", "sasl"],
                 "conda retry (official conda-forge direct, may be slow)"),
            ]
            rc = 1
            for cmd, note in attempts:
                rc = run_stream(cmd, note)
                if rc == 0:
                    break
            if rc != 0:
                raise SystemExit("[FAIL] conda install failed on both channels (instance mirror unavailable?); "
                                 "alternatives: offline wheels on OBS, or report the output above to the maintainers")
            os.environ["PATH"] = os.path.join(sys.prefix, "bin") + os.pathsep + os.environ["PATH"]
            print("[note] if the self-check below fails to import (kernel unaware of the fresh "
                  "conda install), restart the kernel and re-run the connect cell (installed parts are skipped)")
    else:
        print("[env] system deps already present (kinit + sasl), skipping install")

    # --- python packages (pure python, pip is enough) ---
    pip_pkgs = [p for p, m in (("pyhive", "pyhive"), ("thrift", "thrift"),
                               ("thrift-sasl", "thrift_sasl"), ("sasl", "sasl"))
                if not _have(m)]
    if pip_pkgs:
        if run_stream([sys.executable, "-m", "pip", "install"] + pip_pkgs,
                      f"pip install {pip_pkgs}") != 0:
            raise SystemExit("[FAIL] pip install failed")

    # --- self-check: imports + cyrus GSSAPI plugin present (hard prerequisite for auth-conf) ---
    # Note: cyrus's sasl package has no "list mechanisms" API. Use a functional
    # probe instead: actually init + start a GSSAPI exchange, the same code path
    # used when connecting:
    #   start succeeds                          -> plugin present (and a ticket exists)
    #   "No worthy mechs" / "No mechanism available" -> plugin missing (deps incomplete, fatal)
    #   GSSAPI credential errors (no ticket etc.) -> plugin present, works after kinit
    import glob
    from pyhive import hive  # noqa: F401  (import self-check)
    from pyhive.hive import get_installed_sasl  # noqa: F401
    import thrift_sasl  # noqa: F401
    import sasl as cyrus_sasl

    p = cyrus_sasl.Client()
    p.setAttr("host", spn_host)
    p.setAttr("service", "hive")
    assert p.init(), f"cyrus sasl init failed: {p.getError()!r}"
    ok, _mech, _resp = p.start("GSSAPI")
    err = p.getError()
    err = err.decode("utf-8", "replace") if isinstance(err, bytes) else (err or "")
    if ok:
        print("[OK] python deps ready; GSSAPI plugin usable; kinit =", shutil.which("kinit"))
    elif "worthy mechs" in err.lower() or "no mechanism available" in err.lower():
        for pat in (os.path.join(sys.prefix, "lib*", "sasl2", "*"),
                    "/usr/lib/*/sasl2/*", "/usr/lib64/sasl2/*"):
            for h in glob.glob(pat):
                if "gssapi" in os.path.basename(h).lower():
                    print("  gssapi plugin file:", h)
        raise SystemExit(
            f"cyrus sasl is missing its GSSAPI plugin ({err})\n"
            "root/sudo env: check whether libsasl2-modules-gssapi-mit got installed;\n"
            "conda env: send the output of !ls $CONDA_PREFIX/lib/sasl2/ to the maintainers")
    else:
        print("[OK] python deps ready; GSSAPI plugin present (no ticket yet, effective after kinit; "
              f"probe info: {err.splitlines()[0] if err else '-'})")
        print("kinit =", shutil.which("kinit"))


def write_krb5_conf(realm, kdc_hosts, kdc_port, spn_host, path=None):
    """Generate krb5.conf and set KRB5_CONFIG. Returns the conf file Path.

    dns_canonicalize_hostname=false is the key: the SPN middle segment
    hadoop.xxx is a fake domain that does not exist in DNS — the Kerberos
    client must be forbidden from resolving it and use it verbatim as the SPN.
    udp_preference_limit=1 forces AS/TGS requests onto TCP — matching the TCP
    ports probed earlier.
    """
    krb5_file = Path(path) if path else Path.cwd() / "krb5.conf"
    lines = [
        "[libdefaults]",
        f"    default_realm = {realm}",
        "    dns_canonicalize_hostname = false",   # <- the key
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
    os.environ["KRB5_CONFIG"] = str(krb5_file)   # read by both kinit and cyrus-sasl
    print(f"[OK] wrote {krb5_file} and set KRB5_CONFIG")
    return krb5_file


def kinit_user(username, realm, krb5_file):
    """kinit to obtain the user ticket (TGT, valid 24h). Skips if a valid
    ticket exists; otherwise prompts for the password via getpass.

    kinit may come from apt (krb5-user, /usr/bin) or conda (krb5,
    $CONDA_PREFIX/bin) — auto-detected.
    """
    import getpass

    kinit = shutil.which("kinit") or os.path.join(sys.prefix, "bin", "kinit")
    klist = shutil.which("klist") or os.path.join(sys.prefix, "bin", "klist")

    def _run(cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True,
                              env={**os.environ, "KRB5_CONFIG": str(krb5_file)}, **kw)

    r = _run([klist])
    if r.returncode == 0 and "krbtgt" in r.stdout:
        print("[OK] valid ticket already present, skipping kinit:")
        print("\n".join(r.stdout.splitlines()[:4]))
        return
    principal = f"{username}@{realm}"
    pw = getpass.getpass(f"Enter password for {principal}: ")
    r = _run([kinit, principal], input=pw + "\n")
    assert r.returncode == 0, f"[FAIL] kinit failed (wrong password / KDC unreachable?): {r.stderr.strip()}"
    print(f"[OK] kinit succeeded: {principal}")


def connect_hive(hive_host, hive_port, database, username, realm, spn_host, timeout_ms=30000):
    """Connect to HiveServer2 (the core: decouple the TCP address from the SPN).

    With auth=KERBEROS pyhive uses the TCP host as the SPN host -> guaranteed
    mismatch (it would ask the KDC for hive/<internal-IP>@REALM while the
    cluster registered a fixed-string SPN). The official escape hatch is
    thrift_transport=...: the TCP layer connects to the internal IP while the
    SASL layer gets the SPN middle segment as host. pyhive's
    get_installed_sasl automatically prefers cyrus's sasl package once
    installed (mandatory for qop=auth-conf).
    """
    from thrift.transport import TSocket
    from pyhive import hive
    from pyhive.hive import get_installed_sasl
    import thrift_sasl

    principal = f"hive/{spn_host}@{realm}"
    print("Trying SPN:", principal)

    def make_transport():
        tcp = TSocket.TSocket(hive_host, hive_port)
        tcp.setTimeout(timeout_ms)
        sasl_factory = lambda: get_installed_sasl(
            host=spn_host, sasl_auth="GSSAPI", service="hive")
        return thrift_sasl.TSaslClientTransport(sasl_factory, "GSSAPI", tcp)

    conn = hive.connect(thrift_transport=make_transport(),
                        database=database, username=username)
    print("[OK] connected! Effective SPN =", principal)
    return conn


def connect_mrs_hive(hive_host="10.0.0.15", hive_port=21066, database="default",
                     username="hhx", realm="252A63EC_2C90_4B5A_B4D7_17A3077B1CB8.COM",
                     kdc_hosts=("10.0.0.15", "10.0.0.51"), kdc_port=21732):
    """One-call MRS Hive connection (Kerberos-secured cluster): probe -> setup
    -> krb5 -> kinit -> connect.

    Defaults are the values measured on 2026-08-19 (single source of truth:
    hive_export/MRS_RUN.md §0). Fully idempotent: after ticket expiry or a
    kernel restart just re-run the cell — completed steps are skipped.
    """
    kdc_hosts = list(kdc_hosts)
    spn_host = "hadoop." + realm.lower()   # measured-correct SPN middle segment (the haddop_ variant is wrong)
    print(f"principal = hive/{spn_host}@{realm}")
    probe_network(hive_host, hive_port, kdc_hosts, kdc_port)
    setup_environment(spn_host)
    krb5_file = write_krb5_conf(realm, kdc_hosts, kdc_port, spn_host)
    kinit_user(username, realm, krb5_file)
    return connect_hive(hive_host, hive_port, database, username, realm, spn_host)


def fetch_breast_cancer(conn, table="breast_cancer", batch=5):
    """Read the whole Hive table, returning a DataFrame (columns keep Hive's
    underscored style, e.g. mean_radius).

    Pitfall (measured): pyhive defaults to arraysize=10000, so fetchall packs
    all 569 rows into one huge SASL-encrypted frame; some libsasl2 builds
    (known regression in 2.1.28) cannot decode large frames and raise
      TTransportException: sasl_decode ... Unable to find a callback: 32775
    Fix: fetch in small batches (default 5 rows) — the same reason LIMIT 5 in
    the conn notebook is stable.
    """
    import pandas as pd

    cur = conn.cursor()
    cur.arraysize = batch
    cur.execute(f"SELECT * FROM {table}")
    cols = [d[0].split(".")[-1] for d in cur.description]   # strip any db.table. prefix
    try:
        rows = cur.fetchall()
    except Exception as e:
        if "sasl_decode" in str(e) or "32775" in str(e):
            msg = f"""Small-batch fetch still triggers the sasl decode failure (libsasl2 2.1.28 known regression).
Fix: run the two lines below in a new cell, then Kernel - Restart Kernel and re-run from the top:
  import subprocess
  subprocess.run(['conda', 'install', '-y', '-c', 'conda-forge',
                  '--override-channels', 'libsasl2=2.1.27'], check=True)"""
            raise SystemExit(msg) from e
        raise
    finally:
        cur.close()
    print(f"fetched {len(rows)} rows")
    return pd.DataFrame(rows, columns=cols)
