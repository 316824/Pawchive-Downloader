"""Bounded Fanbox/Patreon ID lookup and asynchronous platform selection."""
from __future__ import annotations

import re
import weakref

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QObject, QThread, Signal, QTimer, Qt, QRectF, QBuffer, QIODevice
from PySide6.QtGui import QImageReader, QPixmap, QPainter, QPainterPath, QColor, QFont
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMessageBox
from shiboken6 import isValid

from artist_cards import normalize_profile, avatar_url
from link_utils import parse_url
from ui_i18n import language_for

COPY = {
    'select': ('请选择画师所属平台', '請選擇畫師所屬平台', 'Choose the artist platform', '作家のプラットフォームを選択'),
    'hint': ('这个 ID 在多个平台存在，请点击对应画师卡片。', '此 ID 存在於多個平台，請點擊對應的畫師卡片。', 'This ID exists on more than one platform. Choose an artist card.', 'このIDは複数のプラットフォームにあります。カードを選択してください。'),
    'none': ('未找到该画师，或网站暂时无法访问。请检查 ID 或稍后重试。', '找不到此畫師，或網站暫時無法存取。請檢查 ID 或稍後重試。', 'Artist not found, or the website is unavailable. Check the ID or try again later.', '作家が見つからないか、サイトに接続できません。IDを確認して再試行してください。'),
    'invalid': ('请输入画师数字 ID 或完整的 Pawchive 画师链接。', '請輸入畫師數字 ID 或完整的 Pawchive 畫師連結。', 'Enter a numeric artist ID or a complete Pawchive artist URL.', '数字の作家IDまたはPawchiveの作家URLを入力してください。'),
    'title': ('查找画师', '尋找畫師', 'Find artist', '作家を検索'),
    'cancel': ('取消', '取消', 'Cancel', 'キャンセル'),
}
LANGUAGES = ('简体中文', '繁體中文', 'English', '日本語')
MAX_HTML_BYTES = 2*1024*1024
MAX_AVATAR_BYTES = 512*1024


def text(parent, key):
    language=language_for(parent)
    return COPY[key][LANGUAGES.index(language) if language in LANGUAGES else 0]


def input_profiles(value):
    value=str(value or '').strip()
    if re.fullmatch(r'[0-9]{1,20}', value):
        return tuple(f'https://pawchive.pw/{platform}/user/{value}' for platform in ('fanbox','patreon'))
    link=parse_url(value)
    return (link.profile_url,) if link else ()


def _body(response, limit, interrupted):
    length=int(response.headers.get('content-length') or 0)
    if length<0 or length>limit: raise ValueError('response_too_large')
    content=bytearray()
    for chunk in response.iter_content(64*1024):
        if interrupted(): raise OSError('cancelled')
        content.extend(chunk)
        if len(content)>limit: raise ValueError('response_too_large')
    return bytes(content)


def _image_bytes(data):
    buffer=QBuffer(); buffer.setData(data)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly): return b''
    try:
        reader=QImageReader(buffer); reader.setDecideFormatFromContent(True)
        size=reader.size()
        if not size.isValid() or min(size.width(),size.height())<1 or max(size.width(),size.height())>2048 or size.width()*size.height()>2_000_000:
            return b''
        image=reader.read()
        return data if not image.isNull() else b''
    finally: buffer.close()


def lookup_profiles(profiles, *, interrupted=lambda:False, session=None):
    """An HTTP 200 without an artist header is not a positive result."""
    owned=session is None
    session=session or requests.Session()
    session.headers['User-Agent']='PawchiveDownloader/1.3'
    result=[]
    try:
        for profile in profiles:
            if interrupted(): break
            profile=normalize_profile(profile)
            if not profile: continue
            try:
                response=session.get(profile,timeout=(6,12),stream=True)
                try:
                    response.raise_for_status()
                    final=parse_url(getattr(response,'url','') or profile)
                    if final is None or final.profile_url != profile: continue
                    html=_body(response,MAX_HTML_BYTES,interrupted).decode('utf-8','replace')
                finally: response.close()
                soup=BeautifulSoup(html,'html.parser')
                name=soup.select_one("a.user-header__profile [itemprop='name'], .user-header [itemprop='name']")
                name=name.get_text(' ',strip=True)[:160] if name else ''
                if not name: continue
                data=b''
                try:
                    response=session.get(avatar_url(profile),timeout=(4,8),stream=True)
                    try:
                        response.raise_for_status()
                        data=_image_bytes(_body(response,MAX_AVATAR_BYTES,interrupted))
                    finally: response.close()
                except (requests.RequestException,OSError,ValueError,RuntimeError): pass
                if interrupted(): break
                result.append(dict(profile=profile,name=name,avatar=data,
                                   platform=profile.split('/')[3].title()))
            except (requests.RequestException,OSError,ValueError,RuntimeError):
                continue
    finally:
        if owned: session.close()
    return result


class ArtistLookupWorker(QThread):
    result=Signal(object)

    def __init__(self, profiles, parent=None):
        super().__init__(parent); self.profiles=tuple(profiles)

    def run(self):
        self.result.emit(lookup_profiles(self.profiles,interrupted=self.isInterruptionRequested))


class ArtistChoiceCard(QPushButton):
    def __init__(self, record, parent=None):
        super().__init__(parent); self.record=dict(record)
        self.setMinimumSize(190,235); self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{record['platform']}: {record['name']}")
        self.setToolTip(record['profile']); self._avatar=QPixmap()
        if record.get('avatar'): self._avatar.loadFromData(record['avatar'])
        if self._avatar.isNull() and parent is not None:
            self._avatar=parent.windowIcon().pixmap(160,160)

    def paintEvent(self,event):
        painter=QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
            path=QPainterPath(); path.addRoundedRect(rect,16,16); painter.setClipPath(path)
            painter.fillRect(rect,QColor('#20283a'))
            if not self._avatar.isNull():
                image=self._avatar.scaled(self.size(),Qt.AspectRatioMode.KeepAspectRatioByExpanding,Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap((self.width()-image.width())//2,(self.height()-image.height())//2,image)
            footer=QRectF(rect.left(),rect.bottom()-76,rect.width(),76)
            painter.fillRect(footer,QColor(12,18,30,218))
            painter.setPen(QColor('#c1b6ff')); painter.setFont(QFont('Microsoft YaHei UI',9))
            painter.drawText(footer.adjusted(12,7,-12,-44),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,self.record['platform'])
            painter.setPen(QColor('#f2f4ff')); painter.setFont(QFont('Microsoft YaHei UI',10))
            painter.drawText(footer.adjusted(12,26,-12,-7),Qt.AlignmentFlag.AlignLeft|Qt.TextFlag.TextWordWrap,self.record['name'])
        finally:painter.end()


class ArtistPlatformDialog(QDialog):
    def __init__(self, parent, records):
        super().__init__(parent); self.lang=language_for(parent); self.selected_record=None
        self.setWindowTitle(text(parent,'select')); self.resize(470,355)
        layout=QVBoxLayout(self); layout.setContentsMargins(20,20,20,20)
        hint=QLabel(text(parent,'hint')); hint.setWordWrap(True); layout.addWidget(hint)
        row=QHBoxLayout()
        for record in records:
            card=ArtistChoiceCard(record,self)
            card.clicked.connect(lambda checked=False,r=record:self.choose(r));row.addWidget(card)
        layout.addLayout(row)
        cancel=QPushButton(text(parent,'cancel'));cancel.clicked.connect(self.reject);layout.addWidget(cancel)

    def choose(self,record):
        self.selected_record=dict(record);self.accept()


class ArtistLookupOperation(QObject):
    """Owned by the workspace: closing a child dialog never destroys a worker."""
    selected=Signal(str)
    finished=Signal(str)

    def __init__(self,parent,value):
        owner=getattr(parent,'parent_window',None) or parent
        super().__init__(owner)
        self._view=weakref.ref(parent);self.value=value;self.selected_record=None
        self.worker=None;self.dialog=None;self._done=False;self._cancelled=False
        registry=getattr(owner,'_artist_lookup_operations',None)
        if registry is None:registry=set();owner._artist_lookup_operations=registry
        self._registry=registry;registry.add(self)

    def start(self):
        if self._done:return
        profiles=input_profiles(self.value)
        if not profiles:
            self._warning('invalid');self._finish('');return
        worker=ArtistLookupWorker(profiles,self.parent());self.worker=worker
        worker.result.connect(self._results);worker.finished.connect(worker.deleteLater)
        worker.start()

    def _warning(self,key):
        view=self._view()
        if not self._cancelled and view is not None and isValid(view) and view.isVisible():
            QMessageBox.warning(view,text(view,'title'),text(view,key))

    def _results(self,records):
        if self._done:return
        view=self._view()
        if self._cancelled or view is None or not isValid(view):self._finish('');return
        if not records:self._warning('none');self._finish('');return
        if len(records)==1:
            self.selected_record=dict(records[0]);self._finish(records[0]['profile']);return
        dialog=ArtistPlatformDialog(view,records);self.dialog=dialog
        dialog.finished.connect(self._choice_finished);dialog.open()

    def _choice_finished(self,result):
        dialog=self.dialog;record=dialog.selected_record if dialog is not None else None
        if record and not self._cancelled:
            self.selected_record=dict(record);self._finish(record['profile'])
        else:self._finish('')
        if dialog is not None:dialog.deleteLater()

    def _finish(self,profile):
        if self._done:return
        self._done=True;self._registry.discard(self)
        if profile and not self._cancelled:self.selected.emit(profile)
        self.finished.emit(profile if not self._cancelled else '')
        self.deleteLater()

    def cancel(self):
        self._cancelled=True
        if self.worker is not None and isValid(self.worker):self.worker.requestInterruption()
        if self.dialog is not None and isValid(self.dialog):self.dialog.reject()
        self._finish('')


def resolve_artist_input(parent,value,on_selected,on_finished=None):
    operation=ArtistLookupOperation(parent,value)
    if callable(on_selected):operation.selected.connect(on_selected)
    if callable(on_finished):operation.finished.connect(on_finished)
    QTimer.singleShot(0,operation.start)
    return operation
