"""Local encrypted token file on Windows; same-user processes are not isolated."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from kiroclops.visual.schema import read_json,write_json,VisualError


def _dpapi(data,protect):
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    target=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    if protect:
        function=crypt.CryptProtectData
        function.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok=function(ctypes.byref(source),'KiroClops',None,None,None,1,ctypes.byref(target))
    else:
        function=crypt.CryptUnprotectData
        function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok=function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    if not ok:raise VisualError('cloud_login_required','Cannot unlock the local service credential')
    try:return ctypes.string_at(target.data,target.size)
    finally:
        kernel.LocalFree.argtypes=[ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(target.data,ctypes.c_void_p))


def save(path,value):
    path=Path(path)
    if os.name=='nt':
        data=json.dumps(value,allow_nan=False).encode()
        write_json(path,{'storage':'windows-dpapi-v1','ciphertext':base64.b64encode(_dpapi(data,True)).decode()})
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        try:
            os.fchmod(descriptor,0o600)
            with os.fdopen(descriptor,'w') as stream:json.dump(value,stream,allow_nan=False)
        except BaseException:
            path.unlink(missing_ok=True);raise


def load(path):
    try:
        value=read_json(path)
        if value.get('storage')=='windows-dpapi-v1':
            if os.name!='nt':raise VisualError('cloud_login_required','Windows credential requires its original user account')
            return json.loads(_dpapi(base64.b64decode(value['ciphertext'],validate=True),False))
        return value  # historical internal-probe or mode-0600 local token
    except (OSError,ValueError,KeyError):
        raise VisualError('cloud_login_required','Service credential is unavailable; log in again') from None
