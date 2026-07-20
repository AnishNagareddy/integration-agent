import catalog


def test_seed_has_builtins_but_not_jira(isolated):
    assert catalog.has("github")
    assert catalog.has("slack")
    assert not catalog.has("jira")  # the gap the agent fills


def test_register_persists(isolated):
    catalog.register(catalog.Capability(name="jira", supported_actions=["create_issue"]))
    assert catalog.has("jira")
    assert catalog.get("jira").supported_actions == ["create_issue"]
