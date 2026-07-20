from providers.llm import _extract_code, _snake


def test_extract_code_from_fenced_block():
    text = "Here you go:\n```python\nx = 1\nprint(x)\n```\nDone."
    assert _extract_code(text).strip() == "x = 1\nprint(x)"


def test_extract_code_without_fences_returns_raw():
    assert _extract_code("x = 1\n").strip() == "x = 1"


def test_snake_case_from_connector_class():
    assert _snake("JiraConnector") == "jira_connector"
    assert _snake("StripeConnector") == "stripe_connector"
    assert _snake("GoogleSheetsConnector") == "google_sheets_connector"
