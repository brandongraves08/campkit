from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils.html import escape

from apps.inventory.models import InventoryVerification
from apps.inventory.services import create_gear_item, create_pantry_item, update_gear_item
from apps.trips.services import add_trip_gear, create_trip

PAGES = (
    "dashboard",
    "recommendations",
    "inventory:verification_queue",
    "inventory:gear_list",
    "inventory:pantry_list",
    "inventory:pantry_create",
    "inventory:gear_create",
    "trips:trip_list",
    "intake:list",
    "intake:create",
)


@pytest.fixture
def rendered_pages(client, owner, household, gear_data, pantry_data, trip_data):
    gear = create_gear_item(actor=owner, household=household, data=gear_data)
    update_gear_item(actor=owner, household=household, item_id=gear.pk,
                     data={"verification_status": InventoryVerification.CONFIRMED})
    pantry = create_pantry_item(actor=owner, household=household, data=pantry_data)
    trip = create_trip(actor=owner, household=household, data=trip_data)
    add_trip_gear(trip_id=trip.pk, gear_item_id=gear.pk, quantity=1, actor=owner)
    client.force_login(owner)
    urls = [reverse(name) for name in PAGES]
    urls.extend([
        reverse("inventory:pantry_detail", args=[pantry.pk]),
        reverse("trips:trip_detail", args=[trip.pk]),
        reverse("meals:meal_list", args=[trip.pk]),
    ])
    return {url: client.get(url) for url in urls}


@pytest.mark.django_db
def test_pages_share_a_labelled_landmark_structure(rendered_pages):
    for url, response in rendered_pages.items():
        assert response.status_code == 200, url
        body = response.content.decode()
        assert '<html lang="en">' in body, url
        assert 'name="viewport"' in body, url
        assert 'class="skip-link"' in body, url
        assert 'id="main"' in body, url
        assert body.count("<h1") == 1, url
        assert '<nav class="site-nav" aria-label="Primary">' in body, url


@pytest.mark.django_db
def test_form_controls_have_accessible_names(rendered_pages):
    from html.parser import HTMLParser

    class Audit(HTMLParser):
        def __init__(self):
            super().__init__()
            self.labels_for = set()
            self.controls = []
            self.buttons = []
            self.images = []
            self.ids = []
            self._in_label = False
            self._button = None
            self._button_text = ""

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.ids.append(attrs["id"])
            if tag == "label":
                self._in_label = True
                if attrs.get("for"):
                    self.labels_for.add(attrs["for"])
            elif tag in {"input", "select", "textarea"}:
                if attrs.get("type") in {"hidden", "submit", "csrfmiddlewaretoken"}:
                    return
                self.controls.append({
                    "tag": tag, "id": attrs.get("id", ""), "aria": attrs.get("aria-label", ""),
                    "type": attrs.get("type", "text"),
                })
            elif tag == "button":
                self._button = attrs
                self._button_text = ""
            elif tag == "img":
                self.images.append(attrs)

        def handle_data(self, data):
            if self._button is not None:
                self._button_text += data

        def handle_endtag(self, tag):
            if tag == "label":
                self._in_label = False
            elif tag == "button" and self._button is not None:
                self.buttons.append({**self._button, "text": self._button_text})
                self._button = None
                self._button_text = ""

    for url, response in rendered_pages.items():
        parser = Audit()
        parser.feed(response.content.decode())
        assert len(parser.ids) == len(set(parser.ids)), f"duplicate id on {url}"
        for control in parser.controls:
            assert control["id"], f"unlabelled {control} on {url}"
            if control["tag"] == "input" and control["type"] in {"checkbox", "radio"}:
                continue
            assert control["id"] in parser.labels_for or control["aria"], (
                f"control without label on {url}: {control}"
            )
        for button in parser.buttons:
            assert button.get("type") in {"submit", "button", "reset"}, f"button type missing on {url}"
            accessible_name = (button.get("aria-label") or "").strip() or button["text"].strip()
            assert accessible_name, f"button without an accessible name on {url}: {button}"
        for image in parser.images:
            assert "alt" in image, f"image without alt on {url}"


@pytest.mark.django_db
def test_post_forms_carry_csrf_and_no_inline_dangerous_handlers(rendered_pages):
    for url, response in rendered_pages.items():
        body = response.content.decode()
        for fragment in body.split("<form")[1:]:
            opening = fragment.split(">", 1)[0]
            form = fragment.split("</form>")[0]
            if 'method="post"' in opening:
                assert "csrfmiddlewaretoken" in form, f"form without CSRF on {url}"
        assert "javascript:" not in body, url
        assert "onsubmit=" not in body, url


@pytest.mark.django_db
def test_static_javascript_is_progressive_and_confirmations_are_data_driven(client, owner, household, pantry_data):
    create_pantry_item(actor=owner, household=household, data=pantry_data)
    client.force_login(owner)
    body = client.get(reverse("inventory:pantry_list")).content.decode()
    assert 'src="/static/js/campkit.js"' in body
    assert 'href="/static/css/campkit.css"' in body
    assert 'data-confirm="' in body
    script = (settings.BASE_DIR / "static" / "js" / "campkit.js").read_text()
    assert "data-confirm" in script
    assert "addEventListener" in script
    css = (settings.BASE_DIR / "static" / "css" / "campkit.css").read_text()
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert "min-height: 44px" in css or "min-height:44px" in css
    assert "font-size: 16px" in css or "font-size:16px" in css


@pytest.mark.django_db
def test_login_page_stays_intact_and_unauthenticated_chrome_is_minimal(client):
    response = client.get(reverse("login"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'name="username"' in body and 'name="password"' in body
    assert "<h1" in body
    assert "Private to" not in body
    assert 'aria-label="Primary"' not in body
