# 环境配置指南

本文档适用于 Windows、macOS 和 Linux。因为本项目已包含 `requirements.txt` 和 `pyproject.toml`，通常不需要手动创建额外的 `setup.py`。

## 1. 创建并激活虚拟环境

### Windows PowerShell

```powershell
# 进入项目目录
cd C:\Users\jblix\Desktop\Clawd-Code

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 确认 Python 版本
python --version
# 应该显示 Python 3.10.x / 3.11.x / 3.12.x
```

如果 PowerShell 提示“脚本被策略阻止”，请执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### macOS / Linux

```bash
cd /path/to/Clawd-Code
python3 -m venv .venv
source .venv/bin/activate
python --version
```

## 2. 安装项目依赖

在虚拟环境中执行：

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# 或者
# python -m pip install -e .
```

## 3. 从任意项目目录运行 `clawd`（Windows）

项目已经定义了 `clawd` 命令入口。若希望不必先激活项目的 `.venv`，并能在任意仓库目录直接运行命令，可使用 `pipx` 安装本项目：

```powershell
# 安装 pipx（只需一次）
py -m pip install --user pipx
py -m pipx ensurepath
```

关闭并重新打开 PowerShell，然后从 Clawd-Code 项目目录运行：

```powershell
cd C:\Users\jblix\Desktop\Clawd-Code
pipx install --editable .
```

安装完成后，在任意目录运行：

```powershell
clawd login
clawd
```

`clawd` 会把你启动命令时所在的目录作为当前工作区。例如在 `D:\my-project` 中执行 `clawd`，Clawd 会在该项目目录下工作。因为使用了 `--editable`，更新此 Clawd-Code checkout 后通常不需要重新安装。

如需在同一 Wi-Fi 下通过手机使用浏览器界面，在目标项目目录运行：

```powershell
clawd web
```

使用终端显示的局域网地址和访问令牌连接。保持终端运行；该服务使用 HTTP，只应在可信任的 Wi-Fi 网络使用，不要将端口暴露到互联网。
连接后，可在网页中点击 **Change working folder** 浏览运行 Clawd 的电脑并选择新的工作目录；点击 **New session** 可在当前目录开始新对话。网页界面采用类似 Nova Code 的工作区布局，但聊天仍由 Clawd 自身的 Python 后端处理。

## 4. 配置 GLM API Key

如果要在 Clawd CLI 中使用 API，请运行 `clawd login` 并按提示选择 provider、输入 API Key。CLI 会将配置保存到 `~/.clawd/config.json`。下面的环境变量和 `.env` 示例适用于自行编写的 Python 脚本，不会自动配置 CLI。

### 方式一：环境变量（适合临时测试）

#### Windows PowerShell

```powershell
$env:GLM_API_KEY = "your_api_key_here"
```

#### macOS / Linux

```bash
export GLM_API_KEY="your_api_key_here"
```

#### 永久设置（Windows）

```powershell
[Environment]::SetEnvironmentVariable("GLM_API_KEY", "your_api_key_here", "User")
```

#### 永久设置（macOS / Linux）

```bash
echo 'export GLM_API_KEY="your_api_key_here"' >> ~/.bashrc
source ~/.bashrc
```

### 方式二：.env 文件（适合开发）

在项目根目录创建 `.env` 文件：

```dotenv
# GLM API Configuration
GLM_API_KEY=your_api_key_here
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
GLM_DEFAULT_MODEL=glm-4

# Optional: Other APIs
# ANTHROPIC_API_KEY=your_anthropic_key
# OPENAI_API_KEY=your_openai_key
```

注意：`.env` 文件只有在 Python 脚本显式调用 `load_dotenv()` 时才会加载，例如：

```python
from dotenv import load_dotenv
load_dotenv()
```

## 5. 使用 NVIDIA API（NVIDIA NIM）

可以使用 NVIDIA API Key。项目通过 OpenAI 兼容接口连接 NVIDIA NIM。运行登录命令后，选择 `nvidia`；Base URL 和默认模型会自动填入 NVIDIA 的推荐值。

1. 在 NVIDIA API Catalog 获取 API Key： https://build.nvidia.com/
2. 在项目目录运行配置命令（确保已激活虚拟环境）：

   ```powershell
   python -m src.cli login
   ```

3. 按提示填写：
   - Provider：`nvidia`
   - API Key：你的 NVIDIA API Key
   - Base URL：默认 `https://integrate.api.nvidia.com/v1`
   - Default Model：默认 `nvidia/nemotron-3.5-lightning-30b-a3b`；如需其他模型，请填入 NVIDIA API Catalog 对应的模型 ID

模型 ID 和可用模型可能会随 NVIDIA API Catalog 更新；请以所选模型页面提供的 ID 为准。配置完成后，程序会将 NVIDIA NIM 设为默认 provider。

## 6. GLM API 信息

### API 端点
- Base URL: `https://open.bigmodel.cn/api/paas/v4`
- 认证方式: API Key
- 文档: https://open.bigmodel.cn/dev/api

### 可用模型
- `glm-4` - 最新的 GLM-4 模型（推荐）
- `glm-4-flash` - 快速版本
- `glm-3-turbo` - GLM-3 Turbo

### Python 调用示例

```python
from zhipuai import ZhipuAI
from dotenv import load_dotenv
import os

load_dotenv()
client = ZhipuAI(api_key=os.getenv("GLM_API_KEY"))

response = client.chat.completions.create(
    model="glm-4",
    messages=[
        {"role": "user", "content": "你好"}
    ],
)
print(response.choices[0].message.content)
```

## 7. 验证配置

### 测试环境变量

#### Windows PowerShell

```powershell
$env:GLM_API_KEY
```

#### macOS / Linux

```bash
echo $GLM_API_KEY
```

### 测试 .env 文件加载

```bash
python -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('GLM_API_KEY'))"
```

### 测试 API 连接

```bash
python -c "from zhipuai import ZhipuAI; from dotenv import load_dotenv; import os; load_dotenv(); client = ZhipuAI(api_key=os.getenv('GLM_API_KEY')); print(client.models.list())"
```

如果该命令成功返回模型列表，说明配置已生效。

## 8. 运行项目

在准备好依赖和 API Key 后，可以按项目 README 中的方式启动：

```bash
python -m src.cli
```

或者使用项目脚本：

```bash
clawd
```

## 常见问题

### Q: API Key 在哪里获取？
A: 访问 https://open.bigmodel.cn/ 注册账号后，进入 API 密钥页面创建即可。

### Q: 我用的是 Windows，命令该怎么写？
A: 使用 PowerShell 命令，例如：

```powershell
cd C:\Users\jblix\Desktop\Clawd-Code
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:GLM_API_KEY = "your_api_key_here"
```

### Q: 有没有免费额度？
A: 新用户通常有试用额度，具体请以智谱开放平台官方说明为准。

### Q: 为什么 `source .venv/bin/activate` 不工作？
A: 这是 Linux/macOS 命令；在 Windows PowerShell 应该使用：

```powershell
.\.venv\Scripts\Activate.ps1
```

## 下一步

请按以下步骤操作：

1. ✅ 创建并激活虚拟环境
2. ✅ 安装依赖：`python -m pip install -r requirements.txt`
3. ✅ 运行 `clawd login`，配置 GLM 或 NVIDIA API Key 及对应的 provider
4. ✅ 启动项目并开始使用

如果你愿意，我也可以继续帮你把这份说明改成更适合这个仓库的最终版，或者直接补上 README / setup 相关内容。
