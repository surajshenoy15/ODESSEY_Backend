import io,mimetypes,zipfile
from fastapi import APIRouter,Depends,File,HTTPException,UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import and_,or_,select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_ped
from app.core.security import utcnow
from app.models.entities import Certificate,CertificateCorrectionRequest,EventConfig,Fixture,Ped,Registration,Student
from app.schemas import CertificateCorrectionCreate,MessageResponse,PedOut,PedProfileUpdate,RegistrationCreate,RegistrationOut,RegistrationUpdate,StudentCreate,StudentOut,StudentUpdate
from app.services.helpers import audit,ensure_editable,qr_png_bytes,registration_code,safe_filename
from app.services.storage import storage
router=APIRouter(prefix='/ped',tags=['PED Portal'])
IMG={'image/jpeg','image/png','image/webp'}; DOC={'application/pdf','image/jpeg','image/png'}

def aware(v):
    if not v:return None
    return v.replace(tzinfo=utcnow().tzinfo) if v.tzinfo is None else v
async def owned(db,ped,rid,students=True):
    q=select(Registration).where(Registration.id==rid,Registration.ped_id==ped.id)
    if students:q=q.options(selectinload(Registration.students))
    r=await db.scalar(q)
    if not r:raise HTTPException(status_code=404,detail='Registration not found')
    return r
@router.get('/me',response_model=PedOut)
async def me(ped:Ped=Depends(get_current_ped)):return ped
@router.put('/me',response_model=PedOut)
async def profile(p:PedProfileUpdate,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    for k,v in p.model_dump(exclude_unset=True).items():
        if k=='declaration_accepted':
            if v:ped.declaration_accepted_at=utcnow()
        else:setattr(ped,k,v)
    await audit(db,'PED',ped.id,'UPDATE_PROFILE','PED',ped.id);await db.commit();await db.refresh(ped);return ped
@router.get('/dashboard')
async def dashboard(ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    rows=(await db.scalars(select(Registration).where(Registration.ped_id==ped.id).options(selectinload(Registration.students),selectinload(Registration.event_config)).order_by(Registration.created_at.desc()))).all();counts={};items=[]
    nexts={'DRAFT':'Complete roster, uploads and payment','PAYMENT_PENDING':'Complete payment','UNDER_REVIEW':'Wait for admin review','CORRECTION_REQUIRED':'Correct requested fields and resubmit','APPROVED':'Present QR and view fixtures','REJECTED':'Contact support'}
    for r in rows:counts[r.status]=counts.get(r.status,0)+1;items.append({'id':r.id,'registration_code':r.registration_code,'event':r.event_config.sport_name,'category':r.event_config.category,'student_count':len(r.students),'status':r.status,'payment_status':r.payment_status,'next_action':nexts.get(r.status,'View details')})
    return {'profile_complete':bool(ped.name and ped.college_name and ped.contact_number),'counts':counts,'registrations':items}
@router.post('/registrations',response_model=RegistrationOut,status_code=201)
async def create_registration(p:RegistrationCreate,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    e=await db.get(EventConfig,p.event_config_id);now=utcnow()
    if not e or not e.is_active:raise HTTPException(status_code=404,detail='Event/category not found')
    if not e.is_registration_open or (e.registration_opens_at and aware(e.registration_opens_at)>now) or (e.registration_closes_at and aware(e.registration_closes_at)<now):raise HTTPException(status_code=409,detail='Registration is closed')
    dup=await db.scalar(select(Registration).where(Registration.ped_id==ped.id,Registration.event_config_id==e.id,Registration.status.notin_(['REJECTED','CANCELLED'])))
    if dup:raise HTTPException(status_code=409,detail='Active registration already exists for this event/category')
    r=Registration(registration_code=registration_code(),ped_id=ped.id,fee_paise=e.fee_paise,**p.model_dump());db.add(r);await db.flush();await audit(db,'PED',ped.id,'CREATE_REGISTRATION','REGISTRATION',r.id);await db.commit();return await owned(db,ped,r.id)
@router.get('/registrations',response_model=list[RegistrationOut])
async def registrations(ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):return (await db.scalars(select(Registration).where(Registration.ped_id==ped.id).options(selectinload(Registration.students)).order_by(Registration.created_at.desc()))).all()
@router.get('/registrations/{rid}',response_model=RegistrationOut)
async def registration(rid:str,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):return await owned(db,ped,rid)
@router.patch('/registrations/{rid}',response_model=RegistrationOut)
async def update_registration(rid:str,p:RegistrationUpdate,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid);ensure_editable(r);data=p.model_dump(exclude_unset=True)
    if r.status=='CORRECTION_REQUIRED' and r.correction_fields:
        invalid=[k for k in data if k not in r.correction_fields]
        if invalid:raise HTTPException(status_code=409,detail={'allowed_fields':r.correction_fields,'invalid_fields':invalid})
    for k,v in data.items():setattr(r,k,v)
    await audit(db,'PED',ped.id,'UPDATE_REGISTRATION','REGISTRATION',r.id,details={'fields':list(data)});await db.commit();return await owned(db,ped,rid)
@router.post('/registrations/{rid}/students',response_model=StudentOut,status_code=201)
async def add_student(rid:str,p:StudentCreate,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid);ensure_editable(r);e=await db.get(EventConfig,r.event_config_id)
    if len(r.students)>=e.team_max_size:raise HTTPException(status_code=409,detail=f'Maximum team size is {e.team_max_size}')
    if await db.scalar(select(Student).where(Student.registration_id==r.id,Student.usn==p.usn)):raise HTTPException(status_code=409,detail='USN already exists in this registration')
    duplicate=await db.scalar(select(Student).join(Registration).where(Student.usn==p.usn,Registration.event_config_id==r.event_config_id,Registration.id!=r.id,Registration.status.notin_(['REJECTED','CANCELLED'])))
    if duplicate:raise HTTPException(status_code=409,detail='USN already registered in this event/category')
    s=Student(registration_id=r.id,**p.model_dump());db.add(s);await db.flush();await audit(db,'PED',ped.id,'ADD_STUDENT','STUDENT',s.id,details={'registration_id':r.id});await db.commit();await db.refresh(s);return s
@router.patch('/registrations/{rid}/students/{sid}',response_model=StudentOut)
async def update_student(rid:str,sid:str,p:StudentUpdate,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid);ensure_editable(r);s=await db.scalar(select(Student).where(Student.id==sid,Student.registration_id==r.id))
    if not s:raise HTTPException(status_code=404,detail='Student not found')
    data=p.model_dump(exclude_unset=True)
    for k,v in data.items():setattr(s,k,v)
    await audit(db,'PED',ped.id,'UPDATE_STUDENT','STUDENT',s.id,details={'fields':list(data)});await db.commit();await db.refresh(s);return s
@router.delete('/registrations/{rid}/students/{sid}',response_model=MessageResponse)
async def delete_student(rid:str,sid:str,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid);ensure_editable(r);s=await db.scalar(select(Student).where(Student.id==sid,Student.registration_id==r.id))
    if not s:raise HTTPException(status_code=404,detail='Student not found')
    await audit(db,'PED',ped.id,'DELETE_STUDENT','STUDENT',s.id);await db.delete(s);await db.commit();return MessageResponse(message='Student removed')
@router.post('/registrations/{rid}/students/{sid}/photo',response_model=StudentOut)
async def photo(rid:str,sid:str,file:UploadFile=File(...),ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid);ensure_editable(r);s=await db.scalar(select(Student).where(Student.id==sid,Student.registration_id==r.id))
    if not s:raise HTTPException(status_code=404,detail='Student not found')
    if file.content_type not in IMG:raise HTTPException(status_code=415,detail='Photo must be JPG, PNG or WEBP')
    data=await file.read(5*1024*1024+1)
    if len(data)>5*1024*1024:raise HTTPException(status_code=413,detail='Photo exceeds 5 MB')
    ext=mimetypes.guess_extension(file.content_type) or '.jpg';path=f'{r.id}/{s.id}{ext}';await storage.upload(settings.SUPABASE_BUCKET_STUDENT_PHOTOS,path,data,file.content_type);s.photo_path=path;await audit(db,'PED',ped.id,'UPLOAD_STUDENT_PHOTO','STUDENT',s.id);await db.commit();await db.refresh(s);return s
@router.post('/registrations/{rid}/bonafide',response_model=RegistrationOut)
async def bonafide(rid:str,file:UploadFile=File(...),ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid);ensure_editable(r)
    if file.content_type not in DOC:raise HTTPException(status_code=415,detail='Bonafide must be PDF, JPG or PNG')
    data=await file.read(10*1024*1024+1)
    if len(data)>10*1024*1024:raise HTTPException(status_code=413,detail='Bonafide exceeds 10 MB')
    ext=mimetypes.guess_extension(file.content_type) or '.pdf';path=f'{r.id}/bonafide{ext}';await storage.upload(settings.SUPABASE_BUCKET_BONAFIDES,path,data,file.content_type);r.bonafide_path=path;await audit(db,'PED',ped.id,'UPLOAD_BONAFIDE','REGISTRATION',r.id);await db.commit();return await owned(db,ped,rid)
async def validate_ready(db,r):
    e=await db.get(EventConfig,r.event_config_id);students=(await db.scalars(select(Student).where(Student.registration_id==r.id))).all()
    if not e.team_min_size<=len(students)<=e.team_max_size:raise HTTPException(status_code=422,detail=f'Team size must be {e.team_min_size}-{e.team_max_size}')
    missing=[s.full_name for s in students if not s.photo_path]
    if missing:raise HTTPException(status_code=422,detail={'missing_student_photos':missing})
    if not r.bonafide_path:raise HTTPException(status_code=422,detail='Bonafide is required')
    if not r.declaration_accepted or not r.consent_accepted:raise HTTPException(status_code=422,detail='Declaration and consent are required')
@router.post('/registrations/{rid}/resubmit',response_model=RegistrationOut)
async def resubmit(rid:str,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid)
    if r.status!='CORRECTION_REQUIRED':raise HTTPException(status_code=409,detail='Not awaiting correction')
    await validate_ready(db,r);r.status='UNDER_REVIEW';r.admin_note=None;r.correction_fields=None;r.submitted_at=utcnow();await audit(db,'PED',ped.id,'RESUBMIT_REGISTRATION','REGISTRATION',r.id);await db.commit();return await owned(db,ped,rid)
@router.get('/fixtures')
async def fixtures(ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    ids=(await db.scalars(select(Registration.event_config_id).where(Registration.ped_id==ped.id))).all();q=select(Fixture).where(Fixture.status=='PUBLISHED',or_(Fixture.visibility.in_(['PUBLIC','ALL_PEDS']),and_(Fixture.visibility=='RELEVANT_PEDS',Fixture.event_config_id.in_(ids or ['none'])))).order_by(Fixture.published_at.desc());rows=(await db.scalars(q)).all();out=[]
    for x in rows:out.append({'id':x.id,'event_config_id':x.event_config_id,'title':x.title,'note':x.note,'version':x.version,'published_at':x.published_at,'download_url':await storage.signed_url(settings.SUPABASE_BUCKET_FIXTURES,x.file_path)})
    return out
@router.get('/certificates')
async def certificates(ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    rows=(await db.execute(select(Certificate,Student,Registration,EventConfig).join(Student,Certificate.student_id==Student.id).join(Registration,Certificate.registration_id==Registration.id).join(EventConfig,Registration.event_config_id==EventConfig.id).where(Registration.ped_id==ped.id,Certificate.status=='PUBLISHED').order_by(EventConfig.sport_name,Student.full_name))).all();out=[]
    for c,s,r,e in rows:out.append({'id':c.id,'student_name':s.full_name,'usn':s.usn,'registration_id':r.id,'registration_code':r.registration_code,'event':e.sport_name,'category':e.category,'certificate_number':c.certificate_number,'version':c.version,'download_url':f"{settings.API_PUBLIC_URL.rstrip('/')}{settings.API_V1_PREFIX}/ped/certificates/{c.id}/download",'download_count':c.download_count})
    return out
@router.get('/registrations/{rid}/certificates.zip')
async def team_certificates(rid:str,ped:Ped=Depends(get_current_ped),db:AsyncSession=Depends(get_db)):
    r=await owned(db,ped,rid,False);rows=(await db.execute(select(Certificate,Student).join(Student,Certificate.student_id==Student.id).where(Certificate.registration_id==r.id,Certificate.status=='PUBLISHED'))).all()
    if not rows:raise HTTPException(status_code=404,detail='No published certificates')
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        for c,s in rows:z.writestr(f'{safe_filename(s.full_name)}-{safe_filename(s.usn)}.pdf',await storage.download(settings.SUPABASE_BUCKET_CERTIFICATES,c.file_path))
    b.seek(0);return StreamingResponse(b,media_type='application/zip',headers={'Content-Disposition':f'attachment; filename={r.registration_code}-certificates.zip'})

@router.get('/registrations/{rid}/qr.png')
async def ped_registration_qr(
    rid: str,
    ped: Ped = Depends(get_current_ped),
    db: AsyncSession = Depends(get_db),
):
    registration = await owned(db, ped, rid, False)
    if registration.status != 'APPROVED' or not registration.qr_token:
        raise HTTPException(status_code=404, detail='QR unavailable')
    return StreamingResponse(
        io.BytesIO(qr_png_bytes(registration.qr_token)),
        media_type='image/png',
        headers={
            'Content-Disposition': f'inline; filename={registration.registration_code}-QR.png'
        },
    )


@router.get('/certificates/{certificate_id}/download')
async def download_certificate(
    certificate_id: str,
    ped: Ped = Depends(get_current_ped),
    db: AsyncSession = Depends(get_db),
):
    row = await db.execute(
        select(Certificate, Student, Registration)
        .join(Student, Certificate.student_id == Student.id)
        .join(Registration, Certificate.registration_id == Registration.id)
        .where(
            Certificate.id == certificate_id,
            Certificate.status == 'PUBLISHED',
            Registration.ped_id == ped.id,
        )
    )
    result = row.first()
    if not result:
        raise HTTPException(status_code=404, detail='Certificate not found')
    certificate, student, registration = result
    data = await storage.download(
        settings.SUPABASE_BUCKET_CERTIFICATES, certificate.file_path
    )
    certificate.download_count += 1
    certificate.last_downloaded_at = utcnow()
    await audit(
        db,
        'PED',
        ped.id,
        'DOWNLOAD_CERTIFICATE',
        'CERTIFICATE',
        certificate.id,
    )
    await db.commit()
    filename = f'{safe_filename(student.full_name)}-{safe_filename(student.usn)}.pdf'
    return StreamingResponse(
        io.BytesIO(data),
        media_type='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@router.post('/certificates/{certificate_id}/correction-request', response_model=MessageResponse)
async def request_certificate_correction(
    certificate_id: str,
    payload: CertificateCorrectionCreate,
    ped: Ped = Depends(get_current_ped),
    db: AsyncSession = Depends(get_db),
):
    certificate = await db.scalar(
        select(Certificate)
        .join(Registration, Certificate.registration_id == Registration.id)
        .where(
            Certificate.id == certificate_id,
            Certificate.status == 'PUBLISHED',
            Registration.ped_id == ped.id,
        )
    )
    if not certificate:
        raise HTTPException(status_code=404, detail='Certificate not found')
    existing = await db.scalar(
        select(CertificateCorrectionRequest).where(
            CertificateCorrectionRequest.certificate_id == certificate.id,
            CertificateCorrectionRequest.status == 'OPEN',
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail='Correction request already open')
    request = CertificateCorrectionRequest(
        certificate_id=certificate.id,
        ped_id=ped.id,
        reason=payload.reason,
    )
    db.add(request)
    await db.flush()
    await audit(
        db,
        'PED',
        ped.id,
        'REQUEST_CERTIFICATE_CORRECTION',
        'CERTIFICATE_CORRECTION_REQUEST',
        request.id,
        payload.reason,
    )
    await db.commit()
    return MessageResponse(message='Certificate correction request submitted')
