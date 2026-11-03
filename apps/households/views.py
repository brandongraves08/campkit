from urllib.parse import urlencode

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseGone,
    HttpResponseNotFound,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.services import recent_changes
from apps.households.models import Household, Invitation, Membership, MembershipRole
from apps.households.permissions import current_household, require_household_owner
from apps.households.services import household_recommendations, household_role
from apps.intake.services import list_verification_requests
from apps.inventory.services import (
    expiring_pantry,
    list_gear,
    list_pantry,
    verification_backlog,
)
from apps.trips.services import trip_readiness, upcoming_trips


class InvitationSignupForm(forms.Form):
    email = forms.EmailField(max_length=150)
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().casefold()
        if len(email) > 150:
            raise ValidationError("Email address is too long.")
        return email

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("password1")
            and cleaned.get("password2")
            and cleaned["password1"] != cleaned["password2"]
        ):
            self.add_error("password2", "Passwords do not match.")
        return cleaned


class SelfSignupForm(forms.Form):
    email = forms.EmailField(max_length=150)
    household_name = forms.CharField(max_length=120)
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().casefold()
        if len(email) > 150:
            raise ValidationError("Email address is too long.")
        return email

    def clean_household_name(self):
        name = self.cleaned_data["household_name"].strip()
        if not name:
            raise ValidationError("Enter a household name.")
        return name

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("password1")
            and cleaned.get("password2")
            and cleaned["password1"] != cleaned["password2"]
        ):
            self.add_error("password2", "Passwords do not match.")
        return cleaned


class CaseInsensitiveAuthenticationForm(AuthenticationForm):
    def clean_username(self):
        return self.cleaned_data["username"].strip().casefold()


class CampKitLoginView(LoginView):
    authentication_form = CaseInsensitiveAuthenticationForm

    def get_success_url(self):
        next_url = self.request.POST.get(
            self.redirect_field_name
        ) or self.request.GET.get(self.redirect_field_name)
        if not next_url:
            memberships = Membership.objects.filter(user=self.request.user)
            active_id = self.request.session.get("active_household_id")
            if (
                memberships.count() > 1
                and not memberships.filter(household_id=active_id).exists()
            ):
                return reverse("select_household")
        return super().get_success_url()


@require_http_methods(["GET", "POST"])
def signup_without_invitation(request):
    if request.method == "GET":
        return render(request, "registration/signup.html", {"form": SelfSignupForm()})

    form = SelfSignupForm(request.POST)
    if not form.is_valid():
        return render(request, "registration/signup.html", {"form": form}, status=400)

    email = form.cleaned_data["email"]
    user_model = get_user_model()
    if user_model.objects.filter(email__iexact=email).exists():
        form.add_error("email", "An account with this email already exists.")
        return render(request, "registration/signup.html", {"form": form}, status=400)

    user = user_model(username=email, email=email)
    try:
        validate_password(form.cleaned_data["password1"], user)
    except ValidationError as exc:
        form.add_error("password1", exc)
        return render(request, "registration/signup.html", {"form": form}, status=400)

    try:
        with transaction.atomic():
            if user_model.objects.filter(email__iexact=email).exists():
                form.add_error("email", "An account with this email already exists.")
                return render(
                    request, "registration/signup.html", {"form": form}, status=400
                )
            user = user_model.objects.create_user(
                username=email, email=email, password=form.cleaned_data["password1"]
            )
            household = Household.objects.create(
                name=form.cleaned_data["household_name"]
            )
            Membership.objects.create(
                user=user, household=household, role=MembershipRole.OWNER
            )
    except IntegrityError:
        form.add_error("email", "An account with this email already exists.")
        return render(request, "registration/signup.html", {"form": form}, status=400)

    return HttpResponse(
        "Account created. Please sign in.",
        status=302,
        headers={"Location": "/accounts/login/"},
    )


@require_http_methods(["GET", "POST"])
def accept_invitation(request, token):
    try:
        invitation = Invitation.objects.select_related("household").get(token=token)
    except Invitation.DoesNotExist:
        return HttpResponseNotFound("Invitation not found.")
    if invitation.is_expired:
        return HttpResponseGone("This invitation has expired.")

    user_model = get_user_model()
    if request.method == "GET":
        if request.user.is_authenticated:
            if request.user.email.strip().casefold() != invitation.email.casefold():
                return HttpResponseForbidden(
                    "This invitation was issued to a different email account."
                )
            return render(
                request,
                "registration/signup.html",
                {"token": token, "accept_existing": True},
            )

        if user_model.objects.filter(email__iexact=invitation.email).exists():
            login_url = f"{reverse('login')}?{urlencode({'next': request.path})}"
            return render(
                request,
                "registration/signup.html",
                {"token": token, "existing_account": True, "login_url": login_url},
            )
        return render(
            request,
            "registration/signup.html",
            {"token": token, "form": InvitationSignupForm()},
        )

    try:
        with transaction.atomic():
            invitation = (
                Invitation.objects.select_for_update()
                .select_related("household")
                .get(token=token)
            )
            if invitation.is_expired:
                return HttpResponseGone("This invitation has expired.")

            if request.user.is_authenticated:
                if request.user.email.strip().casefold() != invitation.email.casefold():
                    return HttpResponseForbidden(
                        "This invitation was issued to a different email account."
                    )
                Membership.objects.get_or_create(
                    user=request.user,
                    household=invitation.household,
                    defaults={"role": MembershipRole.MEMBER},
                )
                invitation.delete()
                request.session["active_household_id"] = str(invitation.household_id)
                return redirect("dashboard")

            form = InvitationSignupForm(request.POST)
            if not form.is_valid():
                return render(
                    request,
                    "registration/signup.html",
                    {"token": token, "form": form},
                    status=400,
                )

            email = form.cleaned_data["email"]
            if invitation.email.casefold() != email:
                return HttpResponseBadRequest(
                    "This invitation was issued to a different email address."
                )

            if user_model.objects.filter(email__iexact=email).exists():
                login_url = f"{reverse('login')}?{urlencode({'next': request.path})}"
                return render(
                    request,
                    "registration/signup.html",
                    {"token": token, "existing_account": True, "login_url": login_url},
                    status=400,
                )

            user = user_model(username=email, email=email)
            try:
                validate_password(form.cleaned_data["password1"], user)
            except ValidationError as exc:
                form.add_error("password1", exc)
                return render(
                    request,
                    "registration/signup.html",
                    {"token": token, "form": form},
                    status=400,
                )

            user = user_model.objects.create_user(
                username=email, email=email, password=form.cleaned_data["password1"]
            )
            Membership.objects.create(
                user=user,
                household=invitation.household,
                role=MembershipRole.MEMBER,
            )
            invitation.delete()
    except Invitation.DoesNotExist:
        return HttpResponseNotFound("Invitation not found.")
    except IntegrityError:
        return HttpResponseBadRequest("The invitation could not be used.")

    return HttpResponse(
        "Account created. Please sign in.",
        status=302,
        headers={"Location": reverse("login")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def select_household(request):
    memberships = list(
        Membership.objects.filter(user=request.user)
        .select_related("household")
        .order_by("household__name")
    )
    if not memberships:
        return HttpResponseForbidden("You do not belong to a household.")

    if request.method == "GET":
        if len(memberships) == 1:
            request.session["active_household_id"] = str(memberships[0].household_id)
            return redirect("dashboard")
        return render(
            request,
            "registration/household_selection.html",
            {
                "memberships": memberships,
                "next_url": request.GET.get("next", reverse("dashboard")),
            },
        )

    try:
        membership = next(
            item
            for item in memberships
            if str(item.household_id) == request.POST.get("household_id")
        )
    except StopIteration:
        return HttpResponseForbidden("You do not belong to that household.")

    request.session["active_household_id"] = str(membership.household_id)
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("dashboard")


@require_POST
def create_invitation(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Authentication is required.")
    try:
        household = current_household(request)
        require_household_owner(request.user, household.pk)
    except PermissionDenied:
        return HttpResponseForbidden("Only a household owner may invite members.")
    form = forms.EmailField()
    try:
        email = form.clean(request.POST.get("email", "")).strip().casefold()
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    invitation = Invitation.objects.create(
        household=household,
        email=email,
        created_by=request.user,
        expires_at=timezone.now() + timezone.timedelta(days=7),
    )
    return HttpResponse(f"Invitation created: {invitation.token}", status=201)


def _recommendation_url(recommendation):
    """Map a service-level action key onto a real route (URLs stay in views)."""
    key = recommendation.get("action_key")
    if key == "verification_queue":
        return reverse("inventory:verification_queue")
    if key == "pantry_list":
        return reverse("inventory:pantry_list")
    if key == "gear_list":
        return reverse("inventory:gear_list")
    if key == "intake_list":
        return reverse("intake:list")
    if key == "trip_detail" and recommendation.get("trip_id"):
        return reverse("trips:trip_detail", args=[recommendation["trip_id"]])
    return None


def _with_action_urls(recommendations):
    for recommendation in recommendations:
        recommendation["action_url"] = _recommendation_url(recommendation)
    return recommendations


@login_required
def dashboard(request):
    """Task-first household overview: next trip, attention items, provenance."""
    household = current_household(request)
    gear = list_gear(actor=request.user, household=household)
    pantry = list_pantry(actor=request.user, household=household)
    backlog = verification_backlog(actor=request.user, household=household)
    open_requests = list(
        list_verification_requests(actor=request.user, household=household)
    )
    trips = list(upcoming_trips(actor=request.user, household=household, limit=1))
    readiness = (
        trip_readiness(trip_id=trips[0].pk, actor=request.user) if trips else None
    )
    recommendations = _with_action_urls(
        household_recommendations(actor=request.user, household=household)
    )
    return render(
        request,
        "dashboard.html",
        {
            "nav": "overview",
            "readiness": readiness,
            "backlog": backlog,
            "open_requests": open_requests,
            "expiring": list(expiring_pantry(actor=request.user, household=household))[
                :3
            ],
            "gear_count": gear.count(),
            "pantry_count": pantry.count(),
            "recommendations": recommendations[:3],
            "recommendation_total": len(recommendations),
            "recent_changes": list(
                recent_changes(actor=request.user, household=household, limit=6)
            ),
        },
    )


@login_required
def recommendations(request):
    """Explained, read-only household suggestions — never inventory."""
    household = current_household(request)
    return render(
        request,
        "recommendations.html",
        {
            "nav": "recommendations",
            "recommendations": _with_action_urls(
                household_recommendations(actor=request.user, household=household)
            ),
            "role": household_role(actor=request.user, household=household),
        },
    )
