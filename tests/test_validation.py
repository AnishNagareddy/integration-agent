from validation import validate

PLAN = {"actions": [{"name": "create_issue", "http_method": "POST", "path": "/issue"}]}


def test_good_code_passes_all_blocking_checks(isolated, good_code):
    report = validate(good_code.model_dump(), PLAN)
    assert report["passed"], report["summary"]
    names = {c["name"] for c in report["checks"] if c["passed"]}
    assert {"static_syntax", "interface_schema", "dry_run", "generated_tests"} <= names


def test_syntax_error_is_caught(isolated, broken_code):
    report = validate(broken_code.model_dump(), PLAN)
    assert not report["passed"]
    assert "static_syntax" in report["failures"]


def test_missing_method_fails_schema(isolated, good_code):
    gen = good_code.model_dump()
    gen["artifacts"][0]["content"] = gen["artifacts"][0]["content"].replace(
        "def create_issue", "def _renamed_create_issue"
    )
    report = validate(gen, PLAN)
    assert not report["passed"]


def test_wrong_endpoint_fails_dry_run(isolated, good_code):
    gen = good_code.model_dump()
    # spec still says the action is POST /issue, but the METHOD calls the wrong path →
    # the dry-run should notice the expected call never happened.
    gen["artifacts"][0]["content"] = gen["artifacts"][0]["content"].replace(
        'self._request("POST", "/issue"', 'self._request("POST", "/WRONGPATH"'
    )
    report = validate(gen, PLAN)
    dry = next(c for c in report["checks"] if c["name"] == "dry_run")
    assert not dry["passed"]
