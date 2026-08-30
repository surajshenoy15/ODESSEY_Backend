from datetime import timedelta
from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import desc,select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token,generate_otp,hash_otp,utcnow,verify_otp_hash,verify_password
from app.models.entities import Admin,OtpCode,Ped
from app.schemas import AdminLogin,PedOtpRequest,PedOtpResponse,PedOtpVerify,TokenResponse
from app.services.email import email_service,otp_html
from app.services.helpers import audit
router=APIRouter(prefix='/auth',tags=['Authentication'])
@router.post('/ped/request-otp',response_model=PedOtpResponse)
async def request_otp(p:PedOtpRequest,db:AsyncSession=Depends(get_db)):
    email=p.email.lower(); now=utcnow()
    if settings.ALLOWED_PED_EMAIL_DOMAINS and email.split('@')[-1] not in {d.lower() for d in settings.ALLOWED_PED_EMAIL_DOMAINS}:
        raise HTTPException(status_code=403,detail='Use an approved official college email domain')
    latest=await db.scalar(select(OtpCode).where(OtpCode.email==email,OtpCode.purpose=='PED_LOGIN').order_by(desc(OtpCode.created_at)).limit(1))
    if latest and latest.created_at:
        created=latest.created_at.replace(tzinfo=now.tzinfo) if latest.created_at.tzinfo is None else latest.created_at; elapsed=(now-created).total_seconds()
        if elapsed<settings.OTP_RESEND_COOLDOWN_SECONDS: raise HTTPException(status_code=429,detail=f'Please wait {int(settings.OTP_RESEND_COOLDOWN_SECONDS-elapsed)} seconds')
    otp=generate_otp(); db.add(OtpCode(email=email,purpose='PED_LOGIN',otp_hash=hash_otp(otp),expires_at=now+timedelta(minutes=settings.OTP_EXPIRE_MINUTES)))
    await email_service.send(db,email,'Your BNMIT ODYSSEY PED login OTP',otp_html(otp,settings.OTP_EXPIRE_MINUTES),'PED_OTP'); await db.commit()
    return PedOtpResponse(message='OTP sent',expires_in_seconds=settings.OTP_EXPIRE_MINUTES*60,debug_otp=otp if settings.TEST_MODE and settings.RETURN_OTP_IN_RESPONSE else None)
@router.post('/ped/verify-otp',response_model=TokenResponse)
async def verify_otp(p:PedOtpVerify,db:AsyncSession=Depends(get_db)):
    email=p.email.lower(); now=utcnow(); o=await db.scalar(select(OtpCode).where(OtpCode.email==email,OtpCode.purpose=='PED_LOGIN',OtpCode.used.is_(False)).order_by(desc(OtpCode.created_at)).limit(1))
    if not o: raise HTTPException(status_code=400,detail='No active OTP')
    exp=o.expires_at.replace(tzinfo=now.tzinfo) if o.expires_at.tzinfo is None else o.expires_at
    if exp<now: raise HTTPException(status_code=400,detail='OTP expired')
    if o.attempts>=settings.OTP_MAX_ATTEMPTS: raise HTTPException(status_code=429,detail='Too many attempts')
    if not verify_otp_hash(p.otp,o.otp_hash): o.attempts+=1; await db.commit(); raise HTTPException(status_code=400,detail='Invalid OTP')
    o.used=True; ped=await db.scalar(select(Ped).where(Ped.official_email==email))
    if not ped: ped=Ped(official_email=email,is_email_verified=True); db.add(ped); await db.flush()
    ped.is_email_verified=True; ped.last_login_at=now
    for k in ('name','college_name','college_location','contact_number'):
        v=getattr(p,k)
        if v: setattr(ped,k,v)
    if p.declaration_accepted: ped.declaration_accepted_at=now
    await audit(db,'PED',ped.id,'PED_LOGIN','PED',ped.id); await db.commit()
    return TokenResponse(access_token=create_access_token(ped.id,'PED','PED'),actor_type='PED',role='PED')
@router.post('/admin/login',response_model=TokenResponse)
async def admin_login(p:AdminLogin,db:AsyncSession=Depends(get_db)):
    a=await db.scalar(select(Admin).where(Admin.email==p.email.lower()))
    if not a or not a.is_active or not verify_password(p.password,a.password_hash): raise HTTPException(status_code=401,detail='Invalid credentials')
    a.last_login_at=utcnow(); await audit(db,'ADMIN',a.id,'ADMIN_LOGIN','ADMIN',a.id); await db.commit()
    return TokenResponse(access_token=create_access_token(a.id,a.role,'ADMIN'),actor_type='ADMIN',role=a.role)
