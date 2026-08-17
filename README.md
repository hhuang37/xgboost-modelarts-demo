# XGBoost ModelArts 统一推理镜像 — 内置兜底模型 + OBS 热切换

> 设计决策:单一镜像——模型打入镜像作兜底,OBS 热切换作运行时可选增强;OBS 任何失败只降级、不阻塞启动。

本目录是统一后的推理服务:**一个镜像同时跑通"自己的 MA"和"公司 MA"**。

- 模型打进镜像作**内置兜底**——OBS 连不上、凭证缺失,服务照样启动、照样推理;
- 配了 AK/SK 就自动启用 **OBS API 模式**,启动探活 + 每次请求前比对 OBS 对象大小,实现零停机**热切换**;
- 启动日志和 `/health` 明确回答"到底连上 OBS 没有"(本次改造的核心需求)。

与旧目录的关系:`0802_start_from_scratch/app_obs.py` 是代码基础(输入校验、热切换骨架原样保留);
`modelarts_minimal` 的镜像契约(ma-user 1000:100、gunicorn、baked model)是镜像基础。

## 文件清单

| 文件 | 角色 |
|---|---|
| `app.py` | 统一推理服务(模式自动检测 + 探活日志 + 兜底) |
| `Dockerfile` | python:3.11-slim + xgboost + esdk-obs-python + 内置模型 |
| `model/xgboost_breast_cancer.json` | 内置兜底模型(旧模型,100 棵树,91225B) |
| `model_mount/` | 本地 `-v` 挂载验证热切换用(内容=旧模型) |
| `sample_request.json` | 30 特征标准推理请求体 |
| `build_and_run.ps1` | 构建 / 本地两种模式验证 / 推 SWR 一键脚本 |

## 同步模式矩阵(自动检测,启动日志会说明原因)

| 环境变量 | 模式 | 行为 |
|---|---|---|
| `OBS_BUCKET` + `AccessKeyID` + `SecretAccessKey` 齐全 | `obs-api` | 启动探活 OBS 并下载/比对;每次请求前查 OBS 大小,不一致重下载重加载(**OBS API 模式**) |
| 任一凭证缺失,或 `OBS_HOT_RELOAD_DISABLE=true` | `local-signature` | 只看本地模型文件 (mtime, size),变化即重载(**本地签名模式**)。适用存储挂载 / 本地 `-v` |
| — 公共行为 — | | OBS 任何失败都降级到内置兜底模型并打 WARNING,**不阻塞启动**;只有"完全没有模型可服务"才让容器失败 |

其他变量:`MODEL_PATH`(默认 `/opt/model/xgboost_breast_cancer.json`,与内置路径一致)、
`OBS_ENDPOINT`(默认 cn-north-4,两套环境同区域)、`OBS_KEY`、`OBS_DOWNLOAD_FORCE`、`OBS_DOWNLOAD_TIMEOUT`。

## 怎么确认"启动时 OBS 连没连上"

1. **看启动日志**(ModelArts 服务日志里搜 `xgb-obs`):
   - `[startup] obs config: endpoint=... bucket=... key=... ak=FAKE****` — 打印解析后的配置,AK 掩码
   - `[startup] sync mode resolved: obs-api (OBS_BUCKET + AccessKeyID + SecretAccessKey configured)` — 进了哪种模式、为什么
   - `[obs-probe] ok status=200 remote_size=... latency_ms=...` 或 `[obs-probe] FAILED status=403 ...` — 探活结果
   - `[startup] OBS unreachable (status=...) — FALLING BACK TO BAKED-IN MODEL` — 降级兜底(醒目 WARNING)
2. **看 `/health`**:`sync_mode` / `sync_mode_reason` / `model_origin`(`obs` 或 `baked`)/ `obs.last_check_ok` / `obs.last_status_code` / `obs.error`。

## 本地验证(2026-08-17 已全部跑通)

```powershell
cd D:\soft\xgboos_demo\0817_new_dev

# 构建(--provenance=false 必须加,否则 SWR 拒收 OCI Index)
.\build_and_run.ps1                # 仅构建
.\build_and_run.ps1 test-local     # 本地签名模式:无凭证→兜底模型推理
.\build_and_run.ps1 test-obs -Ak <真AK> -Sk <真SK>   # OBS API 模式全链路
```

已验证的三条路径:

| 场景 | 结果 |
|---|---|
| 无任何 OBS 凭证启动 | `local-signature` 模式,内置模型推理正常(`/health.model_origin=baked`) |
| `-v model_mount:/opt/model` 挂载后换新模型再推理 | 预测 `0.0509 → 0.1181`,日志出现 `[hot-reload] local signature changed ... reloading` |
| 假 AK/SK 启动(真实网络打到 OBS) | 探活 403,WARNING 降级兜底,服务健康,`/health.obs.last_status_code=403`,每次请求持续重试 |

> 注意:`model_out` 下的模型文件是 2026-08-02 重新训练的,旧模型的预测值与
> 早期项目文档里记录的 `0.00926...` 不同(现为 `0.05085...`),
> 新模型值一致(`0.11806...`)。**热切换验证以"换模型前后预测值发生变化"为准,不要对抄绝对值。**

## 推送到 SWR 并部署

```powershell
docker login -u cn-north-4@<IAM账号> swr.cn-north-4.myhuaweicloud.com
.\build_and_run.ps1 push -SwrRepo swr.cn-north-4.myhuaweicloud.com/<组织>/xgb-bc
# 产出 tag:<组织>/xgb-bc:obs-minimal-v5-0817
```

### 公司 MA 部署要点(吸取 xgb-bc:obs0802v4 的教训)

1. **配置完全照抄 xgb-minimal:v1-0817 那次成功部署**(规格、调度策略、资源池、不配存储挂载)。
   新镜像不需要存储挂载——模型内置,AK/SK 走环境变量。
2. 环境变量:`OBS_BUCKET` / `AccessKeyID` / `SecretAccessKey`(均已确认公司环境允许明文注入)。
3. 健康检查:HTTP `GET /health`。
4. 部署后先看启动日志里的 `[obs-probe]` 行确认 OBS 连通,再验证推理。

### 热切换验证

替换 OBS 对象建议沿用"先 `deleteObject` 再 `putFile`"两步走(obsfs 的 mtime 坑),
验证工具随意(curl / Postman / notebook 均可),带上服务的公网调用 URL、Token 和 OBS AK/SK 即可。

## 排障:服务起不来 + `MountVolume.SetUp failed ... configmap "cm-infer-..." not found`

这是 **Kubernetes 平台层错误,发生在容器启动之前**——你的代码一行都没执行
(卷挂载失败 → Pod 起不来 → 没有应用日志)。与镜像内容无关(xgb-minimal 同环境能起、
xgb-bc:obs0802v4 不能起,差异在服务实例的编排,不在镜像字节)。

处理顺序:

1. **删除服务重建**——configmap 丢失常是平台 operator 偶发,重建即好;
2. 重建时**对照能起来的那次配置逐项核对**(规格 / 调度策略 / 副本数 / 资源池 / 存储挂载);
3. 仍失败 → 拿服务 ID + 事件报错原文找公司 MA 管理员或提华为工单(平台侧才能修)。
