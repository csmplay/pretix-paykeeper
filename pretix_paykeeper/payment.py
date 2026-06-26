import base64
import json
import logging
from collections import OrderedDict
from decimal import Decimal
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests as http_requests
from django import forms
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _
from pretix.base.models import OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri

logger = logging.getLogger('pretix_paykeeper')

TAX_MAP = {
    None: 'none',
    '0': 'vat0',
    '5': 'vat5',
    '7': 'vat7',
    '10': 'vat10',
    '20': 'vat20',
    '22': 'vat22',
    '105': 'vat105',
    '107': 'vat107',
    '110': 'vat110',
    '120': 'vat120',
    '122': 'vat122',
}

TAX_FREE_CODES = {'O', 'E', 'Z', 'G', 'K'}

ITEM_TYPES = {
    'goods', 'service', 'work', 'excise', 'ip', 'payment',
    'agent', 'property_right', 'non_operating', 'sales_tax',
    'resort_fee', 'other',
}


class PaykeeperSettingsForm(forms.Form):
    server_url = forms.URLField(
        label=_('Paykeeper Server URL'),
        help_text=_('Full URL of your Paykeeper server, e.g.') + ' https://example.server.paykeeper.ru',
        required=True,
    )
    api_user = forms.CharField(
        label=_('Username'),
        help_text=_('Username from your Paykeeper cabinet'),
        required=True,
    )
    api_password = forms.CharField(
        label=_('Password'),
        help_text=_('Password from your Paykeeper cabinet'),
        required=True,
        widget=forms.PasswordInput(render_value=True),
    )
    service_name = forms.CharField(
        label=_('Service Name'),
        help_text=_('Displayed as the service name on Paykeeper invoices'),
        required=False,
        initial=_('Event tickets'),
        max_length=255,
    )
    send_receipt = forms.BooleanField(
        label=_('Send 54-FZ receipt'),
        help_text=_('Send fiscal receipt data to the tax service'),
        required=False,
        initial=False,
    )
    final_receipt_enabled = forms.BooleanField(
        label=_('Automatic final receipt (54-FZ)'),
        help_text=_('Automatically issue a final settlement receipt on the day of the event'),
        required=False,
        initial=False,
    )
    secret_word = forms.CharField(
        label=_('Secret word'),
        help_text=_('Secret word from Paykeeper cabinet, used to verify webhook signatures (MD5)'),
        required=False,
        max_length=255,
    )


class PaykeeperPaymentProvider(BasePaymentProvider):
    identifier = 'paykeeper'
    verbose_name = 'Paykeeper'

    @property
    def settings_form_fields(self):
        return OrderedDict([
            ('_enabled', forms.BooleanField(
                label=_('Enable Paykeeper'),
                required=False,
            )),
            ('server_url', PaykeeperSettingsForm.base_fields['server_url']),
            ('api_user', PaykeeperSettingsForm.base_fields['api_user']),
            ('api_password', PaykeeperSettingsForm.base_fields['api_password']),
            ('service_name', PaykeeperSettingsForm.base_fields['service_name']),
            ('send_receipt', PaykeeperSettingsForm.base_fields['send_receipt']),
            ('final_receipt_enabled', PaykeeperSettingsForm.base_fields['final_receipt_enabled']),
            ('secret_word', PaykeeperSettingsForm.base_fields['secret_word']),
        ])

    @property
    def test_mode_message(self):
        return None

    def _get_base_url(self):
        url = self.settings.get('server_url')
        return url.rstrip('/') if url else ''

    def _get_auth(self):
        user = self.settings.get('api_user') or ''
        password = self.settings.get('api_password') or ''
        return user, password

    def _get_service_name(self):
        return self.settings.get('service_name') or _('Event tickets')

    def _get_basic_auth_header(self):
        user, password = self._get_auth()
        encoded = base64.b64encode(f'{user}:{password}'.encode()).decode()
        return {'Authorization': f'Basic {encoded}'}

    def _get_token(self):
        url = urljoin(self._get_base_url() + '/', '/info/settings/token/')
        headers = self._get_basic_auth_header()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'

        try:
            resp = http_requests.get(url, headers=headers, timeout=30, verify=True)
            resp.raise_for_status()
            data = resp.json()
            token = data.get('token')
            if not token:
                raise PaymentException(_('Could not obtain security token from Paykeeper.'))
            return token
        except http_requests.exceptions.ConnectionError:
            logger.error('Paykeeper: connection error for token endpoint %s', url)
            raise PaymentException(_('Could not connect to Paykeeper server.'))
        except http_requests.exceptions.HTTPError as e:
            logger.error('Paykeeper: token error %s', e.response.status_code)
            raise PaymentException(_('Paykeeper authentication failed. Check username and password.'))
        except (ValueError, KeyError):
            raise PaymentException(_('Invalid response from Paykeeper server.'))

    def _api_post(self, endpoint, data):
        url = urljoin(self._get_base_url() + '/', endpoint.lstrip('/'))
        headers = self._get_basic_auth_header()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'

        try:
            resp = http_requests.post(url, data=data, headers=headers, timeout=30, verify=True)
            resp.raise_for_status()
            return resp.json()
        except http_requests.exceptions.ConnectionError:
            raise PaymentException(_('Could not connect to Paykeeper server.'))
        except http_requests.exceptions.Timeout:
            raise PaymentException(_('Paykeeper server timed out.'))
        except http_requests.exceptions.HTTPError as e:
            logger.error('Paykeeper: HTTP error %s for %s', e.response.status_code, url)
            raise PaymentException(
                _('Paykeeper returned an error (HTTP {code}).').format(code=e.response.status_code)
            )
        except ValueError:
            raise PaymentException(_('Invalid response from Paykeeper server.'))

    def _api_get(self, endpoint, params=None):
        url = urljoin(self._get_base_url() + '/', endpoint.lstrip('/'))
        headers = self._get_basic_auth_header()

        try:
            resp = http_requests.get(url, params=params, headers=headers, timeout=30, verify=True)
            resp.raise_for_status()
            return resp.json()
        except http_requests.exceptions.ConnectionError:
            raise PaymentException(_('Could not connect to Paykeeper server.'))
        except http_requests.exceptions.HTTPError as e:
            logger.error('Paykeeper: HTTP error %s for %s', e.response.status_code, url)
            raise PaymentException(
                _('Paykeeper returned an error (HTTP {code}).').format(code=e.response.status_code)
            )
        except ValueError:
            raise PaymentException(_('Invalid response from Paykeeper server.'))

    def _get_name_for_invoice(self, invoice_address):
        if not invoice_address:
            return ''

        name_parts = invoice_address.name_parts
        if name_parts:
            family_name = name_parts.get('family_name', '')
            given_name = name_parts.get('given_name', '')
            middle_name = name_parts.get('middle_name', '')

            parts = [p for p in [family_name, given_name, middle_name] if p]
            if parts:
                return ' '.join(parts)

        return invoice_address.name or ''

    def _get_tax_rate(self, position):
        if not hasattr(position, 'tax_rate') or position.tax_rate is None:
            return None

        tax_code = getattr(position, 'tax_code', None)
        if not tax_code:
            tax_rule = getattr(position, 'tax_rule', None)
            if tax_rule:
                tax_code = getattr(tax_rule, 'code', None)
        if tax_code:
            code_prefix = tax_code.split('/')[0] if '/' in tax_code else tax_code
            if code_prefix in TAX_FREE_CODES:
                return None

        rate = position.tax_rate
        if rate == Decimal('0'):
            return '0'
        if rate == Decimal('5'):
            return '5'
        if rate == Decimal('7'):
            return '7'
        if rate == Decimal('10'):
            return '10'
        if rate == Decimal('18'):
            return '18'
        if rate == Decimal('20'):
            return '20'
        return None

    def _build_cart(self, order, payment):
        if not self.settings.get('send_receipt', as_type=bool):
            return None

        cart_items = []
        for pos in order.positions.all():
            item_name = str(pos.item.name)
            if pos.variation:
                item_name = f'{item_name} ({pos.variation})'
            unit_price = float(pos.price)
            total = float(pos.price)
            tax_rate = self._get_tax_rate(pos)
            paykeeper_tax = TAX_MAP.get(tax_rate, 'none')

            raw_type = pos.item.meta_data.get('item_type', '') or ''
            item_type = raw_type if raw_type in ITEM_TYPES else 'service'

            cart_items.append({
                'name': item_name,
                'item_type': item_type,
                'payment_type': 'prepay',
                'price': '{:.2f}'.format(unit_price),
                'quantity': 1,
                'sum': '{:.2f}'.format(total),
                'tax': paykeeper_tax,
            })

        if not cart_items:
            total_amount = float(payment.amount)
            cart_items.append({
                'name': self._get_service_name(),
                'payment_type': 'prepay',
                'price': '{:.2f}'.format(total_amount),
                'quantity': 1,
                'sum': '{:.2f}'.format(total_amount),
                'tax': 'none',
            })

        return json.dumps(cart_items, ensure_ascii=False)

    def _create_invoice(self, order, payment):
        token = self._get_token()
        expiry_date = order.expires.astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')
        order_id = '{}-{}-{}'.format(order.event.slug, order.code, payment.pk)

        callback_url = build_absolute_uri(
            order.event, 'plugins:pretix_paykeeper:callback',
            kwargs={'order': order.code, 'secret': order.secret}
        )

        service_name_value = self._get_service_name()
        cart = self._build_cart(order, payment)
        if cart:
            service_data = {
                'cart': cart,
                'service_name': service_name_value,
                'user_result_callback': callback_url,
            }
            service_name_value = json.dumps(service_data, ensure_ascii=False)

        client_name = ''
        client_phone = ''
        if order.invoice_address:
            client_name = self._get_name_for_invoice(order.invoice_address)
        if not client_name:
            client_name = order.email or ''

        data = {
            'pay_amount': '{:.2f}'.format(payment.amount),
            'clientid': client_name,
            'orderid': order_id,
            'service_name': service_name_value,
            'client_email': order.email or '',
            'client_phone': client_phone,
            'expiry': expiry_date,
            'token': token,
            'user_result_callback': callback_url,
        }

        result = self._api_post('/change/invoice/preview/', data=data)
        return result

    def _check_invoice_status(self, invoice_id):
        return self._api_get('/info/invoice/byid/', params={'id': invoice_id})

    def _revoke_invoice(self, invoice_id):
        token = self._get_token()
        data = {'id': invoice_id, 'token': token}
        return self._api_post('/change/invoice/revoke/', data=data)

    def _get_payment_id(self, invoice_id):
        try:
            status_data = self._check_invoice_status(invoice_id)
            if isinstance(status_data, list) and len(status_data) > 0:
                return status_data[0].get('payment_id')
            elif isinstance(status_data, dict):
                return status_data.get('payment_id')
        except Exception as e:
            logger.error('Paykeeper: failed to get payment_id for invoice %s: %s', invoice_id, str(e))
        return None

    def _build_final_receipt_cart(self, order, payment):
        cart_items = []
        for pos in order.positions.all():
            item_name = str(pos.item.name)
            if pos.variation:
                item_name = f'{item_name} ({pos.variation})'
            unit_price = float(pos.price)
            total = float(pos.price)
            tax_rate = self._get_tax_rate(pos)
            paykeeper_tax = TAX_MAP.get(tax_rate, 'none')

            raw_type = pos.item.meta_data.get('item_type', '') or ''
            item_type = raw_type if raw_type in ITEM_TYPES else 'service'

            cart_items.append({
                'name': item_name,
                'item_type': item_type,
                'payment_type': 'full',
                'price': '{:.2f}'.format(unit_price),
                'quantity': 1,
                'sum': '{:.2f}'.format(total),
                'tax': paykeeper_tax,
            })

        if not cart_items:
            total_amount = float(payment.amount)
            cart_items.append({
                'name': self._get_service_name(),
                'payment_type': 'full',
                'price': '{:.2f}'.format(total_amount),
                'quantity': 1,
                'sum': '{:.2f}'.format(total_amount),
                'tax': 'none',
            })

        return json.dumps(cart_items, ensure_ascii=False)

    def _create_final_receipt(self, order, payment):
        info = json.loads(payment.info) if payment.info else {}
        invoice_id = info.get('invoice_id')
        if not invoice_id:
            logger.error('Paykeeper: no invoice_id for payment %d, cannot create final receipt', payment.pk)
            return False

        payment_id = info.get('payment_id')
        if not payment_id:
            payment_id = self._get_payment_id(invoice_id)
        if not payment_id:
            logger.error('Paykeeper: no payment_id for invoice %s, cannot create final receipt', invoice_id)
            return False

        token = self._get_token()
        cart = self._build_final_receipt_cart(order, payment)

        contact = ''
        if order.invoice_address:
            contact = self._get_name_for_invoice(order.invoice_address)
        if not contact:
            contact = order.email or ''

        receipt_key = f'{order.code}-final-{payment.pk}'
        lock_key = f'pretix_paykeeper_receipt_lock_{receipt_key}'

        if not cache.add(lock_key, 'creating', timeout=300):
            logger.warning(
                'Paykeeper: final receipt for %s already being created, skipping',
                order.code,
            )
            return False

        data = {
            'payment_id': str(payment_id),
            'is_post_sale': 'true',
            'type': 'sale',
            'contact': contact,
            'cart': cart,
            'receipt_key': receipt_key,
            'token': token,
        }

        try:
            result = self._api_post('/change/receipt/print/', data=data)
            receipt_id = result.get('receipt_id')
            if receipt_id:
                logger.info(
                    'Paykeeper: final receipt %s created for order %s (payment %d)',
                    receipt_id, order.code, payment.pk,
                )
                return True
            else:
                logger.error('Paykeeper: no receipt_id in response for %s: %s', order.code, result)
                return False
        except PaymentException as e:
            logger.error('Paykeeper: failed to create final receipt for %s: %s', order.code, str(e))
            return False
        finally:
            cache.delete(lock_key)

    def payment_form_fields(self):
        return OrderedDict()

    def payment_form_render(self, request, total, order=None):
        return ''

    def payment_is_valid_session(self, request):
        return True

    def checkout_prepare(self, request, cart):
        request.session['payment_paykeeper_initialized'] = True
        return True

    def checkout_confirm_render(self, request, order=None, info_data=None):
        return ''

    def execute_payment(self, request, payment):
        try:
            invoice_data = self._create_invoice(payment.order, payment)
        except PaymentException:
            raise
        except Exception as e:
            logger.error('Paykeeper: failed to create invoice for %s: %s', payment.order.code, str(e))
            raise PaymentException(_('Could not create Paykeeper invoice.'))

        invoice_id = invoice_data.get('invoice_id')
        invoice_url = invoice_data.get('invoice_url')

        if not invoice_id or not invoice_url:
            logger.error('Paykeeper: incomplete response for %s: %s', payment.order.code, invoice_data)
            raise PaymentException(_('Paykeeper returned an incomplete response.'))

        payment.info = json.dumps({
            'invoice_id': invoice_id,
            'invoice_url': invoice_url,
            'payment_id': invoice_data.get('payment_id'),
        })
        payment.save(update_fields=['info'])

        return invoice_url

    def payment_prepare(self, request, payment):
        if payment.info:
            try:
                info = json.loads(payment.info)
                existing_id = info.get('invoice_id')
                existing_url = info.get('invoice_url')
                if existing_id and existing_url:
                    try:
                        status_data = self._check_invoice_status(existing_id)
                        if isinstance(status_data, list) and len(status_data) > 0:
                            status = status_data[0].get('status', '')
                        elif isinstance(status_data, dict):
                            status = status_data.get('status', '')
                        else:
                            status = ''
                        if status in ('new', 'pending', 'processing') or (not status and existing_id):
                            return True
                        if status == 'paid':
                            logger.info(
                                'Paykeeper: invoice %s for %s already paid',
                                existing_id, payment.order.code,
                            )
                            return True
                        logger.warning(
                            'Paykeeper: invoice %s for %s has status "%s", recreating',
                            existing_id, payment.order.code, status,
                        )
                    except Exception as e:
                        logger.error(
                            'Paykeeper: failed to check invoice %s status for %s: %s',
                            existing_id, payment.order.code, str(e),
                        )
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        try:
            invoice_data = self._create_invoice(payment.order, payment)
        except Exception as e:
            logger.error('Paykeeper: failed to re-create invoice for %s: %s', payment.order.code, str(e))
            return False

        invoice_id = invoice_data.get('invoice_id')
        invoice_url = invoice_data.get('invoice_url')

        if not invoice_id or not invoice_url:
            logger.error('Paykeeper: incomplete response on recreate for %s: %s', payment.order.code, invoice_data)
            return False

        payment.info = json.dumps({
            'invoice_id': invoice_id,
            'invoice_url': invoice_url,
            'payment_id': invoice_data.get('payment_id'),
        })
        payment.save(update_fields=['info'])

        return True

    def payment_control_render(self, request, payment):
        invoice_id = None
        invoice_url = None
        final_receipt_sent = False
        if payment.info:
            try:
                info = json.loads(payment.info)
                invoice_id = info.get('invoice_id')
                invoice_url = info.get('invoice_url')
                final_receipt_sent = info.get('final_receipt_sent', False)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        html = (
            '<dl>'
            '<dt>{label_id}</dt><dd>{val_id}</dd>'
            '<dt>{label_url}</dt><dd><a href="{val_url}" target="_blank">{val_url}</a></dd>'
            '</dl>'
        ).format(
            label_id=_('Invoice ID'),
            val_id=invoice_id or _('N/A'),
            label_url=_('Invoice URL'),
            val_url=invoice_url or _('N/A'),
        )

        if self.settings.get('final_receipt_enabled', as_type=bool):
            if final_receipt_sent:
                html += '<p><strong>{}</strong></p>'.format(_('Final receipt sent'))
            else:
                button_url = build_absolute_uri(
                    payment.order.event,
                    'plugins:pretix_paykeeper:manual-final-receipt',
                    kwargs={'order': payment.order.code, 'payment_pk': payment.pk},
                )
                html += (
                    '<form method="post" action="{url}" style="margin-top: 10px;">'
                    '<button type="submit" class="btn btn-primary btn-sm">{label}</button>'
                    '</form>'
                ).format(
                    url=button_url,
                    label=_('Send final receipt now'),
                )

        return html

    def payment_control_render_short(self, order, payment):
        return _('Paykeeper invoice')

    def order_pending_mail_render(self, order, **kwargs):
        return _('Please complete your payment via Paykeeper.')

    def payment_pending_render(self, request, payment, **kwargs):
        return ''

    def payment_presale_render(self, payment):
        return 'Paykeeper'

    def payment_refund_supported(self, payment):
        return False

    def payment_partial_refund_supported(self, payment):
        return False

    def calculate_fee(self, price):
        return Decimal('0.00')

    def api_payment_details(self, order, payment):
        info = {}
        if payment.info:
            try:
                info = json.loads(payment.info)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return {
            'invoice_id': info.get('invoice_id'),
            'invoice_url': info.get('invoice_url'),
        }

    def shred_payment_info(self, obj):
        if obj.info:
            obj.info = '{}'
            obj.save(update_fields=['info'])

    def cancel_payment(self, payment):
        if payment.info:
            try:
                info = json.loads(payment.info)
                invoice_id = info.get('invoice_id')
                if invoice_id:
                    try:
                        self._revoke_invoice(invoice_id)
                        logger.info('Paykeeper: revoked invoice %s for %s', invoice_id, payment.order.code)
                    except PaymentException as e:
                        logger.error('Paykeeper: failed to revoke invoice %s for %s: %s', invoice_id, payment.order.code, str(e))
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            payment.info = ''
            payment.save(update_fields=['info'])
