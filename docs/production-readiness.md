# 生产接入准备与业务闭环交付（2026-10-12）

本页保留该阶段的实现和验证记录。文中以文件名列出的内部回执未随社区版分发。[社区版验证范围](verification.md) · [在线合成演示](https://chumaniac.github.io/marine-fisheries-app/)

2026-11-03 当前源码修复与合成隔离证据见优先修复与验证（`reviews/2026-11-03-priority-fixes.md`）；本轮未做现场或生产验收。

最新用户功能验收见 [用户功能最终验收](user-acceptance.md)，包含本轮发现的页面遗漏、修复与浏览器回验。

本轮按用户调整后的标准交付：剩余软件功能必须形成“接入—处理—确认—异常处置—恢复”的闭环；需要真实机构、设备或云账号的细节，提供可执行的配置、检查和部署工具，并以隔离替身验证。当前是生产候选软件与接入包，未对未指定的云账号部署，也未将原业务效果指标写成已验收。

## 已落地的闭环

| 场景 | 正常流程 | 异常与恢复 |
| --- | --- | --- |
| 机构用户登录 | 页面按运行模式切换入口；OIDC Code + S256 PKCE → state/nonce 校验 → 有界加密服务端会话 → Secure/HttpOnly Cookie → 同源业务 API | 回调伪造/重放、nonce 错误、过期、未知成员被拒；每次请求重查成员/公钥；退出撤销服务端会话；生产禁用演示身份 |
| 部署启动 | 统一 `tools.preflight` → 已批准签名模型验签 → 单写入租约 → TLS 入口/API/worker/备份服务 | 配置不完整启动失败；第二写入实例被拒；缺 broker/备份等依赖时 `/ready` 为 503；`/health` 为进程级检查，不等于业务就绪 |
| 故障处理 | 运维页面查看租户告警 → 填写说明接手 → 原请求对账重试 → 实测检查恢复后关闭告警 | 接手不把故障改为正常；长期 busy 不刷新 ledger 成功心跳；单租户复核/监控异常不会杀死后续后台循环；保留最多 1000 条处置记录/租户 |
| 边缘观测 | 视频/相机 → 运动候选 → 分类 → 加密 crop + 持久设备序号 → 先存证据再接收事件 → 稳定 sample receipt | 修复 UTC `Z` 契约不匹配；网络未知重试不重造样本；设备/事件/证据绑定冲突拒绝；认证/格式错误保留为待处理 |
| 低置信度 | 本地保存 → 服务端复核池 → 人工确认或拒绝 → 事务内发布观测/标记拒绝 → 设备查询最终回执 | 未经人工确认不进入事件流；已发布标签不可静默覆盖；崩溃后重试同一 admission；没有在真实复核池代填标签 |
| IPFS 副本 | 复用已有密文/CID → 两个不同节点 ID 的递归 pin 和字节核验 | 已齐备副本直接复用；缺副本可补齐；已提交链状态不因副本故障倒退；副本位置不进入不可变链摘要 |
| 应用定期备份 | 独立服务按配置间隔生成一致应用快照 → 封装 AES-256-GCM → 解密复验 → 原子写成功回执 | 容量/磁盘保留量不足明确失败，保留上一份成功资产；不无限累积、不自动删除旧备份；恢复清除旧浏览器会话 |
| 整套灾备 | 停止已声明写入方 → Flink savepoint → 13 组件快照 + HMAC → 新目录/新卷隔离恢复 → 逐文件比较 → 业务与依赖核验 | 拒绝活跃写入者、路径逃逸、未完成包、已有目标和损坏密文；验收回执绑定快照摘要/代码版本并签名；最终切换仍要求原写入者被隔离和操作员批准 |
| 云端聚合 | 可重建 ZIP + CloudFormation：JWT 入口 → 只读 dashboard 聚合 → HTTPS 源站 | 4 秒源站超时、1MiB 上限、gzip、不缓存身份数据；并发/速率上限；可选 VPC 内网源站与告警 SNS 目标。写业务走同源 API，不把读聚合函数冒充全量网关 |

## 实际验证

- **40 项 Python、7 项前端测试通过**，包括生产登录、状态/nonce/会话撤销、租户隔离、busy 心跳、故障循环恢复、sample 证据绑定、复核后发布、加密恢复和双写入拒绝。事件契约 4 正例/9 反例、Ruff、前端构建通过。链码/JVM 本轮未改，其上轮 3 项 Go 测试和构建证据仍保留。Python 依赖的 219 条弃用警告未隐藏。
- **生产 Compose/TLS 网关配置、候选容器构建与断网烟测通过**：Linux ARM64、只读根文件系统、非 root 用户、真实签名 ONNX；生产演示登录拒绝，OIDC 跳转可建立，缺真实 broker/备份时不伪装 ready。容器证据（`evidence/production-container-check.json`）
- **实际边缘上传**：复用原合成运动片段，55 份加密图像与事件均被实际 API 接收，待投递归零。人工复核分支在隔离测试中走通；真实复核池没有代填标签。边缘证据（`evidence/edge-production-roundtrip.json`）
- **实际双副本**：复用既有已上链证据，两个本地 IPFS 节点核验成功，manifest 与原链摘要未变。副本证据（`evidence/ipfs-replica-check.json`）
- **实际整套恢复**：应用、边缘 SQLite/密文、模型、机构身份配置、Kafka、Flink、双 IPFS、双 peer 和 orderer 共 13 组件恢复到隔离目录/新卷，逐文件摘要一致；约 370MB 的加密封装完成解密复验。恢复的应用可读取既有 DELIVERED 批次及解密证据；原 Flink 从保存点恢复运行。灾备证据（`evidence/system-recovery-check.json`）
- **依赖接入检查通过**：原应用 ready、唯一运行中的 Flink 作业、所需 Kafka topics、Fabric 身份/通道、两个不同 IPFS 节点 ID。接入证据（`evidence/dependency-commission-check.json`）

这些证明软件路径和本地恢复能力。没有启动隔离副本中的第二套 Fabric/Kafka 集群接管生产，没有自动切换流量，也没有把人工填入的布尔值当成独立专家验收。当前高可用取舍是单写入者 + 受控备用恢复，目标 RPO 由备份频率决定，RTO 必须在目标环境测量；不是无中断多活。

## 生产部署顺序

1. 准备目标主机/架构、DNS/TLS、IdP 应用、授权成员、设备站点、Kafka TLS/ACL、Flink 与持久 checkpoint、Fabric MSP/通道，以及至少两个 IPFS 节点。现有网络已完成本机双组织验证，真实机构证书不能由演示 cryptogen 身份替代。
2. 从 `deploy/production/Dockerfile` 构建目标平台镜像并固定审阅后的 digest。生产镜像没有私钥、原图或权重；已签名模型和配置从只读目录挂载。`requirements.lock` 固定 36 个运行依赖，训练依赖不打入镜像。
3. 参考 `deploy/production/settings.example` 在 Git 外建立配置。密钥权限 0600，并保证容器 UID 10001 可读；数据和备份目录须可写。`MARINE_MODEL_PUBLIC_KEY_FILE` 是受信任发布者公钥，不能用待验证 bundle 自带的不可信公钥替代。当前签名模型仅批准用于公开数据试点，并非现场模型获批。
4. 为 Compose 提供 `MARINE_APP_IMAGE`、`MARINE_ENV_FILE`、数据/模型/配置/Fabric/TLS/备份目录与 `MARINE_HOSTNAME`。先运行 `docker compose -f deploy/production/compose.yml run --rm api python -m tools.preflight`。默认入口绑定 127.0.0.1:8443；公网开放需使用目标环境已经批准的入口。
5. 在授权环境启动 API、worker、gateway、backup。Kafka/Flink/Fabric/IPFS 是显式外部有状态依赖，不由应用模板擅自创建通道、改 ACL 或删除卷。所需 topics 见 `tools.commission.REQUIRED_TOPICS`；本地初始化脚本不能当作生产集群改造命令。Flink 仅提交一个作业，升级从已保存且兼容的 savepoint 恢复。
6. 执行 `python -m tools.commission --origin https://目标入口 --flink https://管理入口 --output 接入回执.json`。该工具只读检查，不修饰失败结果。浏览器通过真实机构登录后完成一个经授权的测试租户业务，再确认上线。

外部 OIDC ID Token audience 使用 client ID，设备/API Bearer audience 使用 `MARINE_OIDC_AUDIENCE`；两者不可混用。当前不申请 refresh token，会话最多一小时且不超过 ID Token 到期时间。公钥文件由运维管理轮换。API 禁用访问日志，网关模板不记录回调 query，避免授权码进入日志。协议参考：[OIDC Code Flow](https://openid.net/specs/openid-connect-core-1_0.html#CodeFlowAuth)、[RFC 7636 PKCE](https://www.rfc-editor.org/rfc/rfc7636.html)。

## 恢复与运维操作

- `python -m tools.backup_service --once`：一次应用加密备份并复验。Compose 服务默认每 86400 秒运行，最短 3600 秒；最多 30 个 bundle 后停止并要求归档。它不自动停机备份整个联盟链。
- `python -m tools.stack_recovery snapshot --config 资源清单.json --destination 新目录 --key-file 私有恢复密钥`：整套静止快照。清单必须同时包含 edge SQLite 与对应 `*-evidence` 目录；不得遗漏 MSP、账本、Kafka 日志、Flink savepoint 和 IPFS repo。
- `encrypt/decrypt` 使用上述工具的 `--source/--destination/--key-file`。HMAC 是完整性校验，AES-GCM 封装才提供保密性；离机保管使用 `.enc`，恢复密钥单独保管。历史本地明文快照仍在权限受限目录，未外传。
- `restore --volume-prefix marine-dr-唯一编号` 只创建隔离恢复资源，拒绝覆盖。失败留下 `INCOMPLETE` 供检查；修复后使用新目录/新 prefix 重试，旧成功资产不删除。回收失败副本前，核对所属恢复记录及 `marine.recovery=true` 标签，不运行全局卷清理。
- 完成应用、账本、checkpoint、IPFS 验证后，用 `sign-acceptance` 将检查结果绑定恢复目录和快照。`promotion-check` 验签、核对版本和原写入者隔离状态，仅返回能否受控切换；不执行启动或流量变更。当前演练回执的 `operatorApproved=false`，没有冒称生产切换批准。
- `python -m tools.edge_manage status|sync|retry --outbox ...` 查看队列、补传和恢复指定待处理 ID。样本最终状态通过 `/v1/edge-samples/{id}` 回读；未知结果保留原请求，不能改 ID 掩盖冲突。

## 仍由现场与接入方提供的事实

实际 IdP/域名证书、机构 MSP、云账号预算、生产网络/存储目的地和告警接收目标尚未提供，因此没有真实生产上线。云函数模板可选 VPC 和告警目标，但未创建任何云资源，限流也不等于硬性金额封顶。

真实海域模型精度、鱼类检测计数、功耗/热稳态、海上弱网、预测 85.4%、误判下降 60%、标注节省 40%、首屏 1.5 秒及降本 70% 仍须现场数据或可比基线。按本次允许的简化范围，固定机位运动候选、人工复核兜底、单写入受控恢复、只读云聚合已形成软件到业务的闭环，没有用这些简化替代现场验收结论。
