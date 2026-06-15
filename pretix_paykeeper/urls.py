from django.urls import re_path

from . import views

event_patterns = [
    re_path(
        r'^paykeeper/callback/(?P<order>[^/]+)/(?P<secret>[^/]+)/$',
        views.PaykeeperCallbackView.as_view(),
        name='callback',
    ),
]
