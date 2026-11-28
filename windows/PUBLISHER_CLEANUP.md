# 发布者本机记录清理工具

这是供发布者整理本机记录的 Windows 工具。公开发布软件时，请分发构建好的 Downloader EXE；清理工具单独保管。

## 清理范围

- 当前 Windows 用户创建的 Pawchive 一次性计划任务。
- `%LOCALAPPDATA%\PawchiveDownloader` 内的激活、签到、等级、徽章、主题、语言、日程、下载恢复记录、回收站和错误日志。
- 同一数据目录内的自定义背景与预览缓存、画师档案与头像、订阅基线、今日推荐缓存和已忽略版本记录。
- 系统临时目录中超过 24 小时的 `pawchive_cache_*` 与 `pawchive_preview_*` 应用目录。正在使用软件时不得清理。
- 加上 `-RemoveRegistry` 时，同时清除 `HKCU\Software\PawchiveTools\PawchiveDownloader` 中的默认保存路径等设置。

不会删除用户选择的下载目录、已完成的图片和附件，也不会扫描或删除下载目录中的 `.part` 文件。邀请码生成器和签发私钥不在清理范围内。

## 操作方法

先从托盘完全退出 Downloader，包括通过 Python 启动的软件窗口。工具会检查 EXE 和可识别的 Python 脚本进程，发现仍在运行时停止清理。

在清理工具所在文件夹打开 PowerShell，先预览：

```powershell
.\PawchivePublisherCleanup.cmd -Preview
```

确认显示的目录和记录后执行：

```powershell
.\PawchivePublisherCleanup.cmd -RemoveRegistry
```

输入 `CLEAN` 后才会执行。不要在需要恢复下载、保留订阅或继续使用当前激活记录时执行清理；完成后需重新激活软件。

仅源码环境也可直接预览脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\publisher_cleanup.ps1 -Preview
```

工具会验证清理目标的绝对路径，拒绝驱动器根目录、越界目录及符号链接或目录联接。遇到拒绝信息时，请先核对路径，不要通过手动扩大删除范围来绕过检查。

## 软件内更新缓存

Windows v1.3.2 的更新器使用系统临时目录中的 `pawchive_update_*` 独立目录。应用启动和扫帚清理仅移除超过 24 小时且已确认不活跃的更新目录；发布者工具也纳入此项。正在更新的进程目录不会被删除。失败更新的日志保留在对应临时目录，供用户确认错误原因；下载文件与恢复记录不属于更新缓存。
