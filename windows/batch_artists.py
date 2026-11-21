"""A shared paste board; the workspace owns and advances the scan queue."""
import re

from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QTextEdit,QDialogButtonBox
from artist_lookup import input_profiles
from link_utils import parse_url
from ui_i18n import language_for,t

COPY={
    'title':('批量添加画师','批次新增畫師','Add multiple artists','作家を一括追加'),
    'hint':('每行一个画师 ID 或链接，也可以用空格、逗号分隔。将按输入顺序依次扫描。','每行輸入一個畫師 ID 或連結，也可以用空格、逗號分隔。將依輸入順序逐一掃描。','Enter artist IDs or URLs, separated by new lines, spaces or commas. Artists are scanned in input order.','作家IDまたはURLを改行・空白・コンマで区切って入力してください。入力順にスキャンします。'),
    'count':('有效画师：{count}（重复项自动合并）','有效畫師：{count}（自動合併重複項）','Valid artists: {count} (duplicates merged)','有効な作家：{count}（重複は統合）'),
}


def copy(parent,key,**values):
    languages=('简体中文','繁體中文','English','日本語');language=language_for(parent)
    return COPY[key][languages.index(language) if language in languages else 0].format(**values)


def split_batch_inputs(value):
    """Preserve input order; full URLs retain an explicit platform."""
    result=[];seen=set()
    for token in re.split(r'[\s,，;；]+',str(value or '').strip()):
        token=token.strip().rstrip('。.,，;；')
        if not token or not input_profiles(token):continue
        link=parse_url(token)
        normalized=link.profile_url if link else token
        if normalized in seen:continue
        seen.add(normalized);result.append(normalized)
    return result


class BatchArtistDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent);self.lang=language_for(parent)
        self.setWindowTitle(copy(parent,'title'));self.resize(580,440)
        layout=QVBoxLayout(self);layout.setContentsMargins(22,22,22,22)
        hint=QLabel(copy(self,'hint'));hint.setWordWrap(True);layout.addWidget(hint)
        self.editor=QTextEdit();self.editor.setAcceptRichText(False)
        self.editor.setPlaceholderText('1250474\nhttps://pawchive.pw/fanbox/user/3439325')
        layout.addWidget(self.editor,1)
        self.count=QLabel();layout.addWidget(self.count)
        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t(self.lang,'确定'))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t(self.lang,'取消'))
        self.buttons.accepted.connect(self.accept);self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons);self.editor.textChanged.connect(self._refresh)
        self._refresh()

    @property
    def inputs(self):return split_batch_inputs(self.editor.toPlainText())

    def _refresh(self):
        values=self.inputs;self.count.setText(copy(self,'count',count=len(values)))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(values))
