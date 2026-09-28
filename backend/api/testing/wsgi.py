from django.conf import settings
from django.contrib.staticfiles.handlers import StaticFilesHandler
from mazory_backend.wsgi import application as django_application

if not settings.INTEGRATION_TEST_MODE:
    raise RuntimeError("Test WSGI is only available in isolated test mode")
application = StaticFilesHandler(django_application)
