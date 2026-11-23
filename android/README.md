# Pawchive Downloader for Android

这是 Pawchive Downloader 的 Android 端入口。Android 版由仓库中的 `main.py` 和 `scripts/` 构建，适合在手机上进行基础的图集浏览与下载；Windows 专属的托盘、任务栏进度、发布者清理工具和部分桌面设置不会出现在 Android 端。

## 优先从 Releases 下载 APK

打开 [项目 Releases](https://github.com/316824/Pawchive-Downloader/releases)，展开对应版本的 **Assets**，下载名称以 `.apk` 结尾的 Android 附件。公开 Release 的 APK 可以直接下载，不受 Actions 构建产物保留期限影响。若该版本尚未附带 APK，请使用下面的 Actions 方式。

## 从 GitHub Actions 下载 APK

当前仓库不会把 APK 二进制直接提交到源码目录。APK 由公开的 GitHub Actions 工作流构建并作为构建产物提供：

1. 先登录 GitHub，再打开仓库的 [Actions](https://github.com/316824/Pawchive-Downloader/actions) 页面。
2. 在左侧选择 **Build Pawchive Android APK**。
3. 打开最近一次显示绿色对勾的运行记录。
4. 在页面底部 **Artifacts** 区域点击 `PawchiveDownloader-release-APK` 下载 ZIP。
5. 解压 ZIP，取出其中的 `*-release.apk` 文件并传到手机。
6. 在 Android 文件管理器中点击 APK，按系统提示允许当前文件管理器安装未知来源应用，然后完成安装。

后续构建产物请求保留 90 天（仍受仓库策略限制），旧构建原来只保留 14 天；修改设置不会恢复已经过期的文件。构建记录显示 Success 只表示当时构建成功，Artifacts 显示“—”则当前没有可下载附件。如果 Actions 页面没有可下载的绿色运行记录，请等待维护者重新运行工作流，或从仓库的 Releases 页面获取维护者另外附带的 Android APK。

## Artifacts 为空时如何处理

普通用户：优先查找 Releases 的 APK，或选择日期较新的成功构建。下载 Actions 构建产物需要先登录 GitHub。

维护者：

1. 将最新构建工作流推送到 `main` 后，打开 **Actions → Build Pawchive Android APK**。
2. 点击右侧 **Run workflow**，分支选择 `main`，再点击绿色 **Run workflow**。应运行最新代码，不要只反复打开过期的旧构建。
3. 等待新运行变成绿色，打开它，在 **Artifacts** 下载 `PawchiveDownloader-release-APK` ZIP。
4. 解压后将经过验证的 `*-release.apk` 文件上传到对应 Release 的 **Attach binaries** 区域，再发布或更新 Release。上传 APK 本身，方便手机用户直接安装。

延长 Actions 保留时间只能缓解过期问题。将 APK 作为 Release 附件提供给公众，才能避免要求用户依赖临时构建产物。

## 如何判断 APK 是否可用

- 只下载名称中带有 `release` 的 APK。
- 优先选择最新一次成功运行生成的 APK。
- 如果 Android 提示架构或签名不匹配，请删除旧版后重新安装最新 APK；不要把 Windows 的 `.exe` 文件复制到手机上。

## Android 端使用提示

1. 首次启动时授予系统要求的网络和文件访问权限。
2. 选择可写入的下载目录，并在系统设置中允许应用在下载期间保持活动（不同厂商名称可能不同）。
3. 长时间下载时保持网络稳定，必要时关闭系统的电池优化限制。
4. 下载完成后可以在应用设置的保存目录中查看文件。

Android 端与 Windows 端共用同一仓库，但移动系统的后台限制更严格。遇到下载中断时，重新打开应用并检查任务状态；需要长时间批量下载、托盘通知或任务栏进度时，请使用 Windows 版。

## 从源码构建

仓库根目录的 `.github/workflows/build-android.yml` 是可复现的构建流程。它会安装 Python 3.12、PySide6 Android 依赖、Android SDK/NDK，并生成签名的 release APK。普通用户无需在本机安装这些构建工具，直接使用上面的 Actions 产物即可。

