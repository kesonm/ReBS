#!/usr/bin/env python3
"""Generate a private, OKX-only VMess proxy config from an operator's QR image.

Run as root on the deployment host. Never prints the share URI or credentials.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

ALLOWED_HOSTS = ['openapi.okx.com', 'www.okx.com', 'ws.okx.com']


def text64(value):
    data = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('gb18030')


def read_node(image):
    uri = subprocess.check_output(['zbarimg', '--quiet', '--raw', '-Sbinary', str(image)], stderr=subprocess.DEVNULL).decode('utf-8').strip()
    if not uri.startswith('vmess://'):
        raise ValueError('Expected a VMess QR code')
    token, _, query = uri.split('://', 1)[1].split('#', 1)[0].partition('?')
    payload = text64(token)
    if payload.lstrip().startswith('{'):
        node = json.loads(payload)
        result = {'address':node['add'], 'port':int(node['port']), 'id':str(UUID(node['id'])),
                  'cipher':node.get('scy') or 'auto', 'alterId':int(node.get('aid') or 0),
                  'network':node.get('net') or 'tcp', 'path':node.get('path') or '/',
                  'host':node.get('host') or '', 'tls':node.get('tls') == 'tls',
                  'sni':node.get('sni') or ''}
    else:
        parts = urlsplit('//'+payload)
        params = parse_qs(query)
        get = lambda key, default='': params.get(key, [default])[0]
        host = get('obfsParam') or get('host')
        if host.startswith('{'):
            headers = json.loads(host)
            host = headers.get('Host') or headers.get('host') or ''
        result = {'address':parts.hostname, 'port':parts.port, 'id':str(UUID(unquote(parts.password or parts.username or ''))),
                  'cipher':unquote(parts.username) if parts.password else 'auto',
                  'alterId':int(get('alterId','0')), 'network':get('obfs') or get('type','tcp'),
                  'path':get('path','/'), 'host':host, 'tls':get('tls') in {'1','true','tls'},
                  'sni':get('peer') or get('sni')}
    if result['network'] == 'websocket': result['network'] = 'ws'
    if result['network'] not in {'ws','tcp'}:
        raise ValueError('Unsupported transport')
    if result['cipher'] not in {'auto','aes-128-gcm','chacha20-poly1305','none','zero'}:
        raise ValueError('Unsupported VMess cipher')
    if not result['address'] or not 1 <= result['port'] <= 65535:
        raise ValueError('Invalid node endpoint')
    return result


def build_config(node):
    stream = {'network':node['network'], 'security':'tls' if node['tls'] else 'none'}
    if node['network'] == 'ws':
        stream['wsSettings'] = {'path':node['path'], 'headers':{'Host':node['host']} if node['host'] else {}}
    if node['tls']:
        stream['tlsSettings'] = {'serverName':node['sni'] or node['host'] or node['address'], 'allowInsecure':False}
    return {
        'log':{'loglevel':'warning'},
        'inbounds':[{'tag':'okx-only-in','listen':'127.0.0.1','port':18081,'protocol':'http',
                     'settings':{'allowTransparent':False},'sniffing':{'enabled':False}}],
        'outbounds':[{'tag':'node','protocol':'vmess','settings':{'vnext':[{'address':node['address'],'port':node['port'],
                      'users':[{'id':node['id'],'alterId':node['alterId'],'security':node['cipher']}]}]},'streamSettings':stream},
                     {'tag':'blocked','protocol':'blackhole','settings':{}}],
        'routing':{'domainStrategy':'AsIs','rules':[
            {'type':'field','inboundTag':['okx-only-in'],'network':'tcp','domain':['full:'+host for host in ALLOWED_HOSTS],'outboundTag':'node'},
            {'type':'field','inboundTag':['okx-only-in'],'outboundTag':'blocked'}]},
    }


def main():
    import grp
    parser = argparse.ArgumentParser()
    parser.add_argument('image', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0: raise PermissionError('Root required')
    node = read_node(args.image)
    config = build_config(node)
    directory = Path('/etc/rebs-proxy')
    directory.mkdir(mode=0o750, exist_ok=True)
    group = grp.getgrnam('rebs-proxy').gr_gid
    os.chown(directory,0,group)
    os.chmod(directory,0o750)
    target = directory/'config.json'
    descriptor = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o640)
    with os.fdopen(descriptor,'w') as output:
        json.dump(config,output,indent=2)
        output.write('\n')
    os.chown(target,0,group)
    os.chmod(target,0o640)
    state=Path('/var/lib/rebs-proxy')
    state.mkdir(mode=0o750,exist_ok=True)
    baseline=state/'network-baseline.json'
    if not baseline.exists():
        hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in [Path('/etc/environment'),Path('/etc/resolv.conf')] if path.exists()}
        routes=json.loads(subprocess.check_output(['ip','-j','route','show','default']))
        baseline.write_text(json.dumps({'hashes':hashes,'routes':routes},indent=2))
        os.chmod(baseline,0o640)
    print(json.dumps({'protocol':'vmess','transport':node['network'],'tls':node['tls'],
                      'listen':'127.0.0.1:18081','allowedHosts':ALLOWED_HOSTS,'configMode':'0640'}))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__}))
        raise SystemExit(1)
