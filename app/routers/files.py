import mimetypes
from fastapi import APIRouter,HTTPException,Query
from fastapi.responses import Response
from app.core.config import settings
from app.core.security import decode_token
from app.services.storage import clean_path,storage
router=APIRouter(prefix='/files',tags=['Files'])
@router.get('/{bucket}/{path:path}',include_in_schema=False)
async def file(bucket:str,path:str,token:str=Query(...)):
    if settings.STORAGE_BACKEND!='local': raise HTTPException(status_code=404,detail='Not found')
    p=decode_token(token,'file')
    if p.get('bucket')!=bucket or clean_path(p.get('sub',''))!=clean_path(path): raise HTTPException(status_code=403,detail='Invalid file token')
    return Response(await storage.download(bucket,path),media_type=mimetypes.guess_type(path)[0] or 'application/octet-stream')
