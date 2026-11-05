# Pawchive Downloader

Pawchive Downloader 用于保存 Pawchive 画师主页中的图片原图和帖子附件。Windows 版提供完整桌面工作台、批量队列、后台托盘、签到奖励和安全清理工具；Android 版保留轻量实用下载流程。

## Windows 版

Windows 版支持：

- 画师主页、帖子链接粘贴和拖拽识别；剪贴板哨兵会提示新画师链接，后台完成下载时通过托盘通知。
- 扫描封面、标题和附件格式后选择图集；批量队列支持优先级、暂停、继续、取消、重试、框选置顶和回收站恢复。
- 图片最多五路并发，非图片直链保持单路下载；失败任务自动让位并显示提示。
- 断点续传、下载日程、明暗主题、四种语言、竖屏布局和每个界面的自定义背景裁剪。
- 签到等级、徽章、画师图鉴、成就彩蛋、可解锁主题皮肤，以及完成画师最高成就后生成头像档案卡。
- 窗口 `×` 默认最小化到托盘；完全退出需要从托盘右键菜单选择“退出软件”。
- 发布者清理工具会清除签到、激活、日程、断点、背景、回收站、画师头像缓存和过期临时缓存，不触碰用户下载目录。

Windows 使用、构建和清理说明见 [windows/README.md](windows/README.md)。最新发布包包含 `PawchiveDownloader.exe`、邀请码公钥、发布者清理工具和第三方许可文件。

## Android 版

Android 端以实用下载为主。构建和设备兼容性说明见 [docs/ANDROID.md](docs/ANDROID.md)。Android 构建仅保留公开客户端所需文件，Windows 发布者邀请码私钥不会进入 Android 或开源客户端目录。

## 本地开发

Windows 源码入口为 `windows/download_pawchive_images.py`，构建 spec 为 `windows/PawchiveDownloader.spec`。推荐使用 64 位 Python 3.14 和隔离环境；构建依赖、冻结启动自检和系统 ICU 检查已写入 [windows/README.md](windows/README.md)。

纯 Python 状态和安全测试可以运行：

```powershell
python -m unittest discover -s windows/tests -p "test_*.py"
```

完整 Windows EXE 由 PyInstaller 生成，发布前应运行冻结版启动自检。邀请码生成器只供发布者本人使用，私钥不得上传到 GitHub。

## 许可证

项目代码采用 MIT License，第三方组件的许可信息见 [windows/THIRD_PARTY_NOTICES.md](windows/THIRD_PARTY_NOTICES.md)。
