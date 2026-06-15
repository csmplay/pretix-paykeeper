import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from pretix.base.models import Order, OrderPayment

from .payment import PaykeeperPaymentProvider

logger = logging.getLogger('pretix_paykeeper')


@method_decorator(csrf_exempt, name='dispatch')
class PaykeeperCallbackView(View):
    def post(self, request, *args, **kwargs):
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

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            logger.warning('Paykeeper callback: invalid JSON body for order %s', order_code)
            return HttpResponseBadRequest('Invalid JSON')

        invoice_id = body.get('id') or body.get('invoice_id')

        if not invoice_id:
            logger.warning('Paykeeper callback: missing invoice_id for order %s', order_code)
            return HttpResponseBadRequest('Missing invoice_id')

        payment = order.payments.filter(
            provider='paykeeper',
            info__contains=invoice_id,
        ).last()

        if not payment:
            logger.warning('Paykeeper callback: payment not found for invoice %s', invoice_id)
            return HttpResponse('OK')

        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            return HttpResponse('OK')

        prov = PaykeeperPaymentProvider(order.event)
        invoice = prov._check_invoice_status(invoice_id)
        if not invoice or not isinstance(invoice, list) or len(invoice) == 0:
            return HttpResponse('OK')

        invoice = invoice[0]
        status = invoice.get('status', body.get('status'))

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

        return HttpResponse('OK')

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)
