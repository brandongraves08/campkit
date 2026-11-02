from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseGone, HttpResponseNotFound
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit.services import recent_changes
from apps.households.models import Invitation, Membership, MembershipRole
from apps.households.permissions import current_household, require_household_owner
from apps.households.services import household_recommendations, household_role
from apps.inventory.services import expiring_pantry, list_gear, list_pantry, verification_backlog
from apps.intake.services import list_verification_requests
from apps.trips.services import trip_readiness, upcoming_trips


class InvitationSignupForm(forms.Form):
    email = forms.EmailField()
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") and cleaned.get("password2") and cleaned["password1"] != cleaned["password2"]:
            self.add_error("password2", "Passwords do not match.")
        return cleaned


def signup_without_invitation(request):
    return HttpResponseForbidden("Sign-up is invitation-only.")


def accept_invitation(request, token):
    if request.method == "GET":
        return render(request, "registration/signup.html", {"token": token, "form": InvitationSignupForm()})
    if request.method != "POST":
        return HttpResponse(status=405)
    form = InvitationSignupForm(request.POST)
    if not form.is_valid():
        return render(request, "registration/signup.html", {"token": token, "form": form}, status=400)

    email = form.cleaned_data["email"].strip().casefold()
    try:
        with transaction.atomic():
            invitation = Invitation.objects.select_for_update().select_related("household").get(token=token)
            if invitation.is_expired:
                return HttpResponseGone("This invitation has expired.")
            if invitation.email.casefold() != email:
                return HttpResponseBadRequest("This invitation was issued to a different email address.")
            user_model = get_user_model()
            if user_model.objects.filter(email__iexact=email).exists():
                return HttpResponseBadRequest("An account with this email already exists.")
            user = user_model.objects.create_user(username=email, email=email, password=form.cleaned_data["password1"])
            try:
                validate_password(form.cleaned_data["password1"], user)
            except ValidationError as exc:
                user.delete()
                form.add_error("password1", exc)
                return render(request, "registration/signup.html", {"token": token, "form": form}, status=400)
            Membership.objects.create(user=user, household=invitation.household, role=MembershipRole.MEMBER)
            invitation.delete()
    except Invitation.DoesNotExist:
        return HttpResponseNotFound("Invitation not found.")
    except IntegrityError:
        return HttpResponseBadRequest("The invitation could not be used.")
    return HttpResponse("Account created. Please sign in.", status=302, headers={"Location": "/accounts/login/"})


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
    open_requests = list(list_verification_requests(actor=request.user, household=household))
    trips = list(upcoming_trips(actor=request.user, household=household, limit=1))
    readiness = trip_readiness(trip_id=trips[0].pk, actor=request.user) if trips else None
    recommendations = _with_action_urls(
        household_recommendations(actor=request.user, household=household)
    )
    return render(request, "dashboard.html", {
        "nav": "overview",
        "readiness": readiness,
        "backlog": backlog,
        "open_requests": open_requests,
        "expiring": list(expiring_pantry(actor=request.user, household=household))[:3],
        "gear_count": gear.count(),
        "pantry_count": pantry.count(),
        "recommendations": recommendations[:3],
        "recommendation_total": len(recommendations),
        "recent_changes": list(recent_changes(actor=request.user, household=household, limit=6)),
    })


@login_required
def recommendations(request):
    """Explained, read-only household suggestions — never inventory."""
    household = current_household(request)
    return render(request, "recommendations.html", {
        "nav": "recommendations",
        "recommendations": _with_action_urls(
            household_recommendations(actor=request.user, household=household)
        ),
        "role": household_role(actor=request.user, household=household),
    })
