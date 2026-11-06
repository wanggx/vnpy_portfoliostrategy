# -*- coding: utf-8 -*-
"""QMT built-in runtime capability probe.

Purpose: find out what THIS client+branch actually allows, instead of guessing
from docs. Paste the whole file into a new model, run it once (simulation mode
is fine), then read the [PROBE] lines in the client log.

Safe by design:
  * does NOT trade, does NOT register any timer, does NOT subscribe anything
  * every check is wrapped in try/except
  * the only side effect is a few small temp files that are deleted immediately
  * one-shot: nothing runs after after_init

Output is plain English ASCII on purpose (so the log lines are readable and the
file survives the client's paste/re-encode path the same way the strategy does).

DELETE THIS FILE AFTER USE.
"""

import os
import sys
import time

T0 = time.time()


def p(tag, ok, detail=''):
    """Print one probe line. Keep it short: the log window is narrow."""
    print('[PROBE] %-4s %-30s %s' % ('OK' if ok else 'FAIL', tag, str(detail)[:150]))


def p_info(tag, detail):
    print('[PROBE] --   %-30s %s' % (tag, str(detail)[:150]))


# -----------------------------------------------------------------------------
# 1. environment
# -----------------------------------------------------------------------------

def probe_env():
    p_info('python version', sys.version.replace('\n', ' '))
    p_info('sys.platform', sys.platform)
    try:
        p_info('sys.executable', sys.executable)
    except Exception:
        p_info('sys.executable', 'unavailable')
    try:
        p_info('os.getcwd()', os.getcwd())
    except Exception as exc:
        p_info('os.getcwd()', 'FAIL ' + str(exc)[:100])
    try:
        p_info('sys.path', ' | '.join(sys.path)[:300])
    except Exception:
        p_info('sys.path', 'unavailable')
    try:
        p_info('sys.argv', sys.argv)
    except Exception:
        p_info('sys.argv', 'unavailable')
    try:
        import tempfile
        p_info('tempfile.gettempdir()', tempfile.gettempdir())
    except Exception as exc:
        p_info('tempfile.gettempdir()', 'FAIL ' + str(exc)[:100])
    try:
        p_info('__name__', __name__)
    except Exception:
        p_info('__name__', 'not defined in this exec scope')


# -----------------------------------------------------------------------------
# 2. stdlib / third-party imports (the "whitelist" question)
# -----------------------------------------------------------------------------

IMPORT_LIST = [
    'os', 'sys', 'json', 'io', 'time', 'datetime', 'math', 're', 'csv',
    'collections', 'itertools', 'functools', 'hashlib', 'base64', 'random',
    'struct', 'sqlite3', 'pickle', 'shutil', 'glob', 'logging', 'traceback',
    'socket', 'ssl', 'urllib.request', 'http.client', 'smtplib',
    'email.mime.text', 'threading', 'subprocess', 'ctypes',
    'numpy', 'pandas', 'requests',
]


def probe_imports():
    ok_count = 0
    for name in IMPORT_LIST:
        try:
            __import__(name)
            ok_count += 1
            p('import ' + name, True, '')
        except Exception as exc:
            p('import ' + name, False, str(exc)[:120])
    p_info('import summary', '%d/%d ok' % (ok_count, len(IMPORT_LIST)))


# -----------------------------------------------------------------------------
# 3. file IO
# -----------------------------------------------------------------------------

def probe_file_io():
    import tempfile
    tmp = tempfile.gettempdir()
    cwd = os.getcwd()

    try:
        names = os.listdir(cwd)[:6]
        p('os.listdir(cwd)', True, str(names)[:140])
    except Exception as exc:
        p('os.listdir(cwd)', False, str(exc)[:120])

    try:
        p('os.path.exists(cwd)', True, str(os.path.exists(cwd)))
    except Exception as exc:
        p('os.path.exists(cwd)', False, str(exc)[:120])

    places = [('write cwd', os.path.join(cwd, '_probe_cwd.txt')),
              ('write temp', os.path.join(tmp, '_probe_tmp.txt')),
              ('write c_root', 'C:\\_probe_root.txt')]

    for tag, path in places:
        try:
            f = open(path, 'w')
            f.write('probe')
            f.close()
            p(tag, True, path)
        except Exception as exc:
            p(tag, False, str(exc)[:120])
            continue
        try:
            f = open(path, 'r')
            txt = f.read()
            f.close()
            p(tag.replace('write', 'read '), txt == 'probe', repr(txt)[:60])
        except Exception as exc:
            p(tag.replace('write', 'read '), False, str(exc)[:120])
        try:
            os.remove(path)
        except Exception:
            pass

    # append mode -- this is what an event log / state file would use
    try:
        path = os.path.join(tmp, '_probe_append.txt')
        f = open(path, 'a')
        f.write('x\n')
        f.close()
        f = open(path, 'a')
        f.write('y\n')
        f.close()
        f = open(path, 'r')
        n = len(f.read())
        f.close()
        os.remove(path)
        p('append 2 lines', n == 4, 'len=%d' % n)
    except Exception as exc:
        p('append 2 lines', False, str(exc)[:120])

    # atomic replace (state-file write pattern)
    try:
        path = os.path.join(tmp, '_probe_atomic.json')
        f = open(path + '.tmp', 'w')
        f.write('{"a":1}')
        f.close()
        os.replace(path + '.tmp', path)
        f = open(path, 'r')
        txt = f.read()
        f.close()
        os.remove(path)
        p('os.replace (atomic)', txt == '{"a":1}', txt)
    except Exception as exc:
        p('os.replace (atomic)', False, str(exc)[:120])

    # json round trip
    try:
        import json
        blob = json.dumps({'entry_prices': {'600000.SH': 10.25}, 'day': '20261014'})
        back = json.loads(blob)
        p('json roundtrip', back['entry_prices']['600000.SH'] == 10.25, '')
    except Exception as exc:
        p('json roundtrip', False, str(exc)[:120])


# -----------------------------------------------------------------------------
# 4. network (short timeouts -- never let this hang the shared thread)
# -----------------------------------------------------------------------------

def probe_net():
    # raw TCP
    try:
        import socket
        t = time.time()
        s = socket.create_connection(('www.baidu.com', 80), timeout=2)
        s.close()
        p('tcp connect :80', True, '%.2fs' % (time.time() - t))
    except Exception as exc:
        p('tcp connect :80', False, str(exc)[:120])

    # plain HTTP
    try:
        import urllib.request
        t = time.time()
        r = urllib.request.urlopen('http://www.baidu.com', timeout=2)
        code = r.getcode()
        r.read(64)
        r.close()
        p('http GET', True, 'code=%s %.2fs' % (code, time.time() - t))
    except Exception as exc:
        p('http GET', False, str(exc)[:120])

    # HTTPS (this is what a wecom / dingtalk webhook needs)
    try:
        import urllib.request
        t = time.time()
        r = urllib.request.urlopen('https://www.baidu.com', timeout=2)
        code = r.getcode()
        r.read(64)
        r.close()
        p('https GET', True, 'code=%s %.2fs' % (code, time.time() - t))
    except Exception as exc:
        p('https GET', False, str(exc)[:120])

    # HTTPS POST with JSON body (the webhook shape we would actually use)
    try:
        import json
        import urllib.request
        body = json.dumps({'probe': 1}).encode('utf-8')
        req = urllib.request.Request('https://www.baidu.com',
                                     data=body,
                                     headers={'Content-Type': 'application/json'})
        t = time.time()
        r = urllib.request.urlopen(req, timeout=2)
        code = r.getcode()
        r.close()
        p('https POST json', True, 'code=%s %.2fs' % (code, time.time() - t))
    except Exception as exc:
        p('https POST json', False, str(exc)[:120])

    # SMTP reachable? (mail path). Do not login -- just TCP connect.
    try:
        import socket
        t = time.time()
        s = socket.create_connection(('smtp.qq.com', 25), timeout=2)
        s.close()
        p('tcp smtp.qq.com:25', True, '%.2fs' % (time.time() - t))
    except Exception as exc:
        p('tcp smtp.qq.com:25', False, str(exc)[:120])

    try:
        import socket
        t = time.time()
        s = socket.create_connection(('smtp.qq.com', 465), timeout=2)
        s.close()
        p('tcp smtp.qq.com:465', True, '%.2fs' % (time.time() - t))
    except Exception as exc:
        p('tcp smtp.qq.com:465', False, str(exc)[:120])


# -----------------------------------------------------------------------------
# 5. framework entry points (keep the required ones, do nothing in them)
# -----------------------------------------------------------------------------

def init(C):
    p_info('init(C) called', 'build=%s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    try:
        if not account:
            p_info('account', 'EMPTY (no account selected in the dialog)')
        else:
            p_info('account', '%s type=%s' % (account, accountType))
    except Exception as exc:
        p_info('account', 'unavailable ' + str(exc)[:100])


def after_init(C):
    print('')
    print('[PROBE] ================= RUN START =================')
    for name in ('probe_env', 'probe_imports', 'probe_file_io', 'probe_net'):
        try:
            globals()[name]()
        except Exception as exc:
            p_info(name + ' CRASHED', str(exc)[:140])
    print('[PROBE] ================== RUN END ==================')
    try:
        print('[PROBE] elapsed %.2fs' % (time.time() - T0))
    except Exception:
        pass
    print('')


def handlebar(C):
    return


def stop(C):
    print('[PROBE] stop(C) called')
