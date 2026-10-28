from django.contrib import admin

from .models import AppSettings, Download


@admin.register(Download)
class DownloadAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "owner", "status", "attempt", "max_retries", "created_at")
    list_filter = ("status",)
    search_fields = ("url", "title")


admin.site.register(AppSettings)
