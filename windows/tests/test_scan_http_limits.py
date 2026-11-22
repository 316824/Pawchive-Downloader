"""Untrusted scan responses cannot redirect out or allocate unlimited bodies."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication
from requests.structures import CaseInsensitiveDict
import download_pawchive_images as desktop

PROFILE='https://pawchive.pw/fanbox/user/123'
POST=PROFILE+'/post/1'


class Response:
    def __init__(self,body='',status=200,headers=None):
        self.body=body.encode();self.status_code=status
        self.headers=CaseInsensitiveDict(headers or {});self.encoding='utf-8'
        self.closed=False;self.reads=0
    def raise_for_status(self):pass
    def iter_content(self,size):self.reads+=1;yield self.body
    def close(self):self.closed=True


class ScanHttpLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QCoreApplication.instance() or QCoreApplication([])
    def scan(self,responses):
        with tempfile.TemporaryDirectory(prefix='pawchive_scan_http_') as temporary:
            self.calls=[]
            class Session:
                headers={}
                def get(inner,url,**kwargs):
                    self.calls.append(url)
                    self.assertFalse(kwargs['allow_redirects'])
                    self.assertTrue(kwargs['stream'])
                    return responses[url]
                def close(inner):pass
            worker=desktop.ProfileScanWorker(PROFILE,Path(temporary))
            result=[];errors=[];worker.result.connect(result.append);worker.failed.connect(errors.append)
            with patch.object(desktop.requests,'Session',Session):worker.run()
            return result,errors
    def test_legacy_name_cleaner_agrees_with_static_windows_device_rules(self):
        for name in ('CON .jpg','COM¹.png','LPT².zip','nul.txt'):
            self.assertTrue(desktop.safe_path_component(name).startswith('_'),name)

    def test_profile_redirect_never_requests_external_destination(self):
        response=Response(status=302,headers={'location':'https://outside.invalid/private'})
        result,errors=self.scan({PROFILE:response})
        self.assertFalse(result);self.assertTrue(errors);self.assertEqual(self.calls,[PROFILE])
        self.assertTrue(response.closed)
    def test_declared_oversized_profile_is_closed_without_reading(self):
        response=Response(headers={'content-length':str(9*1024*1024)})
        result,errors=self.scan({PROFILE:response})
        self.assertFalse(result);self.assertTrue(errors)
        self.assertTrue(response.closed);self.assertEqual(response.reads,0)
    def test_bad_cover_uses_placeholder_and_another_artists_post_is_skipped(self):
        cover='https://file.pawchive.pw/thumbnail/bad.jpg'
        profile=Response('<article class="post-card"><a href="'+POST+'"></a>'
            '<div class="post-card__header">Safe</div><img class="post-card__image" src="'+cover+'"></article>'
            '<article class="post-card"><a href="https://pawchive.pw/fanbox/user/other/post/2"></a>'
            '<div class="post-card__header">Other</div></article>')
        image=Response(status=302,headers={'location':'http://127.0.0.1/private'})
        post=Response('<div class="post__files"></div>')
        result,errors=self.scan({PROFILE:profile,cover:image,POST:post})
        self.assertFalse(errors);self.assertEqual([entry['url'] for entry in result[0]],[POST])
        self.assertIsNone(result[0][0]['image']);self.assertTrue(image.closed)
        self.assertEqual(self.calls,[PROFILE,cover,POST])
