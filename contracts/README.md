# 事件契约说明

`events.schema.json` 定义四种内部规范事件，`examples.json` 全部是合成数据；其中哈希与交易号只是格式占位，不是实际存证。

设备上传信封不信任客户端提供的 receivedAt、tenantId 或 siteId；接入服务从服务端时钟和认证映射重新生成/校验这些字段，再发布本 schema 定义的内部事件。模型权重、图像、证书和令牌不在事件中。

Schema 检查结构与部分取值，以下语义由服务强制实施：

- `lower ≤ point ≤ upper`；validUntil 晚于 originAt，sourceWatermark 不晚于 originAt。
- eventId 必须幂等；同 eventId 不同正文返回冲突。
- `classificationOnly` 不得当作轨迹计数，speciesId 和模型版本必须在相应注册表存在。
- 环境范围依设备校准表校验；无数值为 null，不用 0 表示缺测。
- trace.anchored 只能由核对过 VALID 提交事件的服务发布。
- 授权、证书、跨租户隔离、CID 与摘要实物校验不由 JSON Schema 证明。

初版采用严格字段集合；新增可选字段必须先更新消费者契约，再发布生产者。破坏性变更使用 v2。其他业务命令接口和 catch.labels 的完整可执行 schema 留待实现阶段。

执行 `python3 tools/check_contracts.py` 运行四个正例及针对不合法概率、未知事件、缺字段、非法时间、无效链提交、倒置区间等反例的检查。需要本地已有 `jsonschema` 库；本仓库不自动安装依赖。
