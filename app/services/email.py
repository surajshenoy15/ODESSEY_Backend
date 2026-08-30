import base64
import html as html_lib
import logging
from typing import Iterable

import httpx

from app.core.config import settings
from app.models.entities import EmailLog

logger = logging.getLogger(__name__)


def _esc(value) -> str:
    return html_lib.escape(str(value or ''))


def branded_email(title: str, body_html: str, preheader: str = '') -> str:
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f3f5f7;font-family:Arial,Helvetica,sans-serif;color:#0b2341">
<div style="display:none;max-height:0;overflow:hidden">{_esc(preheader)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f5f7;padding:28px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#fff;border:1px solid #e5e7eb;border-radius:18px;overflow:hidden">
<tr><td style="background:#08264a;padding:24px 30px;border-top:5px solid #ff5a1f">
<div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#b9c8dc;font-weight:700">BNMIT</div>
<div style="font-size:28px;line-height:1.05;color:#fff;font-weight:800;margin-top:4px">ODYSSEY</div>
<div style="font-size:12px;color:#b9c8dc;margin-top:6px">Intercollegiate Sports & Cultural Meet</div>
</td></tr>
<tr><td style="padding:30px">
<h1 style="font-size:24px;line-height:1.25;margin:0 0 16px;color:#0b2341">{_esc(title)}</h1>
<div style="font-size:15px;line-height:1.65;color:#34465c">{body_html}</div>
</td></tr>
<tr><td style="padding:18px 30px;background:#f8fafc;border-top:1px solid #e5e7eb;font-size:12px;line-height:1.5;color:#718096">
This is an official automated message from BNMIT ODYSSEY. Please keep your registration ID for event-day support.
</td></tr></table></td></tr></table></body></html>'''


def info_card(rows: Iterable[tuple[str, str]]) -> str:
    cells = ''.join(
        f'<tr><td style="padding:8px 0;color:#718096;width:42%">{_esc(k)}</td><td style="padding:8px 0;font-weight:700;color:#0b2341">{_esc(v)}</td></tr>'
        for k, v in rows
    )
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:16px 0;border-top:1px solid #edf2f7;border-bottom:1px solid #edf2f7">{cells}</table>'


def action_button(label: str, url: str) -> str:
    return f'<p style="margin:22px 0 4px"><a href="{_esc(url)}" style="display:inline-block;background:#ff5a1f;color:#fff;text-decoration:none;font-weight:700;padding:12px 18px;border-radius:8px">{_esc(label)}</a></p>'


class EmailService:
    async def send(self, db, to_email, subject, html, message_type, registration_id=None, attachments=None):
        log = EmailLog(
            recipient=str(to_email).lower(),
            subject=subject,
            message_type=message_type,
            related_registration_id=registration_id,
            status='PENDING',
        )
        db.add(log)
        await db.flush()

        if settings.TEST_MODE and not settings.BREVO_API_KEY:
            log.status = 'SKIPPED_TEST_MODE'
            return log
        if not settings.BREVO_API_KEY:
            log.status = 'FAILED'
            log.error_message = 'BREVO_API_KEY missing'
            return log

        payload = {
            'sender': {'name': settings.BREVO_SENDER_NAME, 'email': str(settings.BREVO_SENDER_EMAIL)},
            'to': [{'email': str(to_email)}],
            'subject': subject,
            'htmlContent': html,
        }
        if attachments:
            payload['attachment'] = [
                {'name': name, 'content': base64.b64encode(data).decode()}
                for name, data in attachments
            ]
        headers = {
            'api-key': settings.BREVO_API_KEY,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if settings.BREVO_SANDBOX_MODE:
            headers['X-Sib-Sandbox'] = 'drop'
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post('https://api.brevo.com/v3/smtp/email', json=payload, headers=headers)
            if response.status_code >= 400:
                log.status = 'FAILED'
                log.error_message = response.text[:2000]
            else:
                log.status = 'SENT'
                log.provider_message_id = response.json().get('messageId')
        except Exception as exc:
            logger.exception('Brevo send failed')
            log.status = 'FAILED'
            log.error_message = str(exc)[:2000]
        await db.flush()
        return log


email_service = EmailService()


def otp_html(otp, minutes):
    return branded_email(
        'Your PED login OTP',
        f'<p>Use the one-time password below to securely access the PED portal.</p>'
        f'<div style="margin:22px 0;padding:18px;background:#f1f5f9;border-radius:12px;text-align:center;font-size:32px;letter-spacing:8px;font-weight:800;color:#08264a">{_esc(otp)}</div>'
        f'<p>This OTP expires in <strong>{minutes} minutes</strong>. Do not share it with anyone.</p>',
        'Your BNMIT ODYSSEY PED login OTP',
    )


def payment_html(registration, event, dashboard_url):
    return branded_email(
        'Payment received — application under review',
        '<p>Your payment has been acknowledged successfully. This does <strong>not</strong> mean final approval yet. The BNMIT ODYSSEY team will verify the roster, student photographs and principal-signed bonafide.</p>'
        + info_card([
            ('Registration ID', registration.registration_code),
            ('College / School', registration.college_name),
            ('Event', event.sport_name),
            ('Category', event.category),
            ('Status', 'Paid — Under Admin Review'),
        ])
        + '<p>You will receive another email when the application is approved, needs correction or is rejected.</p>'
        + action_button('Open PED Dashboard', dashboard_url),
        'Payment received for BNMIT ODYSSEY',
    )


def approval_html(registration, event, dashboard_url):
    return branded_email(
        'Registration approved',
        '<p>Your team registration has been approved. The attached QR code is the official event-day check-in credential.</p>'
        + info_card([
            ('Registration ID', registration.registration_code),
            ('College / School', registration.college_name),
            ('Event', event.sport_name),
            ('Category', event.category),
            ('Approval', 'Approved'),
        ])
        + '<div style="padding:14px 16px;background:#fff7ed;border-left:4px solid #ff5a1f;border-radius:8px"><strong>Event-day requirement:</strong> The team must bring the <strong>original principal-signed bonafide</strong> on the date of the event.</div>'
        + '<p style="margin-top:18px">Attendance is recorded student-by-student. A duplicate team check-in is blocked after confirmation.</p>'
        + action_button('Open PED Dashboard', dashboard_url),
        'Your BNMIT ODYSSEY registration is approved',
    )


def fixture_html(event_name, category, title, version, dashboard_url, note=''):
    note_html = f'<p><strong>Note:</strong> {_esc(note)}</p>' if note else ''
    return branded_email(
        'Fixture / draw published',
        '<p>A fixture copy relevant to your registration has been published or updated.</p>'
        + info_card([
            ('Event', event_name),
            ('Category', category),
            ('Fixture', title),
            ('Version', str(version)),
        ])
        + note_html
        + action_button('View Fixture in PED Portal', dashboard_url),
        'A BNMIT ODYSSEY fixture has been published',
    )


def certificate_html(student_name, event_name, category, dashboard_url):
    return branded_email(
        'Participation certificate ready',
        f'<p>The participation certificate for <strong>{_esc(student_name)}</strong> is ready.</p>'
        + info_card([('Event', event_name), ('Category', category)])
        + '<p>The certificate is attached to this email when available and is also published in the PED portal.</p>'
        + action_button('Open PED Portal', dashboard_url),
        'Your BNMIT ODYSSEY certificate is ready',
    )
