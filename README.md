# 澜知海洋 · 独立用户版

界面与首次使用引导默认使用中文；产品名、命令、文件名和原始业务内容保留原文。

在本机整理渔业采集记录、质量问题与分析结果。

本地 SQLite 与可追溯数据契约连接采集、质量核对、分析和导出；缺少模型或外部链路时明确标注，不伪造分类或上链回执。

这是包含完整可构建源码的社区用户仓库，不依赖原私有仓库。旧仓库的提交历史、用户数据、模型缓存和内部验收记录不在这里。

## 开始使用

前提：Python 3.12、uv。前端修改需要 Node 24；外部 JVM/Go 服务是进阶选项。

```sh
git clone https://github.com/Chumaniac/marine-fisheries-app.git
cd marine-fisheries-app
uv venv --python 3.12
uv pip sync --python .venv/bin/python requirements.local.lock
.venv/bin/python tools/start_app.py --no-open
```

浏览器只连接本机回环地址。桌面版下载与系统支持以 [Releases](https://github.com/Chumaniac/marine-fisheries-app/releases) 的实际资产为准；没有对应资产时从源码安装。桌面构建方式见 [构建说明](docs/build.md)。

## 用一次完整流程

建采集任务 → 填写/导入记录 → 质量核对 → 分类与分析 → 教师/业务复核 → 导出

1. 启动后打开终端给出的本机地址。先用合成示例建立采集记录，再录入自己的获授权资料。
2. 核对缺失字段、单位与质量提示。努力量缺失的记录不能解释成可靠 CPUE；图像分类也不能替代真实捕捞数量/重量。
3. 模型识别需要匹配的 ONNX 与校准 report.json；缺少时使用手动录入/核对流程。完成复核后导出记录和依据。

```sh
.venv/bin/python tools/start_app.py --check-env
.venv/bin/python tools/check_contracts.py
```

## 保存与边界

macOS：~/Library/Application Support/MarineFisheries/workspace；其他系统：~/.local/share/marine-fisheries/workspace。数据库、签名密钥、模型与运行记录不进入 Git。

发布包不含原始鱼类图片、训练集、预训练/微调权重、生产身份配置或 Fabric 私钥。Kafka/Flink/IPFS/Fabric/OIDC 需要独立部署与验收；本地台账不是区块链上链。模型精度、真实渔场效果和生产吞吐量未由源码安装证明。

- [完整使用说明](docs/user-guide.md)
- [隐私、备份与恢复](docs/privacy.md)
- [排错与环境检查](docs/troubleshooting.md)
- [已验证范围](docs/verification.md)
- [参考项目与许可](docs/references.md)

许可证见 [LICENSE](LICENSE)，第三方资料见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
