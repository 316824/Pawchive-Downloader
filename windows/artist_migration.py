"""Bounded, portable artist-card migration codes; contains no login or local path data."""
from __future__ import annotations

import base64
import binascii
from contextlib import nullcontext
from dataclasses import dataclass
import json
import math
import re
import sqlite3
import zlib
from urllib.parse import urlparse

PREFIX='PAWCHIVE_V1:'
MAX_CODE_CHARS=1024*1024
MAX_COMPRESSED_BYTES=768*1024
MAX_JSON_BYTES=2*1024*1024
MAX_ARTISTS=1000
MAX_TIMESTAMP=253402300799.0
INVALID_CODE='无效迁移代码，请完整复制后重试。'
PROFILE_RE=re.compile(r'^https://pawchive\.pw/(fanbox|patreon|subscribestar|discord|boosty)/user/([^/?#\\\s]{1,128})/?$',re.I)
CARD_KEYS={'profile','artist_id','name','avatar_url','posts','files','download_ts','subscription'}
SUB_KEYS={'baseline_ts','latest_ts','last_checked_ts','enabled','initialized','unread'}


class MigrationError(ValueError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    added: int
    updated: int


def _profile(value):
    if not isinstance(value,str) or len(value)>512 or any(ord(char)<33 or ord(char)==127 for char in value): return ''
    match=PROFILE_RE.fullmatch(value)
    if not match: return ''
    try:
        parsed=urlparse(value)
        if parsed.username or parsed.password or parsed.port is not None: return ''
    except ValueError: return ''
    return f'https://pawchive.pw/{match[1].lower()}/user/{match[2]}'


def _avatar(profile):
    return profile.replace('/user/','/').replace('https://pawchive.pw/','https://pawchive.pw/icons/',1)


def _timestamp(value, strict=False):
    if isinstance(value,bool) or not isinstance(value,(float,int)):
        if strict: raise MigrationError(INVALID_CODE)
        return 0.0
    result=float(value)
    if not math.isfinite(result) or not 0<=result<=MAX_TIMESTAMP:
        if strict: raise MigrationError(INVALID_CODE)
        return 0.0
    return result


def _count(value, maximum, strict=False):
    if strict:
        if type(value) is not int or not 0<=value<=maximum: raise MigrationError(INVALID_CODE)
        return value
    if isinstance(value,bool): return 0
    try: return max(0,min(maximum,int(value)))
    except (ValueError,TypeError,OverflowError): return 0


def _name(value, strict=False):
    valid=isinstance(value,str) and 0<len(value)<=160 and not any(ord(char)<32 for char in value)
    if strict and not valid: raise MigrationError(INVALID_CODE)
    return value if valid else 'Unknown Artist'


def _download_ts(card):
    return max((_timestamp(card.get(key,0)) for key in
                ('download_ts','last_download_ts','latest_download_ts','downloaded_ts')),default=0.0)


def _subscription(value, *, strict=False):
    if value is None: return None
    if not isinstance(value,dict) or (strict and set(value)!=SUB_KEYS):
        if strict: raise MigrationError(INVALID_CODE)
        return None
    result={key:_timestamp(value.get(key,0),strict) for key in ('baseline_ts','latest_ts','last_checked_ts')}
    for key,default in (('enabled',True),('initialized',False),('unread',False)):
        raw=value.get(key,default)
        if strict and type(raw) is not bool: raise MigrationError(INVALID_CODE)
        result[key]=bool(raw)
    return result


def _freshness(card):
    sub=card.get('subscription') or {}
    return max(card.get('download_ts',0),sub.get('latest_ts',0),sub.get('baseline_ts',0),sub.get('last_checked_ts',0))


def _merge_portable(old,new):
    """Merge duplicate portable entries without inflating counters or rolling time back."""
    newer=new if _freshness(new)>=_freshness(old) else old
    other=old if newer is new else new
    result={**newer}
    if result['name']=='Unknown Artist' and other['name']!='Unknown Artist': result['name']=other['name']
    for key in ('posts','files','download_ts'): result[key]=max(old[key],new[key])
    a,b=old.get('subscription'),new.get('subscription')
    if a and b:
        latest=b if max(b['latest_ts'],b['last_checked_ts'])>=max(a['latest_ts'],a['last_checked_ts']) else a
        sub={**latest}
        for key in ('baseline_ts','latest_ts','last_checked_ts'): sub[key]=max(a[key],b[key])
        sub['initialized']=a['initialized'] or b['initialized']
        sub['unread']=bool(sub['initialized'] and sub['latest_ts']>sub['baseline_ts'])
        result['subscription']=sub
    else: result['subscription']=a or b
    return result


def _snapshot(store):
    raw_cards=store.get_setting('artist_cards',{})
    raw_subs=store.get_setting('artist_subscriptions',[])
    cards=raw_cards if isinstance(raw_cards,dict) else {}
    subscriptions=raw_subs if isinstance(raw_subs,list) else []
    result={}
    for key,card in cards.items():
        profile=_profile(key)
        if not profile or not isinstance(card,dict): continue
        item=dict(profile=profile,artist_id=profile.rsplit('/',1)[-1],name=_name(card.get('name')),
            avatar_url=_avatar(profile),posts=_count(card.get('posts',0),1_000_000),
            files=_count(card.get('files',0),10_000_000),download_ts=_download_ts(card),subscription=None)
        result[profile]=_merge_portable(result[profile],item) if profile in result else item
    for sub in subscriptions:
        if not isinstance(sub,dict): continue
        profile=_profile(sub.get('profile'))
        if not profile: continue
        item=dict(profile=profile,artist_id=profile.rsplit('/',1)[-1],name=_name(sub.get('name')),
            avatar_url=_avatar(profile),posts=0,files=0,download_ts=_timestamp(sub.get('baseline_ts',0)),
            subscription=_subscription(sub))
        result[profile]=_merge_portable(result[profile],item) if profile in result else item
    return result


def export_code(store, subscription_store=None):
    """Export allowlisted fields from a consistent settings snapshot."""
    with getattr(store,'_lock',nullcontext()):
        artists=list(_snapshot(store).values())
    if len(artists)>MAX_ARTISTS: raise MigrationError('画师数量超过单次迁移上限（1000位）。')
    raw=json.dumps({'version':1,'artists':sorted(artists,key=lambda item:item['profile'])},
                   ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(raw)>MAX_JSON_BYTES: raise MigrationError('画师数据超过单次迁移大小上限。')
    compressed=zlib.compress(raw,9)
    payload=base64.b64encode(compressed).decode('ascii')
    code=f'{PREFIX}{payload}:{zlib.crc32(compressed)&0xffffffff:08X}'
    if len(compressed)>MAX_COMPRESSED_BYTES or len(code)>MAX_CODE_CHARS:
        raise MigrationError('画师数据超过单次迁移大小上限。')
    return code


def _unique_object(pairs):
    value={}
    for key,item in pairs:
        if key in value: raise MigrationError(INVALID_CODE)
        value[key]=item
    return value


def decode_code(code):
    """Validate the complete envelope before bounded decompression and strict schema parsing."""
    try:
        if not isinstance(code,str) or not 0<len(code)<=MAX_CODE_CHARS: raise MigrationError(INVALID_CODE)
        code=code.strip()
        if not code.startswith(PREFIX): raise MigrationError(INVALID_CODE)
        payload,crc=code[len(PREFIX):].rsplit(':',1)
        if not re.fullmatch(r'[0-9A-Fa-f]{8}',crc): raise MigrationError(INVALID_CODE)
        compressed=base64.b64decode(payload,validate=True)
        if not compressed or len(compressed)>MAX_COMPRESSED_BYTES or base64.b64encode(compressed).decode('ascii')!=payload:
            raise MigrationError(INVALID_CODE)
        if zlib.crc32(compressed)&0xffffffff!=int(crc,16): raise MigrationError(INVALID_CODE)
        decoder=zlib.decompressobj()
        raw=decoder.decompress(compressed,MAX_JSON_BYTES+1)
        if len(raw)>MAX_JSON_BYTES or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise MigrationError(INVALID_CODE)
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(MigrationError(INVALID_CODE)))
        if not isinstance(value,dict) or set(value)!={'version','artists'} or type(value['version']) is not int or value['version']!=1:
            raise MigrationError(INVALID_CODE)
        if not isinstance(value['artists'],list) or len(value['artists'])>MAX_ARTISTS: raise MigrationError(INVALID_CODE)
        unique={}
        for item in value['artists']:
            if not isinstance(item,dict) or set(item)!=CARD_KEYS: raise MigrationError(INVALID_CODE)
            profile=_profile(item['profile'])
            if not profile or item['artist_id']!=profile.rsplit('/',1)[-1]: raise MigrationError(INVALID_CODE)
            if item['avatar_url'] not in ('',_avatar(profile)): raise MigrationError(INVALID_CODE)
            clean=dict(profile=profile,artist_id=profile.rsplit('/',1)[-1],name=_name(item['name'],True),
                avatar_url=_avatar(profile),posts=_count(item['posts'],1_000_000,True),
                files=_count(item['files'],10_000_000,True),download_ts=_timestamp(item['download_ts'],True),
                subscription=_subscription(item['subscription'],strict=True))
            unique[profile]=_merge_portable(unique[profile],clean) if profile in unique else clean
        return list(unique.values())
    except (ValueError,TypeError,KeyError,UnicodeError,binascii.Error,zlib.error,RecursionError,OverflowError):
        raise MigrationError(INVALID_CODE) from None


def _write_settings(store,cards,subscriptions):
    if hasattr(store,'_write') and hasattr(store,'_db'):
        # One SQLite transaction prevents a half-import if either value is too
        # large or the disk fills. AppState's reentrant lock is held throughout.
        values=[('artist_cards',store._encode(cards)),('artist_subscriptions',store._encode(subscriptions))]
        with store._write():
            store._db.executemany('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',values)
    else:
        before=[store.get_setting('artist_cards',{}),store.get_setting('artist_subscriptions',[])]
        try:
            store.set_setting('artist_cards',cards)
            store.set_setting('artist_subscriptions',subscriptions)
        except Exception:
            store.set_setting('artist_cards',before[0]); store.set_setting('artist_subscriptions',before[1])
            raise


def import_code(store,code,subscription_store=None):
    artists=decode_code(code)
    # Use the same AppState lock as subscription mutation and deletion; never
    # fetch over the network while holding it.
    with getattr(store,'_lock',nullcontext()):
        existing=_snapshot(store)
        raw_cards=store.get_setting('artist_cards',{})
        cards=dict(raw_cards) if isinstance(raw_cards,dict) else {}
        raw_subs=store.get_setting('artist_subscriptions',[])
        subs=list(raw_subs) if isinstance(raw_subs,list) else []
        added=updated=0
        for incoming in artists:
            profile=incoming['profile']
            old=existing.get(profile)
            merged=_merge_portable(old,incoming) if old else incoming
            aliases=[key for key in cards if _profile(key)==profile]
            old_card=next((cards[key] for key in aliases if isinstance(cards[key],dict)),{})
            card={**old_card,'profile':profile,'artist_id':merged['artist_id'],'name':merged['name'],
                  'posts':merged['posts'],'files':merged['files'],'avatar_url':merged['avatar_url'],
                  'latest_download_ts':merged['download_ts']}
            card.setdefault('avatar_path',''); card.setdefault('avatar_status','missing')
            from artist_cards import ACHIEVEMENTS
            card['unlocked']=[name for name,threshold in ACHIEVEMENTS if card['posts']>=threshold]
            card['achievement']=card['unlocked'][-1] if card['unlocked'] else 'none'
            for key in aliases: cards.pop(key,None)
            cards[profile]=card
            if merged.get('subscription') is not None:
                old_sub=next((item for item in subs if isinstance(item,dict) and _profile(item.get('profile'))==profile),{})
                sub={**old_sub,**merged['subscription'],'profile':profile,'id':merged['artist_id'],'name':merged['name'],
                     'avatar_url':merged['avatar_url']}
                sub.setdefault('avatar_path','')
                subs=[item for item in subs if not isinstance(item,dict) or _profile(item.get('profile'))!=profile]
                subs.append(sub)
            if old is None: added+=1
            elif merged!=old: updated+=1
            existing[profile]=merged
        _write_settings(store,cards,subs)
    return MigrationResult(added,updated)


from PySide6.QtCore import Signal,QTimer
from PySide6.QtWidgets import (QApplication,QDialog,QLabel,QPlainTextEdit,QPushButton,
                              QTabWidget,QVBoxLayout,QHBoxLayout,QWidget)


class ArtistMigrationDialog(QDialog):
    changed=Signal()

    def __init__(self,parent,store,subscription_store=None):
        super().__init__(parent)
        self.store,self.subscription_store=store,subscription_store
        self.setWindowTitle('画师档案数据迁移'); self.resize(660,460)
        layout=QVBoxLayout(self)
        intro=QLabel('迁移画师档案、成就统计与订阅时间记录。不会包含登录信息、设备码或本机文件路径。')
        intro.setWordWrap(True); layout.addWidget(intro)
        self.tabs=QTabWidget(); layout.addWidget(self.tabs,1)
        export=QWidget(); export_layout=QVBoxLayout(export)
        export_layout.addWidget(QLabel('完整复制下方迁移代码，在另一台电脑的“输入恢复”中粘贴。'))
        self.output=QPlainTextEdit(); self.output.setReadOnly(True); export_layout.addWidget(self.output,1)
        self.copy_button=QPushButton('复制迁移代码'); self.copy_button.clicked.connect(self.copy_code)
        export_layout.addWidget(self.copy_button); self.tabs.addTab(export,'导出 / 复制')
        restore=QWidget(); restore_layout=QVBoxLayout(restore)
        restore_layout.addWidget(QLabel('粘贴完整迁移代码。相同画师会自动合并，并保留较新的记录。'))
        self.input=QPlainTextEdit(); self.input.setPlaceholderText('PAWCHIVE_V1:…'); restore_layout.addWidget(self.input,1)
        self.restore_button=QPushButton('恢复画师数据'); self.restore_button.clicked.connect(self.restore_code)
        restore_layout.addWidget(self.restore_button); self.tabs.addTab(restore,'输入恢复')
        self.status=QLabel(''); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.input.textChanged.connect(self._validate_input_size)
        self._copy_timer=QTimer(self); self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy_label)
        close=QPushButton('关闭'); close.clicked.connect(self.accept); layout.addWidget(close)
        self.refresh_export()

    def refresh_export(self):
        try:
            self.output.setPlainText(export_code(self.store,self.subscription_store))
            self.copy_button.setEnabled(True)
        except (ValueError,OSError,RuntimeError) as error:
            self.output.clear(); self.copy_button.setEnabled(False); self.status.setText(str(error))

    def copy_code(self):
        if not self.output.toPlainText(): return
        QApplication.clipboard().setText(self.output.toPlainText())
        self.copy_button.setText('已复制')
        self._copy_timer.start(1600)

    def _reset_copy_label(self):
        self.copy_button.setText('复制迁移代码')

    def _validate_input_size(self):
        too_large=self.input.document().characterCount()>MAX_CODE_CHARS+1
        self.restore_button.setEnabled(not too_large)
        if too_large: self.status.setText(INVALID_CODE)

    def restore_code(self):
        try:
            result=import_code(self.store,self.input.toPlainText(),self.subscription_store)
        except MigrationError:
            self.status.setText(INVALID_CODE); return
        except (ValueError,OSError,RuntimeError,sqlite3.Error):
            self.status.setText('恢复失败，本机原有画师数据已保留，请稍后重试。'); return
        self.status.setText(f'恢复成功：{result.added} 位新增，{result.updated} 位更新。')
        self.refresh_export(); self.changed.emit()
