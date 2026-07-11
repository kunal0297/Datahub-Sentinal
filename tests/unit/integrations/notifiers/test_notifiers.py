import httpx

from sentinel.core.config import Settings
from sentinel.core.incident_engine import IncidentEngine, SeverityContext, SeverityRules
from sentinel.core.models import Incident, IncidentType, Severity
from sentinel.integrations.notifiers.jira import JiraNotifier
from sentinel.integrations.notifiers.slack import SlackNotifier, _format_message
from sentinel.integrations.notifiers.teams import TeamsNotifier

ORDERS_V1 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"


def make_incident() -> Incident:
    return Incident(
        urn="urn:li:incident:abc",
        resource_urns=[ORDERS_V1],
        incident_type=IncidentType.OPERATIONAL,
        severity=Severity.CRITICAL,
        title="[CRITICAL] Breaking change in PR #42",
        description="Raised by pr-impact-analysis because: column removed\n\n"
        "<!-- sentinel:dedup_key=abc123 -->",
        dedup_key="abc123",
        source_agent="pr-impact-analysis",
        link="https://github.com/acme/repo/pull/42",
    )


class TestJiraStub:
    def test_never_configured(self):
        assert JiraNotifier().is_configured() is False

    def test_notify_logs_intent_without_raising(self, caplog):
        caplog.set_level("INFO")
        JiraNotifier().notify(make_incident())
        assert "would create a Jira ticket" in caplog.text
        assert "urn:li:incident:abc" in caplog.text


class TestTeamsStub:
    def test_never_configured(self):
        assert TeamsNotifier().is_configured() is False

    def test_notify_logs_intent_without_raising(self, caplog):
        caplog.set_level("INFO")
        TeamsNotifier().notify(make_incident())
        assert "would post a Teams Adaptive Card" in caplog.text


class TestSlackNotifier:
    def test_not_configured_when_no_webhook_or_token(self):
        settings = Settings(_env_file=None, slack_webhook_url="", slack_bot_token="")
        assert SlackNotifier(settings).is_configured() is False

    def test_configured_with_webhook(self):
        settings = Settings(_env_file=None, slack_webhook_url="https://hooks.slack.test/x")
        assert SlackNotifier(settings).is_configured() is True

    def test_notify_posts_to_webhook_with_formatted_message(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        settings = Settings(_env_file=None, slack_webhook_url="https://hooks.slack.test/x")
        SlackNotifier(settings).notify(make_incident())

        assert captured["url"] == "https://hooks.slack.test/x"
        assert "CRITICAL" in captured["json"]["text"]
        assert "https://github.com/acme/repo/pull/42" in captured["json"]["text"]
        # the raw dedup marker is an implementation detail, not for humans
        assert "sentinel:dedup_key" not in captured["json"]["text"]

    def test_notify_swallows_transport_errors(self, monkeypatch, caplog):
        def fake_post(url, json=None, timeout=None):
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        settings = Settings(_env_file=None, slack_webhook_url="https://hooks.slack.test/x")
        # must not raise -- a broken Slack integration shouldn't break the caller
        SlackNotifier(settings).notify(make_incident())
        assert "Slack notify failed" in caplog.text

    def test_format_message_has_no_raw_html_comment(self):
        text = _format_message(make_incident(), "http://localhost:9002")
        assert "<!--" not in text

    def test_format_message_includes_deep_link_and_owner(self):
        from sentinel.core.models import Owner

        incident = make_incident()
        incident.owner = Owner(urn="urn:li:corpuser:alice", display_name="Alice Nguyen")
        text = _format_message(incident, "http://localhost:9002")
        assert "http://localhost:9002/search?query=" in text
        assert "Alice Nguyen" in text


class TestEngineRoutesToAllConfiguredNotifiers:
    """Every notifier call must be logged even when the concrete notifier is
    a stub, so routing correctness is verifiable without real Jira/Teams
    credentials — this is the Definition of Done requirement for Tier 1's
    Incident Automation Engine."""

    def test_stub_notifiers_are_skipped_but_logged(self, fake_datahub, caplog):
        import logging

        caplog.set_level(logging.INFO)
        rules = SeverityRules.from_yaml("config/severity_rules.yml")
        engine = IncidentEngine(fake_datahub, rules, notifiers=[JiraNotifier(), TeamsNotifier()])

        from sentinel.core.models import IncidentCandidate

        candidate = IncidentCandidate(
            resource_urns=[ORDERS_V1],
            incident_type=IncidentType.OPERATIONAL,
            source_agent="pr-impact-analysis",
            raw_signal="column_removed:discount_pct",
            title="Breaking change",
            context="column removed",
        )
        engine.raise_or_update(candidate, SeverityContext(), "dataset")

        assert "notifier jira not configured" in caplog.text
        assert "notifier teams not configured" in caplog.text

    def test_one_broken_notifier_does_not_block_others(self, fake_datahub):
        calls = []

        class BrokenNotifier:
            name = "broken"

            def is_configured(self):
                return True

            def notify(self, incident):
                raise RuntimeError("simulated transport failure")

        class RecordingNotifier:
            name = "recorder"

            def is_configured(self):
                return True

            def notify(self, incident):
                calls.append(incident.urn)

        rules = SeverityRules.from_yaml("config/severity_rules.yml")
        engine = IncidentEngine(
            fake_datahub, rules, notifiers=[BrokenNotifier(), RecordingNotifier()]
        )
        from sentinel.core.models import IncidentCandidate

        candidate = IncidentCandidate(
            resource_urns=[ORDERS_V1],
            incident_type=IncidentType.OPERATIONAL,
            source_agent="pr-impact-analysis",
            raw_signal="x",
            title="t",
            context="c",
        )
        incident = engine.raise_or_update(candidate, SeverityContext(), "dataset")
        assert calls == [incident.urn]
