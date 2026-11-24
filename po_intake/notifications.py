from datetime import timedelta
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, func, or_, select, update

from po_intake import review
from po_intake.db import Notification, NotificationAttempt, PurchaseOrder, SalesOrder, Snapshot, Version


class DeliveryResult(BaseModel):
    outcome: Literal["sent", "failed", "unknown"]
    provider_receipt: str | None = Field(default=None, min_length=1, max_length=512)
    error: str | None = Field(default=None, max_length=500)
    retryable: bool = False

    @model_validator(mode="after")
    def check_consistency(self):
        if self.outcome == "sent" and not self.provider_receipt:
            raise ValueError("A sent result requires a provider receipt.")
        if self.outcome != "failed" and self.retryable:
            raise ValueError("Only known non-delivery can be retried automatically.")
        return self


class Reconciliation(BaseModel):
    outcome: Literal["delivered", "not_delivered"]
    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    provider_receipt: str | None = Field(default=None, min_length=1, max_length=512)


class RetryAuthorization(BaseModel):
    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")


def clock(db):
    return db.scalar(select(func.clock_timestamp()))


def open_episode(db, settings, scope, intake_id, po_id, reference, reason):
    existing = db.scalar(select(Notification.id).where(
        Notification.episode_scope == scope, Notification.closed_at.is_(None),
    ))
    if existing:
        return
    clean_reason = " ".join(reason.split())[:220]
    link = f"{settings.dashboard_url.rstrip('/')}/?intake={intake_id}"
    db.add(Notification(
        event_key=f"review:{scope}:{uuid4().hex}", episode_scope=scope,
        intake_id=intake_id, po_id=po_id, channel="telegram",
        destination=settings.telegram_chat or None, destination_alias="Private demo Telegram chat",
        payload={"text": f"{reference} needs review\n{clean_reason}\n{link}"},
    ))


def close_episode(db, scope):
    db.execute(update(Notification).where(
        Notification.episode_scope == scope, Notification.closed_at.is_(None),
    ).values(closed_at=func.clock_timestamp()))


def sync_review(db, settings, intake, technical_error=None):
    scope = f"intake:{intake.id}"
    if intake.canonical_id:
        close_episode(db, scope)
        return
    if technical_error:
        open_episode(db, settings, scope, intake.id, None, f"Intake #{intake.id}", technical_error)
        return
    version = review.version_for(db, intake.id)
    if not version:
        return
    if not version.po_id:
        snapshot = review.current_snapshot(db, version)
        assessment = review.latest_assessment(db, snapshot)
        reason = (assessment or {}).get("issues") or ["Verify the PO identity."]
        open_episode(db, settings, scope, intake.id, None, f"Intake #{intake.id}", reason[0])
        return

    close_episode(db, scope)
    po = db.get(PurchaseOrder, version.po_id)
    po_scope = f"po:{po.id}"
    if po.status != "needs_review":
        close_episode(db, po_scope)
    elif version.disposition not in {"equivalent", "exact_resend", "post_order"}:
        snapshot = db.get(Snapshot, po.active_snapshot_id)
        assessment = review.latest_assessment(db, snapshot) or {}
        reason = next(iter(assessment.get("issues", [])), "A proposed revision needs staff review.")
        number = snapshot.draft["po_number"]["value"] or f"PO #{po.id}"
        active = db.get(Version, po.active_version_id)
        open_episode(db, settings, po_scope, active.intake_id, po.id, number, reason)


def queue_confirmation(db, settings, order):
    number = f"SO-{order.id:06d}"
    db.add(Notification(
        event_key=f"order:{order.id}:confirmation", intake_id=order.intake_id,
        po_id=order.po_id, order_id=order.id, channel="gmail",
        destination=settings.gmail_recipients.get(order.customer),
        destination_alias=f"{order.customer} demo email",
        payload={"subject": f"Order confirmed: {order.po_number} / {number}",
                 "text": f"Purchase order {order.po_number} was confirmed as sales order {number}."},
    ))


def current_attempt(db, intent):
    return db.get(NotificationAttempt, intent.current_attempt_id) if intent.current_attempt_id else None


def settle(intent, attempt, result, now, backoff=0):
    attempt.result = result
    attempt.outcome = intent.state = result["outcome"]
    attempt.finished_at = now
    intent.retryable = result["retryable"]
    intent.available_at = now + timedelta(seconds=backoff)


def expire_claims(db, now):
    expired = db.scalars(select(Notification).join(
        NotificationAttempt, NotificationAttempt.id == Notification.current_attempt_id,
    ).where(
        Notification.state == "sending", NotificationAttempt.deadline <= now,
    ).with_for_update(of=Notification, skip_locked=True)).all()
    for intent in expired:
        mark_expired(intent, current_attempt(db, intent), now)


def mark_expired(intent, attempt, now):
    result = DeliveryResult(outcome="unknown", error="Delivery claim expired without a provider result.")
    settle(intent, attempt, result.model_dump(), now)


def claim(db, settings):
    now = clock(db)
    expire_claims(db, now)
    if not settings.outbound_enabled:
        return None

    destinations = []
    if settings.telegram_chat:
        destinations.append(and_(Notification.channel == "telegram", Notification.destination == settings.telegram_chat))
    for customer, email in settings.gmail_recipients.items():
        destinations.append(and_(
            Notification.channel == "gmail", Notification.destination == email,
            select(SalesOrder.id).where(
                SalesOrder.id == Notification.order_id, SalesOrder.customer == customer,
            ).exists(),
        ))
    if not destinations:
        return None

    intent = db.scalar(select(Notification).where(
        Notification.available_at <= now, Notification.closed_at.is_(None), or_(*destinations),
        or_(Notification.state == "pending", and_(
            Notification.state == "failed", Notification.retryable,
            Notification.attempt_count < settings.notification_max_attempts,
        )),
    ).order_by(Notification.available_at, Notification.id).limit(1).with_for_update(skip_locked=True))
    if intent is None:
        return None
    token = uuid4().hex
    deadline = now + timedelta(seconds=settings.notification_lease)
    intent.state = "sending"
    intent.attempt_count += 1
    intent.current_attempt_id = token
    db.add(NotificationAttempt(
        id=token, notification_id=intent.id, number=intent.attempt_count,
        started_at=now, deadline=deadline, outcome="sending",
    ))
    return {
        "notification_id": intent.id, "attempt_id": token, "deadline": deadline,
        "channel": intent.channel, "destination": intent.destination, "payload": intent.payload,
    }


def locked_intent(db, notification_id):
    intent = db.get(Notification, notification_id, with_for_update=True)
    if intent is None:
        raise HTTPException(404, "Notification not found.")
    return intent


def report(db, settings, attempt_id, result):
    attempt = db.get(NotificationAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(404, "Delivery attempt not found.")
    intent = locked_intent(db, attempt.notification_id)
    db.refresh(attempt)
    if intent.current_attempt_id != attempt.id:
        return None
    payload = result.model_dump()
    if attempt.outcome != "sending":
        return intent.state if payload == attempt.result else None
    now = clock(db)
    if attempt.deadline <= now:
        mark_expired(intent, attempt, now)
        return None
    settle(intent, attempt, payload, now, settings.notification_backoff)
    return intent.state


def reconcile(db, settings, notification_id, decision):
    intent = locked_intent(db, notification_id)
    attempt = current_attempt(db, intent)
    if intent.state != "unknown":
        raise HTTPException(409, "Only an unknown delivery can be reconciled.")
    attempt.reconciliation = {
        **decision.model_dump(), "reviewer": settings.reviewer, "at": clock(db).isoformat(),
    }
    intent.state = "sent" if decision.outcome == "delivered" else "failed"
    intent.retryable = False
    return intent.state


def authorize_retry(db, settings, notification_id, authorization):
    intent = locked_intent(db, notification_id)
    if intent.state != "failed" or intent.closed_at is not None:
        raise HTTPException(409, "Retry requires known non-delivery and an open notification.")
    now = clock(db)
    current_attempt(db, intent).retry_authorization = {
        "reason": authorization.reason, "reviewer": settings.reviewer, "at": now.isoformat(),
    }
    intent.state, intent.available_at, intent.retryable = "pending", now, False
    return intent.state


def views(db, settings, intake_ids, po_id=None):
    expire_claims(db, clock(db))
    filters = [Notification.intake_id.in_(intake_ids)]
    if po_id is not None:
        filters.append(Notification.po_id == po_id)
    rows = db.scalars(select(Notification).where(or_(*filters)).order_by(Notification.id)).all()
    views = []
    for intent in rows:
        attempts = db.scalars(select(NotificationAttempt).where(
            NotificationAttempt.notification_id == intent.id,
        ).order_by(NotificationAttempt.number)).all()
        views.append({
            "id": intent.id, "event_key": intent.event_key, "channel": intent.channel,
            "destination_alias": intent.destination_alias, "configured": bool(intent.destination),
            "enabled": settings.outbound_enabled, "state": intent.state, "closed_at": intent.closed_at,
            "available_at": intent.available_at,
            "automatic_retry": intent.state == "failed" and intent.retryable
            and intent.attempt_count < settings.notification_max_attempts and intent.closed_at is None,
            "attempts": [{
                "number": attempt.number, "started_at": attempt.started_at, "finished_at": attempt.finished_at,
                "outcome": attempt.outcome, "result": attempt.result,
                "reconciliation": attempt.reconciliation, "retry_authorization": attempt.retry_authorization,
            } for attempt in attempts],
        })
    return views
