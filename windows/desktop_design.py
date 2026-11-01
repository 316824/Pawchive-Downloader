"""Desktop presentation: shared controls stay connected to the existing workers."""
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPainterPath, QShortcut
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QTextEdit, QVBoxLayout, QWidget)

from desktop_visuals import CardHeading, StageRail, icon
from control_effects import DARK_GLOW, LIGHT_GLOW


COPY = {
    '简体中文': dict(workspace='下载工作台',
        source='画师主页', destination='保存位置', options='下载方式', activity='任务概览',
        source_hint='请输入完整的画师主页链接', folder_hint='选择用于保存图片与附件的文件夹',
        paste='粘贴链接', log='运行日志', log_hint='任务记录会在这里出现', ready='准备就绪',
        setup='等待设置', scan='扫描中', choose='选择图集', download='下载中', complete='已完成',
        error='需要检查', stopped='已停止', stopping='正在停止', albums='当前图集', files='已保存文件',
        speed='实时速度', current='当前文件', waiting='等待开始', focus='收起日志 · Ctrl+L',
        expand='显示日志 · Ctrl+L', copy='复制日志', plan='本次计划',
        newest='新到旧', oldest='旧到新', folders='按图集整理', flat='平铺保存',
        selection='先预选', all_posts='下载全部', steps=['设置','预选','下载','完成'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='已接收 {current:.1f} MB',
        paused='扫描已暂停', scanned='已扫描 {pages} 页 · 收集 {posts} 个图集',
        paste_empty='剪贴板中没有可用的链接。', stop='停止下载', safe_stop='正在停止并清理未完成的图集…',
        settled='本次下载已结束', copy_empty='暂无可复制的日志'),
    '繁體中文': dict(workspace='下載工作台',
        source='畫師首頁', destination='儲存位置', options='下載方式', activity='任務概覽',
        source_hint='請輸入完整的畫師首頁連結', folder_hint='選擇儲存圖片與附件的資料夾',
        paste='貼上連結', log='執行日誌', log_hint='任務紀錄將顯示於此', ready='準備就緒',
        setup='等待設定', scan='掃描中', choose='選擇圖集', download='下載中', complete='已完成',
        error='需要檢查', stopped='已停止', stopping='正在停止', albums='目前圖集', files='已儲存檔案',
        speed='即時速度', current='目前檔案', waiting='等待開始', focus='收起日誌 · Ctrl+L',
        expand='顯示日誌 · Ctrl+L', copy='複製日誌', plan='本次計畫',
        newest='新到舊', oldest='舊到新', folders='按圖集整理', flat='平鋪儲存',
        selection='先預選', all_posts='下載全部', steps=['設定','預選','下載','完成'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='已接收 {current:.1f} MB',
        paused='掃描已暫停', scanned='已掃描 {pages} 頁 · 收集 {posts} 個圖集',
        paste_empty='剪貼簿中沒有可用的連結。', stop='停止下載', safe_stop='正在停止並清理未完成的圖集…',
        settled='本次下載已結束', copy_empty='暫無可複製的日誌'),
    'English': dict(workspace='Download workspace',
        source='Creator profile', destination='Save location', options='Download preferences', activity='Activity',
        source_hint='Enter a full creator profile URL', folder_hint='Choose a folder for images and attachments',
        paste='Paste link', log='Activity log', log_hint='Your task history will appear here', ready='Ready',
        setup='Set up a task', scan='Scanning', choose='Choose posts', download='Downloading', complete='Complete',
        error='Needs attention', stopped='Stopped', stopping='Stopping', albums='Current post', files='Files saved',
        speed='Transfer speed', current='CURRENT FILE', waiting='Waiting to begin', focus='Hide log · Ctrl+L',
        expand='Show log · Ctrl+L', copy='Copy log', plan='THIS SESSION',
        newest='Newest first', oldest='Oldest first', folders='Post folders', flat='Single folder',
        selection='Choose posts', all_posts='All posts', steps=['Set up','Select','Download','Done'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='{current:.1f} MB received',
        paused='Scan paused', scanned='{pages} pages scanned · {posts} posts found',
        paste_empty='No usable link on the clipboard.', stop='Stop download', safe_stop='Stopping and cleaning up the unfinished post…',
        settled='This download has ended', copy_empty='There is no log to copy yet'),
    '日本語': dict(workspace='ダウンロードワークスペース',
        source='クリエイターページ', destination='保存先', options='ダウンロード設定', activity='タスクの状況',
        source_hint='クリエイターページの完全なURLを入力', folder_hint='画像と添付ファイルの保存先を選択',
        paste='リンクを貼付', log='実行ログ', log_hint='タスクの履歴がここに表示されます', ready='準備完了',
        setup='設定待ち', scan='スキャン中', choose='投稿を選択', download='ダウンロード中', complete='完了',
        error='確認が必要', stopped='停止済み', stopping='停止中', albums='現在の投稿', files='保存済み',
        speed='転送速度', current='現在のファイル', waiting='開始待ち', focus='ログを隠す · Ctrl+L',
        expand='ログを表示 · Ctrl+L', copy='ログをコピー', plan='今回の設定',
        newest='新しい順', oldest='古い順', folders='投稿別フォルダー', flat='同じフォルダー',
        selection='投稿を選択', all_posts='すべての投稿', steps=['設定','選択','転送','完了'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='{current:.1f} MB 受信済み',
        paused='スキャン一時停止', scanned='{pages} ページ確認 · {posts} 件の投稿',
        paste_empty='クリップボードに有効なリンクがありません。', stop='ダウンロード停止', safe_stop='停止して未完了の投稿を削除しています…',
        settled='今回のダウンロードは終了しました', copy_empty='コピーできるログがありません'),
}


class ElidedLabel(QLabel):
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(28)

    def set_full_text(self, text):
        self.full_text = text
        self.setToolTip(text)
        self._refresh()

    def _refresh(self):
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle,
                                                 max(20, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()


class RoundedLogo(QWidget):
    def __init__(self,path,parent=None):
        super().__init__(parent)
        self.pixmap=QIcon(str(path)).pixmap(128,128)
        self.setFixedSize(44,44)
        self.setAccessibleName('Pawchive')

    def paintEvent(self,event):
        painter=QPainter(self)
        try:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing|QPainter.RenderHint.SmoothPixmapTransform)
            area=QRectF(self.rect()).adjusted(1,1,-1,-1)
            mask=QPainterPath(); mask.addRoundedRect(area,11,11)
            painter.setClipPath(mask)
            painter.drawPixmap(area,self.pixmap,QRectF(self.pixmap.rect()))
        finally: painter.end()


class LogEditor(QTextEdit):
    def sizeHint(self):
        hint=super().sizeHint()
        hint.setHeight(110)
        return hint

    def append(self,text):
        # Artist titles and filenames are untrusted text, never HTML markup.
        from PySide6.QtGui import QTextCursor
        scrollbar=self.verticalScrollBar(); follow=scrollbar.value()>=scrollbar.maximum()-4
        cursor=QTextCursor(self.document()); cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.document().isEmpty():cursor.insertBlock()
        cursor.insertText(str(text))
        if follow:scrollbar.setValue(scrollbar.maximum())


def text_label(text='', name=''):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    return widget


def build_interface(w, *, ThemeSwitch, SoftComboBox, GlowButton, GlowCheckBox,
                    RainbowProgress, RainbowCredit, languages, icon_path):
    root = QVBoxLayout(w)
    root.setContentsMargins(30, 24, 30, 18)
    root.setSpacing(20)
    header = QHBoxLayout()
    header.setSpacing(14)
    w.logo=RoundedLogo(icon_path)
    header.addWidget(w.logo)
    w.heading = text_label('Pawchive', 'brand')
    w.heading.setText('Pawchive')
    header.addWidget(w.heading,1)
    w.edition = text_label('DESKTOP  /  1.1', 'edition')
    header.addWidget(w.edition)
    w.reward_badge = text_label('', 'rewardBadge')
    w.reward_badge.setMinimumWidth(120)
    w.reward_badge.setMaximumWidth(180)
    w.reward_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w.reward_badge.setToolTip('')
    header.addWidget(w.reward_badge)
    w.tools_button=QPushButton()
    w.tools_button.setObjectName('toolButton'); w.tools_button.setFixedSize(40,40)
    w.tools_menu=QMenu(w)
    w.schedule_action=w.tools_menu.addAction('')
    w.calendar_action=w.tools_menu.addAction('')
    w.resume_action=w.tools_menu.addAction('')
    w.background_action=w.tools_menu.addAction('')
    w.schedule_action.triggered.connect(lambda: w.services.show_schedules() if w.services else None)
    w.calendar_action.triggered.connect(lambda: w.services.show_calendar() if w.services else None)
    w.resume_action.triggered.connect(lambda: w.services.offer_resume() if w.services else None)
    w.background_action.triggered.connect(lambda: w.services.keep_background() if w.services else None)
    w.tools_button.setMenu(w.tools_menu); w.tools_button.setEnabled(w.services is not None)
    header.addWidget(w.tools_button)
    w.focus_button = QPushButton()
    w.focus_button.setObjectName('toolButton')
    w.focus_button.setFixedSize(40, 40)
    w.focus_button.clicked.connect(w.toggle_logs)
    header.addWidget(w.focus_button)
    w.theme = ThemeSwitch(w.apply_theme)
    header.addWidget(w.theme)
    w.clear_cache = QPushButton()
    w.clear_cache.setObjectName('toolButton')
    w.clear_cache.setFixedSize(40, 40)
    w.clear_cache.clicked.connect(w.clear_cache_files)
    header.addWidget(w.clear_cache)
    w.language = SoftComboBox()
    w.language.addItems(languages)
    w.language.setFixedWidth(142)
    w.language.setMinimumHeight(40)
    w.language.currentTextChanged.connect(w.set_lang)
    header.addWidget(w.language)
    root.addLayout(header)

    w.stage_rail = StageRail()
    root.addWidget(w.stage_rail)
    content = QHBoxLayout()
    content.setSpacing(20)
    w.main_scroll = QScrollArea()
    w.main_scroll.setObjectName('configurationScroll')
    w.main_scroll.setWidgetResizable(True)
    w.main_scroll.setFrameShape(QFrame.Shape.NoFrame)
    w.main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    w.main_scroll.setMinimumWidth(460)
    w.main_card = QFrame()
    w.main_card.setObjectName('mainCard')
    box = QVBoxLayout(w.main_card)
    box.setContentsMargins(26, 26, 26, 24)
    box.setSpacing(12)
    w.workspace_heading = CardHeading('', index='01')
    box.addWidget(w.workspace_heading)

    w.profile_label = text_label('', 'sectionLabel')
    w.profile_label.setWordWrap(True)
    box.addWidget(w.profile_label)
    profile_row = QHBoxLayout()
    profile_row.setSpacing(8)
    w.profile = QLineEdit()
    w.profile.setMinimumHeight(54)
    w.profile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    w.profile.textChanged.connect(w.validate_profile)
    profile_row.addWidget(w.profile, 1)
    w.paste_button = GlowButton()
    w.paste_button.setFixedSize(46, 54)
    w.paste_button.clicked.connect(w.paste_profile)
    profile_row.addWidget(w.paste_button)
    box.addLayout(profile_row)
    w.profile_hint = text_label('', 'muted')
    w.profile_hint.setWordWrap(True)
    box.addWidget(w.profile_hint)

    separator = QFrame()
    separator.setObjectName('separator')
    separator.setFixedHeight(1)
    box.addWidget(separator)
    folder_row = QHBoxLayout()
    w.folder_label = text_label('', 'sectionLabel')
    w.folder_label.setWordWrap(True)
    folder_row.addWidget(w.folder_label, 1)
    w.browse = GlowButton()
    w.browse.setMinimumHeight(38)
    w.browse.clicked.connect(w.pick)
    folder_row.addWidget(w.browse)
    box.addLayout(folder_row)
    w.path = QLineEdit()
    w.path.setMinimumHeight(52)
    w.path.setReadOnly(True)
    w.path.setText(w.settings.value('default_save_path', '', type=str))
    box.addWidget(w.path)
    w.default_path_check = GlowCheckBox()
    w.default_path_check.toggled.connect(w.save_default_path_setting)
    box.addWidget(w.default_path_check)

    w.options_panel = QFrame()
    w.options_panel.setObjectName('optionsPanel')
    options = QVBoxLayout(w.options_panel)
    options.setContentsMargins(0, 5, 0, 0)
    options.setSpacing(14)
    ordering = QHBoxLayout()
    w.order_label = text_label('', 'optionLabel')
    ordering.addWidget(w.order_label)
    ordering.addStretch(1)
    w.order_combo = SoftComboBox()
    w.order_combo.addItems(['', ''])
    w.order_combo.setMinimumWidth(176)
    ordering.addWidget(w.order_combo)
    options.addLayout(ordering)
    w.title_check = GlowCheckBox()
    w.title_check.setChecked(True)
    w.select_posts_check = GlowCheckBox()
    w.select_posts_check.setChecked(True)
    options.addWidget(w.title_check)
    options.addWidget(w.select_posts_check)
    box.addWidget(w.options_panel)

    w.rename_container = QWidget()
    w.rename_row = QVBoxLayout(w.rename_container)
    w.rename_row.setContentsMargins(0, 0, 0, 0)
    w.rename_row.setSpacing(12)
    w.rename_check = GlowCheckBox()
    w.rename_check.setChecked(True)
    w.rename_row.addWidget(w.rename_check)
    number_row = QHBoxLayout()
    w.start_number_label = text_label('', 'muted')
    number_row.addWidget(w.start_number_label)
    w.start_number = QSpinBox()
    w.start_number.setRange(0, 999999999)
    w.start_number.setValue(1)
    w.start_number.setMinimumWidth(120)
    number_row.addWidget(w.start_number)
    number_row.addStretch(1)
    w.rename_row.addLayout(number_row)
    box.addWidget(w.rename_container)
    w.rename_container.hide()
    w.title_check.toggled.connect(lambda checked: w.rename_container.setVisible(not checked))
    w.title_check.toggled.connect(w.update_note)
    w.title_check.toggled.connect(w.update_plan)
    w.select_posts_check.toggled.connect(w.update_plan)
    w.order_combo.currentIndexChanged.connect(w.update_plan)
    box.addStretch(1)
    w.start = GlowButton()
    w.start.setObjectName('primaryButton')
    w.start.setMinimumHeight(56)
    w.start.clicked.connect(w.download)
    w.start.setEnabled(False)
    box.addWidget(w.start)
    w.note = text_label('', 'small')
    w.note.setWordWrap(True)
    box.addWidget(w.note)
    w.main_scroll.setWidget(w.main_card)
    content.addWidget(w.main_scroll, 3)

    w.activity_body=QWidget()
    sidebar = QVBoxLayout(w.activity_body)
    sidebar.setContentsMargins(0,0,0,0)
    sidebar.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    sidebar.setSpacing(16)
    w.task_card = QFrame()
    w.task_card.setObjectName('taskCard')
    task = QVBoxLayout(w.task_card)
    task.setContentsMargins(24, 24, 24, 22)
    task.setSpacing(12)
    task_head = QHBoxLayout()
    w.activity_heading = text_label('', 'sectionLabel')
    task_head.addWidget(w.activity_heading, 1)
    w.state_badge = text_label('', 'stateBadge')
    w.state_badge.setMinimumHeight(24)
    task_head.addWidget(w.state_badge)
    task.addLayout(task_head)
    w.plan_label = text_label('', 'eyebrow')
    task.addWidget(w.plan_label)
    w.plan = text_label('', 'small')
    w.plan.setWordWrap(True)
    task.addWidget(w.plan)
    task.addStretch(1)
    progress_row = QHBoxLayout()
    w.percent_label = text_label('0', 'progressNumber')
    w.percent_label.setMinimumHeight(70)
    w.percent_unit = text_label('%', 'progressUnit')
    progress_row.addWidget(w.percent_label)
    progress_row.addWidget(w.percent_unit, 0, Qt.AlignmentFlag.AlignBottom)
    progress_row.addStretch(1)
    w.progress_caption=text_label('', 'small')
    progress_row.addWidget(w.progress_caption,0,Qt.AlignmentFlag.AlignBottom)
    task.addLayout(progress_row)
    w.progress = RainbowProgress()
    w.progress.setMinimumWidth(0)
    w.progress.setFixedHeight(24)
    task.addWidget(w.progress)
    metrics = QGridLayout()
    metrics.setHorizontalSpacing(18)
    metrics.setVerticalSpacing(8)
    w.album_label = text_label('', 'small')
    w.speed_label = text_label('', 'small')
    w.files_label = text_label('', 'small')
    w.album_value = text_label('— / —', 'metric')
    w.speed_value = text_label('—', 'metric')
    w.files_value = text_label('0', 'metric')
    for column, (caption, value) in enumerate(((w.album_label, w.album_value),
                    (w.speed_label, w.speed_value), (w.files_label, w.files_value))):
        metrics.addWidget(caption, 0, column)
        metrics.addWidget(value, 1, column)
        metrics.setColumnStretch(column, 1)
    task.addLayout(metrics)
    line = QFrame()
    line.setObjectName('separator')
    line.setFixedHeight(1)
    task.addWidget(line)
    w.current_label = text_label('', 'eyebrow')
    task.addWidget(w.current_label)
    w.file_name = ElidedLabel()
    w.file_name.setObjectName('fileName')
    task.addWidget(w.file_name)
    w.status_container = QWidget()
    state = QVBoxLayout(w.status_container)
    state.setContentsMargins(0, 0, 0, 0)
    state.setSpacing(12)
    w.status = text_label('', 'muted')
    w.status.setWordWrap(True)
    w.status.setMinimumHeight(28)
    w.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    state.addWidget(w.status)
    controls = QHBoxLayout()
    w.open_folder_button = GlowButton()
    w.open_folder_button.setMinimumHeight(36)
    w.open_folder_button.clicked.connect(w.open_download_folder)
    w.open_folder_button.hide()
    controls.addWidget(w.open_folder_button)
    w.stop_button = QPushButton()
    w.stop_button.setMinimumHeight(36)
    w.stop_button.setObjectName('stopButton')
    w.stop_button.clicked.connect(w.stop_download)
    w.stop_button.hide()
    controls.addWidget(w.stop_button)
    controls.addStretch(1)
    state.addLayout(controls)
    task.addWidget(w.status_container)
    task.addStretch(1)
    sidebar.addWidget(w.task_card, 3)

    w.logs_card = QFrame()
    w.logs_card.setObjectName('logCard')
    logs = QVBoxLayout(w.logs_card)
    logs.setContentsMargins(20, 18, 20, 18)
    logs.setSpacing(12)
    log_header = QHBoxLayout()
    w.log_heading = text_label('', 'sectionLabel')
    log_header.addWidget(w.log_heading, 1)
    w.copy_log = QPushButton()
    w.copy_log.setObjectName('toolButton')
    w.copy_log.setFixedSize(34, 34)
    w.copy_log.clicked.connect(w.copy_log_text)
    log_header.addWidget(w.copy_log)
    logs.addLayout(log_header)
    w.log = LogEditor()
    w.log.setReadOnly(True)
    w.log.setMinimumHeight(110)
    w.log.textChanged.connect(lambda: w.copy_log.setEnabled(not w.log.document().isEmpty()))
    w.copy_log.setEnabled(False)
    logs.addWidget(w.log, 1)
    sidebar.addWidget(w.logs_card, 2)
    w.activity_scroll=QScrollArea()
    w.activity_scroll.setWidgetResizable(True)
    w.activity_scroll.setFrameShape(QFrame.Shape.NoFrame)
    w.activity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    w.activity_scroll.setWidget(w.activity_body)
    content.addWidget(w.activity_scroll,2)
    root.addLayout(content, 1)

    footer = QHBoxLayout()
    w.footer_hint = text_label('', 'footerHint')
    footer.addWidget(w.footer_hint)
    footer.addStretch(1)
    w.credit = RainbowCredit('千泽音奈  ·  QQ：3412892533')
    w.credit.setMinimumWidth(260)
    w.credit.setMaximumWidth(340)
    footer.addWidget(w.credit)
    root.addLayout(footer)
    w.shortcuts = []
    for sequence, callback in [('Ctrl+L', w.toggle_logs), ('Ctrl+Shift+V', w.paste_profile)]:
        shortcut = QShortcut(QKeySequence(sequence), w)
        shortcut.activated.connect(callback)
        w.shortcuts.append(shortcut)
    for button in w.findChildren(QPushButton):
        button.setCursor(Qt.CursorShape.PointingHandCursor)


def apply_style(w, dark, skin='default'):
    p = dict(bg='#10141c', card='#171e29', panel='#141b25', text='#e9edf6',
             muted='#99a6bb', small='#748399', line='#2a3445', accent='#9cb3ff',
             field='#111822', hover='#222f43', button='#a7bcff', button_text='#14203c') if dark else dict(
             bg='#f4f5f2', card='#ffffff', panel='#fafbf8', text='#252e42',
             muted='#68758a', small='#8993a3', line='#dee4e9', accent='#516aca',
             field='#f7f9fb', hover='#edf1fc', button='#536fd1', button_text='#ffffff')
    # Reward skins only tune the accent palette; the light/dark switch still
    # controls contrast and remains available on every unlocked skin.
    skin_values = {
        'ocean': ({
            'bg':'#081923','card':'#0d2635','panel':'#0b202e','text':'#e9fbff',
            'muted':'#9fc8d6','small':'#6e9eae','line':'#1c4c60','accent':'#63d4ef',
            'field':'#0a1d2a','hover':'#143b4d','button':'#74ddf3','button_text':'#06202b',
            'glow':'#55ccea'}, {
            'bg':'#effcff','card':'#ffffff','panel':'#f5fdff','text':'#163544',
            'muted':'#527b89','small':'#7398a4','line':'#c8e6ee','accent':'#198eaf',
            'field':'#f2fbfd','hover':'#e0f5fa','button':'#168eae','button_text':'#ffffff',
            'glow':'#3caec7'}),
        'cyberpunk': ({
            'bg':'#120d1e','card':'#1b1129','panel':'#171025','text':'#f8efff',
            'muted':'#bda7d2','small':'#8c72a6','line':'#3e2c54','accent':'#ff68c7',
            'field':'#160e24','hover':'#322044','button':'#ff78d0','button_text':'#2b0d2a',
            'glow':'#ff5cb8'}, {
            'bg':'#fff5fd','card':'#ffffff','panel':'#fff8fd','text':'#321d3e',
            'muted':'#805b8c','small':'#9a78a4','line':'#ecd7ed','accent':'#d445a4',
            'field':'#fff7fd','hover':'#fce6f6','button':'#d445a4','button_text':'#ffffff',
            'glow':'#df69b7'}),
    }
    values = skin_values.get(str(skin))
    if values:
        p.update(values[0 if dark else 1])
    else:
        p['hover']='#2b243b' if dark else '#f1ecfa'
    p.setdefault('glow', DARK_GLOW if dark else LIGHT_GLOW)
    w.setStyleSheet(f'''
        QWidget {{background:transparent;color:{p['text']};font-family:"Segoe UI","Microsoft YaHei UI";font-size:14px;}}
        QWidget#appRoot,QDialog {{background:{p['bg']};}}
        QLabel {{background:transparent;border:0;padding:0;}}
        QLabel#brand {{font-family:Georgia,"Palatino Linotype";font-size:27px;font-weight:600;letter-spacing:.3px;}}
        QLabel#muted {{color:{p['muted']};font-size:12px;}}
        QLabel#small {{color:{p['muted']};font-size:12px;}}
        QLabel#eyebrow {{color:{p['small']};font-size:10px;font-weight:600;letter-spacing:1px;}}
        QLabel#edition,QLabel#footerHint {{color:{p['small']};font-size:10px;letter-spacing:1px;}}
        QLabel#rewardBadge {{color:{p['accent']};background:{p['hover']};border:1px solid {p['line']};border-radius:10px;padding:6px 10px;font-size:11px;font-weight:600;}}
        QLabel#sectionLabel {{font-size:14px;font-weight:600;}}
        QLabel#optionLabel {{color:{p['muted']};font-size:13px;}}
        QLabel#progressNumber {{font-size:56px;font-weight:600;letter-spacing:-2px;}}
        QLabel#progressUnit {{color:{p['muted']};font-size:19px;padding-bottom:12px;}}
        QLabel#metric {{font-size:17px;font-weight:600;}}
        QLabel#fileName {{font-size:13px;}}
        QLabel#stateBadge {{color:{p['accent']};background:{p['hover']};border-radius:9px;padding:5px 9px;font-size:11px;}}
        QFrame#mainCard,QFrame#taskCard,QFrame#logCard {{background:{p['card']};border:1px solid {p['line']};border-radius:19px;}}
        QFrame#logCard {{background:{p['panel']};}}
        QFrame#separator {{background:{p['line']};border:0;}}
        QScrollArea,QScrollArea>QWidget>QWidget {{background:transparent;border:0;}}
        QLineEdit {{background:{p['field']};border:1px solid {p['line']};border-radius:11px;padding:10px 13px;selection-background-color:{p['accent']};}}
        QLineEdit:focus {{border-color:{p['glow']};}}
        QPushButton {{border:1px solid {p['line']};border-radius:11px;background:transparent;padding:9px 13px;font-size:13px;}}
        QPushButton:hover {{background:{p['hover']};border-color:{p['glow']};}}
        QPushButton:pressed {{background:{p['field']};}}
        QPushButton:disabled {{color:{p['small']};border-color:{p['line']};}}
        QPushButton#primaryButton {{background:{p['button']};color:{p['button_text']};border:0;font-size:15px;font-weight:600;border-radius:13px;}}
        QPushButton#primaryButton:hover {{background:{'#bbcbff' if dark else '#637ddd'};}}
        QPushButton#primaryButton:disabled {{background:{p['hover']};color:{p['small']};}}
        QPushButton#toolButton {{padding:6px;border-color:transparent;border-radius:10px;}}
        QPushButton#toolButton:hover {{border-color:{p['glow']};background:{p['hover']};}}
        QPushButton#toolButton[selected="true"] {{border-color:{p['glow']};background:{p['hover']};}}
        QPushButton[animatedGlow="true"],QPushButton[animatedGlow="true"]:hover,QPushButton[animatedGlow="true"]:disabled {{border-color:transparent;}}
        QPushButton#stopButton {{color:{'#ffadb4' if dark else '#b44457'};}}
        QComboBox {{background:{p['field']};border:1px solid {p['line']};border-radius:10px;padding:8px 26px 8px 12px;min-height:22px;}}
        QComboBox:hover,QComboBox:focus {{border-color:{p['glow']};}}
        QComboBox QAbstractItemView {{background:{p['card']};border:1px solid {p['line']};selection-background-color:{p['hover']};selection-color:{p['text']};outline:0;padding:6px;}}
        QComboBox::drop-down {{background:transparent;border:0;width:26px;}}
        QComboBox::down-arrow {{image:none;width:0;height:0;border:0;}}
        QCheckBox {{spacing:11px;padding:5px 0;background:transparent;}}
        QCheckBox::indicator {{width:19px;height:19px;border:1px solid {p['small']};border-radius:5px;background:{p['field']};}}
        QCheckBox::indicator:hover {{border:1px solid {p['glow']};}}
        QCheckBox::indicator:checked {{background:{p['accent']};border:1px solid {p['accent']};}}
        QCheckBox:disabled {{color:{p['small']};}}
        QSpinBox {{background:{p['field']};border:1px solid {p['line']};border-radius:8px;padding:6px 10px;}}
        QTextEdit {{background:transparent;border:0;padding:0;font-size:12px;selection-background-color:{p['hover']};}}
        QScrollBar:vertical {{background:transparent;width:9px;margin:4px 0;}}
        QScrollBar::handle:vertical {{background:{p['line']};border-radius:4px;min-height:40px;}}
        QScrollBar::handle:vertical:hover {{background:{p['small']};}}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
        QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{background:transparent;}}
        QToolTip {{background:{p['card']};color:{p['text']};border:1px solid {p['line']};padding:7px;}}
        QMenu {{background:{p['card']};color:{p['text']};border:1px solid {p['line']};padding:6px;}}
        QMenu::item {{padding:10px 18px;border-radius:7px;}}
        QMenu::item:selected {{background:{p['hover']};}}
        QPushButton::menu-indicator {{image:none;width:0;height:0;}}
        QListWidget {{background:{p['field']};border:1px solid {p['line']};border-radius:10px;padding:7px;}}
        QListWidget::item {{padding:9px;border-radius:6px;}}
        QListWidget::item:selected {{background:{p['hover']};}}
        QDateTimeEdit {{background:{p['field']};border:1px solid {p['line']};border-radius:9px;padding:8px;}}
        QCalendarWidget QWidget#qt_calendar_navigationbar {{background:{p['card']};}}
        QCalendarWidget QToolButton {{background:transparent;color:{p['text']};border:0;padding:7px;}}
        QCalendarWidget QAbstractItemView {{background:{p['field']};color:{p['text']};selection-background-color:{p['glow']};selection-color:white;}}
    ''')
    w.stage_rail.set_theme(dark)
    w.workspace_heading.set_theme(dark)
    w.palette_tokens = p
    for button in w.findChildren(QPushButton):
        if button.property('animatedGlow'):
            button.setProperty('glowColor',p['glow'])
            button.setProperty('borderColor',p['line'])
            button.update()
    for combo in (w.language,w.order_combo):
        combo.arrow_color=p['muted']; combo.update()
    for button, kind in ((w.paste_button,'paste'), (w.clear_cache,'broom'),
                         (w.copy_log,'copy'), (w.focus_button,'eye'),
                         (w.browse,'folder'), (w.open_folder_button,'folder'), (w.start,'arrow'),(w.tools_button,'settings')):
        button.setIcon(icon(kind, p['muted'] if button is not w.start else p['button_text']))
        button.setIconSize(QSize(17, 17))
    w.progress.track = p['line']
    w.progress.colors = ['#f07e99','#f5b864','#e5ce72','#72cba3','#67c9d8','#91a7ef','#c094dd'] if dark else [
        '#d05d80','#d9983d','#c4a736','#3d9e78','#369aad','#647bd0','#9a66bc']
    w.progress.colors = [QColor(c) for c in w.progress.colors]
    w.progress.update()
    w.set_workflow(w.workflow_state)


def translate_interface(w, text, auxiliary):
    from feature_text import tr
    t = COPY[w.lang]
    w.setWindowTitle(text['title'])
    w.workspace_heading.title_label.setText(t['workspace'])
    w.profile_label.setText(t['source'])
    w.profile.setPlaceholderText(t['source_hint'])
    w.folder_label.setText(t['destination'])
    w.path.setPlaceholderText(t['folder_hint'])
    w.activity_heading.setText(t['activity'])
    w.log_heading.setText(t['log'])
    w.log.setPlaceholderText(t['log_hint'])
    w.plan_label.setText(t['plan'])
    w.current_label.setText(t['current'])
    w.progress_caption.setText({'简体中文':'当前文件进度','繁體中文':'目前檔案進度','English':'FILE PROGRESS','日本語':'現在のファイル進捗'}[w.lang])
    w.album_label.setText(t['albums'])
    w.speed_label.setText(t['speed'])
    w.files_label.setText(t['files'])
    w.paste_button.setToolTip(t['paste'] + ' · Ctrl+Shift+V')
    w.paste_button.setAccessibleName(t['paste'])
    w.focus_button.setToolTip(t['expand'] if w.logs_hidden else t['focus'])
    w.focus_button.setAccessibleName(w.focus_button.toolTip())
    w.copy_log.setToolTip(t['copy'])
    w.copy_log.setAccessibleName(t['copy'])
    w.clear_cache.setToolTip(auxiliary['cache_tooltip'])
    w.clear_cache.setAccessibleName(auxiliary['cache_tooltip'])
    theme_tip={'简体中文':'切换明暗主题','繁體中文':'切換明暗主題','English':'Toggle appearance','日本語':'明暗テーマを切替'}[w.lang]
    w.theme.setToolTip(theme_tip)
    w.theme.setAccessibleName(theme_tip)
    w.start.setToolTip(text['start'])
    w.footer_hint.setText(t['expand'] if w.logs_hidden else t['focus'])
    w.stop_button.setText(t['stop'])
    w.stop_button.setToolTip(auxiliary['close_wait'])
    w.browse.setText(text['browse'])
    w.start.setText(text['start'])
    w.open_folder_button.setText(text['open_folder'])
    w.stage_rail.set_labels(t['steps'])
    w.tools_button.setToolTip(tr(w.lang,'tools'))
    w.tools_button.setAccessibleName(tr(w.lang,'tools'))
    w.schedule_action.setText(tr(w.lang,'schedules'))
    w.calendar_action.setText(tr(w.lang,'calendar'))
    w.resume_action.setText(tr(w.lang,'resume'))
    w.background_action.setText(tr(w.lang,'background'))
