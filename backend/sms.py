"""Sending SMS through Africa's Talking. Only sending: what to send, and when,
is decided in backend/alerts.py.

A thin wrapper around the official africastalking Python SDK (2.0.x):

    africastalking.initialize(username, api_key)
    africastalking.SMS.send(message, [recipients], sender_id=None, enqueue=False)

- With AT_USERNAME = "sandbox" the SDK itself sends to the sandbox API
  (https://api.sandbox.africastalking.com/version1/messaging, checked in the
  SDK source: Service._is_sandbox). Sandbox messages are not delivered to
  real phones; they appear in the Africa's Talking simulator.
- The response looks like:
      {"SMSMessageData": {"Message": "Sent to 1/1 Total Cost: KES 0.8000",
        "Recipients": [{"statusCode": 101, "number": "+2547...", "status": "Success",
                        "cost": "KES 0.8000", "messageId": "ATXid_..."}]}}
  statusCode 100 (Processed), 101 (Sent) and 102 (Queued) mean Africa's
  Talking accepted the message; anything else (e.g. 403 InvalidPhoneNumber,
  405 InsufficientBalance) means it did not.
- Whether the phone actually received it arrives later, as a delivery report
  (POST /sms/delivery-report in backend/app.py).

SMS_DRY_RUN (config, on by default) logs the message instead of sending, so
development and the tests never send a real SMS by accident.
"""
import logging
from dataclasses import dataclass

from backend.config import settings

log = logging.getLogger("sms")

ACCEPTED_STATUS_CODES = {100, 101, 102}  # Processed, Sent, Queued

# delivery_status values written at send time (the delivery report later
# replaces them with Africa's Talking's own status, e.g. "Success").
STATUS_DRY_RUN = "dry_run"
STATUS_SUBMITTED = "submitted"
STATUS_ERROR = "error"  # could not reach Africa's Talking at all


@dataclass
class SendResult:
    message_id: str | None  # Africa's Talking's id, used to match the delivery report
    status: str  # delivery_status to store
    detail: str  # what happened, for the logs


_service = None


def _sms_service():
    """The SDK's SMS service, initialised once with the keys from .env."""
    global _service
    if _service is None:
        import africastalking

        africastalking.initialize(settings.AT_USERNAME, settings.AT_API_KEY)
        _service = africastalking.SMS
    return _service


def send(phone_number: str, message: str) -> SendResult:
    """Send one SMS (or log it, in dry-run mode). Never raises: the result says what happened."""
    if settings.SMS_DRY_RUN:
        log.info("DRY RUN, not sent, to %s: %s", phone_number, message)
        return SendResult(None, STATUS_DRY_RUN, "dry run: logged, not sent")

    try:
        response = _sms_service().send(message, [phone_number], sender_id=settings.AT_SENDER_ID or None)
    except Exception as error:  # network, bad key, SDK validation: report, don't crash the caller
        log.error("SMS to %s failed: %s", phone_number, error)
        return SendResult(None, STATUS_ERROR, f"{type(error).__name__}: {error}"[:200])

    data = response.get("SMSMessageData", {}) if isinstance(response, dict) else {}
    recipients = data.get("Recipients") or []
    if not recipients:
        # Rejected outright (e.g. an invalid number): the reason is in "Message".
        reason = str(data.get("Message") or response)[:30]
        log.error("SMS to %s not accepted: %s", phone_number, response)
        return SendResult(None, reason, f"not accepted: {response}")

    recipient = recipients[0]
    if recipient.get("statusCode") in ACCEPTED_STATUS_CODES:
        log.info("SMS to %s accepted: %s", phone_number, recipient)
        return SendResult(recipient.get("messageId"), STATUS_SUBMITTED, str(recipient))
    log.error("SMS to %s rejected: %s", phone_number, recipient)
    return SendResult(recipient.get("messageId"), str(recipient.get("status", "rejected"))[:30], str(recipient))
