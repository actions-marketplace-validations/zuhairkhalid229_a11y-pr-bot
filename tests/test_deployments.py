from app.webhooks.deployments import extract_preview

SHA = "b" * 40


def _payload(
    *,
    state="success",
    environment="Preview",
    environment_url=None,
    target_url=None,
    log_url=None,
    creator="vercel[bot]",
    deployment_env=None,
):
    return {
        "action": "created",
        "deployment_status": {
            "state": state,
            "environment": environment,
            "environment_url": environment_url,
            "target_url": target_url,
            "log_url": log_url,
        },
        "deployment": {
            "id": 991,
            "sha": SHA,
            "environment": deployment_env or environment,
            "creator": {"login": creator, "type": "Bot"},
        },
        "repository": {"id": 99, "full_name": "acme/site"},
        "installation": {"id": 555},
    }


def test_vercel_preview():
    p = extract_preview(
        _payload(
            environment_url="https://site-git-feat-acme.vercel.app",
            target_url="https://site-git-feat-acme.vercel.app",
            log_url="https://vercel.com/acme/site/abc123",
        )
    )
    assert p is not None
    assert p.url == "https://site-git-feat-acme.vercel.app"
    assert p.provider == "vercel"
    assert p.sha == SHA
    assert p.deployment_id == 991


def test_netlify_deploy_preview_prefers_environment_url_over_dashboard():
    p = extract_preview(
        _payload(
            creator="netlify[bot]",
            environment="Deploy Preview",
            environment_url="https://deploy-preview-12--site.netlify.app",
            target_url="https://app.netlify.com/sites/site/deploys/abc",
        )
    )
    assert p.url == "https://deploy-preview-12--site.netlify.app"
    assert p.provider == "netlify"


def test_falls_back_to_target_url_when_it_is_a_site():
    p = extract_preview(_payload(environment_url=None, target_url="https://site-abc.vercel.app"))
    assert p.url == "https://site-abc.vercel.app"


def test_dashboard_only_urls_yield_nothing():
    assert (
        extract_preview(_payload(environment_url=None, target_url="https://vercel.com/acme/site/abc")) is None
    )
    assert extract_preview(_payload(environment_url="https://app.netlify.com/x", target_url=None)) is None


def test_non_success_states_ignored():
    for state in ("pending", "in_progress", "queued", "failure", "error", "inactive"):
        assert extract_preview(_payload(state=state, environment_url="https://x.vercel.app")) is None


def test_production_environments_ignored():
    assert extract_preview(_payload(environment="Production", environment_url="https://site.com")) is None
    assert (
        extract_preview(_payload(environment="production – site", environment_url="https://site.com")) is None
    )


def test_unknown_provider_still_accepted():
    p = extract_preview(
        _payload(creator="cloudflare-workers-and-pages[bot]", environment_url="https://abc.site.pages.dev")
    )
    assert p.provider == "cloudflare"
    p = extract_preview(_payload(creator="render[bot]", environment_url="https://site-pr-7.onrender.com"))
    assert p.provider == "other"


def test_missing_sha_or_garbage_url_yields_nothing():
    payload = _payload(environment_url="https://x.vercel.app")
    payload["deployment"]["sha"] = None
    assert extract_preview(payload) is None
    assert extract_preview(_payload(environment_url="not a url")) is None
    assert extract_preview(_payload(environment_url="ftp://x.vercel.app")) is None
