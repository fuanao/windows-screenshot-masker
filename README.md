# Windows Screenshot Masker / 截图打码工具

一个面向 Windows 桌面的截图、标注、打码、马赛克、OCR 和翻译工具。启动后常驻系统托盘，支持全局快捷键截图，适合日常沟通、工单处理、隐私信息遮挡和截图标注。

> 默认快捷键：`Ctrl + Shift + A`

## 功能特性

- 系统托盘常驻，支持自定义全局快捷键
- 区域截图、全屏截图、手动长截图
- 快捷键截图后默认进入区域选择模式，顶部显示“区域截图 默认”，全屏作为次级入口
- 单窗口编辑模式：多次截图会进入左侧截图列表
- 标注工具：矩形、椭圆、描边、箭头、直线、画笔、高亮、黑色遮罩、模糊、马赛克、文字
- 支持颜色、文字大小、线宽调节
- 文字标注支持再次点击编辑
- 智能打码：基于 Windows OCR 自动识别手机号、邮箱、身份证号、IP 并添加马赛克
- 编辑后自动同步到剪贴板，方便直接粘贴到聊天窗口
- OCR 使用 Windows 内置 OCR 能力，识别过程在本机执行
- OCR 结果支持一键翻译
- 支持保存 PNG、复制图片到剪贴板

## 编辑器快捷键

- `Ctrl + C`：复制图片到剪贴板
- `Ctrl + S`：保存 PNG
- `Ctrl + Z`：撤销上一处标注
- `Ctrl + +`：放大
- `Ctrl + -`：缩小
- `Ctrl + 0`：适应窗口
- `Ctrl + 鼠标滚轮`：放大或缩小

## 产品文档

- [完整 PRD](docs/PRD.md)
- [P0 MVP 范围](docs/MVP.md)

## 运行环境

- Windows 10 / Windows 11
- Python 3.12 推荐
- 已安装 Windows OCR 语言包，例如中文简体或英文

## 本地运行

```powershell
py -m pip install -r requirements.txt
py screenshot_masker.py
```

## 打包 Windows exe

```powershell
py -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean ScreenshotMasker.spec
```

打包完成后，程序通常位于：

```text
dist/ScreenshotMasker/ScreenshotMasker.exe
```

## 隐私说明

- 截图编辑、打码、模糊、马赛克处理在本地完成。
- OCR 使用 Windows 本机 OCR 能力。
- 翻译功能会调用在线翻译接口，可能会把识别到的文字发送到第三方服务。
- 不建议对包含密码、密钥、客户隐私、合同等敏感信息的截图使用在线翻译。

## 开源协议

本项目使用 MIT License。
