# 开源生态与就业市场调研

检索日期：2026 年 10 月 9 日。方法：读取官方项目文档、GitHub 仓库元数据与默认分支提交、雇主招聘页面、政府文件及原始论文。这里是支持技术选型的定性样本，不是全市场招聘统计或热门榜排名。

## 开源仓库快照

以下九个仓库在查询时均未归档。SHA 是当日默认分支观察点，用于溯源资料，不是生产依赖锁定；不能把默认分支直接当稳定发布版。GitHub `pushed_at` 可能涵盖非默认分支，本表使用实际读取的默认分支提交。

| 项目 | 观察提交 | GitHub 识别的许可证 | 本方案用途 |
| --- | --- | --- | --- |
| [pytorch/vision](https://github.com/pytorch/vision) | [9a8d5453](https://github.com/pytorch/vision/commit/9a8d5453bdbcd882f7c7f401064b7514581636c3) | BSD-3-Clause | 官方 B0 与迁移学习入口 |
| [opencv/opencv](https://github.com/opencv/opencv) | [0f0b88d9](https://github.com/opencv/opencv/commit/0f0b88d9263aec8c26ce7a61bc71a1c8c37f1693) | Apache-2.0 | CLAHE、滤波、图像处理 |
| [optuna/optuna](https://github.com/optuna/optuna) | [35b97d82](https://github.com/optuna/optuna/commit/35b97d82441d171d19222aec3cc0bea52a747b8a) | MIT | TPE、剪枝、试验追踪 |
| [apache/flink](https://github.com/apache/flink) | [687ef8c4](https://github.com/apache/flink/commit/687ef8c43630a1691ba2d87c6430635adce31b86) | Apache-2.0 | 事件时间、窗口与有状态流 |
| [apache/kafka](https://github.com/apache/kafka) | [dca38163](https://github.com/apache/kafka/commit/dca38163e3732755f8d916e17ee4bbaeb6cba9f1) | Apache-2.0 | 持久事件日志与重放 |
| [apache/echarts](https://github.com/apache/echarts) | [34f4d927](https://github.com/apache/echarts/commit/34f4d927eafe251f26f63afd3aeefa30f2feba30) | Apache-2.0 | 生产仪表盘图表 |
| [hyperledger/fabric](https://github.com/hyperledger/fabric) | [1f06f620](https://github.com/hyperledger/fabric/commit/1f06f620a21dbd7089dca01fd54b0fbbc5798d5a) | Apache-2.0 | 联盟链与多组织背书 |
| [ipfs/kubo](https://github.com/ipfs/kubo) | [833e04a2](https://github.com/ipfs/kubo/commit/833e04a240d8c533509ddaaa3c202b39b0bb3024) | API 为 NOASSERTION，原文另核 | 私有内容寻址存储 |
| [spring-cloud/spring-cloud-gateway](https://github.com/spring-cloud/spring-cloud-gateway) | [c5021b65](https://github.com/spring-cloud/spring-cloud-gateway/commit/c5021b656f3ae7c45adc842db106ed90c778bcc6) | Apache-2.0 | 服务入口、认证、路由、限流 |

Kubo 的 [LICENSE](https://github.com/ipfs/kubo/blob/833e04a240d8c533509ddaaa3c202b39b0bb3024/LICENSE)说明其从 MIT-only 向 MIT/Apache-2.0 双许可过渡，部分历史贡献仍为 MIT-only。NOASSERTION 表示自动识别未给出 SPDX，不能解释成“无许可限制”。许可证核查只覆盖顶层说明，未完成依赖 SBOM 或商业法律审查。

这些项目能形成成熟组件组合，系统价值主要在真实数据质量、可恢复事件契约、设备运维与可信边界。没有用 GitHub 星标数量替代生产适用性，也没有引入未经核验的所谓“海洋 AI 一键系统”。

## 版本与依赖决策

官方文档页面当日可见 OpenCV 4.13.0、Gateway 5.0.3、Flink stable 跳转的 2.3 文档和 Optuna stable 的 5.0.0 文档；这些是分别观察到的文档版本，不构成相互兼容的部署组合。OpenCV 默认开发分支当日为 5.x，尤其不能将其和 4.x 文档混作同一个发布物。

实施第一步固定受支持的发布版、镜像 digest 和锁文件：PyTorch/torchvision/ONNX/设备执行后端成一组；Spring Boot/Spring Cloud BOM/JDK 成一组；Flink/Kafka connector/client broker 成一组；Fabric peer/orderer/CA/Go contract API 成一组。分别运行契约、导出、恢复和升级测试后再定生产版本。本设计没有声称已经完成这张兼容性矩阵。

闭源项目可以采用符合条件的宽松许可组件，但须保留 LICENSE/NOTICE、修改说明及第三方权利。数据、模型权重、地图和代码分别检查许可证，不从框架许可推导数据可商用。首期不为了检测功能直接引入未知商业许可边界的模型发行包。

## 就业证据

| 来源与读取结果 | 可支持的判断 | 不能支持的判断 |
| --- | --- | --- |
| [Aquabyte Embedded Software Engineer](https://jobs.lever.co/aquabyte/ecff34ad-d659-4da1-af8d-0266103ba9bf)，正文可读，旧金山湾区混合办公 | 相机/传感器、ARM/Linux、多语言开发、低带宽和现场可靠性具有实际岗位需求 | 中国招聘量、面向应届生的普适门槛、所有远程地点可入职 |
| [AKVA Observe Product Manager](https://www.akvagroup.no/stilling/?id=565)，截止 2026-10-11，挪威 | 企业正在整合摄像头、AI、实时数据与精准投喂产品 | 这不是算法工程职位，不能算作算法招聘增长数量 |
| [AKVA 招聘列表](https://www.akvagroup.no/karriere/ledige-stillinger/)，读取时包含数字产品技术支持等岗位 | 系统运营与支持也属于海洋数字产品交付 | 动态列表不是长期稳定空缺，不能保证申请时仍开放 |
| [WEF Future of Jobs 2025](https://www.weforum.org/publications/the-future-of-jobs-report-2025/digest/)，2025-01-07 发布 | AI/大数据、软件与安全技能有跨行业增长预期 | 不是 2026 年中国海洋渔业招聘普查，也不是职位保证 |
| [农业农村部 2026 应用场景征集](https://scs.moa.gov.cn/gdxw/202604/t20260420_6487519.htm)，2026-04-20 | 精准养殖、智能预测、供应链调度与溯源是政策关注场景 | 政策文件不等于已形成同等规模岗位需求 |

交叉检索还发现工业视觉岗位的雇主搜索摘要，但对应页面正文未能稳定读取，未把摘要作为当前岗位要求的独立强证据。区块链领域找到的部分招聘线索较旧或非 Fabric，不纳入增长结论。没有给出无依据的岗位同比、薪资中位数或录用概率。

本项目的就业排序是基于上述样本与技能可迁移性的**设计判断**：优先建立 CV/边缘或实时数据的深度，再用前端产品交付与联盟链治理扩展。若面向中国具体城市求职，后续应另采集去重的 30–50 个有效职位，记录发布时间、地域、资历、职责与投递反馈，才能形成当地量化结论。

## 技术原始来源

每项来源仅支持所列技术事实。方案中的参数、成本、SLO 和性能数字由本项目定义，不能归因给这些来源。

| 来源 | 支持内容 |
| --- | --- |
| [Fish4Knowledge Ground Truth](https://groups.inf.ed.ac.uk/vision/DATASETS/FISH4KNOWLEDGE/WEBSITE/GROUNDTRUTH/) | 鱼类识别与检测研究数据入口，使用权另核 |
| [FishNet 研究项目](https://fishnet-2023.github.io/) | 鱼类识别、检测与性状研究方向，不保证本地海域适配 |
| [torchvision EfficientNet](https://docs.pytorch.org/vision/main/models/efficientnet.html) | 模型构建入口 |
| [torchvision B0 源码定义](https://docs.pytorch.org/vision/2.0/_modules/torchvision/models/efficientnet.html) | 原始 B0 参数规模与权重预处理定义；该链接为带版本源码 |
| [OpenCV CLAHE](https://docs.opencv.org/4.13.0/d5/daf/tutorial_py_histogram_equalization.html) | 自适应直方图均衡 |
| [Focal Loss 原论文](https://arxiv.org/abs/1708.02002) | 易样本降权机制；原研究面向密集检测 |
| [Optuna TPE](https://optuna.readthedocs.io/en/stable/reference/samplers/generated/optuna.samplers.TPESampler.html) | TPE 搜索机制与参数接口 |
| [ONNX Runtime 量化](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html) | 静态量化、校准与精度检查 |
| [Gateway Server WebFlux](https://docs.spring.io/spring-cloud-gateway/reference/spring-cloud-gateway-server-webflux.html) | Gateway 官方组件与版本入口 |
| [Kafka Design](https://kafka.apache.org/41/design/design/) | 分区、持久日志及交付语义；链接明确 4.1 文档 |
| [Flink Generating Watermarks](https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/event-time/generating_watermarks/) | 事件时间、水位线、idle source 与对齐 |
| [Conformal prediction beyond exchangeability](https://arxiv.org/abs/2202.13415) | 非交换条件下区间保证需要额外分析 |
| [ECharts Canvas 与 SVG](https://echarts.apache.org/handbook/en/best-practices/canvas-vs-svg/) | 渲染器权衡 |
| [Web Vitals](https://web.dev/articles/vitals) | LCP/INP/CLS 度量；1.5 秒是本项目更严格目标 |
| [Fabric MSP](https://hyperledger-fabric.readthedocs.io/en/latest/membership/membership.html) | MSP、证书信任与 NodeOUs |
| [Fabric Endorsement](https://hyperledger-fabric.readthedocs.io/en/latest/endorsement-policies.html) | 背书策略与状态级约束 |
| [Fabric Private Data](https://hyperledger-fabric.readthedocs.io/en/latest/private-data/private-data.html) | 通道内私有数据集合与哈希 |
| [IPFS Privacy](https://docs.ipfs.tech/concepts/privacy-and-encryption/) | 内容隐私与加密边界 |
| [IPFS CIDs](https://docs.ipfs.tech/concepts/content-addressing/) | 内容标识符与文件哈希的差别 |
| [RFC 8785](https://datatracker.ietf.org/doc/html/rfc8785) | JSON 规范化，属于 Informational RFC |
| [个人信息保护法](https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm) | 最小必要、个人信息权利与安全义务 |
| [区块链信息服务管理规定](https://www.cac.gov.cn/2019-01/10/c_1123971164.htm) | 区块链信息服务的适用性与管理要求 |

实际部署时重新确认法规、版本与招聘页面。此调研没有代表任何机构完成法律审查或安全签核。
