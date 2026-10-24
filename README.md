# Amo Draft PR · 新模块

这是给作者评审的 **Draft PR 文件集**，不是完整可运行应用。

## 内容

- `docs/AMO-CUSTOMIZATION.md`：改动说明与设计问题
- `PR-DESCRIPTION.md`：PR 描述
- `components/qq-bridge/src/core/amo-memory.js`：分层记忆模块（候选）
- `scripts/amo-memory-viewer.mjs`：记忆/日记查看器（候选）
- `extensions/amo-voice/`：语音、唱歌、RVC、分离脚本（可选 sidecar）

## 为什么不包含核心替换

当前安装目录是构建产物，与上游 `v0.4.0` 源码存在构建差异。
直接整包替换会回退上游已有的 assistant、feedback 等改动。

因此这个 Draft PR 只放 **新增模块**，核心接入点由作者决定是否接收。

## 使用

这些文件不能单独运行：

- `amo-memory.js` 需要接到 social bridge 的日记/提示词流程；
- `amo-memory-viewer.mjs` 需要控制台代理；
- `extensions/amo-voice` 需要 GPT-SoVITS、RVC、UVR、ffmpeg、虚拟声卡。

## 不含

模型、音频、配置、token、聊天记录、个人角色卡。
