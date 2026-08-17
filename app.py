"""Unified XGBoost inference service for ModelArts custom images.

Single image, two-tier model strategy (docs/adr/0001):
  - a baked-in model ships inside the image, so the service ALWAYS starts,
    even when OBS is unreachable or credentials are absent;
  - when OBS credentials are configured, the service syncs the model from
    OBS at startup and before every request — replacing the OBS object
    takes effect on the next request (hot swap, zero downtime).

Sync mode is auto-detected from the environment and logged at startup:

  OBS API mode (sync_mode=obs-api)
      OBS_BUCKET + AccessKeyID + SecretAccessKey all set, and
      OBS_HOT_RELOAD_DISABLE is not "true". Startup probes OBS with
      getObjectMetadata and logs endpoint/bucket/key/AK-mask/status/
      latency. Any OBS failure downgrades to the baked-in model with a
      loud WARNING — it never blocks startup while a model file exists.

  Local-signature mode (sync_mode=local-signature)
      Credentials absent (or OBS_HOT_RELOAD_DISABLE=true). Watches the
      local model file's (mtime, size) signature and reloads on change.
      Use this with a ModelArts OBS storage mount (MODEL_PATH pointing
      into the mount) or a local docker -v mount.

Environment variables:
    MODEL_PATH       Local model path. Default: /opt/model/xgboost_breast_cancer.json
                     (the Dockerfile bakes the fallback model at exactly this path).
    OBS_ENDPOINT     OBS regional endpoint. Default: https://obs.cn-north-4.myhuaweicloud.com
    OBS_BUCKET       OBS bucket name. Enables OBS API mode when set with AK/SK.
    OBS_KEY          Object key. Default: models/xgboost_breast_cancer.json
    AccessKeyID      IAM user AK (OBS API mode)
    SecretAccessKey  IAM user SK (OBS API mode)
    OBS_HOT_RELOAD_DISABLE  "true" forces local-signature mode (default: false)
    OBS_DOWNLOAD_FORCE      "true" to re-download on every startup (default: false)
    OBS_DOWNLOAD_TIMEOUT    Seconds before giving up on an OBS call (default: 30)
"""
import logging
import math
import os
import time

import numpy as np
import xgboost as xgb
from flask import Flask, jsonify, request
from werkzeug.exceptions import BadRequest


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("xgb-obs")


OBS_ENDPOINT = os.environ.get(
    "OBS_ENDPOINT", "https://obs.cn-north-4.myhuaweicloud.com"
).rstrip("/")
OBS_BUCKET = os.environ.get("OBS_BUCKET", "")
OBS_KEY = os.environ.get(
    "OBS_KEY", "models/xgboost_breast_cancer.json"
)
AK = os.environ.get("AccessKeyID", "")
SK = os.environ.get("SecretAccessKey", "")
MODEL_PATH = os.environ.get(
    "MODEL_PATH", "/opt/model/xgboost_breast_cancer.json"
)
FORCE = os.environ.get("OBS_DOWNLOAD_FORCE", "false").lower() in (
    "1", "true", "yes", "on"
)
TIMEOUT = int(os.environ.get("OBS_DOWNLOAD_TIMEOUT", "30"))
HOT_RELOAD_DISABLED = os.environ.get(
    "OBS_HOT_RELOAD_DISABLE", "false"
).lower() in ("1", "true", "yes", "on")

MODE_OBS_API = "obs-api"
MODE_LOCAL_SIGNATURE = "local-signature"

# Resolved at startup by _resolve_sync_mode(); read-only afterwards.
SYNC_MODE = MODE_LOCAL_SIGNATURE
MODE_REASON = "not resolved yet"

# "baked" until a successful OBS sync proves otherwise; shown in /health.
_MODEL_ORIGIN = "baked"

# Last OBS probe/sync result, surfaced in /health so operators can tell
# whether OBS is reachable without reading container logs.
_OBS_STATE = {
    "last_check_ok": None,
    "last_check_at": None,
    "last_status_code": None,
    "remote_size": None,
    "error": None,
}

# Lazily-created OBS client (reused across requests).
_obs_client = None


def _mask_ak(ak: str) -> str:
    if not ak:
        return "<unset>"
    if len(ak) <= 4:
        return "****"
    return ak[:4] + "****"


def _sdk_available() -> bool:
    try:
        from obs import ObsClient  # noqa: F401
        return True
    except ImportError:
        return False


def _get_obs_client():
    """Return a singleton ObsClient, creating it on first call."""
    global _obs_client
    if _obs_client is None:
        from obs import ObsClient  # type: ignore
        _obs_client = ObsClient(
            access_key_id=AK,
            secret_access_key=SK,
            server=OBS_ENDPOINT,
            timeout=TIMEOUT,
        )
    return _obs_client


def _local_size() -> int:
    try:
        return os.path.getsize(MODEL_PATH)
    except OSError:
        return 0


def _record_obs_check(ok, status_code=None, remote_size=None, error=None):
    _OBS_STATE["last_check_ok"] = ok
    _OBS_STATE["last_check_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _OBS_STATE["last_status_code"] = status_code
    _OBS_STATE["remote_size"] = remote_size
    _OBS_STATE["error"] = str(error) if error else None


def _obs_metadata():
    """getObjectMetadata probe. Returns (ok, status_code, remote_size, error)."""
    start = time.monotonic()
    try:
        client = _get_obs_client()
        resp = client.getObjectMetadata(OBS_BUCKET, OBS_KEY)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        remote_size = int(getattr(resp, "contentLength", 0) or 0)
        if resp.status >= 300:
            err = (
                f"errorCode={getattr(resp, 'errorCode', '')}, "
                f"errorMessage={getattr(resp, 'errorMessage', '')}"
            )
            log.warning(
                "[obs-probe] FAILED status=%d %s latency_ms=%d "
                "endpoint=%s bucket=%s key=%s ak=%s",
                resp.status, err, elapsed_ms, OBS_ENDPOINT,
                OBS_BUCKET, OBS_KEY, _mask_ak(AK),
            )
            return False, resp.status, None, err
        log.info(
            "[obs-probe] ok status=%d remote_size=%d latency_ms=%d "
            "endpoint=%s bucket=%s key=%s ak=%s",
            resp.status, remote_size, elapsed_ms, OBS_ENDPOINT,
            OBS_BUCKET, OBS_KEY, _mask_ak(AK),
        )
        return True, resp.status, remote_size, None
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        err = f"{type(exc).__name__}: {exc}"
        log.warning(
            "[obs-probe] EXCEPTION %s latency_ms=%d endpoint=%s bucket=%s",
            err, elapsed_ms, OBS_ENDPOINT, OBS_BUCKET,
        )
        return False, None, None, err


def _download_from_obs() -> bool:
    """Download the model to MODEL_PATH (atomic tmp + replace).

    Returns True on success. Failures are logged, never raised — callers
    decide whether a fallback model keeps the service alive.
    """
    tmp_path = MODEL_PATH + ".tmp"
    start = time.monotonic()
    try:
        client = _get_obs_client()
        resp = client.getObject(OBS_BUCKET, OBS_KEY, downloadPath=tmp_path)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if resp.status >= 300:
            err = (
                f"status={resp.status}, "
                f"errorCode={getattr(resp, 'errorCode', '')}, "
                f"errorMessage={getattr(resp, 'errorMessage', '')}"
            )
            log.error(
                "[obs-download] FAILED %s latency_ms=%d bucket=%s key=%s",
                err, elapsed_ms, OBS_BUCKET, OBS_KEY,
            )
            return False
        os.replace(tmp_path, MODEL_PATH)
        log.info(
            "[obs-download] ok bytes=%d etag=%s latency_ms=%d -> %s",
            os.path.getsize(MODEL_PATH),
            getattr(resp, "etag", ""),
            elapsed_ms,
            MODEL_PATH,
        )
        return True
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        log.error("[obs-download] EXCEPTION %s bucket=%s key=%s",
                  err, OBS_BUCKET, OBS_KEY)
        return False
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _resolve_sync_mode():
    """Pick the sync mode from env, logging exactly why."""
    global SYNC_MODE, MODE_REASON
    if HOT_RELOAD_DISABLED:
        SYNC_MODE, MODE_REASON = MODE_LOCAL_SIGNATURE, (
            "OBS_HOT_RELOAD_DISABLE=true"
        )
        return
    missing = [
        name for name, value in (
            ("OBS_BUCKET", OBS_BUCKET),
            ("AccessKeyID", AK),
            ("SecretAccessKey", SK),
        ) if not value
    ]
    if missing:
        SYNC_MODE, MODE_REASON = MODE_LOCAL_SIGNATURE, (
            "missing env vars: " + ", ".join(missing)
        )
        return
    if not _sdk_available():
        SYNC_MODE, MODE_REASON = MODE_LOCAL_SIGNATURE, (
            "esdk-obs-python not installed in the image"
        )
        return
    SYNC_MODE, MODE_REASON = MODE_OBS_API, (
        "OBS_BUCKET + AccessKeyID + SecretAccessKey configured"
    )


def _startup_model_sync() -> None:
    """Resolve the sync mode, probe OBS, and ensure a model is on disk.

    Never raises while a usable model file exists (baked-in or mounted):
    OBS failures only downgrade sync. Raises RuntimeError only when there
    is no model to serve at all — in that case the container must not
    report healthy.
    """
    _resolve_sync_mode()

    local_bytes = _local_size()
    baked = local_bytes > 0
    log.info(
        "[startup] model file %s: %s",
        MODEL_PATH,
        f"present ({local_bytes} bytes)" if baked else "MISSING",
    )
    log.info(
        "[startup] obs config: endpoint=%s bucket=%s key=%s ak=%s "
        "force_download=%s timeout=%ss",
        OBS_ENDPOINT, OBS_BUCKET or "<unset>", OBS_KEY, _mask_ak(AK),
        FORCE, TIMEOUT,
    )
    log.info(
        "[startup] sync mode resolved: %s (%s)", SYNC_MODE, MODE_REASON,
    )

    if SYNC_MODE == MODE_OBS_API:
        ok, status_code, remote_size, error = _obs_metadata()
        _record_obs_check(ok, status_code, remote_size, error)
        if ok:
            if baked and remote_size == local_bytes and not FORCE:
                log.info(
                    "[startup] OBS object matches local file "
                    "(size=%d) — keeping it, no download needed",
                    remote_size,
                )
                _set_model_origin("obs")
            else:
                log.info(
                    "[startup] OBS size=%s vs local=%s → downloading",
                    remote_size, local_bytes,
                )
                if _download_from_obs():
                    _set_model_origin("obs")
                    _record_obs_check(True, status_code,
                                      _local_size(), None)
                elif baked:
                    log.warning(
                        "[startup] OBS download failed — FALLING BACK TO "
                        "BAKED-IN MODEL (%d bytes). Hot swap will retry "
                        "on the next request; check the errors above.",
                        local_bytes,
                    )
        elif baked:
            log.warning(
                "[startup] OBS unreachable (status=%s error=%s) — FALLING "
                "BACK TO BAKED-IN MODEL (%d bytes). Hot swap keeps "
                "retrying on every request; /health shows obs.last_check.",
                status_code, error, local_bytes,
            )
        if not baked and _local_size() == 0:
            raise RuntimeError(
                "No model available: OBS sync failed and no baked-in "
                "model file exists at MODEL_PATH="
                f"{MODEL_PATH}. Fix the OBS errors above (credentials / "
                "endpoint / network) or bake a model into the image."
            )
        return

    # Local-signature mode: the file must already exist (baked or mounted).
    if not baked:
        raise RuntimeError(
            f"No model available in local-signature mode: MODEL_PATH="
            f"{MODEL_PATH} does not exist. Either bake a model into the "
            "image, mount one (docker -v / ModelArts storage mount + "
            "MODEL_PATH), or set OBS_BUCKET + AccessKeyID + "
            "SecretAccessKey to enable OBS API mode."
        )
    log.info(
        "[startup] local-signature mode: watching (mtime, size) of %s — "
        "swap the file (or replace the mounted OBS object) to hot-swap",
        MODEL_PATH,
    )


def _set_model_origin(origin: str) -> None:
    global _MODEL_ORIGIN
    _MODEL_ORIGIN = origin


# ----- Hot reload -----

_LAST_MODEL_SIG: list = [None]  # (mtime, size) of last loaded model


def _local_model_signature():
    """Return (mtime, size) tuple for the local model file, or None if missing."""
    try:
        stat = os.stat(MODEL_PATH)
        return (stat.st_mtime, stat.st_size)
    except OSError:
        return None


def _reload_local_if_changed() -> None:
    """Local-signature mode: check (mtime, size); reload if changed."""
    sig = _local_model_signature()
    if sig is None:
        log.warning("[hot-reload] model file missing at %s", MODEL_PATH)
        return
    if _LAST_MODEL_SIG[0] == sig:
        return  # unchanged
    log.info(
        "[hot-reload] local signature changed %s -> %s, reloading",
        _LAST_MODEL_SIG[0], sig,
    )
    booster.load_model(MODEL_PATH)
    _LAST_MODEL_SIG[0] = sig
    log.info(
        "[hot-reload] complete: size=%d, rounds=%d",
        sig[1], booster.num_boosted_rounds(),
    )


def _sync_model_from_obs_if_needed() -> None:
    """Before each inference, ensure the latest model is loaded.

    OBS API mode: query the OBS object size; if it differs from the local
    file, re-download and reload. Metadata failures degrade to the current
    model (logged, surfaced in /health) — inference never fails because of
    the sync check itself.
    """
    if SYNC_MODE != MODE_OBS_API:
        _reload_local_if_changed()
        return

    local_bytes = _local_size()

    ok, status_code, remote_size, error = _obs_metadata()
    _record_obs_check(ok, status_code, remote_size, error)
    if not ok:
        return  # keep serving the current model

    if remote_size == local_bytes and local_bytes > 0:
        _set_model_origin("obs")
        return  # fast path: same model

    log.info(
        "[hot-reload] OBS size=%d vs local=%d → re-downloading",
        remote_size, local_bytes,
    )
    if _download_from_obs():
        booster.load_model(MODEL_PATH)
        _LAST_MODEL_SIG[0] = _local_model_signature()
        _set_model_origin("obs")
        log.info(
            "[hot-reload] complete: size=%d, features=%d, rounds=%d",
            _local_size(), len(feature_names),
            booster.num_boosted_rounds(),
        )
    else:
        log.error("[hot-reload] download failed, keeping current model")


_startup_model_sync()

booster = xgb.Booster()
booster.load_model(MODEL_PATH)
_LAST_MODEL_SIG[0] = _local_model_signature()

feature_names = list(booster.feature_names or [])
if not feature_names:
    raise RuntimeError("The model does not contain feature_names.")

app = Flask("xgb-breast-cancer")
log.info(
    "[startup] model ready: xgboost=%s, features=%d, rounds=%d, "
    "origin=%s, sync_mode=%s",
    xgb.__version__,
    len(feature_names),
    booster.num_boosted_rounds(),
    _MODEL_ORIGIN,
    SYNC_MODE,
)


class InputError(ValueError):
    pass


def parse_rows(payload):
    if not isinstance(payload, dict):
        raise InputError("Request body must be a JSON object.")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise InputError("'data' must be a JSON object.")

    req_data = data.get("req_data")
    if not isinstance(req_data, list) or not req_data:
        raise InputError("'data.req_data' must be a non-empty array.")

    expected = set(feature_names)
    rows = []

    for row_index, record in enumerate(req_data):
        if not isinstance(record, dict):
            raise InputError(
                f"data.req_data[{row_index}] must be a JSON object."
            )

        provided = set(record)
        missing = [name for name in feature_names if name not in provided]
        unexpected = sorted(provided - expected)

        if missing or unexpected:
            raise InputError(
                f"data.req_data[{row_index}] has invalid fields; "
                f"missing={missing}, unexpected={unexpected}"
            )

        values = []
        for name in feature_names:
            raw_value = record[name]
            if isinstance(raw_value, bool):
                raise InputError(
                    f"data.req_data[{row_index}].{name} must be numeric."
                )
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise InputError(
                    f"data.req_data[{row_index}].{name} must be numeric."
                ) from exc
            if not math.isfinite(value):
                raise InputError(
                    f"data.req_data[{row_index}].{name} must be finite."
                )
            values.append(value)

        rows.append(values)

    return np.asarray(rows, dtype=np.float32)


@app.get("/health")
def health():
    if _MODEL_ORIGIN == "obs":
        model_source = f"obs://{OBS_BUCKET}/{OBS_KEY}"
    else:
        model_source = f"baked-in:{MODEL_PATH}"
    return jsonify(
        {
            "status": "ok",
            "xgboost_version": xgb.__version__,
            "feature_count": len(feature_names),
            "model_source": model_source,
            "model_origin": _MODEL_ORIGIN,
            "model_path": MODEL_PATH,
            "sync_mode": SYNC_MODE,
            "sync_mode_reason": MODE_REASON,
            "obs": {
                "endpoint": OBS_ENDPOINT,
                "bucket": OBS_BUCKET,
                "key": OBS_KEY,
                "last_check_ok": _OBS_STATE["last_check_ok"],
                "last_check_at": _OBS_STATE["last_check_at"],
                "last_status_code": _OBS_STATE["last_status_code"],
                "remote_size": _OBS_STATE["remote_size"],
                "error": _OBS_STATE["error"],
            },
        }
    )


@app.post("/")
def infer():
    if not request.is_json:
        raise InputError("Content-Type must be application/json.")

    payload = request.get_json()
    rows = parse_rows(payload)
    _sync_model_from_obs_if_needed()  # check for model changes before predicting
    matrix = xgb.DMatrix(rows, feature_names=feature_names)
    probabilities = booster.predict(matrix)

    return jsonify(
        [
            {"predictresult": float(probability)}
            for probability in probabilities
        ]
    )


@app.errorhandler(InputError)
def handle_input_error(error):
    return jsonify({"error": str(error)}), 400


@app.errorhandler(BadRequest)
def handle_bad_json(error):
    return jsonify({"error": "Request body must be valid JSON."}), 400


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.exception("unhandled inference error")
    return jsonify({"error": "Internal inference error."}), 500
