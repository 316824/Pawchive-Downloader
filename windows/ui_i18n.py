"""Shared four-language copy and Qt dialog localization for the desktop UI."""
from pathlib import Path
import sys

from feature_text import LANGUAGES, TEXT, tr

# The source text is Simplified Chinese; translations follow LANGUAGES[1:].
COPY = {
 '扫描结束后自动加入下载队列': ('掃描結束後自動加入下載佇列', 'Automatically queue all collections after scanning', 'スキャン完了後にすべてダウンロードキューへ追加'),
 '收起日志': ('收起日誌', 'Hide log', 'ログを隠す'),
 '显示日志': ('顯示日誌', 'Show log', 'ログを表示'),
 '按画师名创建主文件夹': ('按畫師名建立主資料夾', 'Create a main folder named after the artist', '作家名のメインフォルダーを作成'),
 '正在准备保存文件夹…': ('正在準備儲存資料夾…', 'Preparing the save folder…', '保存先フォルダーを準備中…'),
 '准备保存文件夹失败，请选择可写且较短的保存路径': ('準備儲存資料夾失敗，請選擇可寫且較短的儲存路徑', 'Could not prepare the folder. Choose a writable, shorter save path.', 'フォルダーを準備できません。書き込み可能な短い保存先を選んでください。'),
 '有画师更新': ('有畫師更新', 'Artist updates', '作家の更新があります'),
 '画师数量超过单次迁移上限（1000位）。': ('畫師數量超過單次遷移上限（1000位）。', 'A transfer can contain at most 1,000 artists.', '一度に移行できる作家は1,000名までです。'),
 '画师数据超过单次迁移大小上限。': ('畫師資料超過單次遷移大小上限。', 'The artist data exceeds the transfer size limit.', '作家データが移行サイズの上限を超えています。'),
 '退出 Pawchive': ('退出 Pawchive', 'Quit Pawchive', 'Pawchiveを終了'),
 '检测到当前下载任务正在进行，是否继续退出？': ('偵測到下載任務正在進行，是否仍要退出？', 'Downloads are in progress. Quit anyway?', 'ダウンロード中です。終了しますか？'),
 '正在整理图片编号…': ('正在整理圖片編號…', 'Organizing image numbers…', '画像の番号を整理中…'),
 '图片编号未完成，可重启后继续恢复': ('圖片編號未完成，可重新啟動後繼續恢復', 'Image numbering is unfinished. Restart to resume.', '画像の番号整理が未完了です。再起動後に再開できます。'),
 '正在准备下载队列…': ('正在準備下載佇列…', 'Preparing the download queue…', 'ダウンロードキューを準備中…'),
 '准备下载失败': ('準備下載失敗', 'Could not prepare the download', 'ダウンロードの準備に失敗しました'),
 '正在检查画师更新…': ('正在檢查畫師更新…', 'Checking artist updates…', '作家の更新を確認中…'),
 '正在等待后台请求结束并安全退出…': ('正在等待背景請求結束並安全退出…', 'Waiting for background requests to finish safely…', 'バックグラウンド処理の終了を待っています…'),
 '核心受损，正在重组数据流...': ('核心受損，正在重組資料流…', 'Core damaged. Rebuilding the data stream…', 'コアが損傷しました。データを再構築中…'),
 '扫描暂停': ('掃描暫停', 'Scan paused', 'スキャンを一時停止'),
 '扫描已完成，共获取 {count} 个图集': ('掃描已完成，共取得 {count} 個圖集', 'Scan complete: {count} collections found', 'スキャン完了：{count} 件のコレクション'),
 '（点击窗口任意一处位置查看图集）': ('（點擊視窗任意位置查看圖集）', '(Click anywhere in the window to view collections)', '（ウィンドウをクリックしてコレクションを表示）'),
 '方向键 / WASD 控制 · 点击游戏区域获取焦点': ('方向鍵 / WASD 控制 · 點擊遊戲區域取得焦點', 'Arrow keys / WASD · Click the game to focus', '方向キー / WASD · ゲームをクリックして操作'),
 '幽灵协议 · 5s': ('幽靈協議 · 5s', 'Ghost protocol · 5s', 'ゴーストプロトコル · 5秒'),
 '赛博磁铁 · 4s': ('賽博磁鐵 · 4s', 'Cyber magnet · 4s', 'サイバーマグネット · 4秒'),
 '超频引擎 · 6s': ('超頻引擎 · 6s', 'Overclock engine · 6s', 'オーバークロック · 6秒'),
 '反制脉冲 · 清障完成': ('反制脈衝 · 清障完成', 'Counter pulse · Obstacles cleared', 'カウンターパルス · 障害物消去'),
 '此版本暂无详细更新说明，请前往 GitHub 发布页面查看更新内容和下载文件。': ('此版本暫無詳細更新說明，請前往 GitHub 發布頁查看更新與下載。', 'No detailed release notes are available. Visit the GitHub release page for details and downloads.', '詳細な更新内容はありません。GitHubのリリースページをご覧ください。'),
 '\n\n（更新说明较长，其余内容请在发布页面查看。）': ('\n\n（更新說明較長，其餘內容請於發布頁查看。）', '\n\n(Release notes are long. Read the rest on the release page.)', '\n\n（更新内容が長いため、続きはリリースページをご覧ください。）'),
 '关闭': ('關閉', 'Close', '閉じる'),
 '打开': ('開啟', 'Open', '開く'),
 '保存': ('儲存', 'Save', '保存'),
 '取消': ('取消', 'Cancel', 'キャンセル'),
 '确定': ('確定', 'OK', '確認'),
 '是': ('是', 'Yes', 'はい'),
 '否': ('否', 'No', 'いいえ'),
 '全部是': ('全部是', 'Yes to All', 'すべてはい'),
 '全部否': ('全部否', 'No to All', 'すべていいえ'),
 '应用': ('套用', 'Apply', '適用'),
 '重置': ('重設', 'Reset', 'リセット'),
 '帮助': ('說明', 'Help', 'ヘルプ'),
 '重试': ('重試', 'Retry', '再試行'),
 '忽略': ('忽略', 'Ignore', '無視'),
 '中止': ('中止', 'Abort', '中止'),
 '放弃': ('捨棄', 'Discard', '破棄'),
 '最小化窗口': ('最小化視窗', 'Minimize window', 'ウィンドウを最小化'),
 '最大化窗口': ('最大化視窗', 'Maximize window', 'ウィンドウを最大化'),
 '还原窗口': ('還原視窗', 'Restore window', 'ウィンドウを元に戻す'),
 '最大化或还原窗口': ('最大化或還原視窗', 'Maximize or restore window', '最大化または元に戻す'),
 '关闭窗口': ('關閉視窗', 'Close window', 'ウィンドウを閉じる'),
 '竖屏模式下请先切回窗口模式': ('直向模式下請先切回視窗模式', 'Return to window mode first', '縦画面モードを解除してください'),
 '批量添加画师': ('批次新增畫師', 'Add multiple artists', '作家を一括追加'),
 '取消剩余批量画师': ('取消剩餘批次畫師', 'Cancel remaining artists', '残りの作家をキャンセル'),
 '请输入画师 ID 或完整的画师主页链接': ('請輸入畫師 ID 或完整的畫師首頁連結', 'Enter an artist ID or a complete profile URL', '作家IDまたは完全なプロフィールURLを入力'),
 '请输入画师数字 ID 或完整的 Pawchive 画师链接。': ('請輸入畫師數字 ID 或完整的 Pawchive 畫師連結。', 'Enter a numeric artist ID or a complete Pawchive artist URL.', '数字の作家IDまたはPawchiveの作家URLを入力してください。'),
 '✓ 画师 ID 格式有效，将检查 Fanbox 与 Patreon': ('✓ 畫師 ID 格式有效，將檢查 Fanbox 與 Patreon', '✓ Artist ID format is valid; Fanbox and Patreon will be checked', '✓ 作家IDの形式は有効です。FanboxとPatreonを確認します'),
 '正在查找画师…': ('正在尋找畫師…', 'Finding artist…', '作家を検索中…'),
 '下载内容': ('下載內容', 'Download content', 'ダウンロード内容'),
 '图片与附件': ('圖片與附件', 'Images and attachments', '画像と添付ファイル'),
 '仅图片': ('僅圖片', 'Images only', '画像のみ'),
 '仅直链附件': ('僅直連附件', 'Direct-link attachments only', '直接リンクの添付ファイルのみ'),
 '全部暂停': ('全部暫停', 'Pause all', 'すべて一時停止'),
 '全部继续': ('全部繼續', 'Resume all', 'すべて再開'),
 '失败任务': ('失敗任務', 'Failed tasks', '失敗したタスク'),
 '失败任务 ({count})': ('失敗任務 ({count})', 'Failed tasks ({count})', '失敗したタスク ({count})'),
 '下载失败的图集': ('下載失敗的圖集', 'Failed collections', 'ダウンロードに失敗したコレクション'),
 '全部重试': ('全部重試', 'Retry all', 'すべて再試行'),
 '{count} 个失败或正在重试的图集 · 与主下载队列实时同步': ('{count} 個失敗或正在重試的圖集 · 與主下載佇列即時同步', '{count} failed or retrying collections · Synced with the main queue', '失敗・再試行中のコレクション {count} 件 · メインキューと同期'),
 '画师档案数据迁移': ('畫師檔案資料遷移', 'Transfer artist data', '作家データの移行'),
 '迁移画师档案、成就统计与订阅时间记录。不会包含登录信息、设备码或本机文件路径。': ('遷移畫師檔案、成就統計與訂閱時間紀錄。不包含登入資訊、裝置碼或本機路徑。', 'Transfer artist profiles, achievement statistics and subscription dates. Login details, device codes and local paths are excluded.', '作家プロフィール、実績統計、購読日時を移行します。ログイン情報、端末コード、ローカルパスは含まれません。'),
 '完整复制下方迁移代码，在另一台电脑的“输入恢复”中粘贴。': ('完整複製下方遷移代碼，在另一台電腦的「輸入恢復」中貼上。', 'Copy the complete code and paste it into Restore on the other computer.', 'コード全体をコピーし、移行先の「復元」に貼り付けてください。'),
 '复制迁移代码': ('複製遷移代碼', 'Copy transfer code', '移行コードをコピー'),
 '导出 / 复制': ('匯出 / 複製', 'Export / Copy', 'エクスポート / コピー'),
 '粘贴完整迁移代码。相同画师会自动合并，并保留较新的记录。': ('貼上完整遷移代碼。相同畫師會自動合併並保留較新的紀錄。', 'Paste the complete code. Existing artists are merged, keeping newer records.', 'コード全体を貼り付けてください。同じ作家のデータは新しい記録を優先して統合します。'),
 '恢复画师数据': ('恢復畫師資料', 'Restore artist data', '作家データを復元'),
 '输入恢复': ('輸入恢復', 'Restore', '復元'),
 '已复制': ('已複製', 'Copied', 'コピーしました'),
 '无效迁移代码，请完整复制后重试。': ('無效遷移代碼，請完整複製後重試。', 'Invalid transfer code. Copy the entire code and try again.', '無効な移行コードです。全体をコピーして再試行してください。'),
 '恢复失败，本机原有画师数据已保留，请稍后重试。': ('恢復失敗，本機原有畫師資料已保留，請稍後重試。', 'Restore failed. Existing data was kept. Please try again later.', '復元に失敗しました。既存のデータは保持されています。後でもう一度お試しください。'),
 '恢复成功：{added} 位新增，{updated} 位更新。': ('恢復成功：新增 {added} 位，更新 {updated} 位。', 'Restored: {added} added, {updated} updated.', '復元しました：追加 {added} 件、更新 {updated} 件。'),
 '编辑背景图': ('編輯背景圖', 'Edit background', '背景を編集'),
 '请选择 JPG、PNG、JPEG 或 WEBP 图片（最大 20 MB）': ('請選擇 JPG、PNG、JPEG 或 WEBP 圖片（最大 20 MB）', 'Choose a JPG, PNG, JPEG or WEBP image (up to 20 MB)', 'JPG、PNG、JPEG、WEBP画像を選択してください（最大20 MB）'),
 '拖动裁剪框，右下角手柄可调整大小；滚轮缩放，空白处拖动平移。': ('拖動裁剪框，右下角手柄可調整大小；滾輪縮放，空白處拖動平移。', 'Drag the crop box or its lower-right handle. Scroll to zoom; drag empty space to pan.', '切り抜き枠をドラッグし、右下のハンドルでサイズを変更します。ホイールでズーム、空白をドラッグして移動できます。'),
 '选择图片…': ('選擇圖片…', 'Choose image…', '画像を選択…'),
 '重置视图': ('重設檢視', 'Reset view', '表示をリセット'),
 '保存此裁剪': ('儲存此裁剪', 'Save crop', '切り抜きを保存'),
 '选择背景图片': ('選擇背景圖片', 'Choose background image', '背景画像を選択'),
 '窗口实际预览': ('視窗實際預覽', 'Window preview', 'ウィンドウのプレビュー'),
 '背景效果预览': ('背景效果預覽', 'Background preview', '背景のプレビュー'),
 '裁剪起点 {x:.3f}, {y:.3f} · 范围 {width:.3f} × {height:.3f}': ('裁剪起點 {x:.3f}, {y:.3f} · 範圍 {width:.3f} × {height:.3f}', 'Crop origin {x:.3f}, {y:.3f} · Size {width:.3f} × {height:.3f}', '切り抜き位置 {x:.3f}, {y:.3f} · 範囲 {width:.3f} × {height:.3f}'),
 '无法使用这张图片': ('無法使用這張圖片', 'Cannot use this image', 'この画像を使用できません'),
 '无法保存背景': ('無法儲存背景', 'Cannot save background', '背景を保存できません'),
 '无法切换主题': ('無法切換主題', 'Cannot change theme', 'テーマを変更できません'),
 '背景与界面主题': ('背景與介面主題', 'Backgrounds and interface themes', '背景とインターフェーステーマ'),
 '每个界面可以使用不同的背景。原图只在裁剪时读取，运行时使用受限尺寸的缩略图。': ('每個介面可以使用不同背景。原圖僅於裁剪時讀取，執行時使用受限尺寸的縮圖。', 'Use a different background on each screen. Originals are read only while cropping; the app displays bounded previews.', '画面ごとに背景を設定できます。元画像は編集時のみ読み込み、通常はサイズを制限したプレビューを表示します。'),
 '下载工作台': ('下載工作台', 'Download workspace', 'ダウンロードワークスペース'),
 '签到日历': ('簽到日曆', 'Check-in calendar', 'チェックインカレンダー'),
 '下载日程': ('下載日程', 'Download schedules', 'ダウンロード予定'),
 '邀请码窗口': ('邀請碼視窗', 'Activation window', '招待コード画面'),
 '图集选择窗口': ('圖集選擇視窗', 'Collection selection', 'コレクションの選択'),
 '退出提示': ('退出提示', 'Exit prompt', '終了確認'),
 '续传提示': ('續傳提示', 'Resume prompt', '再開確認'),
 '扫描窗口': ('掃描視窗', 'Scan window', 'スキャン画面'),
 '画师档案卡': ('畫師檔案卡', 'Artist cards', '作家カード'),
 '画师更新订阅': ('畫師更新訂閱', 'Artist update subscriptions', '作家の更新購読'),
 '编辑界面': ('編輯介面', 'Screen to edit', '編集する画面'),
 '默认主题': ('預設主題', 'Default theme', '既定のテーマ'),
 '自定义背景': ('自訂背景', 'Custom background', 'カスタム背景'),
 '签到奖励主题': ('簽到獎勵主題', 'Check-in reward theme', 'チェックイン報酬テーマ'),
 '当前来源': ('目前來源', 'Current source', '現在の表示元'),
 '遮罩深度': ('遮罩深度', 'Mask depth', 'マスクの濃さ'),
 '当前界面尚未设置自定义背景': ('目前介面尚未設定自訂背景', 'No custom background for this screen', 'この画面にはカスタム背景がありません'),
 '选择或编辑背景…': ('選擇或編輯背景…', 'Choose or edit background…', '背景を選択・編集…'),
 '清除当前背景': ('清除目前背景', 'Clear this background', 'この背景を削除'),
 '完成': ('完成', 'Done', '完了'),
 '当前缩略图不可用，将在下次编辑时重建。': ('目前縮圖無法使用，將於下次編輯時重建。', 'Preview unavailable. Edit the background to rebuild it.', 'プレビューを表示できません。背景の編集で再作成できます。'),
 '发现 Pawchive 新版本': ('發現 Pawchive 新版本', 'Pawchive update available', 'Pawchiveの更新があります'),
 '忽略该版本': ('忽略此版本', 'Ignore this version', 'このバージョンを無視'),
 '稍后再说': ('稍後再說', 'Not now', '後で'),
 '前往下载': ('前往下載', 'Go to download', 'ダウンロードページへ'),
 '查看新版本的更新说明和下载页面': ('查看新版本說明與下載頁面', 'View release notes and downloads', '更新内容とダウンロードページを表示'),
 '● 发现新版本 {tag}': ('● 發現新版本 {tag}', '● Update available: {tag}', '● 新しいバージョン {tag}'),
}

# Validation errors originate in UI-independent storage/image code in English.
# Keep that API stable and translate only when presenting them to the user.
ERROR_COPY = {
 'Unknown background page.': ('未知的背景界面。','未知的背景介面。','不明な背景画面です。'),
 'The selected image could not be opened.': ('无法打开所选图片。','無法開啟所選圖片。','選択した画像を開けません。'),
 'Choose an image file, not a folder.': ('请选择图片文件，不要选择文件夹。','請選擇圖片檔案，而非資料夾。','フォルダーではなく画像ファイルを選択してください。'),
 'Only JPG, JPEG, PNG and WEBP images are supported.': ('仅支持 JPG、JPEG、PNG 和 WEBP 图片。','僅支援 JPG、JPEG、PNG 和 WEBP 圖片。','JPG、JPEG、PNG、WEBP画像に対応しています。'),
 'The selected image size could not be read.': ('无法读取所选图片的大小。','無法讀取所選圖片的大小。','選択した画像のサイズを取得できません。'),
 'The image must be between 1 byte and 20 MB.': ('图片不能是空文件，且大小不能超过 20 MB。','圖片不能為空檔案，且大小不得超過 20 MB。','空の画像や20 MBを超える画像は使用できません。'),
 'Background settings are unexpectedly large.': ('背景设置数据过大。','背景設定資料過大。','背景設定のデータが大きすぎます。'),
 'Unknown background mode.': ('未知的背景模式。','未知的背景模式。','不明な背景モードです。'),
 'The preview image is invalid.': ('预览图片无效。','預覽圖片無效。','プレビュー画像が無効です。'),
 'The generated preview is too large.': ('生成的预览图片过大。','產生的預覽圖片過大。','作成したプレビュー画像が大きすぎます。'),
 'The image dimensions are invalid.': ('图片尺寸无效。','圖片尺寸無效。','画像の寸法が無効です。'),
 'The image dimensions exceed the safe limit.': ('图片像素尺寸超过安全限制。','圖片像素尺寸超過安全限制。','画像の寸法が安全上の上限を超えています。'),
 'The preview dimensions exceed the safe limit.': ('预览图片尺寸超过安全限制。','預覽圖片尺寸超過安全限制。','プレビュー画像の寸法が上限を超えています。'),
 'The preview could not be saved.': ('无法保存预览图片。','無法儲存預覽圖片。','プレビュー画像を保存できません。'),
 'The selected file is not a readable image.': ('所选文件不是可读取的图片。','所選檔案不是可讀取的圖片。','選択したファイルは読み取り可能な画像ではありません。'),
 'The selected image could not be decoded.': ('无法解码所选图片。','無法解碼所選圖片。','選択した画像を読み込めません。'),
 'The crop is empty.': ('裁剪区域为空。','裁剪區域為空。','切り抜き範囲が空です。'),
 'The generated preview is too large; choose a smaller crop.': ('预览图片过大，请缩小裁剪范围。','預覽圖片過大，請縮小裁剪範圍。','プレビューが大きすぎます。切り抜き範囲を小さくしてください。'),
}


def language_for(widget, default='简体中文'):
    seen = set()
    while widget is not None and id(widget) not in seen:
        seen.add(id(widget))
        for name in ('lang', 'language'):
            value = getattr(widget, name, None)
            if isinstance(value, str) and value in LANGUAGES:
                return value
        widget = widget.parent() if hasattr(widget, 'parent') else None
    return default if default in LANGUAGES else LANGUAGES[0]


def t(language, source, **values):
    index = LANGUAGES.index(language) if language in LANGUAGES else 0
    if source in ERROR_COPY:
        return source if index==2 else ERROR_COPY[source][{0:0,1:1,3:2}[index]]
    if source in TEXT:
        return tr(language, source, **values)
    text = source if index == 0 or source not in COPY else COPY[source][index-1]
    return text.format(**values) if values else text


def install_qt_language(language):
    """Translate Qt's own labels, including file and input dialog controls."""
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    if getattr(app, '_pawchive_qt_language', None) == language:
        return
    old = getattr(app, '_pawchive_qt_translator', None)
    if old is not None:
        app.removeTranslator(old)
    code = {'简体中文':'zh_CN', '繁體中文':'zh_TW', 'English':'en_US', '日本語':'ja_JP'}.get(language, 'zh_CN')
    QLocale.setDefault(QLocale(code))
    translator = QTranslator(app)
    if language != 'English':
        roots = [Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'translations',
                 Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))]
        for root in roots:
            if translator.load(str(root / ('qtbase_' + code + '.qm'))):
                app.installTranslator(translator)
                break
    app._pawchive_qt_translator = translator
    app._pawchive_qt_language = language
    if old is not None:
        old.deleteLater()


def localize_dialog(dialog, language=None):
    """Set standard button captions explicitly; never translate user content."""
    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QDialogButtonBox, QMessageBox
    language = language or language_for(dialog)
    code = {'简体中文':'zh_CN', '繁體中文':'zh_TW', 'English':'en_US', '日本語':'ja_JP'}.get(language, 'zh_CN')
    dialog.setLocale(QLocale(code))
    captions = {'Ok':'确定','Cancel':'取消','Close':'关闭','Open':'打开','Save':'保存',
                'Yes':'是','No':'否','YesToAll':'全部是','NoToAll':'全部否','Apply':'应用',
                'Reset':'重置','Help':'帮助','Retry':'重试','Ignore':'忽略','Abort':'中止','Discard':'放弃'}
    boxes = dialog.findChildren(QDialogButtonBox)
    if isinstance(dialog, QDialogButtonBox):
        boxes.append(dialog)
    if isinstance(dialog, QMessageBox):
        boxes.append(dialog)
    for box in boxes:
        enum = QMessageBox.StandardButton if isinstance(box, QMessageBox) else QDialogButtonBox.StandardButton
        for name, source in captions.items():
            button = box.button(getattr(enum, name))
            if button is not None and not button.property('pawchiveCustomCaption'):
                button.setText(t(language, source))
