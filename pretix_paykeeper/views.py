import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from pretix.base.models import Order, OrderPayment
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.multidomain.urlreverse import build_absolute_uri

from .payment import PaykeeperPaymentProvider

logger = logging.getLogger('pretix_paykeeper')


def _find_payment_global(identifier):
    str_id = str(identifier)
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
        if str(info.get('payment_id')) == str_id:
            return p
        if str(info.get('invoice_id')) == str_id:
            return p
    return None


def _extract_status(api_response):
    if isinstance(api_response, list) and len(api_response) > 0:
        return api_response[0].get('status')
    if isinstance(api_response, dict):
        return api_response.get('status')
    return None


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
        order_code = kwargs.get('order')

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            logger.warning('Paykeeper callback: invalid JSON body for order %s', order_code)
            return HttpResponseBadRequest('Invalid JSON')

        identifier = body.get('id') or body.get('invoice_id') or body.get('payment_id')
        callback_status = body.get('status')

        logger.info(
            'Paykeeper callback (ignored, use webhook): order=%s identifier=%s status=%s',
            order_code, identifier, callback_status,
        )
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
        logger.warning('Paykeeper webhook raw body: %s', request.body.decode(errors='replace'))

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            logger.warning('Paykeeper webhook: invalid JSON body')
            return HttpResponseBadRequest('Invalid JSON')

        identifier = body.get('id') or body.get('invoice_id') or body.get('payment_id')
        callback_status = body.get('status')

        logger.warning(
            'Paykeeper webhook: identifier=%s status=%s body=%s',
            identifier, callback_status, body,
        )

        if not identifier:
            logger.warning('Paykeeper webhook: missing identifier')
            return HttpResponse('OK')

        payment = _find_payment_global(identifier)

        if not payment:
            logger.warning('Paykeeper webhook: payment not found for identifier %s', identifier)
            return HttpResponse('OK')

        _process_payment(payment.order, payment, callback_status)
        return HttpResponse('OK')


class ManualFinalReceiptView(EventPermissionRequiredMixin, View):
    permission = 'can_view_orders'

    def post(self, request, *args, **kwargs):
        order_code = kwargs.get('order')
        payment_pk = kwargs.get('payment_pk')

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
