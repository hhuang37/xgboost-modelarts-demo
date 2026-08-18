# XGBoost ModelArts Demo

Bilingual (zh-CN / en-US) glossary for the XGBoost breast-cancer online-inference demo. The canonical terms below must be used consistently in `README.md` and `README_EN.md`. Code literals (log tags, env-var names, mode strings) are part of the contract and are never translated.

## Language

**Hot swap** (零停机热切换):
Replacing the model object on OBS so the next inference request is served by the new model — no restart, no container rebuild. The flagship capability of this demo.
_Avoid_: live reload, model refresh, redeploy

**Hot reload** (热加载):
The service's internal act of re-loading a model file that changed underneath it; logged as `[hot-reload]`. The mechanism behind hot swap.
_Avoid_: using "hot swap" for this internal step

**Baked-in fallback model** (内置兜底模型):
The baseline model shipped inside the Docker image; served whenever OBS is unreachable or credentials are missing. Guarantees the service always starts.
_Avoid_: default model, embedded model

**Sync mode** (同步模式):
How the service discovers model updates; auto-detected from environment variables at startup, reason logged, current value reported by `/health` as `sync_mode`.

**OBS API mode** (OBS API 模式):
Sync mode that checks the cloud object before each request; the only mode that supports hot swap. Requires `OBS_BUCKET` + `AccessKeyID` + `SecretAccessKey` all set. Mode string: `obs-api`.
_Avoid_: OBS mode, cloud mode

**Local-signature mode** (本地签名模式):
Sync mode that watches the local model file's (mtime, size) and reloads on change; for storage-mount / `-v` mount scenarios. Mode string: `local-signature`.
_Avoid_: local mode, mount mode

**OBS probe** (探活):
The startup connectivity check against OBS; logs `[obs-probe] ok` or `[obs-probe] FAILED`.
_Avoid_: health check (that is `/health`), ping

**Model origin** (模型来源):
`/health` field `model_origin`: `obs` = currently serving model was synced from OBS; `baked` = from the image.
