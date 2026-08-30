from fastapi import Depends,HTTPException
from fastapi.security import HTTPAuthorizationCredentials,HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import decode_token
from app.models.entities import Admin,Ped
bearer=HTTPBearer(auto_error=False)
async def token_payload(c:HTTPAuthorizationCredentials|None=Depends(bearer)):
    if not c: raise HTTPException(status_code=401,detail='Authentication required')
    return decode_token(c.credentials,'access')
async def get_current_ped(p:dict=Depends(token_payload),db:AsyncSession=Depends(get_db)):
    if p.get('actor_type')!='PED': raise HTTPException(status_code=403,detail='PED access required')
    user=await db.get(Ped,p.get('sub'))
    if not user or not user.is_active: raise HTTPException(status_code=401,detail='PED account unavailable')
    return user
async def get_current_admin(p:dict=Depends(token_payload),db:AsyncSession=Depends(get_db)):
    if p.get('actor_type')!='ADMIN': raise HTTPException(status_code=403,detail='Admin access required')
    user=await db.get(Admin,p.get('sub'))
    if not user or not user.is_active: raise HTTPException(status_code=401,detail='Admin account unavailable')
    return user
def require_admin_roles(*roles):
    async def dep(a:Admin=Depends(get_current_admin)):
        if a.role!='SUPER_ADMIN' and a.role not in roles: raise HTTPException(status_code=403,detail='Insufficient permission')
        return a
    return dep
