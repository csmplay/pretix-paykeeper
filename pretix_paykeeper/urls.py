from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r'^_paykeeper/webhook/?$',
        views.PaykeeperWebhookView.as_view(),
        name='webhook',
    ),
]

event_patterns = [
    re_path(
        r'^paykeeper/callback/(?P<order>[^/]+)/(?P<secret>[^/]+)/$',
        views.PaykeeperCallbackView.as_view(),
        name='callback',
    ),
    re_path(
        r'^paykeeper/manual-final-receipt/(?P<order>[^/]+)/(?P<payment_pk>\d+)/$',
        views.ManualFinalReceiptView.as_view(),
        name='manual-final-receipt',
    ),
]
