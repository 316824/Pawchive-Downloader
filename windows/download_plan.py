"""A shared confirmation for recommended posts and saved artist profiles."""
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,
    QPushButton,QFileDialog,QCheckBox,QComboBox,QDialogButtonBox)
from feature_text import LANGUAGES
from ui_i18n import language_for, localize_dialog, t


_COPY={
    'confirm':('确认下载设置','確認下載設定','Confirm download settings','ダウンロード設定の確認'),
    'post':('下载此图集的图片','下載此圖集的圖片','Download this collection’s images','このコレクションの画像をダウンロード'),
    'updates':('扫描该画师的新图集并选择下载','掃描該畫師的新圖集並選擇下載','Scan new collections and choose downloads','新しいコレクションを検索して選択'),
    'artist':('扫描该画师的全部图集并选择下载','掃描該畫師的全部圖集並選擇下載','Scan this artist’s collections and choose downloads','この作家のコレクションを検索して選択'),
    'choose':('选择保存位置','選擇儲存位置','Choose a save folder','保存先を選択'),
    'browse':('选择保存位置…','選擇儲存位置…','Choose save folder…','保存先を選択…'),
    'folders':('按图集标题分别存入文件夹','按圖集標題分別存入資料夾','Save each collection in a title folder','コレクションごとにタイトル別フォルダーへ保存'),
    'number':('下载后按日期顺序编号重命名（图片从 1 开始）','下載後按日期順序編號重新命名（圖片從 1 開始）','Number images in date order after downloading (starting at 1)','ダウンロード後に日付順で画像に番号を付ける（1から）'),
    'attachments':('直链附件请在预览窗口点击“附件下载”单独保存。','直連附件請在預覽視窗點擊「附件下載」另外儲存。','Save direct-link attachments separately using “Download attachments” in the preview.','直接リンクの添付ファイルは、プレビューの「添付ファイルをダウンロード」から個別に保存してください。'),
    'scan':('开始扫描','開始掃描','Start scan','スキャン開始'),
    'download':('开始下载','開始下載','Start download','ダウンロード開始'),
}


def _copy(language,key):
    return _COPY[key][LANGUAGES.index(language) if language in LANGUAGES else 0]


def _checkbox(parent,dialog,text,template):
    """Reuse the actual workspace control without importing the entry module."""
    factory=getattr(parent,'checkbox_factory',None)
    if factory is None:
        factory=type(template) if isinstance(template,QCheckBox) else QCheckBox
    check=factory(text)
    check.setParent(dialog)
    check.setProperty('lightTheme',not getattr(parent,'_dark',True))
    return check


def ask_download_plan(parent, profile, *, post=None, updates_after=None):
    language=language_for(parent)
    dialog=QDialog(parent)
    dialog.lang=language
    dialog.setWindowTitle(_copy(language,'confirm'))
    dialog.setMinimumWidth(520)
    layout=QVBoxLayout(dialog)
    description=QLabel(_copy(language,'post' if post else
        'updates' if updates_after is not None else 'artist')+'\n'+profile)
    description.setWordWrap(True); layout.addWidget(description)
    row=QHBoxLayout(); folder=QLineEdit(parent.path.text()); folder.setReadOnly(True)
    browse=QPushButton(_copy(language,'browse'))
    def choose():
        selected=QFileDialog.getExistingDirectory(dialog,_copy(language,'choose'),folder.text(),
            options=QFileDialog.Option.DontUseNativeDialog)
        if selected: folder.setText(selected)
    browse.clicked.connect(choose); row.addWidget(folder,1); row.addWidget(browse)
    layout.addLayout(row)
    by_title=_checkbox(parent,dialog,_copy(language,'folders'),parent.title_check)
    by_title.setChecked(parent.title_check.isChecked()); layout.addWidget(by_title)
    rename=_checkbox(parent,dialog,_copy(language,'number'),parent.rename_check)
    rename.setChecked(parent.rename_check.isChecked()); layout.addWidget(rename)
    content=QComboBox(dialog)
    for name,value in (('图片与附件','all'),('仅图片','images'),('仅直链附件','attachments')):
        content.addItem(t(language,name),value)
    content.setCurrentIndex(content.findData('images' if post else parent.content_combo.currentData()))
    if post:
        content.setEnabled(False)
        content.hide()
        layout.addWidget(QLabel(_copy(language,'attachments')))
    else:
        layout.addWidget(content)
    buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
    okay=buttons.button(QDialogButtonBox.StandardButton.Ok)
    okay.setProperty('pawchiveCustomCaption',True)
    okay.setText(_copy(language,'scan' if not post else 'download'))
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t(language,'取消'))
    okay.setEnabled(bool(folder.text().strip()))
    folder.textChanged.connect(lambda text:okay.setEnabled(bool(text.strip())))
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    localize_dialog(dialog,language)
    try:
        if dialog.exec()!=QDialog.DialogCode.Accepted: return None
        return dict(folder=folder.text().strip(),by_title=by_title.isChecked(),
            rename_order=rename.isChecked(),content_mode=content.currentData())
    finally: dialog.deleteLater()
