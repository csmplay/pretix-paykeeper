import hashlib
import hmac
import json
import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs

from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment
from pretix.multidomain.urlreverse import build_absolute_uri

from .payment import PaykeeperPaymentProvider

logger = logging.getLogger('pretix_paykeeper')


def _verify_webhook_key(body, event):
    key = body.get('key')
    if not key:
        return False

    secret_word = event.settings.get('payment_paykeeper_secret_word', '') or ''
    if not secret_word:
        return False

    id_val = body.get('id', '')
    sum_val = body.get('sum', '')
    clientid = body.get('clientid', '')
    orderid = body.get('orderid', '')

    params = id_val + sum_val + clientid + orderid
    expected = hashlib.md5((params + secret_word).encode('utf-8')).hexdigest()

    return hmac.compare_digest(key, expected)


def _find_payment_global(identifier, orderid=None):
    str_id = str(identifier)

    if orderid:
        parts = orderid.rsplit('-', 1)
        if len(parts) == 2:
            try:
                payment_pk = int(parts[1])
            except ValueError:
                pass
            else:
                with scopes_disabled():
                    try:
                        p = OrderPayment.objects.select_related(
                            'order', 'order__event'
                        ).get(pk=payment_pk, provider='paykeeper')
                        info = json.loads(p.info) if p.info else {}
                        if str(info.get('invoice_id')) == str_id or str(info.get('payment_id')) == str_id:
                            return p
                    except (OrderPayment.DoesNotExist, ValueError):
                        pass

    with scopes_disabled():
        candidates = OrderPayment.objects.filter(
            provider='paykeeper',
            info__contains=str_id,
        ).select_related('order', 'order__event').order_by('-pk')

        for p in candidates:
            if not p.info:
                continue
            try:
                info = json.loads(p.info)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if str(info.get('invoice_id')) == str_id:
                return p
            if str(info.get('payment_id')) == str_id:
                return p
        return None


def _extract_status(api_response):
    if isinstance(api_response, list) and len(api_response) > 0:
        return api_response[0].get('status')
    if isinstance(api_response, dict):
        return api_response.get('status')
    return None


@scopes_disabled()
def _process_payment(order, payment, callback_status=None):
    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
        return

    prov = PaykeeperPaymentProvider(order.event)
    info = json.loads(payment.info) if payment.info else {}
    invoice_id = info.get('invoice_id')

    if not invoice_id:
        logger.warning('Paykeeper callback: no invoice_id in payment info for order %s', order.code)
        return

    try:
        invoice = prov._check_invoice_status(invoice_id)
    except Exception as e:
        logger.error('Paykeeper callback: failed to check invoice status for %s: %s', order.code, str(e))
        return

    status = _extract_status(invoice) or callback_status

    if status == 'paid':
        try:
            payment.confirm()
            logger.info('Paykeeper callback: payment %d confirmed for order %s', payment.pk, order.code)
        except Exception as e:
            logger.error('Paykeeper callback: failed to confirm payment %d: %s', payment.pk, str(e))
    elif status in ('expired', 'rejected'):
        try:
            payment.fail(info={'error': {'status': status}})
            logger.info('Paykeeper callback: payment %d marked as failed (%s) for order %s', payment.pk, status, order.code)
        except Exception as e:
            logger.error('Paykeeper callback: failed to fail payment %d: %s', payment.pk, str(e))
    else:
        logger.info('Paykeeper callback: unhandled status "%s" for invoice %s', status, invoice_id)


def _order_redirect(order):
    url = build_absolute_uri(order.event, 'presale:event.order', kwargs={
        'order': order.code,
        'secret': order.secret,
    })
    if order.status == Order.STATUS_PAID:
        url += '?paid=yes'
    return redirect(url)


@method_decorator(csrf_exempt, name='dispatch')
class PaykeeperCallbackView(View):
    def post(self, request, *args, **kwargs):
        return HttpResponse('OK')

    def get(self, request, *args, **kwargs):
        order_code = kwargs.get('order')
        order_secret = kwargs.get('secret')

        try:
            order = Order.objects.get(
                code=order_code,
                secret=order_secret,
            )
        except Order.DoesNotExist:
            logger.warning('Paykeeper callback: order %s not found', order_code)
            return HttpResponseBadRequest('Order not found')

        return _order_redirect(order)


@method_decorator(csrf_exempt, name='dispatch')
class PaykeeperWebhookView(View):
    def post(self, request, *args, **kwargs):
        content_type = request.content_type or ''
        if 'json' in content_type:
            try:
                body = json.loads(request.body)
            except (json.JSONDecodeError, ValueError):
                logger.warning('Paykeeper webhook: invalid JSON body')
                return HttpResponseBadRequest('Invalid JSON')
        else:
            raw = parse_qs(request.body.decode(errors='replace'))
            body = {k: v[0] if len(v) == 1 else v for k, v in raw.items()}

        identifier = body.get('invoice_id') or body.get('payment_id') or body.get('id')
        callback_status = body.get('status')

        if not identifier:
            logger.warning('Paykeeper webhook: missing identifier')
            return HttpResponse('OK')

        payment = _find_payment_global(identifier, orderid=body.get('orderid'))

        if not payment:
            logger.warning('Paykeeper webhook: payment not found for identifier %s', identifier)
            return HttpResponse('OK')

        if not _verify_webhook_key(body, payment.order.event):
            logger.warning(
                'Paykeeper webhook: invalid key for payment %d identifier=%s',
                payment.pk, identifier,
            )
            return HttpResponse('OK')

        webhook_sum = body.get('sum', '')
        try:
            webhook_amount = Decimal(webhook_sum)
        except InvalidOperation:
            logger.warning(
                'Paykeeper webhook: invalid sum for payment %d: %s',
                payment.pk, webhook_sum,
            )
            return HttpResponse('OK')
        if webhook_amount != payment.amount:
            logger.warning(
                'Paykeeper webhook: sum mismatch for payment %d: got %s, expected %s',
                payment.pk, webhook_sum, payment.amount,
            )
            return HttpResponse('OK')

        webhook_clientid = body.get('clientid', '')
        prov = PaykeeperPaymentProvider(payment.order.event)
        expected_clientid = ''
        if payment.order.invoice_address:
            expected_clientid = prov._get_name_for_invoice(payment.order.invoice_address)
        if not expected_clientid:
            expected_clientid = payment.order.email or ''
        if webhook_clientid and expected_clientid and webhook_clientid != expected_clientid:
            logger.warning(
                'Paykeeper webhook: clientid mismatch for payment %d: got %s, expected %s',
                payment.pk, webhook_clientid, expected_clientid,
            )
            return HttpResponse('OK')

        webhook_payment_id = body.get('id')
        if webhook_payment_id:
            info = json.loads(payment.info) if payment.info else {}
            if not info.get('payment_id'):
                info['payment_id'] = str(webhook_payment_id)
                payment.info = json.dumps(info)
                payment.save(update_fields=['info'])

        _process_payment(payment.order, payment, callback_status)
        return HttpResponse('OK')


@method_decorator(csrf_exempt, name='dispatch')
class ManualFinalReceiptView(View):
    def post(self, request, *args, **kwargs):
        order_code = kwargs.get('order')
        payment_pk = kwargs.get('payment_pk')

        with scopes_disabled():
            try:
                order = Order.objects.get(
                    code=order_code,
                    event=request.event,
                )
            except Order.DoesNotExist:
                return HttpResponseBadRequest('Order not found')

            try:
                payment = order.payments.get(
                    pk=payment_pk,
                    provider='paykeeper',
                )
            except OrderPayment.DoesNotExist:
                return HttpResponseBadRequest('Payment not found')

        if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
            return HttpResponseBadRequest('Payment is not confirmed')

        if not payment.info:
            return HttpResponseBadRequest('Payment has no info')

        try:
            info = json.loads(payment.info)
        except (json.JSONDecodeError, KeyError, TypeError):
            return HttpResponseBadRequest('Invalid payment info')

        if info.get('final_receipt_sent'):
            return HttpResponse('Final receipt already sent', status=200)

        if not info.get('invoice_id'):
            return HttpResponseBadRequest('No invoice_id in payment info')

        prov = PaykeeperPaymentProvider(order.event)
        try:
            success = prov._create_final_receipt(order, payment)
            if success:
                info['final_receipt_sent'] = True
                payment.info = json.dumps(info)
                payment.save(update_fields=['info'])
                logger.info(
                    'Paykeeper: manual final receipt sent for order %s (payment %d)',
                    order.code, payment.pk,
                )
                return HttpResponse('Final receipt sent successfully')
            else:
                return HttpResponseBadRequest('Failed to create final receipt')
        except Exception as e:
            error_msg = str(e)
            logger.error(
                'Paykeeper: failed to send manual final receipt for order %s (payment %d): %s',
                order.code, payment.pk, error_msg,
            )
            return HttpResponseBadRequest(f'Error: {error_msg}')

    def get(self, request, *args, **kwargs):
        return HttpResponseBadRequest('Method not allowed')


@method_decorator(csrf_exempt, name='dispatch')
class ManualPaymentIdView(View):
    def post(self, request, *args, **kwargs):
        order_code = kwargs.get('order')
        payment_pk = kwargs.get('payment_pk')

        with scopes_disabled():
            try:
                order = Order.objects.get(
                    code=order_code,
                    event=request.event,
                )
            except Order.DoesNotExist:
                return HttpResponseBadRequest('Order not found')

            try:
                payment = order.payments.get(
                    pk=payment_pk,
                    provider='paykeeper',
                )
            except OrderPayment.DoesNotExist:
                return HttpResponseBadRequest('Payment not found')

        payment_id = request.POST.get('payment_id', '').strip()
        if not payment_id:
            return HttpResponseBadRequest('payment_id is required')

        info = json.loads(payment.info) if payment.info else {}
        info['payment_id'] = payment_id
        payment.info = json.dumps(info)
        payment.save(update_fields=['info'])

        logger.info(
            'Paykeeper: manual payment_id set to %s for order %s (payment %d)',
            payment_id, order.code, payment.pk,
        )
        return HttpResponse('payment_id saved')

    def get(self, request, *args, **kwargs):
        return HttpResponseBadRequest('Method not allowed')
