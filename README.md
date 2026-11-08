# Muter

![Muter](muter/assets/muter.png)

Muter 是 Windows x64 会议麦克风工具。在本机压低环境噪声，并可用本人声音样本减少旁人和电视人声。实时降噪与耳机试听均在本机处理，音频不保存。

## 安装和使用

从 [GitHub Releases](https://github.com/cuppur/muter/releases/latest) 下载并运行最新的 `Muter-Setup-*-x64.exe`。安装包包含运行环境、模型和 VB-Audio 提供的原版基础 VB-CABLE。Windows 可能在安装驱动时要求管理员授权或重启。

1. 选「环境降噪」「人声隔离」或「加强人声隔离」。人声隔离需要一次本人声音录入。
2. 连上耳机，点「开启实时试听」，用「原声」和「降噪后」比较。
3. 点「开启会议降噪」。飞书等会议软件使用「系统默认」麦克风。
4. 关闭主窗口后，Muter 会在 Windows 右下角托盘继续运行。单击图标或再次打开快捷方式可恢复主界面。
5. 右键图标可以开启或停止会议降噪、暂停或恢复传声、切换模式、控制试听或打开高级设置。选择「退出 Muter」会停止音频处理、恢复设备并关闭应用。

Windows 可能将托盘图标收在右下角的「^」折叠区域。处理模式在停止当前音频会话后切换。

## 处理模式

| 模式 | 用途 |
| --- | --- |
| 环境降噪 | GTCRN 压低风扇、键盘等环境声 |
| 人声隔离 | VoxLock 按本人声音样本提取目标人声 |
| 加强人声隔离 | 在人声提取后按本人讲话概率渐变衰减，增加约 160 ms 的模型前视 |
| 人声隔离 · FP32 | 全精度对照模式 |

加强模式主要减少本人停顿时的旁人漏音。实际效果受同时讲话、声源位置和麦克风影响。已有声音样本时，启动默认选择日常人声隔离。

本地运行日志位于 `%LOCALAPPDATA%\MuterPrototype\app.log`。「导出诊断」会打包设备信息与日志。声音样本和历史音频文件由应用独立保存。

## 从源码构建

使用 Windows x64、Python 3.13 与 Inno Setup。先安装发布版取得相同版本的模型与驱动资源。将安装目录下的 `models` 复制到仓库根目录，并把 `components/vbcable` 和 `components/vbcable.zip` 复制到仓库的 `vendor` 目录。

```powershell
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install pyinstaller==6.22.3
.venv/Scripts/python.exe -m unittest discover -s tests
.venv/Scripts/python.exe tools/build_windows.py
ISCC.exe installer/Muter.iss
```

`tools/build_windows.py` 生成应用目录，Inno Setup 生成单文件安装 EXE。运行源码可使用 `.venv/Scripts/python.exe run_muter.py`。验证用的公开语音素材与生成报告归放在本机 `artifacts` 目录。

VB-CABLE 是 VB-Audio 的 Donationware，欢迎支持其开发。第三方组件按各自许可提供，详情见 [第三方声明](THIRD_PARTY_NOTICES.md) 和 [VB-Audio 许可](https://vb-audio.com/Services/licensing.htm)。
