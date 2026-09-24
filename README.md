# JR Music Studio

**面向制作人与 AI agents 的音乐创作工作台。** 从想法开始，与 Hermes 讨论方案、制作候选、对比试听、指挥修改，再由人决定 Accept / Reject / Rollback。

当前阶段：**Producer Console MVP**。已实际接通 **Hermes → 官方 Comfy MCP → ComfyUI → YuE2**，支持最高 300 秒生成。已验证 Windows JR 与 Linux Hermes，以及 RTX 3060 / RTX 4080 SUPER 生成路径；其他部署组合仍需验证。

## 已有能力

- 从零讨论并确认方案：默认由 agent 按专业 Skills 写歌词与 style，再由 YuE2 ABC planning 规划旋律并生成初稿；保留 agent 编写 ABC／专业规格的可选路径。
- 已有 style＋歌词可直接生成，无需先编写或导入 ABC；规划谱随音频归档，可另存编辑候选继续整曲、段落或局部修改。
- 候选对比与版本历史；整曲、段落、局部音高修改和保留约束。
- 乐谱、歌词配谱、谱面试听；可选音频转谱、歌词时间线与人工校正。
- 导出 **MIDI + MusicXML** 到外部乐谱软件或 DAW。
- 银月、小舞两个 Hermes 身份；任务阶段、实际模型回执、创作 JSON 检查与有限格式恢复。
- 原稿校验失败后可按诊断定向修复，保留歌词和原始记录；最多两轮，校验通过后继续原服务器生成。
- 从 Hermes 读取 MCP 列表，选择服务器及每位 agent 默认值；已提交任务固定目标。
- 29 个编曲与 18 个作词专业技能，冻结每个任务使用的版本。
- 听感检查与单轮修订：按 Terry 方法核对歌词负荷、前奏目标、结尾和段落发展，生成新候选后自动复测，再由制作人试听记录结论。

乐谱和 MIDI/MusicXML 表示保存的创作输入，**不保证与 YuE2 实唱逐音一致**。300 秒是上限，长度、演唱密度与音乐质量仍需试听确认。这是本地工作台，尚非可直接暴露公网的多用户服务。

## 快速开始

第一次使用？先看 **[中文图文新手攻略](guides/zh-CN/JR_Music_Studio_Beginner_Guide_CN.html)**，从新建歌曲、讨论方案到试听、修订与乐谱导出，附 13 张操作截图。

GitHub 文件页显示的是 HTML 源码。点击右上方 **Download raw file（下载原始文件）**，保存后用浏览器打开即可阅读；图片已嵌入，无需额外下载。更多说明见 [公开攻略目录](guides/README.md)。

需要 Python 3.13；音频生成与核验还需 FFmpeg / FFprobe、Hermes、官方 Comfy CLI / MCP、ComfyUI 及匹配的 YuE2 节点与模型。账号、模型与服务器权限不包含在仓库中。

```bash
git clone https://github.com/Goldlionren/JR-Music-Studio.git
cd JR-Music-Studio
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements-runtime.txt
python -m jr_music.server init-auth
python -m jr_music.server serve --console-port 8767
```

打开 [本机工作台](http://127.0.0.1:8767/)。上述命令启动工程与候选管理；**真实创作仍需配置 Hermes**。身份文件创建于 data/，重复 init-auth 不覆盖现有凭据。

按固定提交下载并验证专业技能：

```bash
python tools/fetch_music_skills.py
```

根据 [通用样例](config/hermes-deployment.example.json) 替换所有 REPLACE_ME 路径与 SSH 别名，另存 data/hermes-deployment.json。每个 Hermes 仅配置其自己的 agent token，不使用制作人 token；使用 SSH 隧道访问 JR 回环 API。

```bash
python tools/package_hermes_worker.py --output data/hermes-worker
python -m jr_music.server serve --console-port 8767 --hermes-config data/hermes-deployment.json
```

将 worker 包复制到 Linux Hermes 主机，以 Hermes 自身 Python 环境运行 producer_worker.py；其旁放置私有 client-config.json，参见 [样例](config/client.example.json)。HERMES_BIN 可指定 Hermes CLI。MCP 发现还需受限 SSH key、强制运行 mcp_broker.py，以及预先确认的主机指纹。该 key 仅探测／暂存；实际生成仍使用 Hermes 内官方 MCP。当前暂存支持 SSH→Windows Comfy 主机，HTTP MCP 与自动部署尚待完善。

可选歌词／波形检查依赖 requirements-audio-analysis.txt 和本地 Whisper 模型，在本机 CPU 上执行；转谱另需 SheetSage2 节点／模型。转谱路径主要针对 Windows 本机 ComfyUI，尚未统一多服务器路由。检查显示证据与可调目标，不把 ASR 字速当作说唱分类，也不自动判定音乐质量或采用候选。

## Suno 与后续方向

系统**具备扩展对接 Suno 的基础与可行性**：词曲意图和 ARR-SPEC / LYR-SPEC 可映射不同后端，[上游编曲技能](https://github.com/jtydhr88/music-composition-skills) 也有 Suno 输出指导。**当前没有 Suno 生产调用适配器**；鉴权、调用方式、费用、异步回执和归档需独立接入。

后续重点：听感检测的声学标定、独立 Critic 驱动的有预算多轮修订、个人审美验证、谱与实唱精确对齐、更多 MCP 兼容及安装运维产品化。完整 DAW／滚动钢琴优先使用外部软件。

## 测试

```bash
python -m unittest discover -s tests -v
```

公开测试使用合成样例，无需私人歌曲、服务器凭据或 GPU；通过测试不等于真实模型、声学对齐或音乐审美验收。

## 鸣谢

特别感谢 **Terry（Terry Jia，GitHub: jtydhr88）** 的专业音乐技能，作为本项目词曲制作的主要方法：

- [music-composition-skills（关联镜像）](https://github.com/Goldlionren/music-composition-skills) · [Terry 原仓库](https://github.com/jtydhr88/music-composition-skills)
- [lyric-writing-skills](https://github.com/jtydhr88/lyric-writing-skills)

感谢 Hermes Agent、ComfyUI / Comfy CLI、YuE2、SheetSage2、abcjs 及其他依赖项目。

## 开源协议

自有代码采用 **[MIT License](LICENSE)**：允许使用、修改、分发与商业使用，须保留版权及许可声明，按现状提供，不作担保。

第三方组件遵循各自许可，见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。JR 的 MIT 不改变外部服务条款、模型许可或歌曲素材权利。

本仓库发布应用代码、运行资源、公开测试，以及 guides/ 下单独授权公开的新手攻略（含示例截图）。内部设计说明、Low Level Design 文档、内部用户／维护手册、其他个人作品、实验、个人审美资料及部署凭据不随仓库发布。
