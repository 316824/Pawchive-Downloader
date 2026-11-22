"""Avatar bytes, legacy caches and bounded retries independent of MIME labels."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QBuffer,QIODevice,QObject,Signal,QCoreApplication,QEvent
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication,QWidget
from app_state import AppState
from artist_cards import (AvatarFetchWorker,ArtistAvatarLoader,record_download,
                          update_artist_metadata,visible_avatar_paths,list_cards,
                          valid_avatar_path,MAX_AVATAR_PIXELS)
from subscriptions import SubscriptionStore

PROFILE='https://pawchive.pw/fanbox/user/3439325'


class Response:
    def __init__(self,body,content_type='application/octet-stream',**headers):
        self.status_code=200
        self.body=body;self.headers={'content-type':content_type,'content-length':str(len(body)),**headers}
        self.closed=False
    def raise_for_status(self): pass
    def iter_content(self,chunk_size):
        for offset in range(0,len(self.body),7): yield self.body[offset:offset+7]
    def close(self): self.closed=True


class Session:
    def __init__(self,response): self.response=response;self.headers={};self.closed=False
    def get(self,url,**kwargs): return self.response
    def close(self): self.closed=True


class FakeWorker(QObject):
    result=Signal(str,str,str)
    finished=Signal()
    created=[]
    def __init__(self,data_dir,profile,parent=None):
        super().__init__(parent);self.profile=profile;self.created.append(self)
    def start(self): pass


class AvatarCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_avatar_validation_')
        self.root=Path(self.temp.name)
        self.store=AppState(self.root/'state')
        self.parent=QWidget();self.loaders=[];FakeWorker.created=[]

    def tearDown(self):
        for loader in self.loaders:
            loader.close()
        for worker in list(getattr(self.parent,'_cabinet_avatar_workers',{}).values()):
            worker.finished.emit()
        self.parent.deleteLater();self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.store.close();self.temp.cleanup()

    def jpeg(self):
        image=QImage(48,32,QImage.Format.Format_RGB32);image.fill(QColor('#35d479'))
        buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer,'JPG'));return bytes(buffer.data())

    def fetch(self,body,content_type='application/octet-stream',**headers):
        response=Response(body,content_type,**headers);session=Session(response)
        results=[];worker=AvatarFetchWorker(self.root/'downloads',PROFILE)
        worker.result.connect(lambda *args:results.append(args))
        with patch('artist_cards.requests.Session',return_value=session): worker.run()
        self.assertTrue(response.closed);self.assertTrue(session.closed)
        self.assertEqual(len(results),1)
        return results[0]

    def test_octet_stream_jpeg_is_validated_saved_and_displayable(self):
        profile,filename,error=self.fetch(self.jpeg())
        self.assertEqual(profile,PROFILE);self.assertFalse(error)
        self.assertEqual(Path(filename).suffix,'.jpg')
        image=QImage(filename);self.assertFalse(image.isNull())
        self.assertEqual((image.width(),image.height()),(48,32))
        self.assertGreater(image.pixelColor(24,16).green(),150)
        self.assertTrue(valid_avatar_path(filename))

    def test_header_mime_does_not_choose_wrong_file_extension(self):
        _,filename,error=self.fetch(self.jpeg(),'image/webp')
        self.assertFalse(error);self.assertEqual(Path(filename).suffix,'.jpg')

    def test_html_and_undecodable_octet_stream_are_not_cached(self):
        for body,mime in ((b'<html>blocked</html>','application/octet-stream'),
                          (b'not an image','image/jpeg'),(self.jpeg(),'text/html')):
            _,filename,error=self.fetch(body,mime)
            self.assertFalse(filename);self.assertTrue(error)
            self.assertEqual(list((self.root/'downloads'/'artist-cards').iterdir()),[])

    def test_declared_truncated_body_is_rejected(self):
        body=self.jpeg();_,filename,error=self.fetch(body,**{'content-length':str(len(body)+20)})
        self.assertFalse(filename);self.assertEqual(error,'incomplete_avatar')
        self.assertEqual(list((self.root/'downloads'/'artist-cards').iterdir()),[])

    def test_decoding_failure_closes_and_removes_temporary_image(self):
        # The header is valid and its declared length matches the response,
        # but the PNG pixel data is truncated. Reject it during full decode.
        image=QImage(48,32,QImage.Format.Format_RGB32);image.fill(QColor('#35d479'))
        buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer,'PNG'));body=bytes(buffer.data())
        _,filename,error=self.fetch(body[:len(body)//2],'image/png')
        self.assertFalse(filename);self.assertEqual(error,'invalid_avatar_image')
        self.assertEqual(list((self.root/'downloads'/'artist-cards').iterdir()),[])

    def test_oversized_dimensions_are_rejected_before_full_decode(self):
        image=QImage(3000,2000,QImage.Format.Format_RGB32);image.fill(QColor('#333333'))
        self.assertGreater(image.width()*image.height(),MAX_AVATAR_PIXELS)
        buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly);self.assertTrue(image.save(buffer,'PNG'))
        _,filename,error=self.fetch(bytes(buffer.data()),'application/octet-stream')
        self.assertFalse(filename);self.assertEqual(error,'invalid_avatar_image')
        self.assertEqual(list((self.root/'downloads'/'artist-cards').iterdir()),[])

    def test_failed_replacement_does_not_remove_existing_valid_avatar(self):
        _,filename,error=self.fetch(self.jpeg());self.assertFalse(error)
        original=Path(filename).read_bytes()
        self.fetch(b'bad image','image/webp')
        self.assertEqual(Path(filename).read_bytes(),original)

    def test_legacy_webp_named_jpeg_remains_usable(self):
        path=self.root/'legacy.webp';path.write_bytes(self.jpeg())
        record_download(self.store,PROFILE)
        update_artist_metadata(self.store,PROFILE,avatar_path=str(path))
        self.assertEqual(visible_avatar_paths(self.store,[PROFILE]),{PROFILE:str(path)})
        loader=ArtistAvatarLoader(self.store,self.parent,worker_class=FakeWorker);self.loaders.append(loader)
        loader.request([PROFILE]);self.assertFalse(FakeWorker.created)

    def test_broken_cache_is_ignored_and_refetched(self):
        path=self.root/'broken.webp';path.write_bytes(b'not a picture')
        record_download(self.store,PROFILE)
        update_artist_metadata(self.store,PROFILE,avatar_path=str(path))
        self.assertFalse(visible_avatar_paths(self.store,[PROFILE]))
        loader=ArtistAvatarLoader(self.store,self.parent,worker_class=FakeWorker);self.loaders.append(loader)
        loader.request([PROFILE]);self.assertEqual(len(FakeWorker.created),1)

    def test_temporary_failure_retries_after_cooldown_and_is_bounded(self):
        SubscriptionStore(self.store).add(PROFILE,baseline_ts=100)
        loader=ArtistAvatarLoader(self.store,self.parent,worker_class=FakeWorker);self.loaders.append(loader)
        with patch('artist_cards.time.monotonic',return_value=100):
            loader.request([PROFILE]);worker=FakeWorker.created[-1]
            worker.result.emit(PROFILE,'','timeout');worker.finished.emit()
            self.assertTrue(loader._defer_timer.isActive())
            loader.request([PROFILE]);self.assertEqual(len(FakeWorker.created),1)
        with patch('artist_cards.time.monotonic',return_value=131):
            loader._pump();self.assertEqual(len(FakeWorker.created),2)
            worker=FakeWorker.created[-1];worker.result.emit(PROFILE,'','timeout');worker.finished.emit()
        with patch('artist_cards.time.monotonic',return_value=192):
            loader._pump();self.assertEqual(len(FakeWorker.created),3)
            worker=FakeWorker.created[-1];worker.result.emit(PROFILE,'','timeout');worker.finished.emit()
        with patch('artist_cards.time.monotonic',return_value=1000):
            loader._pump();loader.request([PROFILE]);self.assertEqual(len(FakeWorker.created),3)
            self.assertFalse(loader._defer_timer.isActive())
        self.assertFalse(list_cards(self.store),'Avatar retries must not create achievement cards')

    def test_page_change_does_not_retry_hidden_failed_artist(self):
        SubscriptionStore(self.store).add(PROFILE,baseline_ts=100)
        loader=ArtistAvatarLoader(self.store,self.parent,worker_class=FakeWorker);self.loaders.append(loader)
        with patch('artist_cards.time.monotonic',return_value=100):
            loader.request([PROFILE]);worker=FakeWorker.created[-1]
            worker.result.emit(PROFILE,'','timeout');worker.finished.emit()
            loader.request([])
        with patch('artist_cards.time.monotonic',return_value=200): loader._pump()
        self.assertEqual(len(FakeWorker.created),1)
        self.assertFalse(loader._defer_timer.isActive())
