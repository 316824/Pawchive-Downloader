"""A shared confirmation for recommended posts and saved artist profiles."""
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,
    QPushButton,QFileDialog,QCheckBox,QComboBox,QDialogButtonBox)


def ask_download_plan(parent, profile, *, post=None, updates_after=None):
    dialog=QDialog(parent)
    dialog.setWindowTitle('确认下载设置')
    dialog.setMinimumWidth(520)
    layout=QVBoxLayout(dialog)
    description=QLabel(('下载此图集的图片' if post else
        '扫描该画师的新图集并选择下载' if updates_after is not None else '扫描该画师的全部图集并选择下载')+'\n'+profile)
    description.setWordWrap(True); layout.addWidget(description)
    row=QHBoxLayout(); folder=QLineEdit(parent.path.text()); folder.setReadOnly(True)
    browse=QPushButton('选择保存位置…')
    def choose():
        selected=QFileDialog.getExistingDirectory(dialog,'选择保存位置',folder.text(),
            options=QFileDialog.Option.DontUseNativeDialog)
        if selected: folder.setText(selected)
    browse.clicked.connect(choose); row.addWidget(folder,1); row.addWidget(browse)
    layout.addLayout(row)
    by_title=QCheckBox('按图集标题分别存入文件夹')
    by_title.setChecked(parent.title_check.isChecked()); layout.addWidget(by_title)
    rename=QCheckBox('下载后按日期顺序编号重命名（图片从 1 开始）')
    rename.setChecked(parent.rename_check.isChecked()); layout.addWidget(rename)
    content=QComboBox()
    for name,value in (('图片与附件','all'),('仅图片','images'),('仅直链附件','attachments')):
        content.addItem(name,value)
    content.setCurrentIndex(content.findData('images' if post else parent.content_combo.currentData()))
    if post:
        content.setEnabled(False)
        layout.addWidget(QLabel('直链附件请在预览窗口点击“附件下载”单独保存。'))
    else:
        layout.addWidget(content)
    buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
    okay=buttons.button(QDialogButtonBox.StandardButton.Ok)
    okay.setText('开始扫描' if not post else '开始下载')
    okay.setEnabled(bool(folder.text().strip()))
    folder.textChanged.connect(lambda text:okay.setEnabled(bool(text.strip())))
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    try:
        if dialog.exec()!=QDialog.DialogCode.Accepted: return None
        return dict(folder=folder.text().strip(),by_title=by_title.isChecked(),
            rename_order=rename.isChecked(),content_mode=content.currentData())
    finally: dialog.deleteLater()
