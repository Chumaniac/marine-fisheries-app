<img src="showcase/marine.svg" width="56" height="56" alt="" />

# 澜知海洋

[![Build](https://github.com/Chumaniac/marine-fisheries-app/actions/workflows/user-edition.yml/badge.svg?branch=main)](https://github.com/Chumaniac/marine-fisheries-app/actions/workflows/user-edition.yml) [![License](https://img.shields.io/badge/license-MIT-555)](LICENSE)

整理渔业采集批次，追踪质量问题并回看观测记录。

[在线试用](https://chumaniac.github.io/marine-fisheries-app/) · [桌面下载](https://github.com/Chumaniac/marine-fisheries-app/releases)

公开页面使用合成序列演示正常、弱网和离线状态。可切换鱼种或推进存证步骤，图表随操作更新。

趋势图的表格和 CSV 共用当前鱼种的 11 个时间点。预测行带示意上下限，历史行保留观测值。

## 核心功能

- 比较观测曲线、预测区间与批次状态。
- 在本机版登记采集任务，查看问题和处置记录。
- 导出记录供复核，并保留原批次的来源信息。

## 本机快速开始

前提：Python 3.12、uv。前端修改需要 Node 24；外部 JVM/Go 服务是进阶选项。

```sh
git clone https://github.com/Chumaniac/marine-fisheries-app.git
cd marine-fisheries-app
uv venv --python 3.12
uv pip sync --python .venv/bin/python requirements.local.lock
.venv/bin/python tools/start_app.py --no-open
```

浏览器只连接本机回环地址。桌面版下载与系统支持以 [Releases](https://github.com/Chumaniac/marine-fisheries-app/releases) 的实际资产为准；没有对应资产时从源码安装。桌面构建方式见 [构建说明](docs/build.md)。

## 操作步骤

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

## 常见问题

**能在海上直接使用网页版吗？** 网页提供合成样例。本机采集环境与配置见[运行说明](docs/local-runtime.md)，现场性能需要实际航次验证。

[参与贡献](CONTRIBUTING.md)

本机工作台的「下载当前地图 CSV」导出地图使用的聚合坐标、观测数和数据来源。切换筛选后需先刷新快照，未知的观测数留空。
