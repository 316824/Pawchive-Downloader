"""Bounded queue covers and logs for artists with hundreds of collections."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication,QEvent,QSize,QThread,Qt
from PySide6.QtGui import QColor,QImage,QPainter,QPixmap
from PySide6.QtWidgets import QApplication

import desktop_design as design


class QueueMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_queue_memory_')
        self.root=Path(self.temp.name);self.widgets=[]

    def tearDown(self):
        for widget in self.widgets:
            widget.close();widget.deleteLater()
        self.widgets.clear();self.app.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def thumbnail(self):
        widget=design.QueueThumbnail();self.widgets.append(widget);return widget

    def image(self,width,height,name):
        image=QImage(width,height,QImage.Format.Format_RGB32);image.fill(QColor('#dd3344'))
        painter=QPainter(image)
        if width>height:painter.fillRect(width//3,0,width//3,height,QColor('#22bb66'))
        else:painter.fillRect(0,height//3,width,height//3,QColor('#22bb66'))
        painter.end()
        path=self.root/name
        self.assertTrue(image.save(str(path),'JPEG'))
        return path,image

    def assert_small_cover(self,widget):
        pixmap=widget.pixmap()
        self.assertFalse(pixmap.isNull())
        self.assertEqual(pixmap.size(),QSize(56,56))
        self.assertLessEqual(pixmap.toImage().sizeInBytes(),56*56*4)
        color=pixmap.toImage().pixelColor(28,28)
        self.assertGreater(color.green(),color.red())

    def test_image_reader_requests_bounded_size_before_decoding_on_gui_thread(self):
        calls=[]
        class Reader:
            def __init__(self,path):calls.append(('path',path));self.scaled=None
            def setDecideFormatFromContent(self,value):calls.append(('format',value))
            def setAutoTransform(self,value):calls.append(('transform',value))
            def size(self):return QSize(8000,6000)
            def setScaledSize(self,size):self.scaled=QSize(size);calls.append(('scaled',size))
            def read(self):
                if self.scaled is None:raise AssertionError('The full image was decoded before scaling was requested.')
                if max(self.scaled.width(),self.scaled.height())>112:raise AssertionError('The decode target was unbounded.')
                calls.append(('read',QThread.currentThread()))
                image=QImage(self.scaled,QImage.Format.Format_RGB32);image.fill(QColor('#22bb66'));return image
        widget=self.thumbnail()
        with patch.object(design,'QImageReader',Reader):widget.set_source('offline-large-cover.jpg')
        self.assertLess(next(i for i,c in enumerate(calls) if c[0]=='scaled'),
                        next(i for i,c in enumerate(calls) if c[0]=='read'))
        self.assertEqual(next(c[1] for c in calls if c[0]=='read'),self.app.thread())
        self.assert_small_cover(widget)

    def test_real_panoramas_and_portraits_have_fixed_cropped_pixel_dimensions(self):
        for width,height,name in ((8192,64,'panorama.jpg'),(64,8192,'portrait.jpg')):
            path,image=self.image(width,height,name)
            for source in (str(path),QPixmap.fromImage(image)):
                with self.subTest(name=name,source=type(source).__name__):
                    widget=self.thumbnail();widget.set_source(source);self.assert_small_cover(widget)

    def test_corrupt_missing_and_unreadable_covers_use_a_placeholder(self):
        corrupt=self.root/'corrupt.jpg';corrupt.write_bytes(b'not an image')
        for path in (corrupt,self.root/'missing.jpg'):
            with self.subTest(path=path):
                widget=self.thumbnail();widget.set_source(path)
                self.assertTrue(widget.pixmap().isNull());self.assertEqual(widget.text(),'◇')
        widget=self.thumbnail()
        with patch.object(design,'QImageReader',side_effect=OSError('not readable')):
            widget.set_source(self.root/'denied.jpg')
        self.assertTrue(widget.pixmap().isNull());self.assertEqual(widget.text(),'◇')

    def test_unreasonable_or_invalid_metadata_is_rejected_without_decoding(self):
        for dimensions in ((1_000_000,1_000_000),(9000,9000),(0,0),(-1,-1)):
            reads=[]
            class Reader:
                def __init__(self,path):pass
                def setDecideFormatFromContent(self,value):pass
                def setAutoTransform(self,value):pass
                def size(self):return QSize(*dimensions)
                def read(self):reads.append(True);raise AssertionError('Unsafe dimensions must not be decoded.')
            with self.subTest(dimensions=dimensions):
                widget=self.thumbnail()
                with patch.object(design,'QImageReader',Reader):widget.set_source('hostile-header.jpg')
                self.assertFalse(reads);self.assertTrue(widget.pixmap().isNull());self.assertEqual(widget.text(),'◇')

    def test_hundreds_of_real_queue_rows_retain_only_small_pixmaps(self):
        path,_=self.image(8192,64,'shared-panorama.jpg')
        rows=[]
        for index in range(360):
            row=design.QueueItemWidget(dict(id=str(index),title='Collection '+str(index),image=str(path)))
            rows.append(row);self.widgets.append(row)
        retained=0
        for row in rows:
            self.assert_small_cover(row.thumbnail)
            retained+=row.thumbnail.pixmap().toImage().sizeInBytes()
        self.assertLessEqual(retained,360*56*56*4)

    def test_log_retains_latest_two_thousand_blocks_as_plain_text(self):
        log=design.LogEditor();self.widgets.append(log)
        for index in range(2030):log.append(f'{index}: <b>画师</b> & <img src="https://invalid.test/cover">')
        lines=log.toPlainText().splitlines()
        self.assertEqual(log.document().maximumBlockCount(),2000)
        self.assertEqual(log.document().blockCount(),2000)
        self.assertEqual(len(lines),2000)
        self.assertTrue(lines[0].startswith('30: <b>画师</b>'))
        self.assertTrue(lines[-1].startswith('2029: <b>画师</b>'))
        self.assertIn('<img src="https://invalid.test/cover">',lines[-1])


if __name__=='__main__':unittest.main()