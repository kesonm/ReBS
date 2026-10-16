#!/usr/bin/env python3
"""Run interactively over SSH. Secrets never enter command arguments or stdout."""
from __future__ import annotations

import getpass
import grp
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ENV_FILE = Path('/etc/rebs/rebs.demo.env')


def env_value(value: str) -> str:
    if not value or any(ord(c)<32 or ord(c)>126 for c in value):
        raise ValueError('仅支持非空的可打印 ASCII 字符。')
    return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'


def configure():
    if os.geteuid()!=0 or not sys.stdin.isatty():
        raise SystemExit('请通过交互 SSH 运行：ssh -t ubuntu "sudo python3 /opt/rebs/current/deploy/configure-demo.py"')
    print('配置欧易模拟盘账户（固定模拟环境、现货交易）。')
    print('请使用模拟盘专用 API Key，授予读取和交易权限。输入不会回显。')
    values={name:getpass.getpass(label).strip() for name,label in [
        ('OKX_DEMO_API_KEY','Demo API Key: '),('OKX_DEMO_SECRET_KEY','Demo Secret Key: '),
        ('OKX_DEMO_PASSPHRASE','Demo Passphrase: '),('REBS_DEMO_ACCESS_TOKEN','设置网页访问口令（24–128 个字符）: ')]}
    access=values['REBS_DEMO_ACCESS_TOKEN']
    if not 24<=len(access)<=128:
        raise SystemExit('网页访问口令长度必须为 24–128 个字符；配置未更改。')
    if getpass.getpass('再次输入网页访问口令: ')!=access:
        raise SystemExit('两次访问口令不一致；配置未更改。')
    content=''.join(name+'='+env_value(value)+'\n' for name,value in values.items())
    ENV_FILE.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='.demo-',dir=ENV_FILE.parent)
    try:
        os.fchmod(descriptor,0o640)
        os.fchown(descriptor,0,grp.getgrnam('rebs').gr_gid)
        with os.fdopen(descriptor,'w',encoding='utf-8') as target:
            target.write(content);target.flush();os.fsync(target.fileno())
        os.replace(temporary,ENV_FILE)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    subprocess.run(['systemctl','restart','rebs'],check=True)
    print('配置已保存到服务器私有环境文件，正在验证只读账户查询；不会测试下单。')
    for _ in range(20):
        connection=http.client.HTTPConnection('127.0.0.1',18080,timeout=30)
        try:
            connection.request('GET','/api/okx/demo/overview',headers={'Authorization':'Bearer '+access})
            response=connection.getresponse()
            body=json.loads(response.read())
            if response.status==200 and body.get('environment')=='demo':
                print('模拟账户连接成功。通过 HTTPS 或 SSH 本地转发打开网页，使用刚设置的网页访问口令解锁。')
                return
            print('服务已启动，账户验证未通过：'+str(body.get('detail','请检查模拟盘凭据和权限。')))
            raise SystemExit(1)
        except (ConnectionError,OSError): time.sleep(1)
        finally: connection.close()
    raise SystemExit('服务启动检查超时。请运行 sudo systemctl status rebs 排查。')


if __name__=='__main__':
    try: configure()
    except (KeyboardInterrupt,EOFError): raise SystemExit('\n已取消。')
    except ValueError as exc: raise SystemExit(str(exc)) from None
