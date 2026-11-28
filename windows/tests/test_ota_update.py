"""Offline OTA validation, cancellation, cleanup and real Windows replacement."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication
import requests
import ota_update as ota


def executable():
    value = bytearray(1024)
    value[:2] = b'MZ';struct.pack_into('<I', value, 60, 128)
    value[128:132] = b'PE\0\0'
    struct.pack_into('<HH', value, 132, 0x8664, 1)
    struct.pack_into('<HH', value, 148, 112, 2)
    struct.pack_into('<H', value, 152, 0x20b)
    struct.pack_into('<H', value, 220, 2)
    return bytes(value)


class Response:
    def __init__(self, content=b'', status=200, headers=None, chunks=None):
        self.content=content;self.status_code=status;self.closed=False;self.chunks=chunks
        self.headers={'Content-Length':str(len(content)), **(headers or {})}
    def close(self):self.closed=True
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError(str(self.status_code))
    def iter_content(self, **kwargs):yield from self.chunks if self.chunks is not None else [self.content]


def release(content, extension='exe', digest=True, tag='Windows-v1.4.1'):
    name='PawchiveDownloader-Windows-x64.'+extension
    package=NS(name=name,url=f'https://github.com/316824/Pawchive-Downloader/releases/download/{tag}/{name}',
        size=len(content),digest='sha256:'+hashlib.sha256(content).hexdigest() if digest else '')
    return NS(tag=tag,assets=(package,))


class OtaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QCoreApplication.instance() or QCoreApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_ota_tests_')
        self.root=Path(self.temp.name)
        self.gettemp=patch.object(ota.tempfile,'gettempdir',return_value=str(self.root));self.gettemp.start()
    def tearDown(self):
        self.gettemp.stop()
        # The rollback fixture starts the old one-shot EXE asynchronously;
        # its marker can exist just before Windows releases its image handle.
        deadline=time.monotonic()+3
        while True:
            try:self.temp.cleanup();break
            except PermissionError:
                if time.monotonic()>=deadline:raise
                time.sleep(.02)
    def stage(self, content=None, **kwargs):
        content=executable() if content is None else content
        response=Response(content)
        with patch.object(ota.requests,'get',return_value=response) as get:
            staged=ota.stage_release(release(content,**kwargs))
        self.assertTrue(response.closed);self.assertEqual(get.call_args.kwargs['timeout'],3)
        return staged
    def test_verified_exe_from_windows_prefixed_tag_and_cleanup(self):
        staged=self.stage();self.assertEqual(staged.exe.read_bytes(),executable())
        self.assertEqual(staged.sha256,hashlib.sha256(executable()).hexdigest())
        self.assertTrue(ota.cleanup_staged(staged));self.assertFalse(staged.root.exists())
    def test_initial_repository_and_redirect_host_are_allowlisted(self):
        for url in ('http://github.com/316824/Pawchive-Downloader/releases/download/v1.4/a.exe',
                    'https://github.com/evil/Pawchive-Downloader/releases/download/v1.4/a.exe',
                    'https://github.com@127.0.0.1/316824/Pawchive-Downloader/releases/download/v1.4/a.exe',
                    'https://github.com/316824/Pawchive-Downloader/releases/download/v1.4/a.exe?x=1'):
            self.assertFalse(ota.safe_asset_url(url))
        bad=Response(status=302,headers={'Location':'https://127.0.0.1/new.exe'})
        with patch.object(ota.requests,'get',return_value=bad) as get:
            with self.assertRaises(ota.UpdateError):ota.stage_release(release(executable()))
        self.assertTrue(bad.closed);self.assertEqual(get.call_count,1)
        self.assertFalse(list(self.root.glob(ota.PREFIX+'*')))
    def test_github_signed_cdn_redirect_with_query_is_accepted(self):
        redirect=Response(status=302,headers={'Location':'https://release-assets.githubusercontent.com/github-production-release-asset/abc?token=x'})
        payload=Response(executable())
        with patch.object(ota.requests,'get',side_effect=[redirect,payload]):
            staged=ota.stage_release(release(executable()))
        self.assertTrue(redirect.closed and payload.closed);ota.cleanup_staged(staged)
    def test_sidecar_hash_and_missing_hash(self):
        content=executable();item=release(content,digest=False);name=item.assets[0].name
        checksum=(hashlib.sha256(content).hexdigest()+'  '+name+'\n').encode()
        item.assets+=(NS(name=name+'.sha256',url=item.assets[0].url+'.sha256',size=len(checksum),digest=''),)
        with patch.object(ota.requests,'get',side_effect=[Response(checksum),Response(content)]):
            staged=ota.stage_release(item)
        ota.cleanup_staged(staged)
        with patch.object(ota.requests,'get') as get:
            with self.assertRaisesRegex(ota.UpdateError,'SHA256'):ota.stage_release(release(content,digest=False))
        get.assert_not_called();self.assertFalse(list(self.root.glob(ota.PREFIX+'*')))
    def test_checksum_mismatch_and_incomplete_download_never_leave_stage(self):
        item=release(executable());item.assets[0].digest='sha256:'+'0'*64
        for sample,response in ((item,Response(executable())),(release(executable()),Response(b'MZ'))):
            with patch.object(ota.requests,'get',return_value=response):
                with self.assertRaises(ota.UpdateError):ota.stage_release(sample)
            self.assertFalse(list(self.root.glob(ota.PREFIX+'*')))
    def test_oversized_http_and_html_and_cancelled_stream(self):
        cases=[Response(executable(),headers={'Content-Length':str(ota.MAX_PACKAGE_BYTES+1)}),
               Response(b'<html>',headers={'Content-Type':'text/html'})]
        for response in cases:
            with patch.object(ota.requests,'get',return_value=response):
                with self.assertRaises(ota.UpdateError):ota.stage_release(release(executable()))
            self.assertTrue(response.closed)
        stop=threading.Event();response=Response(executable())
        with patch.object(ota.requests,'get',return_value=response):
            with self.assertRaises(ota.UpdateError):
                ota.stage_release(release(executable()),interrupted=stop.is_set,progress=lambda *_:stop.set())
        self.assertFalse(list(self.root.glob(ota.PREFIX+'*')))
    def test_wrong_pe_architecture_dll_and_plain_mz_are_rejected(self):
        wrong=bytearray(executable());struct.pack_into('<H',wrong,132,0x14c)
        dll=bytearray(executable());struct.pack_into('<H',dll,150,0x2002)
        for content in (b'MZ'+bytes(1022),bytes(wrong),bytes(dll)):
            with self.assertRaises(ota.UpdateError):self.stage(content)
        self.assertFalse(list(self.root.glob(ota.PREFIX+'*')))
    def test_zip_only_extracts_one_checked_exe(self):
        value=io.BytesIO()
        with zipfile.ZipFile(value,'w') as archive:
            archive.writestr('portable/PawchiveDownloader.exe',executable())
            archive.writestr('README.md','not installed')
        staged=self.stage(value.getvalue(),extension='zip')
        self.assertEqual(staged.exe.read_bytes(),executable())
        self.assertFalse((staged.root/'README.md').exists());ota.cleanup_staged(staged)
    def test_zip_traversal_symlink_bomb_and_duplicate_exe_are_rejected(self):
        for mode in ('traversal','symlink','bomb','duplicate'):
            value=io.BytesIO()
            with zipfile.ZipFile(value,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('PawchiveDownloader.exe',executable())
                if mode=='traversal':archive.writestr('../escape.txt','bad')
                elif mode=='symlink':
                    entry=zipfile.ZipInfo('linked');entry.external_attr=(0o120777<<16)
                    archive.writestr(entry,'../escape')
                elif mode=='bomb':archive.writestr('huge',b'0'*100_000)
                else:archive.writestr('extra/PawchiveDownloader.exe',executable())
            with self.assertRaises(ota.UpdateError):self.stage(value.getvalue(),extension='zip')
        self.assertFalse((self.root/'escape.txt').exists());self.assertFalse(list(self.root.glob(ota.PREFIX+'*')))
    def test_stale_cleanup_keeps_active_owner_and_unmarked_foreign_directory(self):
        staged=self.stage();marker=staged.root/'owner.json';owner=json.loads(marker.read_text())
        owner['created']=time.time()-2*24*3600;ota._write_json(marker,owner)
        foreign=self.root/(ota.PREFIX+'foreign');foreign.mkdir();(foreign/'keep').write_text('keep')
        self.assertEqual(ota.cleanup_stale_updates(),0)
        owner['owner_pid']=2**31-1;ota._write_json(marker,owner)
        self.assertEqual(ota.cleanup_stale_updates(),1);self.assertTrue((foreign/'keep').exists())
    def test_cleanup_rejects_tampered_marker_and_non_temp_root(self):
        staged=self.stage();marker=staged.root/'owner.json';marker.write_text('{}')
        self.assertFalse(ota.cleanup_staged(staged));self.assertTrue(staged.root.exists())
        self.assertFalse(ota.cleanup_staged(ota.StagedUpdate(self.root,self.root/'anything','v1.4.1')))
    def test_cleanup_ignores_non_object_owner_json(self):
        staged=self.stage();marker=staged.root/'owner.json'
        for value in ([],None,1,'owner'):
            marker.write_text(json.dumps(value),encoding='utf-8')
            self.assertIsNone(ota._owner(staged.root))
            self.assertEqual(ota.cleanup_stale_updates(max_age=0),0)
            self.assertFalse(ota.cleanup_staged(staged));self.assertTrue(staged.root.is_dir())
    def test_worker_communicates_from_background_without_widget_access(self):
        finished=[];ready=[];progress=[];worker=ota.OtaDownloadWorker(release(executable()))
        worker.finished.connect(lambda:finished.append(True));worker.ready.connect(ready.append)
        worker.progress.connect(lambda a,b:progress.append((a,b)))
        with patch.object(ota.requests,'get',return_value=Response(executable())):
            worker.start()
            deadline=time.monotonic()+3
            while not finished and time.monotonic()<deadline:self.app.processEvents();time.sleep(.002)
        self.assertEqual(finished,[True]);self.assertEqual(len(ready),1);self.assertTrue(progress)
        worker.wait(3000);ota.cleanup_staged(ready[0]);worker.deleteLater()
    def native_app(self, name, exit_code=0):
        compiler=Path(os.environ['SystemRoot'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        self.assertTrue(compiler.is_file(), 'The Windows .NET Framework C# compiler is required for this fixture')
        source=self.root/(name+'.cs');target=self.root/(name+'.exe')
        source.write_text('using System;using System.IO;class Program {static int Main(){'
            'File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"started-'+name+'.txt"),"started");'
            'return '+str(exit_code)+';}}',encoding='utf-8')
        result=subprocess.run([str(compiler),'/nologo','/platform:x64','/target:winexe',
            '/out:'+str(target),str(source)],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout.decode('utf-8','replace')+result.stderr.decode('utf-8','replace'))
        ota.validate_exe(target)
        return target
    @unittest.skipUnless(os.name=='nt','Windows installer integration')
    def test_real_updater_waits_replaces_restarts_and_self_cleans_special_path(self):
        staged=self.stage()
        folder=self.root/'中文 path & %PATH%';folder.mkdir();target=folder/'PawchiveDownloader.exe'
        shutil.copyfile(self.native_app('old'),target)
        shutil.copyfile(self.native_app('new'),staged.exe)
        staged=ota.StagedUpdate(staged.root,staged.exe,staged.version,ota._digest(staged.exe))
        parent=subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)'])
        old=ota._digest(target)
        try:
            with patch.dict(os.environ,{'PAWCHIVE_OTA_NO_ERROR_UI':'1'}):
                process=ota.launch_updater(staged,target,parent.pid)
            self.assertEqual(ota._digest(target),old)
            self.assertEqual(process.wait(timeout=35),0,
                (staged.root/'update-failed.txt').read_text(encoding='utf-8-sig') if (staged.root/'update-failed.txt').exists() else '')
            self.assertEqual(ota._digest(target),staged.sha256)
            self.assertTrue((folder/'started-new.txt').is_file())
            self.assertFalse(staged.root.exists());self.assertFalse(list(folder.glob('.pawchive-*')))
        finally:
            parent.wait(timeout=5)
    @unittest.skipUnless(os.name=='nt','Windows rollback integration')
    def test_real_updater_rolls_back_unlaunchable_pe_and_preserves_failure_log(self):
        self._rollback_fixture(unlaunchable=True)
    @unittest.skipUnless(os.name=='nt','Windows rollback integration')
    def test_real_updater_rolls_back_immediate_nonzero_exit(self):
        self._rollback_fixture(unlaunchable=False)
    def _rollback_fixture(self, *, unlaunchable):
        staged=self.stage()
        folder=self.root/'rollback';folder.mkdir();target=folder/'PawchiveDownloader.exe'
        shutil.copyfile(self.native_app('old'),target);old=ota._digest(target)
        if not unlaunchable:
            shutil.copyfile(self.native_app('failed',7),staged.exe)
            staged=ota.StagedUpdate(staged.root,staged.exe,staged.version,ota._digest(staged.exe))
        parent=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.5)'])
        try:
            with patch.dict(os.environ,{'PAWCHIVE_OTA_NO_ERROR_UI':'1'}):
                process=ota.launch_updater(staged,target,parent.pid)
            self.assertEqual(process.wait(timeout=35),1)
            self.assertEqual(ota._digest(target),old)
            self.assertTrue((staged.root/'update-failed.txt').is_file())
            deadline=time.monotonic()+3
            while not (folder/'started-old.txt').exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue((folder/'started-old.txt').exists())
            if not unlaunchable:self.assertTrue((folder/'started-failed.txt').exists())
            self.assertFalse(list(folder.glob('.pawchive-*')))
        finally:parent.wait(timeout=5)
    @unittest.skipUnless(os.name=='nt','Windows marker validation')
    def test_real_installer_rejects_changed_owner_token_without_touching_target(self):
        staged=self.stage();folder=self.root/'tamper';folder.mkdir();target=folder/'PawchiveDownloader.exe'
        shutil.copyfile(self.native_app('old'),target);old=ota._digest(target)
        parent=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.5)'])
        try:
            with patch('ota_update.subprocess.Popen') as spawn:
                ota.launch_updater(staged,target,parent.pid)
            config=json.loads((staged.root/'install.json').read_text(encoding='utf-8'))
            config['stage_token']='0'*32;ota._write_json(staged.root/'install.json',config)
            system=Path(os.environ['SystemRoot'])/'System32'
            with patch.dict(os.environ,{'PAWCHIVE_OTA_NO_ERROR_UI':'1'}):
                process=subprocess.Popen([str(system/'cmd.exe'),'/d','/c','updater.bat'],cwd=staged.root,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(process.wait(timeout=10),1);self.assertEqual(ota._digest(target),old)
            self.assertFalse((folder/'started-old.txt').exists());self.assertTrue(staged.root.is_dir())
        finally:parent.wait(timeout=5)


if __name__=='__main__':unittest.main()
