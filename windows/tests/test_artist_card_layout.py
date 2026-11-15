"""Single-paint card rows and wrapping under narrow/large-font layouts."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from PySide6.QtGui import QColor,QFont,QImage
from PySide6.QtWidgets import QApplication,QLabel,QWidget
from app_state import AppState
from artist_cards import record_download,update_artist_metadata
from feature_dialogs import ArtistCardsDialog
from subscriptions import SubscriptionStore

PROFILES=['https://pawchive.pw/fanbox/user/3439325','https://pawchive.pw/fanbox/user/260215']


class Parent(QWidget):
    lang='简体中文'
    background_store=None
    _subscription_loading_profiles=set()
    def __init__(self,state):
        super().__init__()
        self.subscription_store=SubscriptionStore(state)
        self.downloads=[]
    def confirm_artist_download(self,profile,updates_after=None):
        self.downloads.append(profile)


class ArtistCardLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_card_layout_')
        self.state=AppState(Path(self.temp.name)/'state')
        self.parent=Parent(self.state)
        self.parent.setStyleSheet('QListWidget::item {padding:9px;}')
        cache=self.state.data_dir/'artist-cards';cache.mkdir()
        for profile,name,color in zip(PROFILES,['kanibiimu','kurogami'],['#44cc55','#cc4455']):
            path=cache/(name+'.png')
            image=QImage(64,64,QImage.Format.Format_RGB32);image.fill(QColor(color));image.save(str(path))
            record_download(self.state,profile,posts=30,files=1700)
            update_artist_metadata(self.state,profile,name=name,avatar_path=str(path))
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No network in layout tests'))
        self.network.start()
        self.dialog=ArtistCardsDialog(self.parent,self.state)

    def wait(self):
        end=time.monotonic()+.15
        while time.monotonic()<end:
            self.qt.processEvents();time.sleep(.002)

    def tearDown(self):
        self.dialog.reject();self.dialog.deleteLater();self.parent.close();self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.qt.processEvents();self.network.stop();self.state.close();self.temp.cleanup()

    def test_only_custom_row_draws_avatar_and_text_while_item_remains_accessible(self):
        self.dialog.show();self.wait()
        self.assertEqual(self.dialog.cards.count(),2)
        previous_bottom=-1
        for index in range(2):
            item=self.dialog.cards.item(index);row=self.dialog.cards.itemWidget(item)
            self.assertFalse(item.text(),'Native delegate must not repeat the custom row text')
            self.assertTrue(item.icon().isNull(),'Native delegate must not repeat the custom avatar')
            self.assertIn('ID',item.data(Qt.ItemDataRole.AccessibleTextRole))
            self.assertEqual(len(row.findChildren(QLabel,'artistCardAvatar')),1)
            self.assertEqual(len(row.findChildren(QLabel,'artistCardDescription')),1)
            avatar=row.findChild(QLabel,'artistCardAvatar');description=row.findChild(QLabel,'artistCardDescription')
            self.assertLess(avatar.geometry().right(),description.geometry().left())
            self.assertGreaterEqual(description.height(),description.heightForWidth(description.width()))
            rect=self.dialog.cards.visualItemRect(item)
            self.assertGreater(rect.top(),previous_bottom);previous_bottom=rect.bottom()
        self.dialog.open_card(self.dialog.cards.item(0))
        self.assertEqual(len(self.parent.downloads),1)

    def test_long_names_large_font_and_narrow_width_expand_rows_instead_of_overlapping(self):
        self.parent.lang='English';self.dialog.lang='English'
        update_artist_metadata(self.state,PROFILES[0],name='Long artist name '*10)
        self.dialog.setFont(QFont('Microsoft YaHei UI',16))
        self.dialog.resize(560,560);self.dialog.refresh();self.dialog.show();self.wait()
        for width in (560,840,560):
            self.dialog.resize(width,560);self.wait()
            for index in range(self.dialog.cards.count()):
                row=self.dialog.cards.itemWidget(self.dialog.cards.item(index))
                description=row.findChild(QLabel,'artistCardDescription')
                self.assertGreaterEqual(description.height(),description.heightForWidth(description.width()))
                self.assertTrue(row.rect().contains(description.geometry()))
