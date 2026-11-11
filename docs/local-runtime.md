# 本地系统运行说明

**已有演示交付环境请优先使用[演示交付指南](demo-delivery.md)**，不要重新执行本页的首次安装、网络初始化、数据播种或训练步骤。以下安装命令仅供新环境建设参考。

此版本用于闭源开发、集成验证与展示。默认端口只绑定回环地址，不能直接当公网部署模板。`showcase/` 是保留的初始合成展示；`web/` 才是连接实际 API 的应用。

## 环境与安装

需要 Python 3.12、Node 22+、pnpm、JDK 17+、Maven、Docker。设定 `JAVA_HOME` 为实际 JDK 目录。当前已在 Apple ARM64、Python 3.12.9、Node 24.19.0、JDK 26 上执行本地验证；JVM 产物以 Java 17 编译。Python 依赖精确版本见 `requirements.lock`，前端见 `web/pnpm-lock.yaml`。

在仓库根目录执行：

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.lock
cd web
pnpm install --frozen-lockfile
pnpm run build
cd ..
mvn -f jvm/pom.xml -Dmaven.repo.local=.runtime/m2 package
```

前端构建只使用本地依赖，不引用 CDN。若机器没有 Node/pnpm，须先安装官方运行时；仓库不修改用户全局运行时或权限。

## Kafka Flink IPFS

```sh
docker compose -f deploy/compose.yml up -d
.venv/bin/python deploy/initialize_topics.py
docker compose -f deploy/compose.yml exec -T -u 0 jobmanager chown -R flink:flink /opt/flink/checkpoints
docker compose -f deploy/compose.yml cp jvm/stream/target/stream-0.2.0.jar jobmanager:/tmp/marine-stream.jar
docker compose -f deploy/compose.yml exec -T jobmanager flink list -r
```

**只有没有运行中的本项目作业时**才提交；不要因重启 API 重复提交流作业：

```sh
docker compose -f deploy/compose.yml exec -T jobmanager flink run -d /tmp/marine-stream.jar
```

检查点在本项目专用卷中。上述本地恢复配置不是跨主机 JobManager HA；升级时保存 savepoint 并核验状态兼容，不能把重提作业称为无损生产升级。

本地 Kafka 为单 broker、复制因子 1；只允许通过回环端口访问。Flink sink 为 at-least-once，读模型以窗口键、单调计数与水位线防止旧重放覆盖，按分区持久化完成后提交 offset。当前Compose包含两个本地IPFS节点，没有公共bootstrap或swarm监听；本机双节点不等于跨主机灾备。

## Fabric 本地双组织网络

只使用本项目 `.runtime` 下的官方样例副本。验证时的 samples 提交为 `5789681b4f4d24e58fa40f19a69f5496892374b6`，Fabric 二进制与镜像为 2.5.12。

1. 从 [Fabric 官方发布](https://github.com/hyperledger/fabric/releases/tag/v2.5.12)下载匹配平台的工具包，解压到 `.runtime/fabric/`，使其包含 `bin/` 和 `config/`。
2. 将 [fabric-samples](https://github.com/hyperledger/fabric-samples) 克隆到 `.runtime/fabric-samples`，签出上述提交。
3. 执行下列约束脚本，再启动网络。该脚本把公开端口改为回环地址，移除 peer 的 Docker socket 挂载，并禁用官方样例的破坏性 cleanup。

```sh
.venv/bin/python deploy/prepare_fabric.py
docker pull hyperledger/fabric-peer:2.5.12
docker pull hyperledger/fabric-orderer:2.5.12
docker run --rm --memory 1g -v "$PWD/chaincode:/src" -w /src golang:1.24 sh -c 'go mod download && go mod vendor && go test -mod=vendor ./...'
cd .runtime/fabric-samples/test-network
COMPOSE_PROJECT_NAME=marine-fabric PATH="$PWD/../bin:$PATH" bash network.sh up -i 2.5.12
COMPOSE_PROJECT_NAME=marine-fabric PATH="$PWD/../bin:$PATH" bash network.sh createChannel -c trace-common
COMPOSE_PROJECT_NAME=marine-fabric PATH="$PWD/../bin:$PATH" bash network.sh deployCCAAS -c trace-common -ccn marine -ccp ../../../chaincode -ccv 1.2 -ccs 1 -ccep "AND('Org1MSP.peer','Org2MSP.peer')"
cd ../../..
.venv/bin/python deploy/create_private_channel.py
```

上面的 sequence 1 适用于**新网络**。当前已有开发网络为1.2 / sequence 3。已有网络先查询 committed 状态，不重复创建通道/证书，不盲目重部署。现有链码容器需要替换时，仅停止 `peer0org1_marine_ccaas` 和 `peer0org2_marine_ccaas`，不得运行全局 Docker 清理，也不得删除 peer 卷。

本地 `trace-common` 有 Org1/Org2 两组织、双方背书；`origin-audit` 仅 Org1 加入，已验证 Org2 不能读取。身份由 cryptogen 生成用于测试，不是生产 CA 入网、吊销或五方治理验收。应用使用 User1 交易身份而非 Admin 身份，CA/管理身份不暴露给浏览器。

## API 网关与界面

容器准备就绪后，在仓库根目录保持以下进程运行：

```sh
IPFS_REPLICA_APIS='["http://127.0.0.1:15002"]' MARINE_IPFS_MIN_REPLICAS=2 .venv/bin/python tools/run_local.py start
```

访问 `http://127.0.0.1:8871/`，点击“连接本地会话”。会话是回环环境的演示身份，不能用于公网。以下端口仅用于本机：

| 服务 | 端口 |
| --- | --- |
| Spring Cloud Gateway 与前端入口 | 8871 |
| FastAPI | 8870 |
| Kafka 外部本机 listener | 19092 |
| Flink 管理界面 | 18081 |
| IPFS API / 副本API | 15001 / 15002 |
| Fabric orderer / peer1 / peer2 | 7050 / 7051 / 9051 |

停止本项目原生服务：`.venv/bin/python tools/run_local.py stop`。该命令通过项目锁与自己的 supervisor PID 定位，不停止 Docker 容器。仅暂停本项目流组件可执行 `docker compose -f deploy/compose.yml stop`，保留所有卷。不要用 `down -v` 删除已有证据。

首次环境没有观测或预测，页面保持空白。需要展示合成闭环时，明确执行一次 `.venv/bin/python tools/seed_local.py`；它会写入合成事件和合成成熟标签。不要把该脚本用于现场数据租户，也不要在每次启动时重跑。界面会标明模型来源与过期预测。

API 契约见 [openapi.json](openapi.json)。Cookie 客户端先从 `/v1/session` 或 `/v1/me` 获取 `scopeHash`，写入时携带 `X-Marine-Scope`；该字段不是凭据，仍需有效会话。Bearer 客户端使用其绑定身份的令牌。缓存的离线身份租期不超过当前会话有效期（本地默认 1 小时），过期须重新认证。

观测与预测按站点、物种隔离。地理坐标可通过页面登记站点或配置 `MARINE_SITES_FILE` 提供；文件为包含 `tenantId/siteId/longitude/latitude/dataSource` 的数组，输出降到 0.1° 精度；未授权坐标不应填入。

## 真实图像模型

官方 [NorFisk 数据](https://doi.org/10.18710/H5G3K5) 为 CC0。下载原包到 `data/raw/NorFisk_v1.0.zip`，校验 MD5 `6ec799e74054dec2dadb6f9179e51ea5`，安全解压到 `data/raw/norfisk/`。原始图像与权重不进入 Git。

```sh
.venv/bin/python tools/prepare_norfisk.py
TORCH_HOME=.runtime/torch .venv/bin/python -m marine.learning --manifest data/processed/norfisk/manifest.csv --root data/processed/norfisk --trials 60 --epochs 12 --finetune-epochs 3
.venv/bin/python tools/export_embeddings.py
```

成功模型存在时训练命令会拒绝覆盖。启动应用直接加载成功 ONNX，不重训练、不重下载。新实验需要新的 `--output` 目录；已成功的原图、切分和权重继续保留。训练中断且尚未产出模型时，Optuna 数据库与特征缓存支持继续同一搜索。

目前部署为 FP32 ONNX，两种 INT8 候选都未通过质量门槛，不能仅因体积变小就替换。`python -m tools.quantize_model` 使用 300 张训练图校准，并在验证集比较逐通道 INT8；它复用已有候选文件，不更改服务主模型。

上传图像会进入有上限的加密人工复核池。复核必须填写真实的采集组，防止相邻帧跨切分。只有人工提交的已知标签可导出：

```sh
.venv/bin/python -m tools.export_reviews --tenant demo --output data/processed/review-round-01
```

输出目录必须为空。导出样本全部标为 train，必须与独立验证/测试清单组合后训练；不得把模型预测、自信程度或本次测试标签当作人工复核。

## 验证命令

```sh
.venv/bin/python -m pytest -q
.venv/bin/python tools/check_contracts.py
cd web && pnpm test && pnpm run build && cd ..
mvn -o -f jvm/pom.xml -Dmaven.repo.local=.runtime/m2 package
docker run --rm --memory 1g -v "$PWD/chaincode:/src" -w /src golang:1.24 go test -mod=vendor ./...
.venv/bin/python tools/verify_stream.py
```

最后一个命令向本项目 Kafka 写入独立 `stream-check` 租户的合成测试事件，验证去重和超迟到旁路。生产目标、外部扫描、付费生成及真实渔业作业不在这些命令中。

## 2026-10-12 新增软件的使用

当前完成证据以 [软件闭环交付](software-completion.md) 为准。本机 `trace-common` 已在用户明确批准后升级至 **1.2 / sequence 3**；不要再次执行旧版本部署示例覆盖它。

- 自动预测：API 生命周期每 60 秒检查标签，正常接入 `/v1/labels` 和事件即可，不需要调用训练接口。`/v1/forecast-models/{version}/promote` 只能选择已通过门禁的同租户模型，用于明确的操作员回滚。相同输入复用候选，拒绝的模型不替换现役模型。`/health` 可检查预测调度是否异常。
- 流转：页面可以创建、变更状态、发起/接收交接、登记加工和冷链；本地测试可选择捕捞或加工机构后重新连接。生产身份由成员注册表决定，不能通过这个演示入口取得。`/v1/ledger/status` 显示待提交/未知/已确认命令；`/v1/batches/{id}/ledger-verify` 实际核对账本。超时重复同步会查原回执，不修改请求内容碰运气。
- 离线文件每件 256KiB；恢复先同步同一依赖链的前置操作。冲突记录不会自动改写预期版本。需要用户核对后重试，或明确删除本地待处理记录；这不删除服务端记录。手工冷链上下限是该批次输入要求，系统没有编造通用食品安全温度。

外部身份模式设置 `MARINE_MODE=production`、`MARINE_OIDC_ISSUER`、`MARINE_OIDC_AUDIENCE`（默认 marine-api）、`MARINE_OIDC_JWKS_FILE` 和 `MARINE_MEMBERS_FILE`。JWKS 只含 RSA 公钥，使用 RS256；令牌中的 tenant/role 不取代成员表。成员文件按 subject 映射 `tenant`、`role`、必填 `organization`（Org1MSP 或 Org2MSP）、可选 `disabled/deviceId`。文件变更可撤销成员或轮换公钥；私钥不放进应用。操作员通过 `PUT /v1/devices/{id}` 注册 `siteId/enabled`，设备成员只能上传对应设备及站点。生产浏览器入口已提供OIDC Code + PKCE会话登录，具体配置见[生产接入准备](production-readiness.md)；外部设备/API仍使用已验证的Bearer，演示身份在生产模式禁用。

Kafka Python 客户端可通过 `KAFKA_CLIENT_CONFIG_FILE` 读取只含安全配置的 JSON（`security_protocol`、证书路径及所需 SASL 字段）；Flink 使用 `KAFKA_PROPERTIES_FILE` 的标准 Java properties。生产模式必须使用 SSL/SASL_SSL。ACL 由实际 broker 管理，不因提供配置入口就已部署。`FLINK_CHECKPOINT_URI` 默认指向专用命名卷；跨主机存储需按目标环境配置兼容插件和权限。

### 备份与恢复

```sh
.venv/bin/python -m tools.recovery backup --source artifacts/database --destination artifacts/recovery/new-backup --model artifacts/models/norfisk
.venv/bin/python -m tools.recovery verify --source artifacts/recovery/new-backup
.venv/bin/python -m tools.recovery restore --source artifacts/recovery/new-backup --destination artifacts/recovery/new-restored
```

目的目录必须不存在。备份包含解密密钥，按敏感材料管理，目录 0700、文件 0600，不提交 Git、不上传未授权目标。恢复先写临时目录并核验，再原子改名。该工具备份应用 SQL、引用的密文与所选模型；Fabric 的 peer/orderer 卷和 MSP、Kafka 日志、Flink 检查点仍是独立运行组件，不把应用备份称为整个联盟链灾备。

### 边缘与模型发布

```sh
.venv/bin/python -m tools.edge_capture --source /path/to/video.mp4 --model artifacts/models/norfisk --outbox artifacts/edge.sqlite3 --tenant demo --site site-A --device camera-A --frames 300 --clock-uncertainty-ms 50
```

`--source 0` 可选择已获授权的本机摄像头；网络上传另传 `--endpoint` 和 `--token-file`。时钟不确定度大于 5 秒会拒绝采集；队列满、认证错误待处理或磁盘不足会停止采集。运动候选仅适用于固定机位基线，不能保证每个运动物体都是鱼。真实设备功耗和温度未伪造。

`tools.model_release` 可在明确 `--approved` 后用原始 Ed25519 私钥文件打包签名；`install/activate` 使用固定公钥验签、核对哈希并检查 ONNX/预处理。最多保留 5 个版本，满后须显式归档。重选已安装版本即回滚，仍须通过签名和内容校验。`tools.calibrate_classifier` 只对新的版本目录执行；当前校准候选失败，已有 `calibration.json` 会拒绝重复拟合。观察 `/v1/monitoring` 获取漂移警告和复核耗时，不能将这些记录直接转写为业务效果成绩。

### 云函数与成本

`deploy/serverless/handler.py` 暴露 `handler`，接收 AWS HTTP API v2 格式事件。部署包需要锁定版本的 httpx 及其依赖，配置固定的 `MARINE_ORIGIN=https://...`；仅接受 GET `/v1/dashboard`，由源站执行身份验证。生产源站、账户与费用未指定，因此未上传或创建云资源。

`python -m tools.cost_report --usage usage.json --rates rates.json [--baseline 数值]` 使用键完全一致的用量与单价映射，拒绝负数/NaN。没有可比基线就返回空的节省率；不用示例价格代替真实账单。

独立验证工具：`tools.verify_operations`（恢复/签名/查询资源），`tools.verify_forecast_cycle`（新合成站点自动预测），`tools.verify_logistics`（稳定请求的双组织链路），`tools.verify_stream`（流去重和超迟到）。脚本里的现有 checkpoint/成功报告应先检查并复用，不为了重复截图重新造一轮业务。


## 2026-10-15 有界读取与投递恢复

- 服务端 outbox 每轮最多 100 条，每条自动尝试最多 8 次；耗尽后为 `action_required`，原 ID/payload 保留。运维页列出待核对事件（每页 100 条），操作员填写核查说明后重新尝试同一原请求，审计与重置同事务提交。待核对事件也计入受理积压限额。
- 浏览器离线队列最多自动尝试 8 次或 24 小时，永久冲突也保留等待人工处理。核对后重试会重置本轮预算，保持原请求 ID、依赖及账户范围；独立批次继续同步。投递确认仍以服务端回执为准。
- 仪表盘观测/轨迹总计使用完整机构、站点与鱼种范围；曲线最多 600 个时间点，窗口热区最多 480 条。批次最多显示 100 条，并返回 `hasMoreBatches`；窗口与站点覆盖范围在 `coverage` 中明确标注。
- 溯源每类每页 100 条，分别使用 `evidenceAfter`、`ledgerAfter`、`issueAfter` 游标。总数、冷链未解决数量及状态判断覆盖整个批次。前端翻页替换明细，重新查询回到首页；旧批次的链上核验响应不会覆盖新选择。
- 来源观测每页 500 条，`before` 游标继续读取，图像可用性只查询本页引用。已有来源选择时先完成操作或取消选择再翻页，防止悄悄丢失待提交选择。复核候选先筛选待审核，再在最早 1,000 条中抽样；全量计数和候选池覆盖独立返回。
- 预测最近 30 分钟的来源超过 5,000 条或流窗口超过 2,000 条时不产生新的截断预测，旧成功结果保留并沿用原有效期。大规模场景需要另行提高处理能力或拆分站点，不能把旧预测显示为新结果。

新增 GitHub `Product quality` 工作流使用锁定 Python/前端依赖与 CPU PyTorch，执行合成测试、契约、前端构建与提交产物一致性检查；不连接 Kafka、IPFS、Fabric 或真实设备。

浏览器复核使用独立临时数据库与合成记录：在仓库根目录设置 `PYTHONPATH=.`、`MARINE_SYNTHETIC_CHECK=1`、`QUALITY_PORT` 后运行 `tools/quality_browser_fixture.py`；另一个终端设置 `QUALITY_URL=http://127.0.0.1:<port>`，用 Node 运行 `web/tests/browser-quality.cjs`。需要本机已有 Chrome 和 Playwright，可用 `PLAYWRIGHT_MODULE` 指定已安装模块。脚本涵盖分页、全量计数、晚到响应、原请求重试和手机布局，结束后停止该临时服务。
