from urllib.parse import urlparse

from django.conf import settings


class ProviderUnavailable(Exception):
    def __init__(self, code, *, diagnostics=None):
        super().__init__(code)
        self.diagnostics = diagnostics or {}


def checked_url(url):
    parsed = urlparse(url)
    if (
        settings.INTEGRATION_TEST_MODE
        and parsed.scheme == "http"
        and parsed.hostname == "test-provider"
        and parsed.port == 9000
    ):
        return url
    if (
        parsed.scheme != "https"
        or parsed.hostname not in settings.PROVIDER_ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ProviderUnavailable("provider_not_allowed")
    return url
