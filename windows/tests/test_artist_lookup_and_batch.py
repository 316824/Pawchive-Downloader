"""Platform disambiguation and cabinet deletion use isolated local fixtures."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt,QBuffer,QIODevice,QCoreApplication,QEvent,QThread
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication,QWidget,QMessageBox,QCheckBox
import requests
from app_state import AppState
from artist_cards import record_download,update_artist_metadata,list_cards
from artist_lookup import input_profiles,lookup_profiles,resolve_artist_input,ArtistPlatformDialog
from batch_artists import split_batch_inputs,BatchArtistDialog
from feature_dialogs import ArtistCardsDialog
from subscriptions import SubscriptionStore

FANBOX='https://pawchive.pw/fanbox/user/123'
PATREON='https://pawchive.pw/patreon/user/123'


class Response:
    def __init__(self,url,body,status=200):
        self.url=url;self.body=body;self.status=status
        self.headers={'content-length':str(len(body))};self.closed=False
    def raise_for_status(self):
        if self.status>=400:raise requests.HTTPError(str(self.status))
    def iter_content(self,size):
        for offset in range(0,len(self.body),19):yield self.body[offset:offset+19]
    def close(self):self.closed=True


class Session:
    def __init__(self,replies):self.replies=replies;self.headers={};self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append(url)
        return self.replies.get(url,Response(url,b'',404))


class Check(QCheckBox):pass


class Parent(QWidget):
    lang='简体中文';_dark=True;background_store=None
    checkbox_factory=Check
    _subscription_loading_profiles=set()
    def __init__(self,state):
        super().__init__();self.subscription_store=SubscriptionStore(state);self.opened=[]
    def open_artist_profile(self,profile):self.opened.append(profile)


class ArtistLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):self.parent=QWidget();self.parent.lang='简体中文';self.operations=[]
    def tearDown(self):
        for operation in self.operations:
            from shiboken6 import isValid
            if isValid(operation):operation.cancel()
        for worker in self.parent.findChildren(QThread):worker.requestInterruption();worker.wait(1000)
        self.parent.close();self.parent.deleteLater();self.app.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    def wait(self,condition):
        end=time.monotonic()+3
        while not condition() and time.monotonic()<end:self.app.processEvents();time.sleep(.002)
        self.assertTrue(condition())
    def header(self,name):return f'<a class="user-header__profile"><span itemprop="name">{name}</span></a>'.encode()
    def test_numeric_id_has_two_candidates_but_explicit_url_stays_on_its_platform(self):
        self.assertEqual(input_profiles('123'),(FANBOX,PATREON))
        self.assertEqual(input_profiles(PATREON+'/post/9'),(PATREON,))
        for value in ('123xxx','not a URL','https://evil.invalid/fanbox/user/123','１２３'):
            self.assertEqual(input_profiles(value),())
    def test_empty_200_not_found_and_wrong_redirect_are_not_artists(self):
        session=Session({FANBOX:Response(FANBOX,b'<html>challenge</html>'),PATREON:Response(PATREON,b'',404)})
        self.assertFalse(lookup_profiles((FANBOX,PATREON),session=session))
        session=Session({FANBOX:Response('https://pawchive.pw/',self.header('wrong page'))})
        self.assertFalse(lookup_profiles((FANBOX,),session=session))
    def test_matching_header_returns_platform_name_and_valid_image(self):
        image=QImage(32,32,QImage.Format.Format_RGB32);image.fill(QColor('#22cc77'))
        buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly);image.save(buffer,'JPG')
        icon='https://pawchive.pw/icons/fanbox/123'
        session=Session({FANBOX:Response(FANBOX,self.header('画师の猫')),icon:Response(icon,bytes(buffer.data()))})
        result=lookup_profiles((FANBOX,),session=session)
        self.assertEqual(result[0]['profile'],FANBOX);self.assertEqual(result[0]['name'],'画师の猫')
        self.assertEqual(result[0]['platform'],'Fanbox');self.assertTrue(result[0]['avatar'])
    def test_async_unique_result_calls_back_on_gui_thread(self):
        records=[dict(profile=FANBOX,name='Artist',platform='Fanbox',avatar=b'')]
        selected=[];done=[];threads=[]
        with patch('artist_lookup.lookup_profiles',return_value=records):
            op=resolve_artist_input(self.parent,'123',lambda p:(selected.append(p),threads.append(QThread.currentThread())),done.append)
            self.operations.append(op);self.wait(lambda:bool(done))
        self.assertEqual(selected,[FANBOX]);self.assertEqual(done,[FANBOX])
        self.assertEqual(threads,[self.app.thread()])
    def test_two_platforms_wait_for_explicit_card_selection(self):
        records=[dict(profile=p,name=n,platform=platform,avatar=b'') for p,n,platform in
                 ((FANBOX,'Fanbox artist','Fanbox'),(PATREON,'Patreon artist','Patreon'))]
        selected=[];done=[]
        with patch('artist_lookup.lookup_profiles',return_value=records):
            op=resolve_artist_input(self.parent,'123',selected.append,done.append)
            self.operations.append(op);self.wait(lambda:op.dialog is not None)
            self.assertIsInstance(op.dialog,ArtistPlatformDialog);self.assertFalse(selected)
            self.assertEqual(len(op.dialog.findChildren(__import__('artist_lookup').ArtistChoiceCard)),2)
            op.dialog.choose(records[1]);self.wait(lambda:bool(done))
        self.assertEqual(selected,[PATREON]);self.assertEqual(done,[PATREON])
    def test_cancel_before_lookup_does_not_start_network_or_emit_selected(self):
        selected=[];done=[]
        with patch('artist_lookup.lookup_profiles',side_effect=AssertionError('No lookup after cancellation')):
            op=resolve_artist_input(self.parent,'123',selected.append,done.append);self.operations.append(op)
            op.cancel();self.app.processEvents()
        self.assertEqual(selected,[]);self.assertEqual(done,[''])
        self.assertFalse(self.parent._artist_lookup_operations)
    def test_batch_parser_preserves_order_deduplicates_and_honors_platform(self):
        values=split_batch_inputs('123, '+FANBOX+'\n123 456，'+PATREON+'; invalid\n'+FANBOX+'/post/7')
        self.assertEqual(values,['123',FANBOX,'456',PATREON])
        dialog=BatchArtistDialog(self.parent)
        dialog.editor.setPlainText('\n'.join(values));self.assertEqual(dialog.inputs,values)
        dialog.close();dialog.deleteLater()


class ArtistBatchDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_card_deletion_')
        self.state=AppState(Path(self.temp.name)/'state');self.parent=Parent(self.state)
        self.profiles=[FANBOX,PATREON]
        cache=self.state.data_dir/'artist-cards';cache.mkdir()
        avatar=cache/'avatar.png';image=QImage(64,64,QImage.Format.Format_RGB32);image.fill(QColor('#22dd77'));image.save(str(avatar))
        for profile in self.profiles:
            record_download(self.state,profile,posts=1,files=1)
            update_artist_metadata(self.state,profile,name=profile.split('/')[3],avatar_path=str(avatar))
            self.parent.subscription_store.add(profile,baseline_ts=100)
        self.parent.subscription_store.set_baseline(FANBOX,200,unread=True)
        self.dialog=ArtistCardsDialog(self.parent,self.state)
    def tearDown(self):
        self.dialog.reject();self.dialog.deleteLater();self.parent.close();self.parent.deleteLater()
        self.app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.state.close();self.temp.cleanup()
    def wait(self):
        end=time.monotonic()+.1
        while time.monotonic()<end:self.app.processEvents();time.sleep(.002)
    def test_single_delete_requires_yes_and_removes_subscription_together(self):
        self.dialog.cards.setCurrentRow(0);profile=self.dialog.cards.item(0).data(Qt.ItemDataRole.UserRole)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.No) as confirm:
            self.dialog.remove_artist();confirm.assert_called_once()
        self.assertEqual(len(list_cards(self.state)),2)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):self.dialog.remove_artist()
        self.assertEqual(len(list_cards(self.state)),1);self.assertIsNone(self.parent.subscription_store.get(profile))
    def test_batch_delete_uses_existing_checkbox_style_and_has_no_second_confirmation(self):
        self.dialog.show();self.wait();self.dialog.toggle_batch_delete();self.wait()
        self.assertEqual(len(self.dialog._row_checkboxes),2)
        self.assertTrue(all(isinstance(check,Check) for check in self.dialog._row_checkboxes.values()))
        self.dialog.select_current_page()
        with patch.object(QMessageBox,'question',side_effect=AssertionError('Batch delete does not ask again')):
            self.dialog.toggle_batch_delete()
        self.assertFalse(list_cards(self.state));self.assertFalse(self.parent.subscription_store.list())
    def test_total_count_and_update_badge_have_unclipped_geometry(self):
        self.dialog.show();self.wait()
        self.assertIn('2',self.dialog.total_label.text())
        self.assertGreaterEqual(self.dialog.total_label.width(),self.dialog.total_label.sizeHint().width())
        marker=self.dialog._status_labels[FANBOX]
        self.assertEqual(marker.text(),'●');self.assertEqual(marker.size().width(),20)
        row=marker.parentWidget();self.assertTrue(row.rect().contains(marker.geometry()))
    def test_card_click_uses_parent_update_preview_entry(self):
        self.dialog.open_card(self.dialog.cards.item(0))
        self.assertEqual(len(self.parent.opened),1)
