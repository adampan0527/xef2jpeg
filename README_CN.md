# XEF2JPEG 转换器

一款 Windows 桌面应用程序，用于将 Kinect V2 的 .XEF 文件转换为 JPEG 格式。

## 项目概述

**XEF2JPEG** 是一款图形界面应用程序，可将 Kinect V2 传感器录制的 .XEF 文件转换为 JPEG 图像格式。应用程序支持 Windows 10 和 Windows 11 操作系统。

### 功能特性

- 通过文件选择对话框选择 .XEF 输入文件（支持多文件批量转换）
- 支持拖拽 .XEF 文件到窗口
- 可配置输出目录
- 支持多种流类型：深度（Depth）、红外（IR）、彩色（Color）
- 可调节 JPEG 输出质量（60-100）
- 转换过程中显示进度条
- 命令行中使用 tqdm 显示详细转换进度
- 支持命令行参数直接指定输入文件
- 记住上次使用的输入/输出目录
- 支持取消正在进行的转换
- 窗口位置自动保存与恢复

### 输出目录结构

```
XEF2JPEG_Output/
  └── YYYY_MM_DD_HH_MM_SS/
        ├── Depth/     （深度帧，512x424 灰度图）
        ├── IR/        （红外帧，512x424 灰度图）
        └── Color/     （彩色帧，1920x1080 RGB，如果源文件包含）
```

### 目录说明

- `XEF2JPEG_Input/` - 输入 .XEF 文件目录
- `XEF2JPEG_Output/` - 转换后的 JPEG 输出目录

## 环境要求

- Windows 10 或 Windows 11
- Python 3.8+
- uv（Python 包管理器，推荐）
- Pillow 库（图像处理）
- numpy 库（深度/红外帧数据处理）
- tqdm 库（命令行进度条）

> **注意：** Kinect for Windows SDK 2.0 **不是必需的**。本应用程序使用自有的解析器直接读取 XEF 文件。

## 安装步骤

```powershell
# 1. 安装 uv（如果尚未安装）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 创建虚拟环境
uv venv

# 3. 激活虚拟环境
.venv\Scripts\activate

# 4. 安装依赖
uv pip install -r requirements.txt
```

如果不使用 uv，也可以使用 pip：

```bash
pip install -r requirements.txt
```

## 使用方法

### 图形界面模式

```bash
python xef2jpeg.py
```

启动后可以通过以下方式选择文件：
1. 点击 **"Add Files..."** 按钮选择一个或多个 .XEF 文件
2. 将 .XEF 文件直接拖拽到窗口中
3. 通过命令行参数指定文件：`python xef2jpeg.py path/to/file.xef`

### 转换步骤

1. 添加一个或多个 .XEF 文件到文件列表
2. 选择输出目录（默认为当前目录下的 `XEF2JPEG_Output`）
3. 选择流类型：
   - `depth_ir` - 深度 + 红外（默认）
   - `depth_only` - 仅深度
   - `ir_only` - 仅红外
   - `color_only` - 仅彩色
   - `all` - 全部流类型
4. 选择 JPEG 输出质量
5. 点击 **"Convert All"** 开始转换

### 命令行进度条

转换过程中，命令行终端会自动显示 tqdm 进度条，包含以下信息：

```
Extracting frames: 100%|████████████████| 50/50 [00:03<00:00, 15.2 frames/s, depth=25, ir=25]
```

## XEF 文件格式说明

.XEF 是 Kinect V2 传感器的数据录制格式，文件结构如下：

| 部分 | 说明 |
|------|------|
| 文件头 | 44 字节，包含 EVENTS1 魔数、流数量、时间戳 |
| 流描述符 | 0x3333 标记的流类型描述 |
| 事件数据 | 32 字节段描述符 + 帧数据的交错序列 |
| 尾部索引 | 最后 8192 字节，可选的查找表 |

### 支持的流类型

| 类型 ID | 名称 | 分辨率 | 说明 |
|---------|------|--------|------|
| 1 | Body（骨架） | 可变 | 人体骨架数据 |
| 2 | Calibration（标定） | 可变 | 传感器标定数据 |
| 3 | Depth（深度） | 512x424 | 16 位深度值（毫米） |
| 4 | IR（红外） | 512x424 | 16 位红外强度 |
| 5 | Opaque（元数据） | 640 字节 | 传感器元数据 |
| 6 | Telemetry（遥测） | 20 字节 | 传感器状态数据 |
| 7 | Color（彩色） | 1920x1080 | BGRA 格式彩色图像 |

## 关键文件

| 文件 | 说明 |
|------|------|
| `xef2jpeg.py` | 主应用程序入口 |
| `xef_parser.py` | XEF 文件解析器模块 |
| `requirements.txt` | Python 依赖列表 |
| `feature_list.json` | 功能需求清单（207 项功能） |
| `claude-progress.txt` | 开发进度日志 |
| `CLAUDE.md` | Claude Code 开发指南 |
| `README.md` | 英文说明文档 |
| `README_CN.md` | 中文说明文档（本文件） |

## 应用架构

本项目基于 tkinter 的桌面应用程序，具有以下特点：

- 使用原生 Windows 文件选择对话框
- 基于 ctypes 的拖拽支持（WM_DROPFILES）
- 多线程转换，不阻塞用户界面
- 线程安全的 UI 更新（通过 `root.after()` 机制）
- 转换过程中支持取消操作
- 自动检测系统主题（亮色/暗色）
- 高 DPI 显示适配

## 开发说明

本项目使用 Agent Harness 架构进行开发，通过 `feature_list.json` 驱动功能实现。

```bash
# 查看当前功能完成情况
grep '"passes": true' feature_list.json | wc -l
```

## 许可证

MIT
