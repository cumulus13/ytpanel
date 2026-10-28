import os

from django import forms

from .models import AppSettings
from .services import formats


class AppSettingsForm(forms.ModelForm):
    class Meta:
        model = AppSettings
        fields = [
            "max_simultaneous", "per_user_queue_cap", "backend", "js_runtime", "default_format",
            "default_ratelimit_kib", "socket_timeout", "cookies_file",
            "default_retries", "retry_delay_base", "retry_delay_max", "retry_permanent",
            "allowed_dirs", "ssrf_guard", "retention_days", "retention_delete_files",
        ]
        widgets = {"allowed_dirs": forms.Textarea(attrs={"rows": 3})}

    def clean_max_simultaneous(self):
        v = self.cleaned_data["max_simultaneous"]
        if not 1 <= v <= 50:
            raise forms.ValidationError("must be between 1 and 50")
        return v

    def clean_socket_timeout(self):
        v = self.cleaned_data["socket_timeout"]
        if not 5 <= v <= 300:
            raise forms.ValidationError("must be between 5 and 300 seconds")
        return v

    def clean_default_retries(self):
        v = self.cleaned_data["default_retries"]
        if v < -1 or v > 1_000_000:
            raise forms.ValidationError("use -1 for infinite, or 0..1000000")
        return v

    def clean_retry_delay_base(self):
        v = self.cleaned_data["retry_delay_base"]
        if v < 1:
            raise forms.ValidationError("must be at least 1 second")
        return v

    def clean_default_format(self):
        try:
            return formats.validate_format(self.cleaned_data["default_format"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean_allowed_dirs(self):
        lines = [x.strip() for x in self.cleaned_data["allowed_dirs"].splitlines() if x.strip()]
        for line in lines:
            if not os.path.isabs(line) or "\0" in line:
                raise forms.ValidationError("not an absolute path: %s" % line)
            if os.path.realpath(line) in ("/", os.path.realpath(os.sep)):
                raise forms.ValidationError("refusing the filesystem root")
        return "\n".join(lines)

    def clean_cookies_file(self):
        v = self.cleaned_data["cookies_file"].strip()
        if v and not os.path.isabs(v):
            raise forms.ValidationError("must be an absolute path")
        return v

    def clean(self):
        data = super().clean()
        base, cap = data.get("retry_delay_base"), data.get("retry_delay_max")
        if base and cap and cap < base:
            self.add_error("retry_delay_max", "must be >= the base delay")
        return data
