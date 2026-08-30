import hashlib
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_ped
from app.core.security import utcnow
from app.models.entities import Payment, Ped, Registration, WebhookEvent
from app.schemas import MessageResponse, PaymentOrderOut, PaymentVerifyIn
from app.services.email import email_service, payment_html
from app.services.helpers import audit
from app.services.payment import payment_service

router = APIRouter(prefix='/payments', tags=['Payments'])


async def owned_registration(db: AsyncSession, ped: Ped, registration_id: str):
    registration = await db.scalar(
        select(Registration)
        .where(Registration.id == registration_id, Registration.ped_id == ped.id)
        .options(selectinload(Registration.students), selectinload(Registration.event_config))
    )
    if not registration:
        raise HTTPException(status_code=404, detail='Registration not found')
    return registration


def validate_payment_ready(registration: Registration):
    event = registration.event_config
    if registration.status not in {'DRAFT', 'PAYMENT_PENDING', 'CORRECTION_REQUIRED'}:
        raise HTTPException(status_code=409, detail='Payment not allowed in current status')
    if not event.team_min_size <= len(registration.students) <= event.team_max_size:
        raise HTTPException(status_code=422, detail=f'Team size must be {event.team_min_size}-{event.team_max_size}')
    missing_photos = [s.full_name for s in registration.students if not s.photo_path]
    if missing_photos:
        raise HTTPException(status_code=422, detail={'missing_student_photos': missing_photos})
    missing_emails = [s.full_name for s in registration.students if not s.email]
    if missing_emails:
        raise HTTPException(status_code=422, detail={'missing_student_emails': missing_emails})
    if not registration.bonafide_path:
        raise HTTPException(status_code=422, detail='Principal-signed bonafide is required')
    if not registration.declaration_accepted or not registration.consent_accepted:
        raise HTTPException(status_code=422, detail='Declaration and consent are required')


@router.post('/registrations/{registration_id}/order', response_model=PaymentOrderOut)
async def create_order(
    registration_id: str,
    ped: Ped = Depends(get_current_ped),
    db: AsyncSession = Depends(get_db),
):
    registration = await owned_registration(db, ped, registration_id)
    if registration.payment_status == 'PAID':
        raise HTTPException(status_code=409, detail='Already paid')
    validate_payment_ready(registration)
    order = await payment_service.create_order(
        registration.fee_paise,
        registration.registration_code,
        {
            'registration_id': registration.id,
            'registration_code': registration.registration_code,
            'ped_email': ped.official_email,
        },
    )
    payment = Payment(
        registration_id=registration.id,
        order_id=order['id'],
        amount_paise=registration.fee_paise,
        currency=order.get('currency', settings.RAZORPAY_CURRENCY),
        status='CREATED',
        raw_payload=order,
    )
    db.add(payment)
    registration.payment_status = 'ORDER_CREATED'
    registration.status = 'PAYMENT_PENDING'
    await db.flush()
    await audit(db, 'PED', ped.id, 'CREATE_PAYMENT_ORDER', 'PAYMENT', payment.id, details={'order_id': order['id']})
    await db.commit()
    return PaymentOrderOut(
        key_id=settings.RAZORPAY_KEY_ID or 'test_key',
        order_id=order['id'],
        amount=registration.fee_paise,
        currency=order.get('currency', settings.RAZORPAY_CURRENCY),
        registration_id=registration.id,
        registration_code=registration.registration_code,
        test_mode=settings.TEST_MODE and settings.ALLOW_TEST_PAYMENT,
    )


async def _mark_paid(db: AsyncSession, registration: Registration, payment: Payment, payment_id: str | None, raw_payload=None):
    if payment.status == 'PAID':
        return False
    payment.payment_id = payment_id or payment.payment_id
    payment.status = 'PAID'
    payment.paid_at = utcnow()
    if raw_payload is not None:
        payment.raw_payload = raw_payload
    registration.payment_status = 'PAID'
    registration.status = 'UNDER_REVIEW'
    registration.submitted_at = utcnow()
    await email_service.send(
        db,
        registration.ped.official_email,
        f'Payment received — {registration.registration_code}',
        payment_html(
            registration,
            registration.event_config,
            f"{settings.PUBLIC_APP_URL.rstrip('/')}/#/ped",
        ),
        'PAYMENT_SUCCESS',
        registration.id,
    )
    return True


@router.post('/verify', response_model=MessageResponse)
async def verify_checkout(
    payload: PaymentVerifyIn,
    ped: Ped = Depends(get_current_ped),
    db: AsyncSession = Depends(get_db),
):
    registration = await db.scalar(
        select(Registration)
        .where(Registration.id == payload.registration_id, Registration.ped_id == ped.id)
        .options(selectinload(Registration.event_config), selectinload(Registration.ped))
    )
    if not registration:
        raise HTTPException(status_code=404, detail='Registration not found')
    payment = await db.scalar(
        select(Payment).where(
            Payment.registration_id == registration.id,
            Payment.order_id == payload.razorpay_order_id,
        )
    )
    if not payment:
        raise HTTPException(status_code=404, detail='Payment order not found')
    if payment.status == 'PAID':
        return MessageResponse(message='Payment already verified')
    if not payment_service.verify_checkout(payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature):
        payment.status = 'SIGNATURE_FAILED'
        await db.commit()
        raise HTTPException(status_code=400, detail='Payment signature failed')

    payment.signature = payload.razorpay_signature
    await _mark_paid(db, registration, payment, payload.razorpay_payment_id)
    await audit(db, 'PED', ped.id, 'VERIFY_PAYMENT', 'PAYMENT', payment.id, details={'payment_id': payment.payment_id})
    await db.commit()
    return MessageResponse(message='Payment verified; application is now under admin review')


@router.post('/razorpay/webhook')
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header('', alias='X-Razorpay-Signature'),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    if not payment_service.verify_webhook(raw_body, x_razorpay_signature):
        raise HTTPException(status_code=400, detail='Invalid webhook signature')
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail='Invalid webhook JSON') from exc

    event_type = payload.get('event', 'unknown')
    event_id = payload.get('id') or hashlib.sha256(raw_body).hexdigest()
    if await db.scalar(select(WebhookEvent).where(WebhookEvent.external_event_id == event_id)):
        return {'status': 'already_processed'}

    webhook = WebhookEvent(external_event_id=event_id, event_type=event_type, payload=payload)
    db.add(webhook)
    await db.flush()
    payment_entity = payload.get('payload', {}).get('payment', {}).get('entity', {})
    order_entity = payload.get('payload', {}).get('order', {}).get('entity', {})
    order_id = payment_entity.get('order_id') or order_entity.get('id')
    payment = await db.scalar(select(Payment).where(Payment.order_id == order_id)) if order_id else None

    if payment and event_type in {'payment.captured', 'order.paid'}:
        registration = await db.scalar(
            select(Registration)
            .where(Registration.id == payment.registration_id)
            .options(selectinload(Registration.event_config), selectinload(Registration.ped))
        )
        if registration:
            await _mark_paid(db, registration, payment, payment_entity.get('id'), payload)
    elif payment and event_type == 'payment.failed':
        payment.payment_id = payment_entity.get('id') or payment.payment_id
        payment.status = 'FAILED'
        payment.raw_payload = payload
        registration = await db.get(Registration, payment.registration_id)
        if registration and registration.payment_status != 'PAID':
            registration.payment_status = 'FAILED'
            registration.status = 'PAYMENT_PENDING'

    webhook.processed = True
    await db.commit()
    return {'status': 'ok'}
