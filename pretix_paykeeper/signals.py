import json
import logging
from zoneinfo import ZoneInfo

from django.dispatch import receiver
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment
from pretix.base.signals import periodic_task, register_payment_providers
from pretix.base.services.mail import mail
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
    failures = []

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
                error_msg = str(e)
                logger.error(
                    'Paykeeper: failed to send final receipt for order %s (payment %d): %s',
                    order.code, payment.pk, error_msg,
                )
                failures.append({
                    'order': order.code,
                    'payment_pk': payment.pk,
                    'error': error_msg,
                    'event': order.event,
                })

    if failures:
        _send_failure_summary(failures)


def _send_failure_summary(failures):
    event = failures[0]['event']

    admins = event.get_users_with_permission('can_view_orders')
    if not admins:
        return

    emails = [u.email for u in admins if u.email]
    if not emails:
        return

    lines = []
    for f in failures:
        lines.append('- {} (payment {}): {}'.format(f['order'], f['payment_pk'], f['error']))

    mail(
        emails,
        'Paykeeper: {} final receipt(s) failed today'.format(len(failures)),
        'pretix_paykeeper/email/failure_summary.txt',
        context={
            'event': event.name,
            'count': len(failures),
            'failures': '\n'.join(lines),
        },
        event=event,
        auto_email=True,
    )


@receiver(periodic_task, dispatch_uid="paykeeper_check_pending_payments")
@scopes_disabled()
@minimum_interval(minutes_after_success=5, minutes_after_error=2)
def check_pending_payments(sender, **kwargs):
    from .payment import PaykeeperPaymentProvider

    pending_payments = OrderPayment.objects.filter(
        provider='paykeeper',
        state=OrderPayment.PAYMENT_STATE_PENDING,
    ).select_related('order', 'order__event')

    for payment in pending_payments:
        if not payment.info:
            continue

        try:
            info = json.loads(payment.info)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

        invoice_id = info.get('invoice_id')
        if not invoice_id:
            continue

        prov = PaykeeperPaymentProvider(payment.order.event)

        try:
            status_data = prov._check_invoice_status(invoice_id)
        except Exception as e:
            logger.error(
                'Paykeeper periodic: failed to check invoice %s for order %s: %s',
                invoice_id, payment.order.code, str(e),
            )
            continue

        if isinstance(status_data, list) and len(status_data) > 0:
            status = status_data[0].get('status', '')
        elif isinstance(status_data, dict):
            status = status_data.get('status', '')
        else:
            continue

        if status == 'paid':
            if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                continue
            try:
                payment.confirm()
                logger.info(
                    'Paykeeper periodic: payment %d confirmed for order %s',
                    payment.pk, payment.order.code,
                )
            except Exception as e:
                logger.error(
                    'Paykeeper periodic: failed to confirm payment %d: %s',
                    payment.pk, str(e),
                )
        elif status in ('expired', 'rejected'):
            if payment.state in (
                OrderPayment.PAYMENT_STATE_CONFIRMED,
                OrderPayment.PAYMENT_STATE_FAILED,
            ):
                continue
            try:
                payment.fail(info={'error': {'status': status}})
                logger.info(
                    'Paykeeper periodic: payment %d marked as failed (%s) for order %s',
                    payment.pk, status, payment.order.code,
                )
            except Exception as e:
                logger.error(
                    'Paykeeper periodic: failed to fail payment %d: %s',
                    payment.pk, str(e),
                )
