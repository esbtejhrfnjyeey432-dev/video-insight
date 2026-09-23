# 智能成片辅助服务

该服务只负责二创模式的最终视频构建。原有课程复盘、原片拆解、脚本、分镜、提示词和导出流程均不依赖它；辅助服务关闭或失败时，原功能继续可用。

## 本地准备

1. 安装 Node.js 22.15 或更高版本。
2. 在项目目录执行 `npm install`，项目已锁定 `@hypit/hypit` 版本。
3. 启动辅助服务：`npm run start:builder`。
4. 启动 FastAPI 前设置 `VI_CREATIVE_BUILDER_ENABLED=true`。默认内网地址为 `http://127.0.0.1:3188`，可用 `VI_CREATIVE_BUILDER_URL` 修改。

启用后，二创工作台出现“06 / 智能成片”。浏览器只接触 VideoInsight API，不会看到或执行任何构建 CLI。

## 云端构建安全开关

云端配置从 `HYPIT_HUB_API_KEY` 环境变量读取凭据，不把密钥写入工程。设置 `HYPIT_RUNTIME=cloud` 后，服务会先运行费用查询并记录结果；只有服务端再明确设置 `HYPIT_PAID_AUTHORIZED=true` 才会继续付费构建。未授权时任务进入 failed，原二创成果保留。

本地配置只提供媒体处理、WhisperX 对齐和 Hyperframes 渲染。当前文本生成视频模板需要云端 Seedance 能力，因此本地配置用于工程校验和本地处理，不会伪装成已生成成片。

## 当前边界

- 输入只有纯文本 transcript 时，没有可靠的逐词时间戳；系统用语义选区绑定台词，但不能凭空伪造逐词时间。要输出准确 SRT，需把现有 ASR 结果的逐词时间一并传入。
- `remix.pptx`、`remix.pdf` 继续复用原应用的导出功能，不由成片引擎重复生成。
- Render 当前仍是纯 Python 服务。要在线启用，需要改为同时包含 Node、FFmpeg、WhisperX 和构建 CLI 的容器；在完成部署验证前不要打开功能开关。
