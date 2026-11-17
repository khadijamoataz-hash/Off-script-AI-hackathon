"""The shared, JavaScript-free UI keeps navigation and gate controls accessible."""

from html.parser import HTMLParser
from xml.etree.ElementTree import Element

import pytest

import ui


class Document(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Element(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_data(self, data):
        node = self.stack[-1]
        node.text = (node.text or "") + data


def text(node: Element) -> str:
    return "".join(node.itertext())


def test_page_has_focusable_skip_target_and_named_primary_navigation():
    doc = Document(ui.page("My project", "<h1>Review your idea</h1>")).root
    skip = doc.find(".//body/a")
    main = doc.find(".//main")
    assert skip is not None and main is not None
    assert skip.get("href") == "#" + main.get("id")
    assert text(skip) == "Skip to main content"
    assert main.get("tabindex") == "-1"
    assert main.find(".//h1").text == "Review your idea"
    nav = doc.find('.//nav[@aria-label="Primary"]')
    assert nav is not None
    assert [(a.get("href"), text(a)) for a in nav.findall("a")] == [
        ("/#projects", "Projects"), ("/status", "System status"), ("/#new", "New idea")
    ]
    assert not doc.findall(".//script")


@pytest.mark.parametrize("ready,status", [(True, "ready"), (False, "unavailable"), (None, "")])
def test_model_status_has_text_equivalent_and_escapes_untrusted_names(ready, status):
    injected = 'very-long-model"><img src=x onerror="bad">/' + "model" * 60
    doc = Document(ui.page("<unsafe title>", "<h1>Status</h1>", model=injected, model_ready=ready)).root
    chip = doc.find('.//a[@class="chip"]')
    assert chip is not None
    assert chip.get("href") == "/status"
    assert chip.get("aria-label") == f'Local model{(" " + status) if status else ""}: {injected}'
    assert text(chip) == injected
    assert chip.find("span").get("aria-hidden") == "true"
    assert doc.find(".//title").text == "<unsafe title> · Foundry"
    assert len(doc.findall(".//img")) == 1  # Only the brand logo is markup.
    assert not doc.findall('.//*[@onerror]')


def test_failed_gate_only_offers_allowed_choices_and_describes_typed_choices():
    doc = Document(ui.choice_buttons(
        "project-1", ui.CHOICE_TEXT, ("repeat", "stop"), recommended="continue"
    )).root
    buttons = doc.findall('.//button[@name="choice"]')
    assert [button.get("value") for button in buttons] == ["repeat", "stop"]
    assert all(button.get("type") == "submit" for button in buttons)
    assert "Available choices: Repeat, Stop." in text(doc)
    assert "Other choices are unavailable at this step." in text(doc)
    assert "Recommended" not in text(doc)
    reply = doc.find('.//label/input[@name="reply"]')
    assert reply is not None and reply.get("placeholder") == "repeat, stop"
    assert "Your choice" in text(doc.find(".//label"))
    assert [form.get("action") for form in doc.findall(".//form")] == [
        "/projects/project-1/decide", "/projects/project-1/decide"
    ]


def test_choice_actions_preserve_post_routes_without_adding_approval_controls():
    doc = Document(ui.choice_buttons(
        "project-2", ui.CHOICE_TEXT, tuple(ui.CHOICE_TEXT), "stop",
        action="validate-gate", typed=False,
    )).root
    forms = doc.findall(".//form")
    assert len(forms) == 1
    assert forms[0].attrib == {"method": "post", "action": "/projects/project-2/validate-gate"}
    assert [button.get("value") for button in forms[0].findall(".//button")] == list(ui.CHOICE_TEXT)
    stop = forms[0].find('.//button[@value="stop"]')
    assert "Recommended" in text(stop)
    assert not doc.findall(".//input")
    assert not doc.findall(".//script")


def test_unknown_choices_are_not_actionable_and_attributes_remain_escaped():
    injected = '"><script>alert("bad")</script>'
    doc = Document(ui.choice_buttons(
        injected, ["continue", injected, "continue"], ("continue", injected),
        recommended=injected, action=injected,
    )).root
    assert [button.get("value") for button in doc.findall('.//button[@name="choice"]')] == ["continue"]
    assert doc.find(".//form").get("action") == f"/projects/{injected}/{injected}"
    assert not doc.findall(".//script")
    assert "Recommended" not in text(doc)


def test_empty_gate_has_no_empty_form_or_typed_bypass():
    doc = Document(ui.choice_buttons("project-3", ["continue", "unknown"], ())).root
    assert "No choices are available at this step." in text(doc)
    assert not doc.findall(".//form")
    assert not doc.findall(".//input")
    assert not doc.findall(".//button")


def test_live_page_keeps_refresh_but_disables_motion_for_reduced_motion_users():
    doc = Document(ui.page("Working", ui.working_card("safety check", "step1"), refresh=3)).root
    assert doc.find('.//meta[@http-equiv="refresh"]').get("content") == "3"
    assert "Running the safety check on your brief" in text(doc.find(".//main"))
    assert doc.find('.//div[@class="progress"]').get("aria-hidden") == "true"
    styles = doc.find(".//style").text
    reduced = styles.split("@media(prefers-reduced-motion:reduce)", 1)[1]
    assert "@view-transition{navigation:none}" in reduced
    assert "animation:none!important" in reduced
    assert "transition:none!important" in reduced
    assert "*::before,*::after" in reduced
    assert "::view-transition-old(root),::view-transition-new(root)" in reduced
    assert not doc.findall(".//script")


def test_mobile_page_retains_navigation_and_shrinkable_project_cards():
    body = '<div class="proj-list"><a class="proj" href="/projects/one"><strong>' + "idea" * 100 + "</strong></a></div>"
    doc = Document(ui.page("Projects", body, model="model" * 100)).root
    nav = doc.find(".//nav")
    assert {a.get("href") for a in nav.findall("a")} == {"/status", "/#projects", "/#new"}
    assert doc.find('.//a[@class="proj"]').get("href") == "/projects/one"
    styles = doc.find(".//style").text
    assert ".nav a:not(.cta){display:none}" not in styles
    assert ".topbar .chip{display:none}" not in styles
    assert "minmax(min(100%,300px),1fr)" in styles
    assert "overflow-wrap:anywhere" in styles
