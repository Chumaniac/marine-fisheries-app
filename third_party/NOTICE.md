# 第三方资料与组件

项目自有源码保留权利，不改变第三方软件、数据和模型的许可。

- ECharts 与 zrender 使用 Apache-2.0；本目录保留许可文本，构建时保留 `app.js.LEGAL.txt`。来源：[Apache ECharts](https://github.com/apache/echarts)、[zrender](https://github.com/ecomfe/zrender)。
- NorFisk 原始数据由 NTNU 提供，官方页面列 CC0 1.0。引用：Maximiliano, Crescitelli Alberto, 2020, “NorFisk Dataset”, [doi:10.18710/H5G3K5](https://doi.org/10.18710/H5G3K5), DataverseNO, V1。原始图片不包含在本仓库中。
- EfficientNet-B0 使用 torchvision 官方 ImageNet 预训练权重，再作公开数据微调。框架许可不自动替代训练数据/权重适用条件；本仓库提供训练代码与报告，没有重新分发预训练权重。
- Fabric 样例取自 [hyperledger/fabric-samples](https://github.com/hyperledger/fabric-samples)，位于本地忽略目录，未复制为本项目自有代码。
- Python、Java 与 Go 依赖见各自锁文件或构建清单。该清单不是完整商业法律审查或漏洞扫描结论。

## Inter variable font

The Latin variable WOFF2 in `web/fonts/` is reused from the locally installed Genkoy @fontsource-variable/inter package. Inter is licensed under SIL Open Font License 1.1; the complete copyright and license are retained in `web/fonts/INTER-LICENSE.txt`. No external font CDN is used.
