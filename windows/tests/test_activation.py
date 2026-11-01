"""Ephemeral signing fixtures only; never access the production signing key."""
import base64
import json
import secrets
import sys
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import activation
ORIGINAL_LOAD_PUBLIC_KEY=activation.load_public_key


class Store:
    def __init__(self): self.values={}
    def get_setting(self,key,default=None): return self.values.get(key,default)
    def set_setting(self,key,value): self.values[key]=value


def b64(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.key=Ed25519PrivateKey.generate()
        self.device=activation.device_code('offline-installation-one')
        self.store=Store()
        self.public=patch.object(activation,'load_public_key',return_value=self.key.public_key())
        self.public.start(); self.addCleanup(self.public.stop)

    def token(self,*,device=None,purpose='activate'):
        payload=dict(v=1,app=activation.APP_ID,device=device or self.device,purpose=purpose,
                     nonce=b64(secrets.token_bytes(16)),issued=int(time.time()))
        message=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
        return 'PWI1.'+b64(message)+'.'+b64(self.key.sign(message))

    def test_device_code_is_stable_hashed_and_validated(self):
        self.assertEqual(self.device,activation.device_code('OFFLINE-INSTALLATION-ONE'))
        self.assertNotIn('offline',self.device.lower())
        self.assertEqual(self.device,activation.normalize_device(self.device.lower().replace('-',' ')))
        self.assertNotEqual(self.device,activation.device_code('offline-installation-two'))

    def test_signed_device_token_persists_and_stored_flags_do_not_replace_signature(self):
        activation.activate(self.store,self.token(),self.device)
        reopened=Store(); reopened.values=json.loads(json.dumps(self.store.values))
        self.assertTrue(activation.is_activated(reopened,self.device))
        reopened.set_setting(activation.TOKEN_KEY,'true')
        self.assertFalse(activation.is_activated(reopened,self.device))

    def test_tampering_wrong_device_and_wrong_purpose_fail(self):
        token=self.token(); pieces=token.split('.')
        damaged=pieces[0]+'.'+pieces[1]+'.'+b64(bytes(64))
        for value in (damaged,self.token(device=activation.device_code('another-offline-machine')),
                      self.token(purpose='unlock'),'PWI1.'+'x'*3000):
            with self.subTest(value=value[:12]),self.assertRaises(activation.ActivationError):
                activation.verify_token(value,self.device)

    def test_public_key_manifest_cannot_be_swapped_for_another_self_consistent_key(self):
        import hashlib
        from cryptography.hazmat.primitives import serialization
        raw=self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        document=dict(schema=1,app_id=activation.APP_ID,key_id=hashlib.sha256(raw).hexdigest()[:16],
                      public_key_ed25519=b64(raw))
        with tempfile.TemporaryDirectory(prefix='pawchive_public_key_test_') as folder:
            path=Path(folder)/'public.json'; path.write_text(json.dumps(document),encoding='utf-8')
            with self.assertRaises(activation.ActivationError) as caught:ORIGINAL_LOAD_PUBLIC_KEY(path)
        self.assertEqual(caught.exception.code,'public_key_unavailable')

    def test_tenth_correct_attempt_is_allowed_but_ten_failures_lock_across_restart(self):
        for _ in range(9):
            with self.assertRaises(activation.ActivationError):activation.activate(self.store,'invalid',self.device)
        self.assertEqual(activation.remaining_attempts(self.store),1)
        activation.activate(self.store,self.token(),self.device)
        self.assertTrue(activation.is_activated(self.store,self.device))
        self.store=Store()
        for _ in range(10):
            with self.assertRaises(activation.ActivationError):activation.activate(self.store,'invalid',self.device)
        reopened=Store(); reopened.values=json.loads(json.dumps(self.store.values))
        self.assertEqual(activation.remaining_attempts(reopened),0)
        with self.assertRaises(activation.ActivationError) as caught:
            activation.activate(reopened,self.token(),self.device)
        self.assertEqual(caught.exception.code,'activation_locked')

    def test_damaged_public_manifest_is_an_operational_failure_without_spending_attempts(self):
        with tempfile.TemporaryDirectory(prefix='pawchive_invalid_public_test_') as folder:
            path=Path(folder)/'public.json'
            for document in ([],None,True,'invalid'):
                path.write_text(json.dumps(document),encoding='utf-8')
                with self.subTest(document=document),self.assertRaises(activation.ActivationError) as caught:
                    ORIGINAL_LOAD_PUBLIC_KEY(path)
                self.assertEqual(caught.exception.code,'public_key_unavailable')
        error=activation.ActivationError('fixture public file error','public_key_unavailable')
        with patch.object(activation,'load_public_key',side_effect=error):
            with self.assertRaises(activation.ActivationError):
                activation.activate(self.store,'invalid',self.device)
        self.assertEqual(activation.remaining_attempts(self.store),10)

    def test_unlock_restores_attempts_and_replay_is_rejected_after_restart(self):
        self.store.set_setting(activation.FAILURES_KEY,10)
        code=self.token(purpose='unlock')
        activation.accept_unlock(self.store,code,self.device)
        self.assertEqual(activation.remaining_attempts(self.store),10)
        reopened=Store(); reopened.values=json.loads(json.dumps(self.store.values))
        with self.assertRaises(activation.ActivationError) as caught:activation.accept_unlock(reopened,code,self.device)
        self.assertEqual(caught.exception.code,'unlock_replayed')

    def test_unlock_failures_trigger_persistent_24h_backoff(self):
        for _ in range(10):
            with self.assertRaises(activation.ActivationError):activation.accept_unlock(self.store,'wrong',self.device)
        with self.assertRaises(activation.ActivationError) as caught:
            activation.accept_unlock(self.store,self.token(purpose='unlock'),self.device)
        self.assertEqual(caught.exception.code,'unlock_backoff')
        future=time.time()+activation.UNLOCK_BACKOFF+1
        with patch.object(activation.time,'time',return_value=future):
            activation.accept_unlock(self.store,self.token(purpose='unlock'),self.device)
        self.assertEqual(activation.remaining_attempts(self.store),10)


if __name__=='__main__':unittest.main()
