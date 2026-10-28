import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.models import AppSettings


class Command(BaseCommand):
    help = "Create the settings row, the download root and (optionally) the first superuser from env."

    def handle(self, *args, **opts):
        AppSettings.get_solo()
        os.makedirs(settings.YTP_DOWNLOAD_ROOT, exist_ok=True)
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        if not username or not password:
            self.stdout.write("DJANGO_SUPERUSER_USERNAME/PASSWORD not set: no superuser created")
            return
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write("superuser %r already exists" % username)
            return
        User.objects.create_superuser(username=username, password=password,
                                      email=os.environ.get("DJANGO_SUPERUSER_EMAIL", ""))
        self.stdout.write("superuser %r created" % username)
