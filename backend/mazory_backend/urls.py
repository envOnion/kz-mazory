from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from api.waha_admin_views import waha_dashboard_view, waha_action_view

# Register custom WAHA admin views inside admin.site namespace
_orig_admin_get_urls = admin.site.get_urls
def _custom_admin_get_urls():
    custom_urls = [
        path('waha-dashboard/', waha_dashboard_view, name='waha_dashboard'),
        path('waha-action/<str:action>/', waha_action_view, name='waha_action'),
    ]
    return custom_urls + _orig_admin_get_urls()

admin.site.get_urls = _custom_admin_get_urls

urlpatterns = [
    # Custom WAHA Control Center inside Django Admin
    path('admin/waha-dashboard/', waha_dashboard_view, name='waha_dashboard'),
    path('admin/waha-action/<str:action>/', waha_action_view, name='waha_action'),
    
    # Standard Admin
    path('admin/', admin.site.urls),
    
    # API endpoints
    path('api/', include('api.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
