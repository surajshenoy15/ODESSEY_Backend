import hashlib
import hmac
import uuid

import httpx
from fastapi import HTTPException

from app.core.config import settings


class PaymentService:
    async def create_order(self, amount, receipt, notes):
        if settings.TEST_MODE and settings.ALLOW_TEST_PAYMENT:
            return {
                "id": f"order_test_{uuid.uuid4().hex[:18]}",
                "amount": amount,
                "currency": settings.RAZORPAY_CURRENCY,
                "receipt": receipt,
                "status": "created",
                "notes": notes,
            }
        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            raise HTTPException(status_code=503, detail="Razorpay not configured")
        payload = {
            "amount": amount,
            "currency": settings.RAZORPAY_CURRENCY,
            "receipt": receipt[:40],
            "notes": notes,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.razorpay.com/v1/orders",
                json=payload,
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            )
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Razorpay order creation failed: {response.text[:500]}",
            )
        return response.json()

    def verify_checkout(self, order, payment, signature):
        if (
            settings.TEST_MODE
            and settings.ALLOW_TEST_PAYMENT
            and order.startswith("order_test_")
            and payment.startswith("pay_test_")
            and signature == "test_signature"
        ):
            return True
        expected = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode(),
            f"{order}|{payment}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_webhook(self, body, signature):
        if not settings.RAZORPAY_WEBHOOK_SECRET:
            return False
        expected = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


payment_service = PaymentService()
