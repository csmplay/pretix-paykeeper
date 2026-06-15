from django.utils.translation import gettext_lazy

from . import __version__, __author__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 4.x or above to run this plugin!")


class PretixPluginMeta:
    name = "Paykeeper"
    author = __author__
    version = __version__
    description = gettext_lazy("Paykeeper payment provider plugin for pretix")
    visible = True
    category = "PAYMENT"
    compatibility = "pretix>=4.0.0"


class PluginApp(PluginConfig):
    default = True
    name = "pretix_paykeeper"
    verbose_name = "Paykeeper"
    PretixPluginMeta = PretixPluginMeta

    def ready(self):
        from . import signals  # noqa: F401
