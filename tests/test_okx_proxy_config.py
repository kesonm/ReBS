import base64
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

module_path=Path(__file__).resolve().parents[1]/'deploy'/'configure-okx-proxy.py'
if not module_path.is_file():
    raise unittest.SkipTest('Local deployment files are not included in this checkout')
spec=importlib.util.spec_from_file_location('okx_proxy_config',module_path)
config=importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
DUMMY_UUID='00000000-0000-4000-8000-000000000001'


class ScopedProxyConfigTests(unittest.TestCase):
    def test_legacy_qr_preserves_uuid_websocket_host_path_and_cipher(self):
        token=base64.b64encode(f'auto:{DUMMY_UUID}@node.example:16648'.encode()).decode()
        query=urlencode({'obfs':'websocket','obfsParam':'transport.example','path':'/node?ed=2048','alterId':'0','remarks':'测试'})
        uri=f'vmess://{token}?{query}'
        with patch.object(config.subprocess,'check_output',return_value=uri.encode()):
            node=config.read_node(Path('test.png'))
        self.assertEqual(node['id'],DUMMY_UUID)
        self.assertEqual(node['network'],'ws')
        self.assertEqual(node['host'],'transport.example')
        self.assertEqual(node['path'],'/node?ed=2048')
        self.assertEqual(node['cipher'],'auto')
        self.assertFalse(node['tls'])

    def test_json_qr_keeps_tls_certificate_verification_enabled(self):
        payload={'add':'node.example','port':'443','id':DUMMY_UUID,'net':'ws','path':'/ws','host':'transport.example','tls':'tls','sni':'tls.example','aid':'0'}
        uri='vmess://'+base64.b64encode(json.dumps(payload).encode()).decode()
        with patch.object(config.subprocess,'check_output',return_value=uri.encode()):
            node=config.read_node(Path('test.png'))
        result=config.build_config(node)
        self.assertEqual(result['outbounds'][0]['streamSettings']['tlsSettings'],{'serverName':'tls.example','allowInsecure':False})

    def test_proxy_is_loopback_only_and_has_no_general_purpose_route(self):
        node={'address':'node.example','port':1234,'id':DUMMY_UUID,'cipher':'auto','alterId':0,'network':'ws','path':'/','host':'','tls':False,'sni':''}
        result=config.build_config(node)
        self.assertEqual(len(result['inbounds']),1)
        self.assertEqual(result['inbounds'][0]['listen'],'127.0.0.1')
        self.assertFalse(result['inbounds'][0]['settings']['allowTransparent'])
        allow,deny=result['routing']['rules']
        self.assertEqual(set(allow['domain']),{'full:openapi.okx.com','full:www.okx.com','full:ws.okx.com'})
        self.assertEqual(deny['outboundTag'],'blocked')
        self.assertNotIn('domain',deny)
        self.assertEqual(result['outbounds'][1]['protocol'],'blackhole')


if __name__=='__main__':unittest.main()
