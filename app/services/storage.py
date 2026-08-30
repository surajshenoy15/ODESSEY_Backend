import mimetypes
from pathlib import PurePosixPath
import httpx
from fastapi import HTTPException
from app.core.config import settings
from app.core.security import create_file_token

def clean_path(path):
    value=str(PurePosixPath(path)).lstrip('/')
    if '..' in PurePosixPath(value).parts: raise ValueError('Invalid path')
    return value
class Storage:
    def __init__(self): settings.LOCAL_STORAGE_DIR.mkdir(parents=True,exist_ok=True)
    async def upload(self,bucket,path,data,content_type=None,upsert=True):
        path=clean_path(path)
        if settings.STORAGE_BACKEND=='local':
            p=settings.LOCAL_STORAGE_DIR/bucket/path; p.parent.mkdir(parents=True,exist_ok=True)
            if p.exists() and not upsert: raise HTTPException(status_code=409,detail='File exists')
            p.write_bytes(data); return path
        if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY: raise RuntimeError('Supabase storage not configured')
        url=f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/{bucket}/{path}"
        h={'Authorization':f'Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}','apikey':settings.SUPABASE_SERVICE_ROLE_KEY,'Content-Type':content_type or mimetypes.guess_type(path)[0] or 'application/octet-stream','x-upsert':'true' if upsert else 'false'}
        async with httpx.AsyncClient(timeout=60) as c: r=await c.post(url,content=data,headers=h)
        if r.status_code>=400: raise HTTPException(status_code=502,detail=f'Supabase upload failed: {r.text}')
        return path
    async def download(self,bucket,path):
        path=clean_path(path)
        if settings.STORAGE_BACKEND=='local':
            p=settings.LOCAL_STORAGE_DIR/bucket/path
            if not p.exists(): raise HTTPException(status_code=404,detail='File not found')
            return p.read_bytes()
        url=f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/{bucket}/{path}"; h={'Authorization':f'Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}','apikey':settings.SUPABASE_SERVICE_ROLE_KEY}
        async with httpx.AsyncClient(timeout=60) as c: r=await c.get(url,headers=h)
        if r.status_code==404: raise HTTPException(status_code=404,detail='File not found')
        if r.status_code>=400: raise HTTPException(status_code=502,detail='Supabase download failed')
        return r.content
    async def signed_url(self,bucket,path,seconds=1800):
        path=clean_path(path)
        if settings.STORAGE_BACKEND=='local':
            token=create_file_token(bucket,path,max(1,seconds//60)); return f"{settings.API_PUBLIC_URL.rstrip('/')}{settings.API_V1_PREFIX}/files/{bucket}/{path}?token={token}"
        url=f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/sign/{bucket}/{path}"; h={'Authorization':f'Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}','apikey':settings.SUPABASE_SERVICE_ROLE_KEY,'Content-Type':'application/json'}
        async with httpx.AsyncClient(timeout=30) as c: r=await c.post(url,json={'expiresIn':seconds},headers=h)
        if r.status_code>=400: raise HTTPException(status_code=502,detail='Signed URL failed')
        s=r.json().get('signedURL') or r.json().get('signedUrl')
        return s if s.startswith('http') else f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1{s}"
storage=Storage()
