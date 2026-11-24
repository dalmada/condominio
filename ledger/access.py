from functools import wraps
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_http_methods


def admin_required(view):
    @wraps(view)
    def check(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return never_cache(login_required(check))


def local_setup(request):
    return (
        not settings.PRODUCTION
        and request.META.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}
        and urlsplit("//" + request.get_host()).hostname in {"127.0.0.1", "localhost", "::1"}
        and not get_user_model().objects.exists()
    )


class AdminForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise ValidationError("Esta conta não tem acesso administrativo.")


class AdminLogin(LoginView):
    template_name = "ledger/login.html"
    authentication_form = AdminForm
    redirect_authenticated_user = False

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_staff:
            return redirect("dashboard")
        if local_setup(request):
            return redirect("first_access")
        return super().get(request, *args, **kwargs)


@require_http_methods(["GET", "POST"])
@never_cache
def first_access(request):
    if not local_setup(request):
        raise Http404
    form = UserCreationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        with transaction.atomic():
            if not local_setup(request):
                raise Http404
            if form.is_valid():
                user = form.save(commit=False)
                user.is_staff = True
                user.save()
                login(request, user)
                return redirect("dashboard")
    return render(request, "ledger/login.html", {"form": form, "setup": True})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")
