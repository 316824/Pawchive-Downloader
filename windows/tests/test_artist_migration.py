"""Migration protocol, transaction and live artist-card UI regression checks."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QWidget
from app_state import AppState
from artist_cards import record_download,remove_card,update_artist_metadata,list_cards
from subscriptions import SubscriptionStore
from artist_migration import (export_code,import_code,decode_code,MigrationError,
    ArtistMigrationDialog,MAX_JSON_BYTES,MAX_CODE_CHARS,INVALID_CODE)

PROFILE='https://pawchive.pw/fanbox/user/1234'
SECOND='https://pawchive.pw/fanbox/user/5678'


def envelope(value=None, raw=None):
    raw=raw if raw is not None else json.dumps(value,ensure_ascii=False).encode()
    body=zlib.compress(raw)
    return 'PAWCHIVE_V1:'+base64.b64encode(body).decode()+f':{zlib.crc32(body)&0xffffffff:08X}'


class ArtistMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_artist_migration_')
        self.root=Path(self.temp.name)
        self.source=AppState(self.root/'source')
        self.target=AppState(self.root/'target')
        self.subs=SubscriptionStore(self.source)

    def tearDown(self):
        self.source.close(); self.target.close(); self.temp.cleanup()

    def seed(self):
        record_download(self.source,PROFILE,posts=25,files=100,latest_download_ts=100)
        update_artist_metadata(self.source,PROFILE,name='Artist A',avatar_path='C:/private/avatar.png')
        self.subs.add(PROFILE,name='Artist A',baseline_ts=100)
        self.subs.set_baseline(PROFILE,200,name='Artist A',unread=True)
        self.source.set_setting('activation',{'private_device':'do not export'})
        self.source.set_setting('artist_cards',{**self.source.get_setting('artist_cards'),
            SECOND:{'name':'Artist B','posts':1,'files':5,'latest_download_ts':50,'avatar_path':'D:/other/private.png'}})
        return export_code(self.source,self.subs)

    def test_roundtrip_preserves_only_portable_data_and_is_idempotent(self):
        code=self.seed()
        records=decode_code(code)
        encoded=json.dumps(records)
        self.assertNotIn('private',encoded); self.assertNotIn('avatar_path',encoded)
        self.assertNotIn('activation',encoded)
        self.assertIn('/icons/fanbox/1234',encoded)
        self.target.set_setting('unrelated',{'keep':'settings'})
        result=import_code(self.target,code)
        self.assertEqual((result.added,result.updated),(2,0))
        cards={item['profile']:item for item in list_cards(self.target)}
        self.assertEqual(cards[PROFILE]['name'],'Artist A')
        self.assertEqual(cards[PROFILE]['achievement'],'master')
        self.assertEqual(cards[PROFILE]['latest_download_ts'],100)
        sub=SubscriptionStore(self.target).get(PROFILE)
        self.assertEqual(sub['baseline_ts'],100); self.assertEqual(sub['latest_ts'],200)
        self.assertTrue(sub['unread'])
        result=import_code(self.target,code)
        self.assertEqual((result.added,result.updated),(0,0))
        self.assertEqual(self.target.get_setting('unrelated'),{'keep':'settings'})

    def test_merge_deduplicates_profiles_keeps_new_timestamps_and_local_avatar(self):
        code=self.seed()
        record_download(self.target,PROFILE,posts=30,files=150,latest_download_ts=300)
        update_artist_metadata(self.target,PROFILE,name='newer name',avatar_path='C:/target/avatar.png')
        target_subs=SubscriptionStore(self.target)
        target_subs.add(PROFILE,name='newer name',baseline_ts=300)
        target_subs.set_baseline(PROFILE,400,name='newer name',unread=True)
        imported=decode_code(code)
        imported.append({**imported[0],'profile':PROFILE+'/','download_ts':150,'name':'older duplicate'})
        result=import_code(self.target,envelope({'version':1,'artists':imported}))
        self.assertEqual(result.added,1)
        card=self.target.get_setting('artist_cards')[PROFILE]
        self.assertEqual(card['posts'],30); self.assertEqual(card['latest_download_ts'],300)
        self.assertEqual(card['avatar_path'],'C:/target/avatar.png')
        self.assertEqual(target_subs.get(PROFILE)['baseline_ts'],300)
        self.assertEqual(len(list_cards(self.target)),2)

    def test_recent_check_cannot_override_metadata_from_newer_download_history(self):
        code=self.seed()
        record_download(self.target,PROFILE,posts=30,files=150,latest_download_ts=300)
        update_artist_metadata(self.target,PROFILE,name='newer downloaded artist name')
        sub=SubscriptionStore(self.target)
        sub.add(PROFILE,name='newer downloaded artist name',baseline_ts=300)
        records=self.target.get_setting('artist_subscriptions')
        records[0]['latest_ts']=400; records[0]['last_checked_ts']=500
        self.target.set_setting('artist_subscriptions',records)
        data=decode_code(code)
        incoming=next(item for item in data if item['profile']==PROFILE)
        incoming['name']='old downloaded name checked recently'
        incoming['subscription']['last_checked_ts']=10000
        incoming['subscription']['enabled']=False
        import_code(self.target,envelope({'version':1,'artists':data}))
        card=self.target.get_setting('artist_cards')[PROFILE]
        self.assertEqual(card['name'],'newer downloaded artist name')
        self.assertEqual(card['latest_download_ts'],300)
        self.assertTrue(sub.get(PROFILE)['enabled'])
        self.assertEqual(sub.get(PROFILE)['baseline_ts'],300)
        self.assertEqual(sub.get(PROFILE)['last_checked_ts'],10000)

    def test_corrupt_truncated_wrong_schema_or_oversized_codes_are_rejected_without_writes(self):
        code=self.seed()
        record_download(self.target,SECOND,posts=2,files=2)
        before=self.target.get_setting('artist_cards')
        record=decode_code(code)[0]
        cases=[code[:-1],code.replace('PAWCHIVE_V1:','PAWCHIVE_V2:'),code[:-8]+'00000000',
            'PAWCHIVE_V1:!!!!:00000000', 'x'*(MAX_CODE_CHARS+1),
            envelope({'version':True,'artists':[]}),
            envelope({'version':1,'artists':[{**record,'avatar_path':'C:/escape'}]}),
            envelope({'version':1,'artists':[{**record,'download_ts':float('nan')}]}),
            envelope({'version':1,'artists':[{**record,'artist_id':'mismatch'}]}),
            envelope({'version':1,'artists':[{**record,'avatar_url':'https://evil.example/avatar.jpg'}]}),
            envelope({'version':1,'artists':[{**record,'profile':'https://pawchive.pw/fanbox/user/a\\x00b'}]}),
            envelope(raw=b'{"version":1,"version":1,"artists":[]}'),
            envelope(raw=b'x'*(MAX_JSON_BYTES+1))]
        for invalid in cases:
            with self.subTest(index=cases.index(invalid)):
                with self.assertRaises(MigrationError): import_code(self.target,invalid)
                self.assertEqual(self.target.get_setting('artist_cards'),before)
        compressed=zlib.compress(b'{"version":1,"artists":[]}')+b'trailing payload'
        with self.assertRaises(MigrationError):
            decode_code('PAWCHIVE_V1:'+base64.b64encode(compressed).decode()+f':{zlib.crc32(compressed)&0xffffffff:08X}')

    def test_import_is_atomic_when_second_sqlite_setting_write_fails(self):
        code=self.seed()
        record_download(self.target,SECOND,posts=3,files=3)
        before=self.target.get_setting('artist_cards')
        self.target._db.execute("CREATE TRIGGER migration_test_abort BEFORE INSERT ON settings WHEN NEW.key='artist_subscriptions' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError): import_code(self.target,code)
        self.assertEqual(self.target.get_setting('artist_cards'),before)
        self.assertIsNone(self.target.get_setting('artist_subscriptions'))

    def test_mutation_locks_are_shared_and_stale_metadata_cannot_recreate_removed_card(self):
        code=self.seed()
        one,two=SubscriptionStore(self.target),SubscriptionStore(self.target)
        self.assertIs(one._lock,two._lock); self.assertIs(one._lock,self.target._lock)
        with self.target._lock:
            done=threading.Event()
            def import_background():
                import_code(self.target,code); done.set()
            thread=threading.Thread(target=import_background); thread.start()
            self.assertFalse(done.wait(.03))
        thread.join(3); self.assertTrue(done.is_set())
        remove_card(self.target,PROFILE)
        self.assertIsNone(update_artist_metadata(self.target,PROFILE,name='stale callback'))
        self.assertNotIn(PROFILE,self.target.get_setting('artist_cards'))

    def test_download_baseline_advances_only_actual_download_and_ignores_stale_fetch(self):
        sub=SubscriptionStore(self.target)
        sub.add(PROFILE)  # local-clock placeholder is not a real baseline
        sub.advance_download(PROFILE,100)
        self.assertEqual(sub.get(PROFILE)['baseline_ts'],100)
        sub.set_baseline(PROFILE,300,unread=True)
        sub.advance_download(PROFILE,200)
        self.assertEqual(sub.get(PROFILE)['baseline_ts'],200)
        self.assertTrue(sub.get(PROFILE)['unread'])
        sub.set_baseline(PROFILE,150,unread=False)  # stale in-flight network snapshot
        self.assertEqual(sub.get(PROFILE)['latest_ts'],300)
        self.assertTrue(sub.get(PROFILE)['unread'])

    def test_first_remote_baseline_replaces_local_placeholder_and_preserves_newer_real_records(self):
        sub=SubscriptionStore(self.target)
        sub.add(PROFILE)
        self.assertGreater(sub.get(PROFILE)['baseline_ts'],1000)
        sub.set_baseline(PROFILE,100,unread=False)
        record=sub.get(PROFILE)
        self.assertEqual(record['baseline_ts'],100)
        self.assertEqual(record['latest_ts'],100)
        self.assertFalse(record['unread'])
        sub.set_baseline(PROFILE,300,unread=True)
        sub.set_baseline(PROFILE,200,unread=False)
        self.assertEqual(sub.get(PROFILE)['latest_ts'],300)
        self.assertTrue(sub.get(PROFILE)['unread'])

    def test_artist_workers_keep_native_finished_separate_from_data_result(self):
        from artist_cards import ArtistMetadataWorker,AvatarFetchWorker
        for worker in (ArtistMetadataWorker('invalid-profile'), AvatarFetchWorker(self.root,'invalid-profile')):
            data=[]; stopped=[]
            worker.result.connect(lambda *values:data.append(values))
            worker.finished.connect(lambda:stopped.append(True))
            worker.start()
            deadline=time.monotonic()+3
            while (worker.isRunning() or not stopped) and time.monotonic()<deadline:
                self.qt.processEvents(); time.sleep(.001)
            self.assertTrue(worker.wait(1000))
            self.qt.processEvents()
            self.assertEqual(len(data),1)
            self.assertEqual(stopped,[True])
            self.assertIn('invalid_profile',data[0])
            worker.deleteLater()
        self.qt.processEvents()

    def test_dialog_copy_invalid_feedback_restore_and_card_live_markers(self):
        from feature_dialogs import ArtistCardsDialog
        code=self.seed()
        class Parent(QWidget):
            lang='简体中文'
            background_store=None
            _subscription_loading_profiles={PROFILE}
            def __init__(self,state):
                super().__init__(); self.subscription_store=SubscriptionStore(state); self.called=[]
            def confirm_artist_download(self,profile,updates_after=None): self.called.append((profile,updates_after))
        parent=Parent(self.target)
        dialog=ArtistMigrationDialog(parent,self.target,parent.subscription_store)
        self.assertTrue(dialog.output.isReadOnly())
        dialog.input.setPlainText('broken'); dialog.restore_code()
        self.assertEqual(dialog.status.text(),INVALID_CODE)
        dialog.input.setPlainText(code); dialog.restore_code()
        self.assertIn('2 位新增',dialog.status.text())
        dialog.copy_code(); self.assertTrue(QApplication.clipboard().text().startswith('PAWCHIVE_V1:'))
        cabinet=ArtistCardsDialog(parent,self.target)
        marker=cabinet._status_labels[PROFILE]
        self.assertIn(marker.text(),('◐','◓','◑','◒'))
        parent._subscription_loading_profiles=set(); cabinet._update_markers()
        self.assertEqual(marker.text(),'●')
        item=next(cabinet.cards.item(i) for i in range(cabinet.cards.count()) if cabinet.cards.item(i).data(Qt.ItemDataRole.UserRole)==PROFILE)
        cabinet.open_card(item)
        self.assertEqual(parent.called,[(PROFILE,100)])
        self.assertTrue(parent.subscription_store.get(PROFILE)['unread'])
        cabinet.deleteLater(); dialog.deleteLater(); parent.deleteLater(); self.qt.processEvents()


if __name__=='__main__': unittest.main()
