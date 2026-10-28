from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.settings_page, name="settings"),
    path("login/", auth_views.LoginView.as_view(template_name="core/login.html", redirect_authenticated_user=True),
         name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/snapshot/", views.snapshot, name="api-snapshot"),
    path("api/downloads/", views.add, name="api-add"),
    path("api/downloads/<int:pk>/file/", views.file, name="api-file"),
    path("api/downloads/<int:pk>/<str:name>/", views.action, name="api-action"),
    path("api/purge/", views.purge, name="api-purge"),
]
