# 运行与配置

## 本地模式

从仓库根目录运行命令。先按 [README](../README.md) 建立外置 Python 环境，并在 PowerShell 设置 `PYTHONPATH` 为 `src` 的绝对路径。以下 Windows 命令中的 `$python` 指向该环境的 `Scripts\python.exe`。

| 配置 | 默认或示例 | 用途 |
| --- | --- | --- |
| `project.output_root` | `./meetings` | 会议数据位置 |
| `asr.backend` | `faster_whisper` | 可改为 `qwen3_asr` |
| `asr.device` | `auto` | Whisper 优先尝试 CUDA，失败后使用 CPU |
| `asr.model` | `small` | Whisper 模型名称或已有权重目录 |
| `asr.qwen.chunk_seconds` | `2.0` | Qwen 流式推理块，范围 0.5–4 秒 |
| `asr.qwen.language` | `inherit` | 跟随会议语言，也可用 `auto`、`zh` 或 `en` |
| `SC_INSTALL_ROOT` | Windows 上为 `%LOCALAPPDATA%\DSH-meeting` | 外部模型、缓存和插件状态目录 |

## Qwen 流式后端

建议使用 Linux 或 WSL2 Ubuntu 和支持 CUDA 的 NVIDIA GPU。建议在 Python 3.12 环境运行，先用 `nvidia-smi` 验证 GPU。首次推理可能需要 GCC、G++ 和对应 Python 开发头文件来编译 Triton 内核。参考 [Qwen 官方推理说明](https://github.com/QwenLM/Qwen3-ASR#usage)。

在 Linux 或 WSL 中运行以下命令，并把 `cd` 的路径替换为该环境能访问的仓库根目录。

```bash
python3 -m venv "$HOME/.local/share/dsh-meeting/qwen-venv"
. "$HOME/.local/share/dsh-meeting/qwen-venv/bin/activate"
python -m pip install 'qwen-asr[vllm]==0.0.6'
hf download Qwen/Qwen3-ASR-0.6B --local-dir "$HOME/.local/share/dsh-meeting/models/Qwen3-ASR-0.6B"
cd /path/to/DSH-meeting
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 \
  python scripts/qwen_asr_service.py \
  --model-dir "$HOME/.local/share/dsh-meeting/models/Qwen3-ASR-0.6B"
```

看到 `ready on 127.0.0.1:8787` 后回到界面选 Qwen。服务会先预热流式推理，首次启动可能需要几分钟。宿主与推理服务须能经本机回环端口互通。若使用其它端口，同时改服务的 `--port` 和 Windows 配置中的 `asr.qwen.port`。

Whisper 和 Qwen 可以在同场会议切换。它们共用录音、时间轴和正式转录，但模型设置各自生效。Qwen 的实时草稿可能更新，界面只把话语结束后的定稿写进正式列表。

## DeepSeek Harness 集成

使用支持原生 Meetings 扩展接口的 DeepSeek Harness 桌面版。此集成使用当前宿主的工具和会话 SDK。建议从 Windows 终端运行安装器，安装后重启宿主。

```powershell
& $python scripts/install_dsh_meetings.py install
& $python scripts/install_dsh_meetings.py status
```

安装器使用外部状态目录，并管理 DSH 用户配置中的插件注册。DSH 用户数据目录可用 `--home` 指定，宿主 SDK 路径可以用 `--app-root` 指定。保存过自定义配置时，默认选用 `config/local.yaml`，也可用 `--config` 显式指定。

重新打开 Meetings 后即可使用会议面板。原生模型纪要需给该会议启用 `privacy.allow_cloud_notes`。人手在侧栏发起的文件变更走 DSH 单次审批。目标在工作区外时，给出绝对路径并核对宿主为本次操作请求的授权。

## 数据与网络

- 默认转录走本地模型。Whisper 权重和 Silero VAD 可先下载，随后在离线环境运行。Qwen 音频通过本机回环地址进入推理进程。
- 默认摘要使用本地抽取方式。选用兼容 API 或 DSH 原生模型生成纪要时，相关转录或附件会由配置的服务处理。是否允许使用远程模型由 `privacy.allow_cloud_notes` 控制。
- Azure 转录属于可选配置，需要独立凭据及 `privacy.upload_audio: true`。凭据使用环境变量或 Windows 本机凭据存储。
- 每场会议的原始录音和转录结果保存在 `project.output_root`。共享前应先确认该目录的同步和访问策略。仓库自带的界面截图只使用虚构数据。

## CLI 与 MCP

```powershell
& $python -m scientific_conference --help
& $python -m scientific_conference devices --fast
& $python -m scientific_conference start --title '示例会议' --consent
& $python -m scientific_conference sessions
& $python -m scientific_conference stop --session-id <SESSION_ID>
```

MCP 客户端可以用同一个 Python 作为命令，以 `-m scientific_conference.mcp_server --config <CONFIG_PATH>` 作为参数，设置工作目录为仓库根目录并传入 `PYTHONPATH`。使用模型或录音前先确认相关配置和授权。

## 开发测试

```powershell
& $python -m pytest -q
node --test dsh-meetings-plugin/tests/*.test.js
```

单元测试及接口测试使用合成数据和替身后端。真实 GPU、实际音频设备与 DSH 桌面集成需要对应运行环境。
