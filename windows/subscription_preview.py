"""Disposable, lazy cover preview for unread subscription posts only."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import tempfile
import weakref

from PySide6.QtCore import QObject,QThread,QTimer,QRect,QPoint,Qt,Signal,Slot,QCoreApplication
from PySide6.QtWidgets import QDialog,QLabel,QPushButton,QVBoxLayout,QCheckBox

from background_ui import install_background_host
from feature_text import LANGUAGES
from post_picker import PostPicker
from popular_features import (ImageBytesWorker,_preview_pixmap,_PREVIEW_LOCK,
                              _ACTIVE_PREVIEWS)
from subscriptions import SubscriptionStore,fetch_latest
from ui_i18n import t

_POOLS=set()

_COPY={
 'title':('画师更新预览','畫師更新預覽','Artist updates preview','作家の更新プレビュー'),
 'loading':('正在读取新增图集…','正在讀取新增圖集…','Reading new collections…','新しいコレクションを読み込み中…'),
 'empty':('目前没有待下载的新图集。','目前沒有待下載的新圖集。','There are no new collections to download.','ダウンロードする新しいコレクションはありません。'),
 'failed':('更新图集读取失败，请稍后重试。','更新圖集讀取失敗，請稍後重試。','Could not read the updates. Please try again later.','更新を読み込めません。後で再試行してください。'),
 'waiting':('正在等待后台检查完成…','正在等待背景檢查完成…','Waiting for the background check…','バックグラウンドの確認を待っています…'),
 'cover':('封面暂不可用','封面暫不可用','Cover unavailable','カバーを表示できません'),
 'all':('全选','全選','Select all','すべて選択'),
 'none':('全不选','全不選','Select none','すべて解除'),
 'top':('回到顶部','回到頂端','Back to top','先頭に戻る'),
}


def _copy(language,key):
    return _COPY[key][LANGUAGES.index(language) if language in LANGUAGES else 0]


class _UpdatesWorker(QThread):
    result=Signal(object,str)
    def __init__(self,profile,baseline,parent=None):
        super().__init__(parent);self.profile,self.baseline=profile,baseline
    def run(self):
        try:
            value=fetch_latest(self.profile,baseline_ts=self.baseline,
                               interrupted=self.isInterruptionRequested)
            if not self.isInterruptionRequested():self.result.emit(value,'')
        except Exception as error:
            if not self.isInterruptionRequested():self.result.emit({},str(error)[:160])


class _PreviewPool(QObject):
    """Survive the dialog while byte-only workers wind down; never keep files open."""
    cover=Signal(str,str)
    updates=Signal(object,str)
    def __init__(self,parent):
        # A dialog and even its parent window can close before a bounded HTTP
        # request returns. Retain this owner independently until native finish.
        super().__init__()
        self.window=weakref.ref(parent);_POOLS.add(self)
        parent.destroyed.connect(self.close)
        QCoreApplication.instance().aboutToQuit.connect(self._shutdown)
        self.root=Path(tempfile.mkdtemp(prefix='pawchive_preview_')).resolve()
        with _PREVIEW_LOCK:_ACTIVE_PREVIEWS.add(self.root)
        self.closed=False;self.workers=set();self.asked=set();self.queue=[]
        registry=getattr(parent,'_subscription_preview_pools',None)
        if registry is None:
            registry=set();parent._subscription_preview_pools=registry
        self.registry=registry;registry.add(self)
        self._retry=QTimer(self);self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._pump)

    def fetch(self,profile,baseline):
        if self.closed:return
        worker=_UpdatesWorker(profile,baseline,self)
        worker.result.connect(self._updates_ready);self._start(worker)

    def request(self,urls):
        if self.closed:return
        for url in urls:
            if url and url not in self.asked:
                self.asked.add(url);self.queue.append(url)
        self._pump()

    def _start(self,worker):
        self.workers.add(worker);worker.finished.connect(self._finished)
        worker.finished.connect(worker.deleteLater);worker.start()

    def _pump(self):
        if self.closed:return
        window=self.window()
        if window is None or getattr(window,'_subscription_check_running',False):
            if self.queue:self._retry.start(150)
            return
        while self.queue and sum(len(pool.workers) for pool in _POOLS)<3:
            worker=ImageBytesWorker(self.queue.pop(0),self)
            worker.result.connect(self._cover_ready);self._start(worker)
        if self.queue:self._retry.start(150)

    @Slot(object,str)
    def _updates_ready(self,value,error):
        if not self.closed:self.updates.emit(value,error)

    @Slot(str,object,str)
    def _cover_ready(self,url,payload,error):
        if self.closed or error:return
        pixmap=_preview_pixmap(payload)
        if pixmap.isNull():return
        pixmap=pixmap.scaled(420,300,Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        target=self.root/(hashlib.sha256(url.encode()).hexdigest()+'.png')
        if pixmap.save(str(target),'PNG'):self.cover.emit(url,str(target))

    @Slot()
    def _finished(self):
        self.workers.discard(self.sender())
        if self.closed:
            if not self.workers:self._release()
        else:self._pump()

    def _release(self):
        self.registry.discard(self);_POOLS.discard(self);self.deleteLater()

    @Slot()
    def _shutdown(self):
        self.close()
        for worker in tuple(self.workers):worker.wait()

    @Slot()
    def close(self,*_):
        if self.closed:return
        self.closed=True;self.queue.clear()
        self._retry.stop()
        for worker in self.workers:worker.requestInterruption()
        # Workers only return bounded bytes; all file writes happen in our
        # GUI-thread result slot, which ignores every late result after close.
        if self.root.parent==Path(tempfile.gettempdir()).resolve() and self.root.name.startswith('pawchive_preview_'):
            shutil.rmtree(self.root,ignore_errors=True)
        with _PREVIEW_LOCK:_ACTIVE_PREVIEWS.discard(self.root)
        if not self.workers:self._release()


class UpdatePreviewDialog(QDialog):
    def __init__(self,parent,profile,store=None):
        super().__init__(parent)
        self.parent_window=parent;self.profile=profile
        self.lang=getattr(parent,'lang','简体中文')
        self.store=store or getattr(parent,'subscription_store',None) or SubscriptionStore(parent.services.store)
        self.picker=None;self.download_entries=[];self.cover_copies=[]
        self._closed=False;self._fetching=False
        install_background_host(self,getattr(parent,'background_store',None),'picker')
        self.setWindowTitle(_copy(self.lang,'title'));self.resize(980,750)
        self.layout_box=QVBoxLayout(self);self.layout_box.setContentsMargins(14,14,14,14)
        self.status=QLabel(_copy(self.lang,'loading'));self.status.setWordWrap(True)
        self.layout_box.addWidget(self.status)
        self.cancel=QPushButton(t(self.lang,'关闭'));self.cancel.clicked.connect(self.reject)
        self.layout_box.addWidget(self.cancel)
        self.pool=_PreviewPool(parent);self.pool.cover.connect(self._cover_ready)
        self.pool.updates.connect(self._updates_ready)
        self._visible_timer=QTimer(self);self._visible_timer.setInterval(80)
        self._visible_timer.timeout.connect(self._load_visible)
        self._loading_timer=QTimer(self);self._loading_timer.setInterval(150)
        self._loading_timer.timeout.connect(self._load_record);self._loading_timer.start()
        self._load_record()

    def _load_record(self):
        if self._closed or self.picker is not None:return
        record=self.store.get(self.profile)
        if record is None:
            self.status.setText(_copy(self.lang,'empty'));self._loading_timer.stop();return
        posts=record.get('pending_posts') or []
        if posts:
            self._show_posts(posts);return
        if getattr(self.parent_window,'_subscription_check_running',False):
            self.status.setText(_copy(self.lang,'waiting'));return
        self._loading_timer.stop()
        from datetime import date
        if record.get('last_check_date')==date.today().isoformat():
            self.status.setText(_copy(self.lang,'empty'));return
        if not self._fetching:
            self._fetching=True;self.pool.fetch(self.profile,record.get('baseline_ts',0))

    @Slot(object,str)
    def _updates_ready(self,payload,error):
        if self._closed:return
        if error:
            self.status.setText(_copy(self.lang,'failed'));return
        record=self.store.set_baseline(self.profile,payload.get('latest_ts'),
            name=payload.get('name'),posts=payload.get('posts',[]),unread=True)
        refresh=getattr(self.parent_window,'refresh_subscription_badge',None)
        if callable(refresh):refresh()
        if record and record.get('pending_posts'):self._show_posts(record['pending_posts'])
        else:self.status.setText(_copy(self.lang,'empty'))

    def _show_posts(self,posts):
        if self._closed or self.picker is not None:return
        self._loading_timer.stop();self.status.hide();self.cancel.hide()
        text=dict(choose_title=_copy(self.lang,'title'),cover_missing=_copy(self.lang,'cover'),
                  no_direct='',cancel=t(self.lang,'取消'),select_all=_copy(self.lang,'all'),
                  select_none=_copy(self.lang,'none'),back_top=_copy(self.lang,'top'))
        factory=getattr(self.parent_window,'checkbox_factory',QCheckBox)
        self.picker=PostPicker(self.parent_window,[dict(post) for post in posts],factory,text)
        self.picker.setWindowFlags(Qt.WindowType.Widget);self.picker.setParent(self)
        self.layout_box.insertWidget(0,self.picker,1)
        self.picker.confirm_button.clicked.disconnect()
        self.picker.confirm_button.clicked.connect(self.accept)
        self.picker.cancel_button.clicked.disconnect()
        self.picker.cancel_button.clicked.connect(self.reject)
        self.picker.show();self._visible_timer.start();self._load_visible()

    def _load_visible(self):
        if self._closed or self.picker is None:return
        viewport=self.picker.scroll.viewport();visible=[]
        for index,card in enumerate(self.picker.cards):
            if card is not None and not card.isHidden():
                rect=QRect(card.mapTo(viewport,QPoint(0,0)),card.size())
                if rect.intersects(viewport.rect()):visible.append(self.picker.titles[index].get('image_url'))
        self.pool.request(visible)

    @Slot(str,str)
    def _cover_ready(self,url,path):
        if self._closed or self.picker is None:return
        for index,post in enumerate(self.picker.titles):
            if post.get('image_url')!=url:continue
            post['image']=path;card=self.picker.cards[index]
            if card is not None:
                card.cover.filename=path;card.cover.failed=False;card.cover.update()

    def done(self,result):
        if not self._closed:
            self._closed=True;self._visible_timer.stop();self._loading_timer.stop()
            if self.picker is not None:
                if result==QDialog.DialogCode.Accepted:
                    for index in sorted(self.picker.selected):
                        post=dict(self.picker.titles[index]);image=post.get('image')
                        cache=getattr(self.parent_window,'cache_dir',None)
                        if image and cache:
                            destination=Path(cache)/('subscription-'+self.pool.root.name+'-'+hashlib.sha256(post['url'].encode()).hexdigest()+'.png')
                            try:
                                shutil.copy2(image,destination);post['image']=str(destination)
                                self.cover_copies.append(destination)
                            except OSError:post['image']=None
                        else:post['image']=None
                        post['metadata']={**dict(post.get('metadata') or {}),'subscription_profile':self.profile}
                        self.download_entries.append(post)
                self.picker._stop_building();self.picker._cover_cache.clear()
            self.pool.close()
        super().done(result)

    def closeEvent(self,event):
        event.ignore();self.reject()


def open_update_preview(parent,profile):
    """Viewing never marks read; selected posts enter the ordinary download plan."""
    dialog=UpdatePreviewDialog(parent,profile)
    try:
        if dialog.exec()!=QDialog.DialogCode.Accepted or not dialog.download_entries:return False
        entries=dialog.download_entries
        cover_copies=tuple(dialog.cover_copies)
    finally:dialog.deleteLater()
    started=False
    try:
        from download_plan import ask_download_plan
        plan=ask_download_plan(parent,profile,updates_after=0)
        if plan is None:return False
        parent._apply_download_plan(plan,profile)
        parent._next_record_artist=True
        for entry in entries:entry.setdefault('content_mode',plan.get('content_mode','all'))
        started=bool(parent._start_queue(entries,profile))
        return started
    finally:
        if not started:
            for path in cover_copies:
                try:path.unlink(missing_ok=True)
                except OSError:pass


__all__=['UpdatePreviewDialog','open_update_preview']
