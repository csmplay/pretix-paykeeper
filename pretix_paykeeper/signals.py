import json
import logging
from zoneinfo import ZoneInfo

from django.dispatch import receiver
from django.utils.timezone import now
from pretix.base.models import Order, OrderPayment
from pretix.base.signals import periodic_task, register_payment_providers
from pretix.helpers.periodic import minimum_interval

logger = logging.getLogger('pretix_paykeeper')


@receiver(register_payment_providers, dispatch_uid="payment_paykeeper")
def register_payment_provider(sender, **kwargs):
    from .payment import PaykeeperPaymentProvider

    return PaykeeperPaymentProvider


@receiver(periodic_task, dispatch_uid="paykeeper_final_receipts")
@minimum_interval(minutes_after_success=30)
def issue_final_receipts(sender, **kwargs):
    from .payment import PaykeeperPaymentProvider

    today = now().date()

    orders = Order.objects.filter(
        status=Order.STATUS_PAID,
        payments__provider='paykeeper',
    ).filter(
        payments__state=OrderPayment.PAYMENT_STATE_CONFIRMED,
    ).distinct()

    for order in orders:
        event_tz = ZoneInfo(order.event.settings.timezone)
        event_date = order.event.date_from.astimezone(event_tz).date()
        if event_date != today:
            continue

        if not order.event.settings.get('payment_paykeeper_final_receipt_enabled', as_type=bool):
            continue

        prov = PaykeeperPaymentProvider(order.event)

        for payment in order.payments.filter(
            provider='paykeeper',
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        ):
            if not payment.info:
                continue

            try:
                info = json.loads(payment.info)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

            if info.get('final_receipt_sent'):
                continue

            if not info.get('invoice_id'):
                continue

            try:
                success = prov._create_final_receipt(order, payment)
                if success:
                    info['final_receipt_sent'] = True
                    payment.info = json.dumps(info)
                    payment.save(update_fields=['info'])
                    logger.info(
                        'Paykeeper: final receipt sent for order %s (payment %d)',
                        order.code, payment.pk,
                    )
            except Exception as e:
                logger.error(
                    'Paykeeper: failed to send final receipt for order %s (payment %d): %s',
                    order.code, payment.pk, str(e),
                )
